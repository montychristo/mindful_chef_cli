"""Mock HTTP integration tests; these never contact or mutate a live account."""

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from mindful_chef_cli.client import ClientError, MindfulChefClient
from mindful_chef_cli.storage import SecureStore


DAY = "2030-01-07"
LOGIN = '''<meta name="csrf-token" content="login-csrf-synthetic">
<form action="/session" method="post"><input name="authenticity_token" value="login-csrf-synthetic"><input name="email" type="email"><input name="password" type="password"></form>'''


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        self.values[service, username] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


class Storefront:
    """Small stateful fake of just the observed form and readback contracts."""

    def __init__(self):
        self.selection = ["101", "102"]
        self.prices = {"101": 1500, "102": 1600, "103": 1700}
        self.skipped = False
        self.requests = []
        self.posts = []
        self.expired = False
        self.login_failed = False
        self.fail_write = False
        self.redirect_write = False
        self.skip_schema_changed = False
        self.hidden_date = DAY
        self.method_override = "patch"
        self.with_extras = False
        self.redirect_login_to = None
        self.login_override = None
        self.skip_override = None
        self.maintenance = False
        self.cancelled_product_ids = []
        self.delivery_gets = 0
        self.race_at_delivery_get = None
        self.resume_method = "post"
        self.resume_extra_fields = ""
        self.menu_extra_fields = ""
        self.malformed_price_at_delivery_get = None
        self.restore_page = False
        self.restore_form_method = "post"
        self.restore_extra_fields = ""
        self.recipe_pages = {}
        self.recipe_statuses = {}
        self.recipe_timeouts = set()
        self.suppress_signed_in_recipe = False
        self.login_csrf_cookie = False
        self.public_recipe_cookie = False

    def orders(self):
        status = "Skipped" if self.skipped else "Scheduled"
        resume = f'''<form action="/my-account/deliveries/{DAY}/skips" method="{self.resume_method}"><input name="_method" value="delete"><input name="authenticity_token" value="resume-csrf-synthetic">{self.resume_extra_fields}<button>Unskip</button></form>''' if self.skipped else ""
        if self.skipped and self.restore_page:
            resume = f'<a href="/my-account/deliveries/{DAY}/restores/new">Restore</a>'
        return f'''<a href="/my-account/recipes">My recipes</a>
        <main><div><h2>Example Monday</h2><p>{status}</p><a href="/my-account/deliveries/{DAY}/details">Details</a><a href="/my-account/deliveries/{DAY}">Edit</a></div>
        <div><details aria-label="Delivery details {DAY}"><p>Chosen by you</p>{resume}<a href="/healthy-recipes/example">Example meal</a></details></div></main>'''

    def menu(self):
        cards = "".join(f'''<div data-cart-item-type-value="recipe" data-cart-item-id-value="{recipe_id}" data-cart-item-title-value="Example recipe {recipe_id}" data-cart-item-price-cents-value="{price}" data-cart-item-portions-value="2"><a href="/healthy-recipes/example-{recipe_id}">Example recipe</a></div>''' for recipe_id, price in self.prices.items())
        selected = "".join(f'''<input name="delivery[contents_attributes][{i}][recipe_id]" value="{recipe_id}"><input name="delivery[contents_attributes][{i}][portions]" value="2">''' for i, recipe_id in enumerate(self.selection))
        extras = '<input name="delivery[expanded_product_contents_attributes][p][product_id]" value="500"><input name="delivery[expanded_product_contents_attributes][p][quantity]" value="1">' if self.with_extras else ""
        cancellations = "".join(f'<input name="delivery[cancelled_product_ids][]" value="{product_id}">' for product_id in self.cancelled_product_ids)
        return '<a href="/my-account/recipes">My recipes</a>' + cards + f'''<form id="web_cart_form" action="/my-account/deliveries/{DAY}" method="post" data-cart-min-recipe-count-value="2" data-cart-max-recipe-count-value="5" data-cart-shipping-amount-value="0" data-cart-min-order-amount-value="2500">
          <input name="authenticity_token" value="cart-csrf-synthetic"><input name="_method" value="{self.method_override}">
          <input name="delivery[delivery_date]" value="{self.hidden_date}"><input name="delivery[portion_count_per_meal]" value="2"><input name="delivery[recurring_items_loaded_at]" value="2030-01-01T10:00:00Z">{selected}{extras}{cancellations}{self.menu_extra_fields}</form>'''

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST":
            self.posts.append(request)
            fields = parse_qs(request.content.decode(), keep_blank_values=True)
            if path == "/session":
                if self.redirect_login_to:
                    return httpx.Response(307, headers={"Location": self.redirect_login_to})
                return httpx.Response(200, text=LOGIN if self.login_failed else self.orders(), headers={"Set-Cookie": "synthetic-session=synthetic-value; Path=/; Secure; HttpOnly"})
            if self.fail_write:
                raise httpx.ReadTimeout("Synthetic uncertain response", request=request)
            if self.redirect_write:
                return httpx.Response(307, headers={"Location": f"/my-account/deliveries/{DAY}"})
            if path == f"/my-account/deliveries/{DAY}":
                self.selection = [values[0] for key, values in fields.items() if key.endswith("[recipe_id]")]
                return httpx.Response(200, text=self.menu())
            if path == f"/my-account/deliveries/{DAY}/skips":
                self.skipped = fields.get("_method") != ["delete"]
                return httpx.Response(200, text=self.orders())
            if path == f"/my-account/deliveries/{DAY}/restores":
                self.skipped = False
                return httpx.Response(200, text=self.orders())
            return httpx.Response(404)
        if path == "/session/new":
            override = f'<input name="_method" value="{self.login_override}">' if self.login_override else ""
            headers = ({"Set-Cookie": "synthetic-csrf-cookie=synthetic-csrf-value; Path=/; Secure; HttpOnly"}
                       if self.login_csrf_cookie else {})
            return httpx.Response(200, text=LOGIN.replace("</form>", override + "</form>"), headers=headers)
        if self.expired:
            return httpx.Response(200, text=LOGIN)
        if self.maintenance:
            return httpx.Response(200, text="<h1>Scheduled maintenance</h1>")
        if path.startswith("/healthy-recipes/"):
            if path in self.recipe_timeouts:
                raise httpx.ReadTimeout("Synthetic recipe timeout", request=request)
            if self.suppress_signed_in_recipe and "synthetic-account=" in request.headers.get("cookie", ""):
                return httpx.Response(200, text="<h1>Signed-in view has no structured recipe</h1>")
            headers = ({"Set-Cookie": "synthetic-public-cookie=synthetic-public-value; Path=/; Secure; HttpOnly"}
                       if self.public_recipe_cookie else {})
            return httpx.Response(self.recipe_statuses.get(path, 200),
                                  text=self.recipe_pages.get(path, "<h1>Recipe details unavailable</h1>"), headers=headers)
        if path == "/choose-recipes":
            return httpx.Response(200, text=self.menu())
        if path == "/my-account/orders":
            return httpx.Response(200, text=self.orders())
        if path == f"/my-account/deliveries/{DAY}/restores/new":
            return httpx.Response(200, text=f'<form action="/my-account/deliveries/{DAY}/restores" method="{self.restore_form_method}"><input name="authenticity_token" value="restore-csrf-synthetic">{self.restore_extra_fields}</form>')
        if path == f"/my-account/deliveries/{DAY}":
            self.delivery_gets += 1
            if self.delivery_gets == self.race_at_delivery_get:
                self.selection = ["101", "103"]
            if self.delivery_gets == self.malformed_price_at_delivery_get:
                self.prices["103"] = "unavailable"
            return httpx.Response(200, text=self.menu())
        if path == f"/my-account/deliveries/{DAY}/skips/new":
            reason = "" if self.skip_schema_changed else '<input name="skip_survey[primary_reason]" value="synthetic-away">'
            override = f'<input name="_method" value="{self.skip_override}">' if self.skip_override else ""
            return httpx.Response(200, text=f'''<a href="/my-account/recipes">My recipes</a><form action="/my-account/deliveries/{DAY}/skips" method="post"><input name="authenticity_token" value="skip-csrf-synthetic">{reason}{override}</form>''')
        return httpx.Response(404)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.backend = MemoryKeyring()
        self.store = SecureStore(state_dir=self.root / "state", backend=self.backend)
        self.site = Storefront()
        self.client = MindfulChefClient(self.store, transport=httpx.MockTransport(self.site))

    def tearDown(self):
        self.client.http.close()
        self.client._public_http.close()
        self.temp.cleanup()

    def authenticate(self):
        self.store.save_session({"version": 1, "account_key": "a" * 64, "cookies": []})

    def plan(self):
        self.authenticate()
        return self.client.propose_selection(DAY, ["102", "103"], max_total_cents=4000)

    def assert_error(self, code, action):
        with self.assertRaises(ClientError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_change_and_rollback_preserve_recipe_display_order(self):
        self.authenticate()
        self.site.selection = ["103", "101"]
        plan = self.client.propose_selection(DAY, ["102", "101"], max_total_cents=5000)
        self.client.apply_plan(plan["id"], plan["approval"])
        self.assertEqual(self.site.selection, ["102", "101"])
        restoration = self.client.rollback_plan(plan["id"])
        self.client.apply_plan(restoration["id"], restoration["approval"])
        self.assertEqual(self.site.selection, ["103", "101"])

    def test_resume_follows_emitted_restore_confirmation_page(self):
        self.authenticate()
        self.site.skipped = True
        self.site.restore_page = True
        original_orders = self.site.orders
        self.site.orders = lambda: original_orders() + '<a href="https://help.example.org/">Help</a>'
        plan = self.client.propose_resume(DAY, max_total_cents=4000)
        result = self.client.apply_plan(plan["id"], plan["approval"])
        self.assertEqual(result["status"], "confirmed")
        self.assertFalse(self.site.skipped)
        self.assertEqual(self.site.posts[-1].url.path, f"/my-account/deliveries/{DAY}/restores")

    def test_restore_confirmation_rejects_override_and_duplicate_csrf(self):
        self.authenticate()
        self.site.skipped = True
        self.site.restore_page = True
        for fields in ('<input name="_method" value="delete">',
                       '<input name="authenticity_token" value="">'):
            self.site.restore_extra_fields = fields
            self.assert_error("unsupported_resume", lambda: self.client.propose_resume(DAY, max_total_cents=4000))
        self.assertEqual(self.site.posts, [])

    def test_login_submits_current_csrf_and_never_persists_password(self):
        result = self.client.login("planner@example.invalid", "synthetic-test-password")
        submitted = parse_qs(self.site.posts[0].content.decode())
        self.assertEqual(submitted["authenticity_token"], ["login-csrf-synthetic"])
        self.assertEqual(submitted["email"], ["planner@example.invalid"])
        self.assertEqual(submitted["password"], ["synthetic-test-password"])
        self.assertTrue(result["authenticated"])
        self.assertNotIn("synthetic-test-password", json.dumps(list(self.backend.values.values())))
        self.assertFalse(result["password_stored"])

    def test_failed_login_error_withholds_credentials(self):
        self.site.login_failed = True
        error = self.assert_error("login_failed", lambda: self.client.login("planner@example.invalid", "synthetic-test-password"))
        self.assertNotIn("synthetic-test-password", str(error))
        self.assertEqual(self.backend.values, {})

    def test_login_will_not_forward_credentials_to_redirect_host(self):
        self.site.redirect_login_to = "https://untrusted.example.invalid/collect"
        self.assert_error("unsafe_destination", lambda: self.client.login("planner@example.invalid", "synthetic-test-password"))
        self.assertEqual(len(self.site.posts), 1)
        self.assertTrue(all(request.url.host == "www.mindfulchef.com" for request in self.site.requests))

    def test_untrusted_recipe_origin_is_rejected_before_network(self):
        self.assert_error("unsafe_destination", lambda: self.client.recipe("https://untrusted.example.invalid/healthy-recipes/example"))
        self.assertEqual(self.site.requests, [])

    def test_expired_session_does_not_reauthenticate_or_retain_password(self):
        self.authenticate()
        self.site.expired = True
        self.assert_error("auth_expired", self.client.auth_status)
        self.assertEqual(len(self.site.posts), 0)

    def test_menu_output_excludes_form_csrf_and_preserved_private_fields(self):
        self.authenticate()
        result = self.client.menu(DAY)
        self.assertNotIn("cart_form", result)
        self.assertNotIn("products_preserved", result)
        self.assertNotIn("cart-csrf-synthetic", json.dumps(result))
        self.assertEqual(len(result["recipes"]), 3)

    def test_partial_recipe_details_keep_menu_identity_and_unknown_facts(self):
        self.authenticate()
        detail = {"@type": "Recipe", "identifier": "999", "name": "Detailed name differs",
                  "recipeIngredient": ["1 carrot"], "recipeInstructions": [{"text": "Cook the carrot."}],
                  "nutrition": {"calories": "440 calories"}}
        self.site.recipe_pages["/healthy-recipes/example-101"] = (
            '<script type="application/ld+json">' + json.dumps(detail) + "</script>")
        self.site.recipe_pages["/healthy-recipes/example-102"] = "<h1>Synthetic unavailable detail body</h1>"
        self.site.recipe_pages["/healthy-recipes/example-103"] = self.site.recipe_pages["/healthy-recipes/example-101"]
        result = self.client.menu(DAY, details=True)
        recipes = {item["id"]: item for item in result["recipes"]}
        self.assertEqual(len(recipes), 3)
        self.assertEqual(result["detail_coverage"], {"total": 3, "available": 2, "unavailable": 1, "complete": False})
        self.assertEqual(recipes["101"]["detail_status"], "available")
        self.assertEqual(recipes["101"]["name"], "Example recipe 101")
        self.assertEqual(recipes["101"]["url"], "/healthy-recipes/example-101")
        self.assertEqual(recipes["101"]["price_cents"], 1500)
        self.assertEqual(recipes["101"]["ingredients"], ["1 carrot"])
        self.assertEqual(recipes["101"]["steps"], ["Cook the carrot."])
        self.assertEqual(recipes["102"]["detail_status"], "unavailable")
        for field in ("ingredients", "allergens", "may_contain", "steps", "nutrition"):
            self.assertIsNone(recipes["102"][field])
        self.assertEqual(recipes["102"]["name"], "Example recipe 102")
        self.assertEqual(recipes["102"]["price_cents"], 1600)
        self.assertTrue(recipes["102"]["detail_warning"])
        self.assertTrue(result["warnings"])
        self.assertNotIn("Synthetic unavailable detail body", json.dumps(result))
        self.assertEqual(self.site.posts, [])

    def test_all_recipe_details_available_reports_complete_coverage(self):
        self.authenticate()
        for recipe_id in self.site.prices:
            self.site.recipe_pages[f"/healthy-recipes/example-{recipe_id}"] = (
                '<script type="application/ld+json">'
                '{"@type":"Recipe","recipeIngredient":["1 carrot"],"recipeInstructions":["Cook"]}</script>')
        result = self.client.menu(DAY, details=True)
        self.assertEqual(result["detail_coverage"], {"total": 3, "available": 3, "unavailable": 0, "complete": True})
        self.assertEqual(result["warnings"], [])
        self.assertTrue(all(item["detail_status"] == "available" for item in result["recipes"]))

    def test_account_menu_reads_public_recipe_details_without_account_cookies(self):
        self.store.save_session({"version": 1, "account_key": "a" * 64, "cookies": [
            {"name": "synthetic-account", "value": "synthetic-account-value", "domain": "www.mindfulchef.com",
             "path": "/", "expires": None}]})
        self.site.suppress_signed_in_recipe = True
        self.site.public_recipe_cookie = True
        for recipe_id in self.site.prices:
            self.site.recipe_pages[f"/healthy-recipes/example-{recipe_id}"] = (
                '<script type="application/ld+json">'
                '{"@type":"Recipe","recipeIngredient":["1 carrot"],"recipeInstructions":["Cook"]}</script>')
        result = self.client.menu(DAY, details=True)
        self.assertTrue(result["detail_coverage"]["complete"])
        self.assertEqual(result["detail_coverage"]["available"], 3)
        recipe_reads = [request for request in self.site.requests if request.url.path.startswith("/healthy-recipes/")]
        self.assertEqual(len(recipe_reads), 3)
        self.assertTrue(all("synthetic-account=" not in request.headers.get("cookie", "") for request in recipe_reads))
        self.client.auth_status()
        account_reads = [request for request in self.site.requests if request.url.path.startswith("/my-account/")]
        self.assertTrue(all("synthetic-account=synthetic-account-value" in request.headers.get("cookie", "")
                            for request in account_reads))
        self.assertTrue(all("synthetic-public-cookie" not in request.headers.get("cookie", "")
                            for request in account_reads))
        saved_names = {item["name"] for item in self.store.load_session()["cookies"]}
        self.assertIn("synthetic-account", saved_names)
        self.assertNotIn("synthetic-public-cookie", saved_names)

    def test_login_get_and_post_share_csrf_cookie_but_not_public_cookie_jar(self):
        self.site.login_csrf_cookie = True
        self.site.public_recipe_cookie = True
        self.site.recipe_pages["/healthy-recipes/example-101"] = (
            '<script type="application/ld+json">{"@type":"Recipe","recipeIngredient":["1 carrot"]}</script>')
        self.client.recipe("example-101")
        result = self.client.login("planner@example.invalid", "synthetic-test-password")
        self.assertTrue(result["authenticated"])
        post = next(request for request in self.site.requests if request.method == "POST" and request.url.path == "/session")
        self.assertIn("synthetic-csrf-cookie=synthetic-csrf-value", post.headers.get("cookie", ""))
        self.assertNotIn("synthetic-public-cookie", post.headers.get("cookie", ""))
        fields = parse_qs(post.content.decode())
        self.assertEqual(fields["authenticity_token"], ["login-csrf-synthetic"])
        saved_names = {item["name"] for item in self.store.load_session()["cookies"]}
        self.assertIn("synthetic-session", saved_names)
        self.assertNotIn("synthetic-public-cookie", saved_names)

    def test_doctor_public_checks_use_isolated_jar_after_authentication(self):
        self.store.save_session({"version": 1, "account_key": "a" * 64, "cookies": [
            {"name": "synthetic-account", "value": "synthetic-account-value", "domain": "www.mindfulchef.com",
             "path": "/", "expires": None}]})
        self.site.suppress_signed_in_recipe = True
        self.site.recipe_pages["/healthy-recipes/example-101"] = (
            '<script type="application/ld+json">'
            '{"@type":"Recipe","recipeIngredient":["1 carrot"],"recipeInstructions":["Cook"]}</script>')
        self.client.auth_status()
        result = self.client.doctor(authenticated=True)
        self.assertTrue(result["healthy"])
        public_reads = [request for request in self.site.requests if not request.url.path.startswith("/my-account/")]
        self.assertEqual({request.url.path for request in public_reads},
                         {"/session/new", "/choose-recipes", "/healthy-recipes/example-101"})
        self.assertTrue(all("synthetic-account=" not in request.headers.get("cookie", "") for request in public_reads))

    def test_detail_transport_http_and_auth_failures_are_not_tolerated(self):
        self.authenticate()
        for status, code in [(429, "rate_limited"), (403, "auth_expired"), (500, "http_error")]:
            with self.subTest(status=status):
                self.site.recipe_statuses["/healthy-recipes/example-101"] = status
                before = len(self.site.requests)
                self.assert_error(code, lambda: self.client.menu(DAY, details=True))
                details = [r for r in self.site.requests[before:] if r.url.path.startswith("/healthy-recipes/")]
                self.assertEqual(len(details), 1)
        self.site.recipe_statuses.clear()
        self.site.recipe_timeouts.add("/healthy-recipes/example-101")
        before = len(self.site.requests)
        self.assert_error("network_error", lambda: self.client.menu(DAY, details=True))
        self.assertEqual(len([r for r in self.site.requests[before:] if r.url.path.startswith("/healthy-recipes/")]), 1)
        self.assertEqual(self.site.posts, [])

    def test_recipe_login_page_is_auth_error_not_missing_metadata(self):
        self.authenticate()
        self.site.recipe_pages["/healthy-recipes/example-101"] = LOGIN
        self.assert_error("auth_expired", lambda: self.client.menu(DAY, details=True))

    def test_cli_recipe_missing_metadata_returns_safe_error_code(self):
        from mindful_chef_cli.cli import main

        self.site.recipe_pages["/healthy-recipes/example-101"] = "<h1>Synthetic provider-only diagnostic</h1>"
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["recipe", "example-101"], store_factory=lambda **_: self.store,
                          client_factory=lambda store: MindfulChefClient(store, httpx.MockTransport(self.site)))
        self.assertEqual(result, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "recipe_detail_unavailable")
        self.assertNotIn("Synthetic provider-only diagnostic", output.getvalue())
        self.assertNotIn("<h1>", output.getvalue())

    def test_orders_output_excludes_resume_csrf(self):
        self.authenticate()
        self.site.skipped = True
        result = self.client.orders()
        self.assertEqual(result[0]["status"], "skipped")
        self.assertNotIn("resume-csrf-synthetic", json.dumps(result))
        self.assertNotIn("unskip_form", result[0])

    def test_selection_requires_explicit_budget_and_refuses_extras(self):
        self.authenticate()
        self.assert_error("budget_required", lambda: self.client.propose_selection(DAY, ["102", "103"]))
        self.site.with_extras = True
        self.assert_error("extras_present", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(self.site.posts, [])

    def test_selection_apply_verifies_readback_and_can_propose_rollback(self):
        plan = self.plan()
        self.assertEqual(self.site.posts, [])
        result = self.client.apply_plan(plan["id"], plan["approval"])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.site.selection, ["102", "103"])
        self.assertEqual(len(self.site.posts), 1)
        fields = parse_qs(self.site.posts[0].content.decode())
        self.assertEqual(fields["authenticity_token"], ["cart-csrf-synthetic"])
        self.assertEqual(fields["_method"], ["patch"])
        self.assert_error("no_replay", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        rollback = self.client.rollback_plan(plan["id"])
        self.assertEqual(rollback["status"], "proposed")
        self.assertEqual([item["recipe_id"] for item in rollback["summary"]["after_recipes"]], ["101", "102"])
        self.assertEqual(len(self.site.posts), 1)

    def test_wrong_approval_does_not_write(self):
        plan = self.plan()
        self.assert_error("approval_required", lambda: self.client.apply_plan(plan["id"], "wrong-approval"))
        self.assertEqual(self.site.posts, [])

    def test_expired_proposal_does_not_write(self):
        plan = self.plan()
        with patch("mindful_chef_cli.client.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=16)
            self.assert_error("plan_expired", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_changed_selection_blocks_stale_proposal(self):
        plan = self.plan()
        self.site.selection = ["101", "103"]
        self.assert_error("state_changed", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_changed_price_blocks_approved_proposal(self):
        plan = self.plan()
        self.site.prices["103"] += 100
        self.assert_error("price_changed", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_uncertain_write_is_persisted_and_never_replayed(self):
        plan = self.plan()
        self.site.fail_write = True
        self.assert_error("write_uncertain", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.client.show_plan(plan["id"])["status"], "unknown")
        self.assert_error("no_replay", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(len(self.site.posts), 1)
        receipt = self.client.reconcile_plan(plan["id"])
        self.assertTrue(receipt["current_matches_original"])
        self.assertFalse(receipt["replay_permitted"])
        self.assertEqual(len(self.site.posts), 1)

    def test_307_does_not_replay_mutation(self):
        plan = self.plan()
        self.site.redirect_write = True
        self.assert_error("write_uncertain", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(len(self.site.posts), 1)

    def test_skip_uses_provider_reason_and_confirms_skipped_state(self):
        self.authenticate()
        plan = self.client.propose_skip(DAY)
        self.assertEqual(self.site.posts, [])
        result = self.client.apply_plan(plan["id"], plan["approval"])
        self.assertEqual(result["status"], "confirmed")
        fields = parse_qs(self.site.posts[0].content.decode())
        self.assertEqual(fields["skip_survey[primary_reason]"], ["synthetic-away"])
        self.assertEqual(fields["authenticity_token"], ["skip-csrf-synthetic"])

    def test_changed_skip_contract_blocks_proposal(self):
        self.authenticate()
        self.site.skip_schema_changed = True
        self.assert_error("schema_changed", lambda: self.client.propose_skip(DAY))
        self.assertEqual(self.site.posts, [])

    def test_hidden_delivery_date_mismatch_fails_closed(self):
        self.authenticate()
        self.site.hidden_date = "2030-01-14"
        self.assert_error("schema_changed", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(self.site.posts, [])

    def test_unexpected_method_override_fails_closed(self):
        self.authenticate()
        for override in ["delete", "put"]:
            with self.subTest(override=override):
                self.site.method_override = override
                self.assert_error("schema_changed", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(self.site.posts, [])

    def test_changed_account_cannot_apply_another_accounts_plan(self):
        plan = self.plan()
        self.client._session["account_key"] = "b" * 64
        self.assert_error("account_changed", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_locally_modified_plan_invalidates_original_approval(self):
        plan = self.plan()
        stored = self.store.read_private_json(f'plan-{plan["id"]}.json')
        stored["summary"]["max_total_cents"] += 1000
        self.store.write_private_json(f'plan-{plan["id"]}.json', stored)
        self.assert_error("plan_modified", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_resume_requires_budget_then_posts_only_live_unskip_form(self):
        self.authenticate()
        self.site.skipped = True
        self.assert_error("budget_required", lambda: self.client.propose_resume(DAY))
        plan = self.client.propose_resume(DAY, max_total_cents=4000)
        self.assertEqual(plan["summary"]["estimated_total_cents"], 3100)
        self.assertEqual(self.site.posts, [])
        result = self.client.apply_plan(plan["id"], plan["approval"])
        self.assertEqual(result["status"], "confirmed")
        self.assertFalse(self.site.skipped)
        fields = parse_qs(self.site.posts[0].content.decode())
        self.assertEqual(fields["_method"], ["delete"])
        self.assertEqual(fields["authenticity_token"], ["resume-csrf-synthetic"])

    def test_resume_restored_meals_change_blocks_write(self):
        self.authenticate()
        self.site.skipped = True
        plan = self.client.propose_resume(DAY, max_total_cents=4000)
        self.site.selection = ["102", "103"]
        self.assert_error("state_changed", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_unknown_write_blocks_new_plan_for_same_delivery(self):
        plan = self.plan()
        self.site.fail_write = True
        self.assert_error("write_uncertain", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assert_error("delivery_on_hold", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(len(self.site.posts), 1)

    def test_reconcile_requested_state_clears_hold_without_replay(self):
        plan = self.plan()
        self.site.fail_write = True
        self.assert_error("write_uncertain", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.site.selection = ["102", "103"]
        receipt = self.client.reconcile_plan(plan["id"])
        self.assertEqual(receipt["attempt_status"], "reconciled")
        self.assertTrue(receipt["current_matches_requested"])
        self.assertFalse(receipt["replay_permitted"])
        self.assertEqual(len(self.site.posts), 1)

    def test_login_unexpected_method_override_never_submits_credentials(self):
        self.site.login_override = "delete"
        self.assert_error("schema_changed", lambda: self.client.login("planner@example.invalid", "synthetic-test-password"))
        self.assertEqual(self.site.posts, [])
        self.assertEqual(self.backend.values, {})

    def test_skip_unexpected_method_override_never_submits(self):
        self.authenticate()
        self.site.skip_override = "delete"
        self.assert_error("schema_changed", lambda: self.client.propose_skip(DAY))
        self.assertEqual(self.site.posts, [])

    def test_maintenance_page_does_not_pass_authentication_status(self):
        self.authenticate()
        self.site.maintenance = True
        self.assert_error("schema_changed", self.client.auth_status)
        self.assertEqual(self.site.posts, [])

    def test_selection_race_between_preflight_and_fresh_form_blocks_write(self):
        plan = self.plan()
        self.site.race_at_delivery_get = self.site.delivery_gets + 2
        self.assert_error("state_changed", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])

    def test_recurring_product_cancellation_fields_block_recipe_changes(self):
        self.authenticate()
        self.site.cancelled_product_ids = ["500"]
        self.assert_error("extras_present", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(self.site.posts, [])

    def test_resume_invalid_or_ambiguous_form_controls_are_rejected(self):
        self.authenticate()
        self.site.skipped = True
        cases = [
            ("get", ""),
            ("post", '<input name="_method" value="delete">'),
            ("post", '<input name="authenticity_token" value="synthetic-duplicate">'),
            ("post", '<input name="authenticity_token" value="">'),
        ]
        for method, extra_fields in cases:
            with self.subTest(method=method, extra_fields=extra_fields):
                self.site.resume_method = method
                self.site.resume_extra_fields = extra_fields
                self.assert_error("unsupported_resume", lambda: self.client.propose_resume(DAY, 4000))
        self.assertEqual(self.site.posts, [])

    def test_menu_duplicate_csrf_controls_are_rejected(self):
        self.authenticate()
        self.site.menu_extra_fields = '<input name="authenticity_token" value="synthetic-duplicate">'
        self.assert_error("schema_changed", lambda: self.client.propose_selection(DAY, ["102", "103"], 4000))
        self.assertEqual(self.site.posts, [])

    def test_malformed_price_in_final_form_blocks_post(self):
        plan = self.plan()
        self.site.malformed_price_at_delivery_get = self.site.delivery_gets + 2
        self.assert_error("price_unknown", lambda: self.client.apply_plan(plan["id"], plan["approval"]))
        self.assertEqual(self.site.posts, [])


if __name__ == "__main__":
    unittest.main()
