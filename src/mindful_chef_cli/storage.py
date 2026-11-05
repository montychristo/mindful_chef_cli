"""Owner-only application state and OS credential-store sessions.

Passwords are never retained. The session value belongs in the operating
system's credential store; ordinary state files may hold plans and receipts.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator
import uuid


class StorageError(RuntimeError):
    """A credential or state operation could not be performed safely."""


_PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ACCOUNT_KEY = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SERVICE = "mindful_chef_cli.session.v1"
_MAX_BYTES = 1024 * 1024
_SAFE_BACKENDS = {
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.OS_X", "Keyring"),
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.libsecret", "Keyring"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
}


def _json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise StorageError("State must contain finite JSON values.") from None
    if len(encoded) > _MAX_BYTES:
        raise StorageError("State exceeds the size limit.")
    return encoded


def _session_json(value: Any) -> bytes:
    if not isinstance(value, dict):
        raise StorageError("Invalid saved session format.")

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in {"password", "passwd", "passphrase"}:
                    raise StorageError("Passwords must not be retained in session storage.")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    try:
        check(value)
    except RecursionError:
        raise StorageError("Session structure exceeds the nesting limit.") from None
    return _json_bytes(value)


def _check_private(info: os.stat_result, *, directory: bool) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise StorageError("State must be owned by the current user and have the expected file type.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise StorageError("State permissions must be owner-only (0700 directories, 0600 files).")
    if not directory and info.st_nlink != 1:
        raise StorageError("Hard-linked state files are not supported.")


def _open_directory(path: Path, *, create: bool) -> int:
    """Walk with openat/O_NOFOLLOW so a replaced path cannot redirect access."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise StorageError("State requires an absolute path without parent traversal.")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except Exception:
        os.close(current)
        raise


