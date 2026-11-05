"""Privacy-check tests use constructed synthetic values, never real accounts."""

import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.check_public_tree import main, scan_text, scan_tree


class PublicTreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, path, content):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    def test_detects_sensitive_literals_without_returning_content(self):
        sensitive = "not-a-real-" + "credential-0123456789"
        contents = [
            "api_" + "key = '" + sensitive + "'",
            '"pass' + 'word": "' + sensitive + '"',
            "Cookie" + ": _account=" + sensitive,
            '"cookies": [{"name":"session", "value":"' + sensitive + '"}]',
            "owner@" + "ordinary-mail.invalid-example.org",
            "/" + "Users/ExampleOwner/project",
            "+44 " + "7700 900123",
            "42 " + "Fictional Example Road",
            "-----BEGIN " + "PRIVATE KEY-----",
            "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ1c2VyIjoibm9uZSJ9." + "abcdefghijklmno",
        ]
        rules = {finding.rule for finding in scan_text("fixture.txt", "\n".join(contents))}
        self.assertTrue({"credential-literal", "credential-header", "session-cookie-value", "personal-email", "personal-home-path", "personal-phone", "personal-address", "private-key", "jwt"}.issubset(rules))
        output = "\n".join(finding.render() for finding in scan_text("fixture.txt", "\n".join(contents)))
        self.assertNotIn(sensitive, output)
        self.assertNotIn("ExampleOwner", output)

    def test_reserved_addresses_and_explicit_synthetic_values_are_allowed(self):
        content = '\n'.join([
            'email = "hello@example.com"',
            'email = "person@example.net"',
            'email = "person@account.invalid"',
            'token = "synthetic-cookie-value"',
            'password = "<password>"',
            'api_key = "dummy-value"',
            'token = os.environ["TOKEN"]',
            '"cookies": [{"name":"test_session", "value":"synthetic-cookie-value"}]',
        ])
        self.assertEqual(scan_text("example.txt", content), [])

    def test_untracked_source_is_scanned_and_ignored_private_files_are_omitted(self):
        self.write(".gitignore", "private/\n.env\n*.har\n.venv/\n")
        self.write("private/account.json", "owner@" + "ordinary-mail.invalid-example.org")
        self.write(".env", "api_" + "key='unpublishable-value'")
        self.write(".venv/package.py", "api_" + "key='unpublishable-value'")
        self.write("recording.har", "unpublishable capture")
        self.write("src/source.py", "owner@" + "ordinary-mail.invalid-example.org")
        findings = scan_tree(self.root)
        self.assertEqual([(item.path, item.rule) for item in findings], [("src/source.py", "personal-email")])

    def test_git_source_includes_untracked_and_flags_tracked_ignored_private_artifact(self):
        git = shutil.which("git")
        self.assertIsNotNone(git)
        # Fixed Git commands against this test's newly-created temporary tree.
        subprocess.run([git, "init", "-q", str(self.root)], check=True, capture_output=True)  # noqa: S603
        self.write("private/session.json", "{}")
        subprocess.run([git, "-C", str(self.root), "add", "private/session.json"], check=True, capture_output=True)  # noqa: S603
        self.write(".gitignore", "private/\n")
        self.write("private/untracked.json", "owner@" + "ordinary-mail.invalid-example.org")
        self.write("new.txt", "owner@" + "ordinary-mail.invalid-example.org")
        findings = scan_tree(self.root)
        self.assertEqual([(item.path, item.rule) for item in findings], [("new.txt", "personal-email"), ("private/session.json", "tracked-private-artifact")])

    def test_unignored_private_artifact_requires_review(self):
        self.write("account.har", "{}")
        self.assertEqual(scan_tree(self.root)[0].rule, "private-artifact")

    def test_cli_never_prints_a_matched_value(self):
        sensitive = "owner@" + "ordinary-mail.invalid-example.org"
        self.write("source.txt", sensitive)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["--root", str(self.root)])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "source.txt:1:personal-email\n")
        self.assertNotIn(sensitive, output.getvalue())

    def test_symlink_target_is_not_read(self):
        self.write("private.txt", "sensitive")
        (self.root / "link.txt").symlink_to(self.root / "private.txt")
        self.assertIn("symlink-review", {item.rule for item in scan_tree(self.root)})

    def test_symlinked_directory_requires_review_without_traversal(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            (self.root / "linked-directory").symlink_to(Path(elsewhere).resolve(), target_is_directory=True)
            self.assertEqual([(item.path, item.rule) for item in scan_tree(self.root)], [("linked-directory", "symlink-review")])

    def test_clean_tree_has_success_exit_code_and_no_output(self):
        self.write("example.txt", "hello@example.com")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--root", str(self.root)]), 0)
        self.assertEqual(output.getvalue(), "")

    def test_control_characters_in_filenames_are_escaped(self):
        self.write("odd\nname.txt", "owner@" + "ordinary-mail.invalid-example.org")
        rendered = scan_tree(self.root)[0].render()
        self.assertNotIn("\n", rendered)
        self.assertIn("\\n", rendered)


if __name__ == "__main__":
    unittest.main()
