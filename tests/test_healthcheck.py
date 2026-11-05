from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from mindful_chef_cli.client import ClientError
from mindful_chef_cli.storage import StorageError


_SPEC = importlib.util.spec_from_file_location("mindful_chef_cli_healthcheck", Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py")
healthcheck = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(healthcheck)


class MemoryStore:
    def __init__(self, session=None):
        self.session = session
        self.files = {}
        self.session_reads = 0

    @contextmanager
    def lock(self):
        yield

    def load_session(self):
        self.session_reads += 1
        return deepcopy(self.session)

    def read_private_json(self, name, default=None):
        return deepcopy(self.files.get(name, default))

    def write_private_json(self, name, data):
        self.files[name] = deepcopy(data)


class FakeClient:
    calls = []
    failure = None
    counter = 0

    def __init__(self, store=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def doctor(self, authenticated=False):
        self.calls.append(authenticated)
        self.counter += 1
        if self.failure:
            return {"healthy": False, "checks": [{"name": "request", "ok": False, "code": self.failure}],
                    "raw_body": "private-provider-body"}
        return {"healthy": True, "checks": [{"name": "public_menu", "ok": True, "recipe_count": self.counter}],
                "checked_at": str(self.counter), "cookie": "synthetic-cookie", "account": "private-account"}


class HealthcheckTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.failure = None

    def run_check(self, store, client=FakeClient, **kwargs):
        return healthcheck.run_healthcheck(store=store, client_factory=client, **kwargs)["data"]

    def test_initial_missing_login_is_expected_and_public_stays_healthy(self):
        result = self.run_check(MemoryStore())
        self.assertTrue(result["healthy"])
        self.assertTrue(result["requires_login"])
        self.assertFalse(result["authenticated_checked"])
        self.assertEqual(result["authenticated"]["status"], "missing")
        self.assertEqual(FakeClient.calls, [False])

    def test_repeated_health_ignores_timestamp_and_count_changes(self):
        store = MemoryStore(session={"cookies": [{"value": "synthetic-cookie"}]})
        first, second = self.run_check(store), self.run_check(store)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertTrue(second["authenticated_checked"])
        recorded = json.dumps(store.files)
        self.assertNotIn("synthetic-cookie", recorded)
        self.assertNotIn("private-account", recorded)
        self.assertNotIn("recipe_count", recorded)

    def test_new_failure_changes_once_then_remains_quiet(self):
        store = MemoryStore()
        self.run_check(store)
        FakeClient.failure = "schema_changed"
        failed, repeated = self.run_check(store), self.run_check(store)
        self.assertFalse(failed["healthy"])
        self.assertTrue(failed["changed"])
        self.assertFalse(repeated["changed"])
        self.assertNotIn("private-provider-body", json.dumps(store.files))

    def test_expired_saved_session_requires_login_without_attempting_login(self):
        class ExpiredClient(FakeClient):
            def doctor(self, authenticated=False):
                if authenticated:
                    return {"healthy": False, "checks": [{"name": "request", "ok": False, "code": "auth_expired"}]}
                return super().doctor(authenticated=False)
        result = self.run_check(MemoryStore(session={"present": True}), ExpiredClient)
        self.assertFalse(result["healthy"])
        self.assertTrue(result["requires_login"])
        self.assertEqual(result["authenticated"]["status"], "needs_login")

    def test_thrown_auth_error_is_sanitized(self):
        class ExpiredClient(FakeClient):
            def doctor(self, authenticated=False):
                if authenticated:
                    raise ClientError("auth_required", "private-provider-body")
                return super().doctor(authenticated=False)
        result = self.run_check(MemoryStore(session={"present": True}), ExpiredClient)
        self.assertTrue(result["requires_login"])
        self.assertNotIn("private-provider-body", json.dumps(result))

    def test_blocked_credential_store_preserves_public_result_and_safe_code(self):
        class BlockedStore(MemoryStore):
            def load_session(self):
                raise StorageError("private-keyring-diagnostic")
        store = BlockedStore()
        result = self.run_check(store)
        self.assertTrue(result["public"]["healthy"])
        self.assertFalse(result["healthy"])
        self.assertEqual(result["authenticated"]["code"], "storage_unavailable")
        self.assertNotIn("private-keyring-diagnostic", json.dumps(result) + json.dumps(store.files))

    def test_public_only_never_opens_keyring(self):
        store = MemoryStore()
        result = self.run_check(store, public_only=True)
        self.assertTrue(result["healthy"])
        self.assertFalse(result["requires_login"])
        self.assertEqual(store.session_reads, 0)
        self.assertEqual(FakeClient.calls, [False])

    def test_invalid_state_fails_closed_without_overwriting(self):
        store = MemoryStore()
        store.files["health-last.json"] = {"invalid": True}
        result = self.run_check(store)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["state"]["code"], "invalid_state")
        self.assertEqual(store.files["health-last.json"], {"invalid": True})

    def test_unknown_errors_and_codes_do_not_expose_diagnostics(self):
        class LeakyClient(FakeClient):
            def doctor(self, authenticated=False):
                raise ClientError("private-code-value", "private-provider-body")
        result = self.run_check(MemoryStore(), LeakyClient)
        self.assertEqual(result["public"]["code"], "operation_failed")
        self.assertNotIn("private-code-value", json.dumps(result))
        self.assertNotIn("private-provider-body", json.dumps(result))

    def test_main_exit_codes_match_health(self):
        for healthy, expected in ((True, 0), (False, 1)):
            with self.subTest(healthy=healthy), patch.object(healthcheck, "SecureStore", return_value=MemoryStore()), \
                    patch.object(healthcheck, "run_healthcheck", return_value={"ok": True, "data": {"healthy": healthy}}), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(healthcheck.main([]), expected)


if __name__ == "__main__":
    unittest.main()