class SecureStore:
    """Store one named profile without placing secrets in the repository.

    ``backend`` is an injection point for callers' tests. Without it, only an
    explicit allowlist of OS credential backends is accepted. There is no
    plaintext credential-file fallback. Call ``lock`` around a complete
    read/modify/write or network mutation workflow to serialize processes.
    """

    def __init__(self, profile: str = "default", state_dir: str | Path | None = None, *, backend: Any = None,
                 _internal: bool = False):
        if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
            raise StorageError("Profile must be 1–64 letters, digits, underscores or hyphens.")
        if profile == "_writers" and not _internal:
            raise StorageError("This profile name is reserved for internal state.")
        if state_dir is None:
            base = os.environ.get("XDG_STATE_HOME")
            if base and not Path(base).is_absolute():
                raise StorageError("XDG_STATE_HOME must be an absolute path.")
            root = Path(base) / "mindful_chef_cli" if base else Path.home() / ".local" / "state" / "mindful_chef_cli"
        else:
            root = Path(state_dir).expanduser()
        if not root.is_absolute() or ".." in root.parts:
            raise StorageError("State directory must be an absolute path without parent traversal.")
        self.profile = profile
        self.root = root
        self.directory = root / profile
        self._backend = backend
        self._thread_lock = threading.RLock()
        self._locked_fd: int | None = None
        try:
            root_fd = _open_directory(root, create=True)
            try:
                _check_private(os.fstat(root_fd), directory=True)
            finally:
                os.close(root_fd)
            profile_fd = _open_directory(self.directory, create=True)
            try:
                _check_private(os.fstat(profile_fd), directory=True)
            finally:
                os.close(profile_fd)
        except StorageError:
            raise
        except OSError:
            raise StorageError("Cannot create a private state directory; symlinks are not supported.") from None

    def _credential_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import keyring

            backend = keyring.get_keyring()
            identity = (type(backend).__module__, type(backend).__name__)
            if identity not in _SAFE_BACKENDS or backend.priority <= 0:
                raise StorageError("A supported OS credential store is required; plaintext and chained backends are refused.")
            self._backend = backend
            return backend
        except StorageError:
            raise
        except Exception:
            raise StorageError("The OS credential store is unavailable. Configure a supported keyring backend.") from None

    def load_session(self) -> dict[str, Any] | None:
        try:
            raw = self._credential_backend().get_password(_SERVICE, self.profile)
            if raw is None:
                return None
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_BYTES:
                raise StorageError("Invalid saved session format.")
            value = json.loads(raw)
            _session_json(value)
            return value
        except StorageError:
            raise
        except (json.JSONDecodeError, UnicodeError):
            raise StorageError("Saved session is invalid; log in again.") from None
        except Exception:
            raise StorageError("Cannot read the OS credential store.") from None

    def save_session(self, session: dict[str, Any]) -> None:
        encoded = _session_json(session).decode("utf-8")
        try:
            self._credential_backend().set_password(_SERVICE, self.profile, encoded)
        except StorageError:
            raise
        except Exception:
            raise StorageError("Cannot save the session to the OS credential store.") from None

    def delete_session(self) -> None:
        try:
            backend = self._credential_backend()
            if backend.get_password(_SERVICE, self.profile) is not None:
                backend.delete_password(_SERVICE, self.profile)
        except StorageError:
            raise
        except Exception:
            raise StorageError("Cannot remove the session from the OS credential store.") from None

    def state_path(self, name: str) -> Path:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise StorageError("State filename must be a single safe path component.")
        return self.directory / name

    def claim_writer(self, account_key: str) -> None:
        """Bind an account to one writer profile across this state root.

        All callers acquire the account profile lock before the common registry
        lock. The registry never acquires an account profile lock, so concurrent
        claims from different profiles cannot invert that ordering. Read-only
        profiles do not need a claim. Migration is a separate deliberate task.
        """
        if not isinstance(account_key, str) or not _ACCOUNT_KEY.fullmatch(account_key):
            raise StorageError("Invalid account key for writer registration.")
        if self.profile == "_writers":
            raise StorageError("Internal state cannot own an account writer registration.")
        with self.lock():
            registry = SecureStore("_writers", state_dir=self.root, _internal=True)
            with registry.lock():
                owners = registry.read_private_json("writers.json", {})
                if not isinstance(owners, dict) or any(
                    not isinstance(key, str) or not _ACCOUNT_KEY.fullmatch(key)
                    or not isinstance(owner, str) or not _PROFILE.fullmatch(owner) or owner == "_writers"
                    for key, owner in owners.items()
                ):
                    raise StorageError("Writer registry is invalid; explicit state repair is required.")
                owner = owners.get(account_key)
                if owner is not None and owner != self.profile:
                    raise StorageError("This account is bound to another writer profile. Use its original profile or explicitly migrate the account state.")
                if owner is None:
                    owners[account_key] = self.profile
                    registry.write_private_json("writers.json", owners)

    @contextmanager
    def _directory_fd(self) -> Iterator[int]:
        fd = None
        try:
            fd = _open_directory(self.directory, create=False)
            _check_private(os.fstat(fd), directory=True)
        except StorageError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError:
            if fd is not None:
                os.close(fd)
            raise StorageError("Cannot open the private state directory safely.") from None
        try:
            yield fd
        finally:
            os.close(fd)

    def read_private_json(self, name: str, default: Any = None) -> Any:
        self.state_path(name)
        with self._directory_fd() as directory_fd:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                return default
            except OSError:
                raise StorageError("Cannot open state safely; symbolic links are refused.") from None
            try:
                info = os.fstat(fd)
                _check_private(info, directory=False)
                if info.st_size > _MAX_BYTES:
                    raise StorageError("State exceeds the size limit.")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise StorageError("State exceeds the size limit.")
                value = json.loads(raw)
                _json_bytes(value)
                return value
            except (json.JSONDecodeError, UnicodeError, RecursionError):
                raise StorageError("State is invalid JSON.") from None
            finally:
                os.close(fd)

    def write_private_json(self, name: str, data: Any) -> None:
        self.state_path(name)
        encoded = _json_bytes(data)
        with self._directory_fd() as directory_fd:
            temporary = f".tmp-{uuid.uuid4().hex}"
            try:
                try:
                    _check_private(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), directory=False)
                except FileNotFoundError:
                    pass
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "wb", closefd=False) as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            except StorageError:
                raise
            except OSError:
                raise StorageError("Cannot save private state safely.") from None
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an advisory process lock, reentrant for this store instance."""
        with self._thread_lock:
            if self._locked_fd is not None:
                yield
                return
            with self._directory_fd() as directory_fd:
                try:
                    fd = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
                    _check_private(os.fstat(fd), directory=False)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception as exc:
                    if "fd" in locals():
                        os.close(fd)
                    if isinstance(exc, StorageError):
                        raise
                    raise StorageError("Cannot acquire the private state lock safely.") from None
            self._locked_fd = fd
            try:
                yield
            finally:
                self._locked_fd = None
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
