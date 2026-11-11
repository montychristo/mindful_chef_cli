"""Synthetic HTTP contracts for the public provider monitor."""

import json
import unittest

import httpx

from scripts import provider_updates


def _html(text, status=200, **headers):
    return httpx.Response(status, text=text, headers={"content-type": "text/html", **headers})


def _terms(source, *, days=4, date="5th November 2026", nav="November menu"):
    return f'''<link rel="canonical" href="{source['url']}">
    <nav>{nav}</nav><main><h1>Example ordering terms</h1>
    <p>Changes close {days} days before delivery.</p>
    <p>These terms and conditions were last updated on {date}.</p>
    <p>Last updated 1 day ago.</p><footer>{nav}</footer></main>'''


def _help(source, *, answer="Review the delivery on the website.", date="2030-01-01", nav="Original navigation"):
    document = {
        "@type": "FAQPage",
        "url": source["url"],
        "dateModified": date,
        "mainEntity": [{"name": "Example question", "acceptedAnswer": {"text": answer}}],
    }
    return f'<nav>{nav}</nav><script type="application/ld+json">{json.dumps(document)}</script>'


class ProviderUpdateTests(unittest.TestCase):
    def setUp(self):
        self.sources = {source["url"]: source for source in provider_updates.SOURCES}

    def check(self, handle):
        result = provider_updates.check_sources(transport=httpx.MockTransport(handle))
        return {source["key"]: source for source in result}

    def default_page(self, request):
        source = self.sources[str(request.url)]
        return _terms(source) if source["key"] == "terms" else _help(source)

    def test_digest_ignores_shell_and_update_dates_but_detects_policy_text(self):
        first = self.check(lambda request: _html(self.default_page(request)))

        def changed(request):
            source = self.sources[str(request.url)]
            if source["key"] == "terms":
                return _html(_terms(source, date="11th November 2026", nav="Rotated menu"))
            if source["key"] == "delivery_cutoff":
                return _html(_help(source, answer="Review the delivery before the cutoff."))
            return _html(_help(source, answer="Review the delivery  on the website.",
                               date="2030-01-08", nav="Different navigation"))

        second = self.check(changed)
        self.assertTrue(all(item["status"] == "ok" for item in first.values()))
        self.assertTrue(all(item["status"] == "ok" for item in second.values()))
        self.assertEqual({key for key in first if first[key]["digest"] != second[key]["digest"]},
                         {"delivery_cutoff"})
        self.assertNotIn("Review the delivery", json.dumps(second))

    def test_offsite_insecure_or_credential_redirects_are_not_requested(self):
        for target in (
            "https://external.example.invalid/private",
            "http://help.mindfulchef.com/en",
            "https://help.mindfulchef.com:8443/en",
            str(httpx.URL("https://help.mindfulchef.com/en").copy_with(username="synthetic-user")),
        ):
            with self.subTest(target=target):
                seen = []

                def handle(request, seen=seen, target=target):
                    seen.append(str(request.url))
                    source = self.sources[str(request.url)]
                    if source["key"] == "login_help":
                        return httpx.Response(302, headers={"location": target})
                    return _html(self.default_page(request))

                results = self.check(handle)
                self.assertEqual(results["login_help"]["status"], "unavailable")
                self.assertIsNone(results["login_help"]["digest"])
                self.assertEqual(set(seen), set(self.sources))
                self.assertNotIn(target, json.dumps(results))

    def test_anonymous_cookies_are_not_reused_even_on_safe_redirect(self):
        terms = next(source for source in self.sources.values() if source["key"] == "terms")
        seen = []

        def handle(request):
            seen.append(request)
            if str(request.url) == terms["url"]:
                return httpx.Response(302, headers={"location": "?view=current",
                    "set-cookie": "synthetic-visitor=synthetic-value"})
            if request.url.params.get("view") == "current":
                return _html(_terms(terms))
            return _html(self.default_page(request))

        results = self.check(handle)
        self.assertTrue(all(source["status"] == "ok" for source in results.values()))
        self.assertEqual(len(seen), len(self.sources) + 1)
        self.assertTrue(all(request.method == "GET" and "cookie" not in request.headers
                            and "authorization" not in request.headers for request in seen))

    def test_oversize_or_wrong_schema_is_unavailable_without_masking_other_sources(self):
        def handle(request):
            source = self.sources[str(request.url)]
            if source["key"] == "portion_help":
                return _html("example body", **{"content-length": str(provider_updates._MAX_BYTES + 1)})
            if source["key"] == "skip_help":
                return _html("<main>Unexpected public error page</main>")
            return _html(self.default_page(request))

        results = self.check(handle)
        self.assertEqual({key for key, result in results.items() if result["digest"] is None},
                         {"portion_help", "skip_help"})
        self.assertNotIn("Unexpected public error page", json.dumps(results))


if __name__ == "__main__":
    unittest.main()
