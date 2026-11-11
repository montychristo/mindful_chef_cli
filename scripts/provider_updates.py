"""Read and fingerprint a fixed set of public Mindful Chef policy pages."""

from hashlib import sha256
from html import unescape
import json
import re
import sys
import unicodedata

from bs4 import BeautifulSoup, Tag
import httpx

from mindful_chef_cli.storage import SecureStore, StorageError


_HELP = "https://help.mindfulchef.com/en/articles/"
SOURCES = (
    {"key": "terms", "label": "Ordering terms", "url": "https://www.mindfulchef.com/terms-and-conditions"},
    {"key": "login_help", "label": "Sign-in help", "url": _HELP + "299-having-technical-issues"},
    {"key": "delivery_cutoff", "label": "Cutoff for changes", "url": _HELP + "176-cut-off-for-changes"},
    {"key": "skip_help", "label": "Skipping a recipe box", "url": _HELP + "171-how-to-skip-your-recipe-box"},
    {"key": "portion_help", "label": "Portion sizes", "url": _HELP + "143-can-i-mix-portion-sizes-in-one-box"},
    {"key": "recipe_selection_help", "label": "Recipe selection and confirmation",
     "url": _HELP + "159-how-do-i-add-more-portions-of-the-same-meal"},
    {"key": "history_help", "label": "Past recipes and orders",
     "url": _HELP + "149-what-to-do-if-your-recipe-magazine-is-missing"},
)

_HOSTS = {"www.mindfulchef.com", "help.mindfulchef.com"}
_MAX_BYTES = 256 * 1024
_REDIRECTS = {301, 302, 303, 307, 308}
_TIMESTAMP_NOTE = re.compile(
    r"(?:these terms and conditions were\s+)?(?:last\s+)?updated(?:\s+on)?\s*[:–-]?\s*"
    r"(?:\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4})"
    r"(?:\s+at\s+\d{1,2}:\d{2}\s*(?:[AP]M)?)?\.?",
    re.I,
)


def _safe_url(url: httpx.URL) -> None:
    if (url.scheme != "https" or url.host not in _HOSTS or url.port not in {None, 443}
            or url.userinfo or url.fragment):
        raise ValueError("Unapproved provider destination")


def _get_html(client: httpx.Client, source_url: str) -> str:
    url = httpx.URL(source_url)
    for _ in range(3):
        _safe_url(url)
        # An anonymous visitor cookie can be set by the storefront. No request
        # in this monitor needs to retain it, including across redirects.
        client.cookies.clear()
        with client.stream("GET", url) as response:
            if response.status_code in _REDIRECTS:
                locations = response.headers.get_list("location")
                if len(locations) != 1 or not locations[0]:
                    raise ValueError("Invalid provider redirect")
                url = url.join(locations[0])
                continue
            if response.status_code != 200:
                raise ValueError("Provider page unavailable")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if content_type != "text/html" or encoding != "identity":
                raise ValueError("Unexpected provider representation")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_BYTES):
                raise ValueError("Provider page exceeds response limits")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=16 * 1024):
                if len(body) + len(chunk) > _MAX_BYTES:
                    raise ValueError("Provider page exceeds response limits")
                body.extend(chunk)
            return body.decode("utf-8")
    raise ValueError("Provider redirect limit exceeded")


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _visible_text(root: Tag) -> str:
    for element in root.select('nav, footer, script, style, noscript, svg, time, [hidden], [aria-hidden="true"]'):
        element.decompose()
    for element in root.select("p, small"):
        if _TIMESTAMP_NOTE.fullmatch(_normalized(element.get_text(" "))):
            element.decompose()
    return _normalized(root.get_text(" "))


def _answer_text(text: str) -> str:
    if re.search(r"</?[A-Za-z][^>]*>", text):
        return _visible_text(BeautifulSoup(text, "html.parser"))
    return _normalized(unescape(text))


def _faq_text(soup: BeautifulSoup, source_url: str) -> str:
    # The Help Centre renders its shell in JavaScript but publishes each
    # article's content as a canonical FAQPage JSON-LD document.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            document = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        if (not isinstance(document, dict) or document.get("@type") != "FAQPage"
                or document.get("url") != source_url):
            continue
        entries = document.get("mainEntity")
        if not isinstance(entries, list) or not entries:
            break
        parts = []
        for entry in entries:
            answer = entry.get("acceptedAnswer") if isinstance(entry, dict) else None
            text = answer.get("text") if isinstance(answer, dict) else None
            if not isinstance(text, str) or not (body := _answer_text(text)):
                raise ValueError("Provider Help content is missing")
            title = entry.get("name")
            parts.append(_normalized(title) if isinstance(title, str) else "")
            parts.append(body)
        return "\n".join(parts)
    raise ValueError("Provider Help article schema is unavailable")


def _digest(source: dict[str, str], html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if source["key"] == "terms":
        canonical = soup.select_one('link[rel="canonical"]')
        main = soup.find("main")
        if canonical is None or canonical.get("href") != source["url"] or main is None:
            raise ValueError("Provider terms article schema is unavailable")
        text = _visible_text(main)
    else:
        text = _faq_text(soup, source["url"])
    if not text:
        raise ValueError("Provider article content is empty")
    return sha256(text.encode("utf-8")).hexdigest()


def check_sources(*, transport: httpx.BaseTransport | None = None) -> list[dict[str, str | None]]:
    """Return status and content fingerprints with fixed source metadata only."""
    results = []
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(8, connect=4),
        follow_redirects=False,
        trust_env=False,
        headers={"Accept": "text/html", "Accept-Encoding": "identity",
                 "User-Agent": "Mindful-Chef-CLI public monitor"},
    ) as client:
        for source in SOURCES:
            try:
                digest = _digest(source, _get_html(client, source["url"]))
            except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError):
                digest = None
            results.append({**source, "digest": digest, "status": "ok" if digest else "unavailable"})
    return results


def record_sources(store: SecureStore, sources: list[dict[str, str | None]]) -> list[dict[str, str]]:
    """Compare local public sources and retain the last successful baseline."""
    known = {source["key"] for source in SOURCES}
    results = []
    with store.lock():
        previous = store.read_private_json("provider-last.json", {})
        if not isinstance(previous, dict) or any(
            key not in known or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in previous.items()
        ):
            raise StorageError("Invalid provider monitor state")
        for source in sources:
            key, digest = source["key"], source["digest"]
            prior = previous.get(key)
            status = "unavailable"
            if digest is not None:
                status = "baseline" if prior is None else "unchanged" if digest == prior else "changed"
                previous[key] = digest
            results.append({"key": key, "label": source["label"], "url": source["url"], "status": status})
        store.write_private_json("provider-last.json", previous)
    return results


def main() -> int:
    try:
        store = SecureStore(profile="provider-monitor")
        results = record_sources(store, check_sources())
    except StorageError:
        print("Provider monitor state is unavailable.", file=sys.stderr)
        return 1
    print(json.dumps(results))
    return int(any(source["status"] == "unavailable" for source in results))


if __name__ == "__main__":
    raise SystemExit(main())
