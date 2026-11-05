from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from mindful_chef_cli.setup import main, render_setup


class AgentSetupTests(unittest.TestCase):
    def test_codex_settings_parse_and_default_has_no_apply(self):
        data = render_setup("codex")
        parsed = tomllib.loads(data["config_text"])
        self.assertEqual(parsed, data["config"])
        entry = parsed["mcp_servers"]["mindful_chef_cli"]
        self.assertEqual(entry["args"], ["-m", "mindful_chef_cli", "mcp"])
        self.assertEqual(shlex.split(data["registration_command"]),
                         ["codex", "mcp", "add", "mindful_chef_cli", "--", entry["command"], *entry["args"]])
        self.assertFalse(data["account_apply_enabled"])
        self.assertFalse(data["settings_written"])

    def test_claude_command_preserves_named_profile_and_explicit_write_flag(self):
        data = render_setup("claude-code", profile="household", allow_writes=True)
        entry = data["config"]["mcpServers"]["mindful_chef_cli"]
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["args"], ["-m", "mindful_chef_cli", "--profile", "household", "mcp", "--allow-writes"])
        self.assertEqual(shlex.split(data["registration_command"]),
                         ["claude", "mcp", "add", "--transport", "stdio", "mindful_chef_cli", "--",
                          entry["command"], *entry["args"]])

    def test_generic_settings_do_not_require_shell_or_credentials(self):
        data = render_setup("generic")
        entry = json.loads(data["config_text"])["mcpServers"]["mindful_chef_cli"]
        self.assertEqual(set(entry), {"command", "args"})
        self.assertEqual(set(data["config"]), {"mcpServers"})
        self.assertIsNone(data["registration_command"])

    def test_venv_interpreter_symlink_is_not_resolved_to_base_python(self):
        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / "environment with spaces" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(sys.executable)
            data = render_setup("codex", python_executable=interpreter)
            self.assertEqual(data["command"], str(interpreter))
            self.assertIn(str(interpreter), shlex.split(data["registration_command"]))

    def test_shell_metacharacters_are_quoted_not_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            interpreter = Path(temp) / "python$(synthetic-command)"
            interpreter.symlink_to(sys.executable)
            data = render_setup("claude-code", python_executable=interpreter)
            self.assertEqual(shlex.split(data["registration_command"])[-4:],
                             [str(interpreter), "-m", "mindful_chef_cli", "mcp"])

    def test_invalid_profiles_and_nonexecutables_fail_closed(self):
        for profile in ("../other", "_writers", "", "a\nother"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                render_setup("codex", profile=profile)
        with self.assertRaises(ValueError):
            render_setup("unknown")
        with self.assertRaises(ValueError):
            render_setup("codex", python_executable="relative/python")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "not-executable"
            path.write_text("synthetic file")
            os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                render_setup("generic", python_executable=path)

    def test_generation_does_not_create_or_read_private_state(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            before = list(Path(temp).iterdir())
            data = render_setup("claude-desktop")
            self.assertEqual(list(Path(temp).iterdir()), before)
            self.assertNotIn("env", data["config"]["mcpServers"]["mindful_chef_cli"])

    def test_json_command_output_and_safe_error(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--client", "codex"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--python", "/nonexistent/synthetic-python"])
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"]["code"], "invalid_setup")
        self.assertNotIn("nonexistent", output.getvalue())

    def test_unknown_arguments_do_not_echo_entered_values(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--password", "synthetic-secret"])
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "invalid_setup")


if __name__ == "__main__":
    unittest.main()
