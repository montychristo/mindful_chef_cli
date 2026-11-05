"""Synthetic storage and credential safety tests; no provider account required."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from mindful_chef_cli.storage import SecureStore, StorageError


class MemoryBackend:
    def __init__(self):
        self.values = {}

    def get_password(self, service, user):
        return self.values.get((service, user))

    def set_password(self, service, user, value):
        self.values[(service, user)] = value

    def delete_password(self, service, user):
        del self.values[(service, user)]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.backend = MemoryBackend()
        self.store = SecureStore(state_dir=self.base / "state", backend=self.backend)

    def test_session_is_keyring_only_and_profile_isolated(self):
        session = {"cookies": [{"name": "test_session", "value": "synthetic-cookie-value"}]}
        self.store.save_session(session)
        self.assertEqual(self.store.load_session(), session)
        other = SecureStore("another", state_dir=self.base / "state", backend=self.backend)
        self.assertIsNone(other.load_session())
        self.assertEqual(list(self.store.directory.iterdir()), [])
        self.store.delete_session()
        self.store.delete_session()
        self.assertIsNone(self.store.load_session())

    def test_password_cannot_be_saved(self):
        for value in ({"password": "sample"}, {"metadata": [{"Password": "sample"}]}):
            with self.assertRaises(StorageError):
                self.store.save_session(value)
        self.assertFalse(self.backend.values)

    def test_insecure_or_chained_backend_fails_closed(self):
        for module, classname in [("keyring.backends.fail", "Keyring"), ("keyrings.alt.file", "PlaintextKeyring"), ("keyring.backends.chainer", "ChainerBackend")]:
            backend_class = type(classname, (), {"__module__": module, "priority": 10})
            fake_keyring = types.SimpleNamespace(get_keyring=lambda cls=backend_class: cls())
            store = SecureStore(state_dir=self.base / "other")
            with mock.patch.dict(sys.modules, {"keyring": fake_keyring}):
                with self.assertRaises(StorageError):
                    store.load_session()

    def test_missing_or_locked_keyring_does_not_expose_secret(self):
        class LockedBackend:
            def get_password(self, *_):
                raise RuntimeError("synthetic-secret-do-not-display")

        store = SecureStore(state_dir=self.base / "locked", backend=LockedBackend())
        with self.assertRaises(StorageError) as caught:
            store.load_session()
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_corrupted_or_non_object_sessions_refused(self):
        key = ("mindful_chef_cli.session.v1", "default")
        for value in ("{", "[]", '{"password":"sample"}', '{"value":NaN}'):
            self.backend.values[key] = value
            with self.assertRaises(StorageError):
                self.store.load_session()

    def test_private_state_roundtrip_permissions_and_atomic_replacement(self):
        self.assertIsNone(self.store.read_private_json("missing.json"))
        for value in ({"stage": "planned"}, {"stage": "verified"}):
            self.store.write_private_json("receipt.json", value)
            self.assertEqual(self.store.read_private_json("receipt.json"), value)
        self.assertEqual(stat.S_IMODE(self.store.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.state_path("receipt.json").stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.store.directory.iterdir()], ["receipt.json"])

    def test_invalid_profile_and_path_traversal_refused(self):
        for profile in ("../escape", "", "a/b", ".", "x" * 65):
            with self.assertRaises(StorageError):
                SecureStore(profile, state_dir=self.base / "state")
        for name in ("../escape", "/absolute", ".hidden", "a/b", ""):
            with self.assertRaises(StorageError):
                self.store.state_path(name)
        with self.assertRaises(StorageError):
            SecureStore(state_dir="relative/path")

    def test_symlinked_state_root_or_ancestor_refused(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        for path in (link, link / "child"):
            with self.assertRaises(StorageError):
                SecureStore(state_dir=path, backend=self.backend)

    def test_symlinked_and_hardlinked_state_files_refused(self):
        outside = self.base / "outside.json"
        outside.write_text('{"untouched":true}')
        outside.chmod(0o600)
        linked = self.store.state_path("linked.json")
        linked.symlink_to(outside)
        for action in (lambda: self.store.read_private_json("linked.json"), lambda: self.store.write_private_json("linked.json", {})):
            with self.assertRaises(StorageError):
                action()
        self.assertEqual(json.loads(outside.read_text()), {"untouched": True})
        hardlink = self.store.state_path("hardlink.json")
        os.link(outside, hardlink)
        with self.assertRaises(StorageError):
            self.store.read_private_json("hardlink.json")

    def test_world_readable_state_and_directory_refused(self):
        self.store.write_private_json("state.json", {})
        self.store.state_path("state.json").chmod(0o644)
        with self.assertRaises(StorageError):
            self.store.read_private_json("state.json")
        with self.assertRaises(StorageError):
            self.store.write_private_json("state.json", {})
        root = self.base / "open"
        root.mkdir(mode=0o755)
        with self.assertRaises(StorageError):
            SecureStore(state_dir=root)

    def test_state_size_and_non_finite_values_refused(self):
        for value in ({"v": float("nan")}, {"v": "x" * (1024 * 1024)}):
            with self.assertRaises(StorageError):
                self.store.write_private_json("state.json", value)
        invalid = self.store.state_path("invalid.json")
        invalid.write_text('{"v":NaN}')
        invalid.chmod(0o600)
        with self.assertRaises(StorageError):
            self.store.read_private_json("invalid.json")

    def test_lock_is_private_and_reentrant(self):
        with self.store.lock():
            with self.store.lock():
                self.store.write_private_json("receipt.json", {"status": "pending"})
        self.assertEqual(stat.S_IMODE((self.store.directory / ".lock").stat().st_mode), 0o600)
        self.assertIsNone(self.store._locked_fd)

    def test_lock_symlink_refused(self):
        outside = self.base / "outside"
        outside.write_text("untouched")
        (self.store.directory / ".lock").symlink_to(outside)
        with self.assertRaises(StorageError):
            with self.store.lock():
                pass
        self.assertEqual(outside.read_text(), "untouched")

    def test_xdg_state_home_must_be_absolute(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}):
            with self.assertRaises(StorageError):
                SecureStore()

    def test_writer_claim_is_shared_across_profiles_and_account_specific(self):
        account = "a" * 64
        self.store.claim_writer(account)
        with self.store.lock():
            self.store.claim_writer(account)
        other = SecureStore("other", state_dir=self.store.root, backend=self.backend)
        with self.assertRaises(StorageError) as caught:
            other.claim_writer(account)
        self.assertNotIn(account, str(caught.exception))
        other.claim_writer("b" * 64)
        registry_file = self.store.root / "_writers" / "writers.json"
        self.assertEqual(json.loads(registry_file.read_text()), {account: "default", "b" * 64: "other"})
        self.assertEqual(stat.S_IMODE(registry_file.stat().st_mode), 0o600)

    def test_writer_claim_rejects_invalid_keys_and_reserved_profile(self):
        for account in ("", "a" * 63, "A" * 64, "g" * 64, "a" * 65, None):
            with self.assertRaises(StorageError):
                self.store.claim_writer(account)
        with self.assertRaises(StorageError):
            SecureStore("_writers", state_dir=self.store.root)

    def test_corrupted_writer_registry_fails_closed(self):
        self.store.claim_writer("a" * 64)
        registry_file = self.store.root / "_writers" / "writers.json"
        registry_file.write_text("[]")
        with self.assertRaises(StorageError):
            self.store.claim_writer("a" * 64)
        registry_file.write_text(json.dumps({"a" * 64: "../invalid"}))
        with self.assertRaises(StorageError):
            self.store.claim_writer("a" * 64)

    def test_concurrent_profiles_cannot_both_claim_one_account(self):
        stores = [self.store, SecureStore("other", state_dir=self.store.root, backend=self.backend)]
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(store):
            barrier.wait(timeout=3)
            try:
                store.claim_writer("c" * 64)
                outcomes.append("claimed")
            except StorageError:
                outcomes.append("blocked")

        threads = [threading.Thread(target=claim, args=(store,), daemon=True) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "Writer lock ordering deadlocked")
        self.assertEqual(sorted(outcomes), ["blocked", "claimed"])


if __name__ == "__main__":
    unittest.main()
