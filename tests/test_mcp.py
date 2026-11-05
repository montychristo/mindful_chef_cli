import asyncio
from contextlib import contextmanager
from importlib.util import find_spec
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mindful_chef_cli.mcp_server import create_server, run_server
from mindful_chef_cli.storage import SecureStore


class MemoryStore:
    @contextmanager
    def lock(self):
        yield

    def read_private_json(self, name, default=None):
        return default


class FakeClient:
    calls = []

    def __init__(self, store=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def propose_skip(self, date):
        self.calls.append(("propose_skip", date))
        return {"status": "proposed", "id": "test-plan"}

    def apply_plan(self, identifier, approval):
        self.calls.append(("apply_plan", identifier, approval))
        return {"status": "verified"}

    def propose_resume(self, date, max_total_cents=None):
        self.calls.append(("propose_resume", date, max_total_cents))
        return {"status": "proposed"}

    def doctor(self, authenticated=False):
        raise RuntimeError("private-token-or-response-body")


class RejectClient:
    calls = 0

    def __init__(self, store=None):
        type(self).calls += 1
        raise AssertionError("Local feedback must not construct an account client")


@unittest.skipUnless(find_spec("mcp"), "Optional MCP extra is not installed")
class MCPTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []

    def server(self, writes=False):
        return create_server(store=MemoryStore(), allow_writes=writes, client_factory=FakeClient)

    def tools(self, server):
        return {tool.name: tool for tool in asyncio.run(server.list_tools())}

    def call(self, server, name, arguments):
        value = asyncio.run(server.call_tool(name, arguments))
        # FastMCP v1 returns content + structured content for annotated dicts.
        if isinstance(value, tuple):
            return value[1]
        if isinstance(value, dict):
            return value
        return json.loads(value[0].text)

    def test_default_server_has_no_external_write_or_credential_tool(self):
        tools = self.tools(self.server())
        self.assertNotIn("apply_plan", tools)
        self.assertIn("reconcile_plan", tools)
        self.assertIn("propose_selection", tools)
        self.assertNotIn("login", tools)
        self.assertNotIn("refresh", tools)
        self.assertFalse(any("password" in json.dumps(tool.inputSchema) for tool in tools.values()))
        self.assertFalse(any("file" in tool.inputSchema.get("properties", {}) for tool in tools.values()))

    def test_proposal_never_applies(self):
        result = self.call(self.server(), "propose_skip", {"delivery_date": "2030-09-01"})
        self.assertTrue(result["ok"])
        self.assertEqual(FakeClient.calls, [("propose_skip", "2030-09-01")])

    def test_write_opt_in_requires_hash_and_marks_tool_destructive(self):
        server = self.server(writes=True)
        tools = self.tools(server)
        self.assertTrue(tools["apply_plan"].annotations.destructiveHint)
        self.assertIn("approval", tools["apply_plan"].inputSchema["required"])
        result = self.call(server, "apply_plan", {"plan_id": "test-plan", "approval": "reviewed-hash"})
        self.assertTrue(result["ok"])
        self.assertEqual(FakeClient.calls, [("apply_plan", "test-plan", "reviewed-hash")])

    def test_resume_preserves_budget_without_applying(self):
        result = self.call(self.server(), "propose_resume", {"delivery_date": "2030-09-01", "max_total_cents": 6500})
        self.assertTrue(result["ok"])
        self.assertEqual(FakeClient.calls, [("propose_resume", "2030-09-01", 6500)])

    def test_errors_are_safe_structured_results(self):
        result = self.call(self.server(), "doctor", {})
        self.assertFalse(result["ok"])
        self.assertNotIn("private-token", json.dumps(result))

    def test_away_accepts_data_without_paths_or_calendar_access(self):
        result = self.call(self.server(), "away", {
            "deliveries": ["2030-09-01"],
            "events": [{"start": "2030-09-01", "end": "2030-09-08", "away": True, "confidence": "confirmed"}],
        })
        self.assertEqual(result["data"][0]["action"], "propose_skip")
        self.assertEqual(FakeClient.calls, [])

    def test_server_runner_only_chooses_stdio(self):
        server = Mock()
        with patch("mindful_chef_cli.mcp_server.create_server", return_value=server):
            run_server(store=MemoryStore())
        server.run.assert_called_once_with(transport="stdio")


@unittest.skipUnless(find_spec("mcp"), "Optional MCP extra is not installed")
class LocalFeedbackTests(unittest.TestCase):
    tools = MCPTests.tools
    call = MCPTests.call

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SecureStore(state_dir=Path(self.temporary.name).resolve() / "state")
        RejectClient.calls = 0
        self.server = create_server(store=self.store, client_factory=RejectClient)

    def tearDown(self):
        self.assertEqual(RejectClient.calls, 0)

    def test_local_tools_are_available_without_account_write_opt_in(self):
        tools = self.tools(self.server)
        for name in ("preferences_update", "recipe_rate", "recipe_assess"):
            self.assertIn(name, tools)
            self.assertFalse(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
            self.assertFalse(tools[name].annotations.openWorldHint)
        self.assertNotIn("apply_plan", tools)

    def test_preference_update_persists_and_preserves_omitted_settings(self):
        self.store.write_private_json("preferences.json", {"dislikes": ["mushroom"], "max_cook_time_minutes": 40})
        result = self.call(self.server, "preferences_update", {"preferences": {"likes": ["Tomato"]}})
        self.assertTrue(result["ok"])
        stored = self.store.read_private_json("preferences.json")
        self.assertEqual(stored["likes"], ["tomato"])
        self.assertEqual(stored["dislikes"], ["mushroom"])
        self.assertEqual(stored["max_cook_time_minutes"], 40)

    def test_invalid_preferences_do_not_replace_private_state(self):
        before = {"likes": ["tomato"]}
        self.store.write_private_json("preferences.json", before)
        for preferences in ({"allergens": "milk"}, {"protein_targets": {"beef": -1}}, {"unknown_setting": True}):
            with self.subTest(preferences=preferences):
                result = self.call(self.server, "preferences_update", {"preferences": preferences})
                self.assertFalse(result["ok"])
                self.assertEqual(self.store.read_private_json("preferences.json"), before)

    def test_local_rating_updates_only_the_target_recipe(self):
        self.store.write_private_json("ratings.json", [{"recipe_id": "101", "rating": 2},
                                                        {"recipe_id": "102", "rating": 4}])
        result = self.call(self.server, "recipe_rate", {"recipe_id": "101", "rating": 5})
        self.assertTrue(result["ok"])
        self.assertCountEqual(self.store.read_private_json("ratings.json"),
                              [{"recipe_id": "101", "rating": 5}, {"recipe_id": "102", "rating": 4}])

    def test_invalid_rating_does_not_write(self):
        for rating in (0, 6):
            result = self.call(self.server, "recipe_rate", {"recipe_id": "101", "rating": rating})
            self.assertFalse(result["ok"])
            self.assertIsNone(self.store.read_private_json("ratings.json"))

    def test_boolean_rating_is_rejected_without_writing(self):
        from mcp.server.fastmcp.exceptions import ToolError

        with self.assertRaises(ToolError):
            self.call(self.server, "recipe_rate", {"recipe_id": "101", "rating": True})
        self.assertIsNone(self.store.read_private_json("ratings.json"))

    def test_local_assessment_merges_without_overwriting_other_feedback(self):
        self.store.write_private_json("annotations.json", {"101": {"reheat_suitability": "good"},
                                                            "102": {"finishing_effort": "high"}})
        result = self.call(self.server, "recipe_assess", {"recipe_id": "101", "finishing_effort": "low"})
        self.assertTrue(result["ok"])
        stored = self.store.read_private_json("annotations.json")
        self.assertEqual(stored["101"]["reheat_suitability"], "good")
        self.assertEqual(stored["101"]["finishing_effort"], "low")
        self.assertEqual(stored["101"]["assessment_source"], "household")
        self.assertEqual(stored["102"], {"finishing_effort": "high"})

    def test_invalid_or_empty_assessment_does_not_write(self):
        for assessment in ({"reheat_suitability": "excellent"}, {"finishing_effort": "none"}, {}):
            result = self.call(self.server, "recipe_assess", {"recipe_id": "101", **assessment})
            self.assertFalse(result["ok"])
            self.assertIsNone(self.store.read_private_json("annotations.json"))


if __name__ == "__main__":
    unittest.main()
