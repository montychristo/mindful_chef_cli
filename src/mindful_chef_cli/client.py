"""Fixed-origin HTML client with reviewed, single-attempt account changes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from http.cookiejar import Cookie
import hmac
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
import uuid

from bs4 import BeautifulSoup
import httpx

from . import __version__
from .parsing import csrf, parse_forms, parse_menu, parse_orders, parse_recipe
from .storage import SecureStore

ORIGIN = "https://www.mindfulchef.com"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ID = re.compile(r"[0-9]{1,12}\Z")
_PLAN_ID = re.compile(r"[a-f0-9]{32}\Z")
_CONTENT = re.compile(r"delivery\[contents_attributes\]\[[^\]]+\]\[(recipe_id|portions)\]\Z")


class ClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ClientError(code, message)


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _date(value: str) -> str:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        _fail("invalid_date", "Use an ISO delivery date: YYYY-MM-DD.")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("invalid_date", "Invalid delivery date.")
    return value


def _path(value: str) -> str:
    target = urlsplit(urljoin(ORIGIN + "/", value))
    if target.scheme != "https" or target.netloc != "www.mindfulchef.com" or target.username or target.fragment:
        _fail("unsafe_destination", "Only the fixed Mindful Chef HTTPS origin is permitted.")
    if any(c in value for c in ("\\", "\r", "\n", "\x00")):
        _fail("unsafe_destination", "Invalid destination.")
    return target.path + ("?" + target.query if target.query else "")


def _public_menu(menu: dict) -> dict:
    return {k: v for k, v in menu.items() if k not in {"cart_form", "forms", "products_preserved"}}


def _selection(items: list[dict]) -> list[dict]:
    # The provider preserves submission order in the account's meal display.
    # Keep it in snapshots and rollback plans, including repeated recipes.
    return [{"recipe_id": str(x["recipe_id"]), "portions": int(x["portions"])} for x in items]


class MindfulChefClient:
    """Session cookies are created by CLI login, never imported from a browser."""

    def __init__(self, store: SecureStore | None = None, transport: httpx.BaseTransport | None = None):
        self.store = store if store is not None else SecureStore()

        def make_http() -> httpx.Client:
            return httpx.Client(
                base_url=ORIGIN, transport=transport, timeout=httpx.Timeout(30, connect=10),
                follow_redirects=False, trust_env=False,
                headers={"User-Agent": f"Mindful Chef CLI/{__version__} (unofficial personal meal planner)",
                         "Accept": "text/html,application/xhtml+xml"},
            )

        self.http = make_http()
        # The storefront omits structured recipe data for signed-in sessions.
        # Public reads use their own jar; account cookies never cross into it.
        self._public_http = make_http()
        self._session: dict | None = None
        self._loaded = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()
        self._public_http.close()

    def _load(self):
        if not self._loaded:
            self._session = self.store.load_session()
            self._loaded = True
            if self._session:
                if not isinstance(self._session.get("account_key"), str):
                    _fail("invalid_session", "Saved session is invalid. Log in again.")
                for record in self._session.get("cookies", []):
                    domain = record.get("domain", "")
                    if domain.lstrip(".") not in {"mindfulchef.com", "www.mindfulchef.com"}:
                        _fail("invalid_session", "Saved session contains an unexpected cookie domain.")
                    self.http.cookies.jar.set_cookie(Cookie(
                        version=0, name=record["name"], value=record["value"], port=None, port_specified=False,
                        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
                        path=record.get("path", "/"), path_specified=True, secure=True,
                        expires=record.get("expires"), discard=record.get("expires") is None,
                        comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False,
                    ))
        if not self._session:
            _fail("auth_required", "Run mindful_chef_cli auth login in an interactive terminal.")

    def _save(self):
        if self._session:
            self._session["cookies"] = [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "expires": c.expires} for c in self.http.cookies.jar
                if c.domain.lstrip(".") in {"mindfulchef.com", "www.mindfulchef.com"}
            ]
            self.store.save_session(self._session)

    def _request(self, method: str, path: str, fields: list | None = None, *, public: bool = False) -> httpx.Response:
        """Never retry requests. A 307/308 must not replay a mutation."""
        if public and method != "GET":
            _fail("invalid_request", "The public client permits read-only requests.")
        path = _path(path)
        original_method = method
        client = self._public_http if public else self.http
        for _ in range(6):
            try:
                kwargs = {}
                if fields is not None:
                    kwargs = {"content": urlencode(fields), "headers": {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": ORIGIN, "Referer": ORIGIN + path,
                    }}
                response = client.request(method, path, **kwargs)
            except httpx.RequestError:
                _fail("network_error", "Mindful Chef could not be reached. No automatic retry was made.")
            if response.is_redirect:
                target = _path(response.headers.get("location", ""))
                if original_method != "GET" and response.status_code in {307, 308}:
                    _fail("ambiguous_redirect", "Mutation redirect requires manual reconciliation; it was not replayed.")
                method, fields, path = "GET", None, target
                continue
            if response.status_code == 429:
                _fail("rate_limited", "Mindful Chef rate-limited the request. Wait before trying again.")
            if response.status_code in {401, 403}:
                _fail("auth_expired", "Sign-in expired or the request was blocked. Run auth login again.")
            if response.status_code >= 400:
                _fail("http_error", f"Mindful Chef returned HTTP {response.status_code}; response details withheld.")
            if len(response.content) > 12_000_000:
                _fail("schema_changed", "Unexpectedly large provider response.")
            return response
        _fail("redirect_limit", "Mindful Chef returned too many redirects.")

    def _get(self, path: str, *, auth: bool = True, public: bool = False) -> str:
        if auth and public:
            _fail("invalid_request", "Authenticated requests must use the account client.")
        if auth:
            self._load()
        response = self._request("GET", path, public=public)
        soup = BeautifulSoup(response.text, "html.parser")
        login_form = soup.select_one('form[action="/session"] input[type="password"]')
        if auth and (urlsplit(str(response.url)).path.startswith("/session") or login_form):
            _fail("auth_expired", "Sign-in expired. Run mindful_chef_cli auth login again.")
        if auth and path.split("?")[0] == "/my-account/orders" and not soup.select_one('a[href="/my-account/recipes"]'):
            _fail("schema_changed", "Authenticated orders marker is missing. Account access could not be verified.")
        if auth:
            self._save()
        return response.text

    def login(self, email: str, password: str) -> dict:
        if not email or "@" not in email or not password:
            _fail("invalid_credentials", "An email and password are required.")
        with self.store.lock():
            self.http.cookies.clear()
            self._session, self._loaded = None, True
            html = self._get("/session/new", auth=False)
            forms = [f for f in parse_forms(html) if _path(f.get("action") or "/") == "/session"
                     and {"email", "password"}.issubset({k for k, _ in f["fields"]})]
            if len(forms) != 1 or forms[0]["method"] != "post":
                _fail("schema_changed", "The login form changed. No credentials were submitted.")
            fields = [(k, v) for k, v in forms[0]["fields"] if k not in {"email", "password"}]
            if "_method" in dict(fields) or len([k for k, _ in fields if k == "authenticity_token"]) > 1:
                _fail("schema_changed", "Unexpected login form controls. No credentials were submitted.")
            if not any(k == "authenticity_token" and v for k, v in fields):
                fields.append(("authenticity_token", csrf(html)))
            fields.extend([("email", email.strip()), ("password", password)])
            result = self._request("POST", "/session", fields)
            del fields
            if BeautifulSoup(result.text, "html.parser").select_one('input[type="password"]'):
                self.http.cookies.clear()
                _fail("login_failed", "Sign-in failed or needs browser interaction. No password was retained.")
            self._session = {"version": 1, "account_key": sha256(email.strip().casefold().encode()).hexdigest()}
            try:
                check = self._request("GET", "/my-account/orders")
                if urlsplit(str(check.url)).path != "/my-account/orders":
                    _fail("login_failed", "Could not verify authenticated access. No session was saved.")
                if not BeautifulSoup(check.text, "html.parser").select_one('a[href="/my-account/recipes"]'):
                    _fail("schema_changed", "Authenticated account layout changed. No session was saved.")
                self._save()
            except Exception:
                self._session = None
                self.http.cookies.clear()
                raise
            return {"authenticated": True, "profile": self.store.profile, "password_stored": False,
                    "session_storage": "os_keyring"}

    def auth_status(self) -> dict:
        with self.store.lock():
            self._get("/my-account/orders")
            return {"authenticated": True, "profile": self.store.profile, "session_storage": "os_keyring"}

    def refresh(self) -> dict:
        return self.auth_status()

    def logout(self) -> dict:
        with self.store.lock():
            self.store.delete_session()
            self.http.cookies.clear()
            self._session, self._loaded = None, True
            return {"authenticated": False, "local_session_removed": True,
                    "server_session_revoked": False}

    def orders(self, past: bool = False, page: int = 1) -> list:
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
            _fail("invalid_page", "Page must be from 1 to 100.")
        with self.store.lock():
            path = "/my-account/orders" + ("/past" if past else "")
            html = self._get(path + (f"?page={page}" if page > 1 else ""))
            return [{k: v for k, v in order.items() if k != "unskip_form"} for order in parse_orders(html)]

    def _menu(self, delivery_date: str) -> dict:
        html = self._get(f"/my-account/deliveries/{_date(delivery_date)}")
        menu = parse_menu(html)
        if not menu.get("cart_form") or not menu.get("recipes"):
            _fail("not_editable", "Delivery is unavailable, locked, or the menu contract changed.")
        form = menu["cart_form"]
        if _path(form["action"]) != f"/my-account/deliveries/{delivery_date}" or form["method"] != "post":
            _fail("schema_changed", "Unexpected delivery form. Writes are disabled.")
        if menu.get("delivery_date") != delivery_date:
            _fail("schema_changed", "Delivery form date does not match the requested date.")
        overrides = [v.lower() for k, v in form["fields"] if k == "_method"]
        if overrides != ["patch"]:
            _fail("schema_changed", "Unexpected delivery update method. Writes are disabled.")
        if len([k for k, _ in form["fields"] if k == "authenticity_token"]) != 1:
            _fail("schema_changed", "Unexpected CSRF controls. Writes are disabled.")
        return menu

    def menu(self, delivery_date: str, details: bool = False) -> dict:
        with self.store.lock():
            menu = self._menu(delivery_date)
            if details:
                available = 0
                unavailable = 0
                for item in menu["recipes"]:
                    try:
                        if not item.get("url"):
                            _fail("recipe_detail_unavailable", "This recipe has no available detail link.")
                        extra = self.recipe(item["url"])
                    except ClientError as error:
                        if error.code != "recipe_detail_unavailable":
                            raise
                        # A single missing detail page must not erase the menu.
                        # Unknown facts remain unknown for hard planner constraints.
                        item.update({key: None for key in ("ingredients", "allergens", "may_contain", "steps",
                                                          "nutrition", "allergen_source")})
                        item["detail_status"] = "unavailable"
                        item["detail_warning"] = "Detailed recipe metadata is unavailable; missing facts remain unknown."
                        unavailable += 1
                    else:
                        # The live menu owns variant identity, selection price and
                        # source link; detail metadata supplements those facts.
                        identity = {"id", "recipe_id", "name", "url", "price_cents", "portions", "protein",
                                    "food_group", "within_preferences"}
                        item.update({k: v for k, v in extra.items() if v is not None and k not in identity})
                        item["detail_status"] = "available"
                        available += 1
                total = len(menu["recipes"])
                menu["detail_coverage"] = {"total": total, "available": available, "unavailable": unavailable,
                                           "complete": unavailable == 0}
                menu["warnings"] = ([f"Detailed metadata unavailable for {unavailable} of {total} recipes; "
                                      "missing facts remain unknown."] if unavailable else [])
            return _public_menu(menu)

    def recipe(self, slug_or_path: str) -> dict:
        if not isinstance(slug_or_path, str):
            _fail("invalid_recipe", "Supply a recipe slug or Mindful Chef recipe URL.")
        path = _path(slug_or_path if "/" in slug_or_path else "/healthy-recipes/" + slug_or_path)
        if not re.fullmatch(r"/healthy-recipes/[a-zA-Z0-9_-]+(?:\?[^#]*)?", path):
            _fail("invalid_recipe", "Only a Mindful Chef recipe detail path is permitted.")
        # Drop account-specific delivery tracking parameters before returning data.
        availability = parse_qs(urlsplit(path).query).get("availability_id", [])
        path = path.split("?")[0]
        if availability:
            if len(availability) != 1 or not _ID.fullmatch(availability[0]):
                _fail("invalid_recipe", "Invalid recipe availability identifier.")
            path += "?" + urlencode({"availability_id": availability[0]})
        with self.store.lock():
            html = self._get(path, auth=False, public=True)
            if BeautifulSoup(html, "html.parser").select_one('form[action="/session"] input[type="password"]'):
                _fail("auth_expired", "The recipe page requires sign-in. Run mindful_chef_cli auth login again.")
            try:
                result = parse_recipe(html)
            except ValueError:
                _fail("recipe_detail_unavailable", "Detailed recipe metadata is unavailable on this provider page.")
            result.pop("provider_html", None)
            result["url"] = ORIGIN + path.split("?")[0]
            return result

    def _state(self, delivery_date: str, *, require_menu: bool = False) -> dict:
        orders = self.orders()
        matching = [o for o in orders if o.get("delivery_date") == delivery_date]
        if len(matching) != 1:
            _fail("unknown_delivery", "Could not uniquely locate that delivery in upcoming orders.")
        order = matching[0]
        status = order.get("status", "unknown").casefold()
        if status not in {"scheduled", "skipped"}:
            _fail("not_editable", "Delivery is not scheduled or skipped; no mutation can be prepared.")
        skipped = "skip" in status
        if skipped and not require_menu:
            return {"delivery_date": delivery_date, "skipped": True, "selection": None,
                    "portion_count": None, "preserved_fields": None}
        menu = self._menu(delivery_date)
        return self._snapshot(menu, skipped=skipped)

    @staticmethod
    def _snapshot(menu: dict, *, skipped: bool) -> dict:
        preserved = [(k, v) for k, v in menu["cart_form"]["fields"]
                     if not _CONTENT.fullmatch(k) and k not in {"authenticity_token", "_method",
                                                              "delivery[recurring_items_loaded_at]"}]
        return {"delivery_date": menu["delivery_date"], "skipped": skipped,
                "selection": _selection(menu.get("selection", [])),
                "portion_count": menu.get("portion_count"), "preserved_fields": preserved}

    def _create_plan(self, action: str, delivery_date: str, before: dict, after: dict,
                     summary: dict) -> dict:
        self._load()
        self.store.claim_writer(self._session["account_key"])
        self._check_hold(delivery_date)
        plan_id = uuid.uuid4().hex
        plan = {"id": plan_id, "version": 1, "action": action, "delivery_date": delivery_date,
                "created_at": datetime.now(timezone.utc).isoformat(), "expires_after_seconds": 900,
                "account_key": self._session["account_key"], "before": before, "after": after,
                "summary": summary, "status": "proposed"}
        plan["approval"] = _digest(plan)
        self.store.write_private_json(f"plan-{plan_id}.json", plan)
        return self._review(plan)

    @staticmethod
    def _review(plan: dict) -> dict:
        return {k: v for k, v in plan.items() if k not in {"account_key", "before", "after"}}

    def propose_selection(self, delivery_date: str, recipe_ids: list[str],
                          max_total_cents: int | None = None) -> dict:
        delivery_date = _date(delivery_date)
        ids = [str(i) for i in recipe_ids]
        if not ids or any(not _ID.fullmatch(i) for i in ids):
            _fail("invalid_selection", "Supply one or more numeric recipe IDs from the live menu.")
        if max_total_cents is not None and (isinstance(max_total_cents, bool)
                or not isinstance(max_total_cents, int) or max_total_cents < 0):
            _fail("invalid_budget", "max_total_cents must be a nonnegative integer.")
        with self.store.lock():
            self._load()
            self._check_hold(delivery_date)
            before = self._state(delivery_date, require_menu=True)
            if before["skipped"]:
                _fail("delivery_skipped", "Resume the delivery explicitly before choosing meals.")
            menu = self._menu(delivery_date)
            by_id = {str(r.get("id", r.get("recipe_id"))): r for r in menu["recipes"]}
            if any(i not in by_id for i in ids):
                _fail("unavailable_recipe", "A selected recipe is absent from the current menu.")
            if not isinstance(menu.get("min_recipes"), int) or not isinstance(menu.get("max_recipes"), int):
                _fail("schema_changed", "Cannot verify recipe-count limits.")
            if not menu["min_recipes"] <= len(ids) <= menu["max_recipes"]:
                _fail("invalid_selection", "Recipe count is outside this delivery's limits.")
            if not before["portion_count"] or not before["selection"]:
                _fail("schema_changed", "Cannot verify existing portion count and selections.")
            if any(not isinstance(by_id[i].get("price_cents"), int) for i in ids):
                _fail("price_unknown", "Cannot verify recipe prices. No change was planned.")
            # Refuse product extras rather than risk changing recurring products or pricing.
            if menu.get("products_preserved") or any(v for k, v in menu["cart_form"]["fields"]
                                                     if "cancelled_product_ids" in k):
                _fail("extras_present", "This delivery contains extras. Review it on the website; CLI changes are disabled.")
            if any(v not in {"", "0", "false"} for k, v in menu["cart_form"]["fields"]
                   if k == "delivery[set_subscription_default_delivery_option]"):
                _fail("schema_changed", "The form would change subscription defaults. No change was planned.")
            shipping = menu.get("shipping_amount_cents")
            if not isinstance(shipping, int):
                _fail("price_unknown", "Cannot verify delivery charges. No change was planned.")
            total = sum(by_id[i]["price_cents"] for i in ids) + shipping
            if isinstance(menu.get("min_order_amount_cents"), int) and total - shipping < menu["min_order_amount_cents"]:
                _fail("minimum_order", "Selection is below this delivery's minimum order amount.")
            if max_total_cents is None or total > max_total_cents:
                _fail("budget_required", "Set --max-total-cents to cover the reviewed recipe and delivery total.")
            after = dict(before, selection=_selection([{"recipe_id": i, "portions": before["portion_count"]}
                                                     for i in ids]))
            summary = {"before_recipes": before["selection"],
                       "after_recipes": [{"recipe_id": i, "name": by_id[i].get("name", by_id[i].get("title")),
                                          "price_cents": by_id[i]["price_cents"]} for i in ids],
                       "portions": before["portion_count"], "estimated_total_cents": total,
                       "max_total_cents": max_total_cents, "currency": "GBP",
                       "pricing_note": "Undiscounted recipe subtotal plus delivery; final provider discounts may reduce it.",
                       "extras": "none"}
            return self._create_plan("select", delivery_date, before, after, summary)

    def _skip_form(self, delivery_date: str) -> dict:
        html = self._get(f"/my-account/deliveries/{delivery_date}/skips/new?step=away")
        expected = f"/my-account/deliveries/{delivery_date}/skips"
        forms = [f for f in parse_forms(html) if f.get("action") and _path(f["action"]) == expected]
        if len(forms) != 1 or forms[0]["method"] != "post":
            _fail("schema_changed", "The skip form changed. No write was made.")
        if "skip_survey[primary_reason]" not in dict(forms[0]["fields"]):
            _fail("schema_changed", "The holiday skip reason is unavailable.")
        if "_method" in dict(forms[0]["fields"]) or len([k for k, _ in forms[0]["fields"] if k == "authenticity_token"]) != 1:
            _fail("schema_changed", "Unexpected skip form controls. No write was made.")
        return forms[0]

    def _resume_form(self, delivery_date: str) -> dict:
        # Only use a provider-emitted form; never invent an unskip route.
        expected = f"/my-account/deliveries/{delivery_date}/skips"
        html = self._get("/my-account/orders")
        forms = [f for f in parse_forms(html) if f.get("action") and _path(f["action"]) == expected
                 and f["method"] == "post"
                 and [v.lower() for k, v in f["fields"] if k == "_method"] == ["delete"]
                 and len([k for k, v in f["fields"] if k == "authenticity_token"]) == 1
                 and bool(dict(f["fields"]).get("authenticity_token"))]
        if len(forms) == 1:
            return forms[0]
        if forms:
            _fail("unsupported_resume", "Ambiguous restore controls. No write was made.")
        # The current storefront links to a dedicated confirmation page.
        # Follow that exact emitted date-scoped link and validate its POST form.
        page_path = f"/my-account/deliveries/{delivery_date}/restores/new"
        soup = BeautifulSoup(html, "html.parser")
        links = [a["href"] for a in soup.select("a[href]")
                 if a["href"] in {page_path, ORIGIN + page_path}]
        if len(links) != 1:
            _fail("unsupported_resume", "No verified restore page is available. No write was made.")
        restore_html = self._get(links[0])
        restore_path = f"/my-account/deliveries/{delivery_date}/restores"
        forms = [f for f in parse_forms(restore_html) if f.get("action")
                 and _path(f["action"]) == restore_path and f["method"] == "post"
                 and [k for k, _ in f["fields"]] == ["authenticity_token"]
                 and bool(dict(f["fields"]).get("authenticity_token"))]
        if len(forms) != 1:
            _fail("unsupported_resume", "The restore confirmation form changed. No write was made.")
        return forms[0]

    def propose_skip(self, delivery_date: str) -> dict:
        delivery_date = _date(delivery_date)
        with self.store.lock():
            self._load()
            self._check_hold(delivery_date)
            before = self._state(delivery_date)
            if before["skipped"]:
                _fail("already_skipped", "This delivery is already skipped.")
            self._skip_form(delivery_date)
            return self._create_plan("skip", delivery_date, before, dict(before, skipped=True),
                                     {"effect": "Skip this delivery", "reason": "away_or_holiday",
                                      "before_recipes": before["selection"], "payment": "no new order"})

    def _resume_preview(self, delivery_date: str, max_total_cents: int | None) -> tuple[dict, dict]:
        form = self._resume_form(delivery_date)
        menu = self._menu(delivery_date)
        selection = _selection(menu.get("selection", []))
        by_id = {str(r.get("id", r.get("recipe_id"))): r for r in menu["recipes"]}
        pending_cancellations = any(v for k, v in menu["cart_form"]["fields"] if "cancelled_product_ids" in k)
        if (not selection or menu.get("products_preserved") or pending_cancellations
                or not isinstance(menu.get("shipping_amount_cents"), int)):
            _fail("resume_requires_website", "Restored contents or price cannot be verified. Resume on the website.")
        if any(i["recipe_id"] not in by_id or not isinstance(by_id[i["recipe_id"]].get("price_cents"), int)
               for i in selection):
            _fail("price_unknown", "Restored recipes have unknown prices. Resume on the website.")
        total = sum(by_id[i["recipe_id"]]["price_cents"] for i in selection) + menu["shipping_amount_cents"]
        if (isinstance(max_total_cents, bool) or not isinstance(max_total_cents, int)
                or max_total_cents < total):
            _fail("budget_required", "Set --max-total-cents to cover the reviewed restored meals and delivery.")
        return form, {"selection": selection, "portion_count": menu["portion_count"],
                      "estimated_total_cents": total,
                      "recipes": [{"recipe_id": i["recipe_id"], "name": by_id[i["recipe_id"]]["name"],
                                   "price_cents": by_id[i["recipe_id"]]["price_cents"]} for i in selection]}

    def propose_resume(self, delivery_date: str, max_total_cents: int | None = None) -> dict:
        delivery_date = _date(delivery_date)
        with self.store.lock():
            self._load()
            self._check_hold(delivery_date)
            before = self._state(delivery_date)
            if not before["skipped"]:
                _fail("already_active", "This delivery is already active.")
            _, preview = self._resume_preview(delivery_date, max_total_cents)
            after = dict(before, skipped=False, selection=preview["selection"], portion_count=preview["portion_count"])
            return self._create_plan("resume", delivery_date, before, after,
                                     {"effect": "Resume paid delivery", "after_recipes": preview["recipes"],
                                      "estimated_total_cents": preview["estimated_total_cents"],
                                      "max_total_cents": max_total_cents, "currency": "GBP",
                                      "portions": preview["portion_count"], "extras": "none"})

    def _read_plan(self, plan_id: str) -> dict:
        if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
            _fail("invalid_plan", "Invalid plan ID.")
        plan = self.store.read_private_json(f"plan-{plan_id}.json")
        if not isinstance(plan, dict) or plan.get("id") != plan_id:
            _fail("unknown_plan", "Plan was not found in this profile.")
        return plan

    def show_plan(self, plan_id: str) -> dict:
        with self.store.lock():
            return self._review(self._read_plan(plan_id))

    def _verified(self, plan: dict, current: dict) -> bool:
        if plan["action"] == "skip":
            return current["skipped"] == plan["after"]["skipped"]
        if plan["action"] == "resume":
            return (not current["skipped"] and current["selection"] == plan["after"]["selection"]
                    and current["portion_count"] == plan["after"]["portion_count"])
        return _digest(current) == _digest(plan["after"])

    def _hold_key(self, delivery_date: str) -> str:
        return _digest([self._session["account_key"], delivery_date])

    def _check_hold(self, delivery_date: str) -> None:
        holds = self.store.read_private_json("holds.json", {})
        if self._hold_key(delivery_date) in holds:
            _fail("delivery_on_hold", "An earlier write for this delivery is unresolved. Reconcile its plan; do not create another.")

    def _set_hold(self, plan: dict, *, clear: bool = False) -> None:
        holds = self.store.read_private_json("holds.json", {})
        key = self._hold_key(plan["delivery_date"])
        if clear:
            if holds.get(key) == plan["id"]:
                del holds[key]
        else:
            holds[key] = plan["id"]
        self.store.write_private_json("holds.json", holds)

    def apply_plan(self, plan_id: str, approval: str) -> dict:
        with self.store.lock():
            self._load()
            plan = self._read_plan(plan_id)
            if plan["status"] != "proposed":
                _fail("no_replay", "This plan has already been attempted. Use changes reconcile; do not replay it.")
            self._check_hold(plan["delivery_date"])
            if not hmac.compare_digest(str(approval), plan.get("approval", "")):
                _fail("approval_required", "Supply the approval hash from the exact reviewed plan.")
            candidate = {k: v for k, v in plan.items() if k != "approval"}
            if not hmac.compare_digest(_digest(candidate), plan["approval"]):
                _fail("plan_modified", "Plan changed after review; create a new proposal.")
            if plan["account_key"] != self._session["account_key"]:
                _fail("account_changed", "This plan belongs to a different login. Create a new proposal.")
            self.store.claim_writer(self._session["account_key"])
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])).total_seconds()
            if not 0 <= age <= 900:
                _fail("plan_expired", "Plan expired. Create and review a fresh proposal.")
            current = self._state(plan["delivery_date"])
            # Persisted tuples become lists; compare canonical JSON rather than Python containers.
            if _digest(current) != _digest(plan["before"]):
                _fail("state_changed", "Delivery changed since review. Create a new proposal.")
            if _digest(plan["before"]) == _digest(plan["after"]):
                plan["status"] = "confirmed_noop"
                self.store.write_private_json(f"plan-{plan_id}.json", plan)
                return self._review(plan)
            if plan["action"] == "select":
                menu = self._menu(plan["delivery_date"])
                if _digest(self._snapshot(menu, skipped=False)) != _digest(plan["before"]):
                    _fail("state_changed", "Delivery changed during the final review check. Create a new proposal.")
                by_id = {str(r.get("id", r.get("recipe_id"))): r for r in menu["recipes"]}
                ids = [r["recipe_id"] for r in plan["after"]["selection"]]
                if any(i not in by_id for i in ids):
                    _fail("state_changed", "A recipe is no longer available.")
                if not isinstance(menu.get("shipping_amount_cents"), int) or any(
                        not isinstance(by_id[i].get("price_cents"), int) for i in ids):
                    _fail("price_unknown", "Live recipe or delivery prices are unavailable. No write was made.")
                total = sum(by_id[i]["price_cents"] for i in ids) + menu["shipping_amount_cents"]
                if total != plan["summary"]["estimated_total_cents"] or total > plan["summary"]["max_total_cents"]:
                    _fail("price_changed", "Price changed since review. Create a new proposal.")
                form = menu["cart_form"]
                fields = [(k, v) for k, v in form["fields"] if not _CONTENT.fullmatch(k)]
                for i, item in enumerate(plan["after"]["selection"]):
                    fields += [(f"delivery[contents_attributes][{i}][recipe_id]", item["recipe_id"]),
                               (f"delivery[contents_attributes][{i}][portions]", str(item["portions"]))]
            elif plan["action"] == "skip":
                form = self._skip_form(plan["delivery_date"])
                fields = form["fields"]
            elif plan["action"] == "resume":
                form, preview = self._resume_preview(plan["delivery_date"], plan["summary"]["max_total_cents"])
                if (preview["selection"] != plan["after"]["selection"]
                        or preview["portion_count"] != plan["after"]["portion_count"]
                        or preview["estimated_total_cents"] != plan["summary"]["estimated_total_cents"]):
                    _fail("state_changed", "Restored meals or price changed. Create a new proposal.")
                fields = form["fields"]
            else:
                _fail("invalid_plan", "Unknown plan action.")
            if not dict(fields).get("authenticity_token"):
                _fail("schema_changed", "CSRF token is missing; no write was made.")
            plan["status"] = "pending"
            plan["attempted_at"] = datetime.now(timezone.utc).isoformat()
            self._set_hold(plan)
            self.store.write_private_json(f"plan-{plan_id}.json", plan)
            try:
                self._request("POST", form["action"], fields)
                self._save()
                after = self._state(plan["delivery_date"])
                plan["status"] = "confirmed" if self._verified(plan, after) else "unknown"
            except (ClientError, ValueError):
                plan["status"] = "unknown"
            self.store.write_private_json(f"plan-{plan_id}.json", plan)
            if plan["status"] == "confirmed":
                self._set_hold(plan, clear=True)
            if plan["status"] != "confirmed":
                _fail("write_uncertain", "Write outcome is uncertain. Use changes reconcile; never replay this plan.")
            return self._review(plan)

    def reconcile_plan(self, plan_id: str) -> dict:
        with self.store.lock():
            self._load()
            plan = self._read_plan(plan_id)
            if plan["account_key"] != self._session["account_key"]:
                _fail("account_changed", "This receipt belongs to another account.")
            current = self._state(plan["delivery_date"])
            matches = self._verified(plan, current)
            if plan["status"] in {"pending", "unknown"} and matches:
                plan["status"] = "reconciled"
                self.store.write_private_json(f"plan-{plan_id}.json", plan)
                self._set_hold(plan, clear=True)
            return {"id": plan_id, "attempt_status": plan["status"],
                    "current_matches_requested": matches,
                    "current_matches_original": _digest(current) == _digest(plan["before"]),
                    "note": "Read-only observation; matching state does not prove which actor changed it.",
                    "replay_permitted": False}

    def rollback_plan(self, plan_id: str) -> dict:
        with self.store.lock():
            plan = self._read_plan(plan_id)
            self._load()
            if plan["account_key"] != self._session["account_key"]:
                _fail("account_changed", "This receipt belongs to another account.")
            if plan["status"] != "confirmed":
                _fail("rollback_unavailable", "Only a confirmed change can produce a rollback proposal.")
            if plan["action"] != "select":
                _fail("rollback_requires_website", "Restore skipped delivery on the website and review its price and contents.")
            current = self._state(plan["delivery_date"])
            if _digest(current) != _digest(plan["after"]):
                _fail("state_changed", "Delivery changed after the write. Rollback would overwrite someone else's changes.")
            return self.propose_selection(plan["delivery_date"], [i["recipe_id"] for i in plan["before"]["selection"]],
                                          max_total_cents=plan["summary"]["max_total_cents"])

    def doctor(self, authenticated: bool = False) -> dict:
        checks = []
        try:
            html = self._get("/session/new", auth=False, public=True)
            forms = parse_forms(html)
            login_ok = any(f["action"] == "/session" and {"email", "password"}.issubset(dict(f["fields"]))
                           for f in forms)
            checks.append({"name": "login_form", "ok": login_ok and bool(csrf(html))})
            sample = parse_menu(self._get("/choose-recipes", auth=False, public=True))
            checks.append({"name": "public_menu", "ok": bool(sample.get("recipes")),
                           "recipe_count": len(sample.get("recipes", []))})
            if sample.get("recipes") and sample["recipes"][0].get("url"):
                detail = self.recipe(sample["recipes"][0]["url"])
                checks.append({"name": "recipe_detail", "ok": bool(detail.get("ingredients") and detail.get("steps"))})
            if authenticated:
                orders = self.orders()
                checks.append({"name": "authenticated_orders", "ok": bool(orders)})
                editable = next((o for o in orders if o.get("edit_url")), None)
                if editable:
                    menu = self._menu(editable["delivery_date"])
                    checks.append({"name": "account_cart_contract", "ok": bool(menu["cart_form"] and menu["selection"])})
                else:
                    checks.append({"name": "account_cart_contract", "ok": None, "reason": "no editable delivery"})
        except ClientError as exc:
            checks.append({"name": "request", "ok": False, "code": exc.code})
        except ValueError:
            checks.append({"name": "schema", "ok": False, "code": "schema_changed"})
        return {"healthy": bool(checks) and all(c["ok"] is True for c in checks), "checks": checks,
                "authenticated_checked": authenticated, "writes_tested": False,
                "checked_at": datetime.now(timezone.utc).isoformat(), "version": __version__}
