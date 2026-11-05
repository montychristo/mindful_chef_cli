from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mindful_chef_cli.cli import AgentOperations, main


class MemoryStore:
    def __init__(self, profile="default"):
        self.profile = profile
        self.files = {}

    @contextmanager
    def lock(self):
        yield

    def read_private_json(self, name, default=None):
        return deepcopy(self.files.get(name, default))

    def write_private_json(self, name, data):
        self.files[name] = deepcopy(data)


def meal(identifier, **changes):
    result = {"id": identifier, "name": f"Meal {identifier}", "url": f"/recipes/{identifier}",
              "protein": "chicken", "ingredients": ["rice"], "allergens": []}
    result.update(changes)
    return result


class FakeClient:
    calls = []
    recipes = [meal("manual"), meal("automatic")]

    def __init__(self, store=None):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def login(self, email, password):
        self.calls.append(("login", email, password))
        return {"authenticated": True}

    def menu(self, date, details=False):
        self.calls.append(("menu", date, details))
        return {"recipes": deepcopy(self.recipes), "selection": []}

    def orders(self, past=False, page=1):
        self.calls.append(("orders", past, page))
        return [{"recipes": [{"url": "/recipes/manual", "chosen_by": "customer"},
                             {"url": "/recipes/automatic", "chosen_by": "provider"}]}]

    def propose_skip(self, date):
        self.calls.append(("propose_skip", date))
        return {"id": "synthetic-plan", "status": "proposed"}

    def apply_plan(self, identifier, approval):
        self.calls.append(("apply_plan", identifier, approval))
        return {"status": "verified"}

    def propose_resume(self, date, max_total_cents=None):
        self.calls.append(("propose_resume", date, max_total_cents))
        return {"id": "synthetic-resume", "status": "proposed"}

    def doctor(self, authenticated=False):
        self.calls.append(("doctor", authenticated))
        return {"healthy": True}


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class CLITests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        FakeClient.calls = []

    def run_cli(self, arguments, client=FakeClient):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(arguments, store_factory=lambda **kwargs: self.store, client_factory=client)
        return code, json.loads(output.getvalue()), errors.getvalue()

    def test_setup_does_not_open_credential_or_state_storage(self):
        output = io.StringIO()
        def unavailable_store(**kwargs):
            raise AssertionError("Setup must not open private state")
        with redirect_stdout(output):
            code = main(["setup", "--client", "codex"], store_factory=unavailable_store)
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())["data"]
        self.assertFalse(data["settings_written"])
        self.assertFalse(data["account_apply_enabled"])
        self.assertEqual(data["transport"], "stdio")

    def test_recommendation_reports_partial_recipe_detail_coverage(self):
        class PartialClient(FakeClient):
            def menu(self, date, details=False):
                return {"recipes": [meal("available"), meal("unavailable", ingredients=None)],
                        "detail_coverage": {"total": 2, "available": 1, "unavailable": 1, "complete": False},
                        "warnings": ["One recipe has unavailable structured details."]}
        result = AgentOperations(self.store, PartialClient).recommend("2030-09-01", 2)
        self.assertFalse(result["detail_coverage"]["complete"])
        self.assertEqual(result["detail_coverage"]["unavailable"], 1)
        self.assertIn("One recipe has unavailable structured details.", result["warnings"])

    def test_proposal_does_not_apply(self):
        code, output, _ = self.run_cli(["changes", "skip", "2030-09-01"])
        self.assertEqual(code, 0)
        self.assertEqual(output["data"]["status"], "proposed")
        self.assertEqual(FakeClient.calls, [("propose_skip", "2030-09-01")])

    def test_apply_requires_and_forwards_exact_hash(self):
        code, output, _ = self.run_cli(["changes", "apply", "synthetic-plan"])
        self.assertEqual(code, 1)
        self.assertEqual(output["error"]["code"], "invalid_arguments")
        self.assertEqual(FakeClient.calls, [])
        code, _, _ = self.run_cli(["changes", "apply", "synthetic-plan", "--approve", "exact-hash"])
        self.assertEqual(code, 0)
        self.assertEqual(FakeClient.calls, [("apply_plan", "synthetic-plan", "exact-hash")])

    def test_resume_preserves_budget(self):
        code, output, _ = self.run_cli(["changes", "resume", "2030-09-01", "--max-total-cents", "6500"])
        self.assertEqual(code, 0)
        self.assertEqual(output["data"]["status"], "proposed")
        self.assertEqual(FakeClient.calls, [("propose_resume", "2030-09-01", 6500)])

    def test_rejected_secret_flag_is_not_echoed(self):
        code, output, errors = self.run_cli(["auth", "login", "--password", "do-not-print-this"])
        self.assertEqual(code, 1)
        self.assertNotIn("do-not-print-this", json.dumps(output) + errors)

    def test_noninteractive_login_fails_before_credentials(self):
        with patch("mindful_chef_cli.cli.sys.stdin", io.StringIO("person@example.invalid\nsecret\n")):
            code, output, _ = self.run_cli(["auth", "login"])
        self.assertEqual(code, 1)
        self.assertEqual(output["error"]["code"], "interactive_required")
        self.assertEqual(FakeClient.calls, [])

    def test_interactive_login_prompts_without_password_output(self):
        output, errors = io.StringIO(), TTYBuffer()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch("mindful_chef_cli.cli.sys.stdin", TTYBuffer()), \
                patch("builtins.input", return_value="person@example.invalid"), \
                patch("mindful_chef_cli.cli.getpass.getpass", return_value="synthetic-secret"):
            code = main(["auth", "login", "--reauth"], store_factory=lambda **kwargs: self.store,
                        client_factory=FakeClient)
        self.assertEqual(code, 0)
        self.assertEqual(FakeClient.calls, [("login", "person@example.invalid", "synthetic-secret")])
        self.assertNotIn("synthetic-secret", output.getvalue() + errors.getvalue())
        self.assertNotIn("Email:", output.getvalue())

    def test_private_annotations_and_ratings_drive_recommendation(self):
        self.run_cli(["prefs", "init"])
        self.run_cli(["prefs", "annotate", "automatic", "--reheat", "good", "--finishing", "low"])
        self.run_cli(["prefs", "rate", "automatic", "5"])
        code, output, _ = self.run_cli(["recommend", "2030-09-01", "--count", "1"])
        self.assertEqual(code, 0)
        data = output["data"]
        self.assertEqual(data["selected"][0]["id"], "automatic")
        self.assertEqual(data["selected"][0]["reheat_suitability"], "good")
        self.assertEqual(data["history_source"]["customer_selections_matched"], 1)
        self.assertFalse(data["history_source"]["complete"])
        automatic = next(item for item in data["ranked"] if item["id"] == "automatic")
        self.assertFalse(any("Previously selected" in reason for reason in automatic["reasons"]))

    def test_history_failure_is_a_coverage_gap_not_no_history(self):
        class FailingHistory(FakeClient):
            def orders(self, **kwargs):
                raise RuntimeError("sensitive-provider-body")
        code, output, _ = self.run_cli(["recommend", "2030-09-01"], FailingHistory)
        self.assertEqual(code, 0)
        self.assertEqual(output["data"]["history_source"]["status"], "unavailable")
        self.assertNotIn("sensitive-provider-body", json.dumps(output))

    def test_unexpected_exception_never_leaks(self):
        class FailingClient(FakeClient):
            def doctor(self, **kwargs):
                raise RuntimeError("token=sensitive-provider-body")
        code, output, errors = self.run_cli(["doctor"], FailingClient)
        self.assertEqual(code, 1)
        self.assertEqual(output["error"]["code"], "operation_failed")
        self.assertNotIn("sensitive-provider-body", json.dumps(output) + errors)
        self.assertNotIn("Traceback", errors)

    def test_away_has_no_client_access_and_reads_only_named_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            deliveries = Path(directory) / "deliveries.json"
            events = Path(directory) / "events.json"
            deliveries.write_text(json.dumps(["2030-09-01"]))
            events.write_text(json.dumps([{"start": "2030-09-01", "end": "2030-09-08",
                                            "away": True, "confidence": "confirmed"}]))
            code, output, _ = self.run_cli(["away", "--deliveries", str(deliveries), "--events", str(events)])
        self.assertEqual(code, 0)
        self.assertEqual(output["data"][0]["action"], "propose_skip")
        self.assertEqual(FakeClient.calls, [])

    def test_invalid_preferences_do_not_replace_saved_config(self):
        operations = AgentOperations(self.store)
        operations.set_preferences({"likes": ["rice"]})
        with self.assertRaises(ValueError):
            operations.set_preferences({"likes": "invalid"})
        self.assertEqual(operations.preferences()["preferences"]["likes"], ["rice"])


if __name__ == "__main__":
    unittest.main()
