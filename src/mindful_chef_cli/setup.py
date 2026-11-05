"""Print local agent configuration without reading accounts or changing settings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any


CLIENTS = ("codex", "claude-code", "claude-desktop", "generic")
_PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SERVER = "mindful_chef_cli"


class _SetupParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Reject unsupported arguments without echoing arbitrary entered values.
        raise ValueError("Invalid setup arguments.")


def _interpreter(value: str | os.PathLike[str] | None) -> str:
    value = os.fspath(value) if value is not None else sys.executable
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("Supply a valid absolute Python interpreter path.")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("The Python interpreter must be an existing executable at an absolute path.")
    # Resolving a venv symlink would replace its environment with base Python.
    return os.path.abspath(path)


def _skill_source() -> str | None:
    checkout = Path(__file__).absolute().parents[2] / "skills" / "mindful-chef-cli"
    installed = Path(sys.prefix) / "share" / _SERVER / "skills" / "mindful-chef-cli"
    return next((str(path) for path in (checkout, installed) if (path / "SKILL.md").is_file()), None)


def render_setup(client: str, *, python_executable: str | os.PathLike[str] | None = None,
                 profile: str = "default", allow_writes: bool = False) -> dict[str, Any]:
    """Return launch settings without a subprocess, account lookup or file write."""
    if client not in CLIENTS:
        raise ValueError("Unknown agent client.")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile) or profile == "_writers":
        raise ValueError("Invalid profile name.")
    if not isinstance(allow_writes, bool):
        raise ValueError("allow_writes must be true or false.")
    command = _interpreter(python_executable)
    args = ["-m", _SERVER]
    if profile != "default":
        args.extend(["--profile", profile])
    args.append("mcp")
    if allow_writes:
        args.append("--allow-writes")
    entry: dict[str, Any] = {"command": command, "args": args}
    if client == "codex":
        config = {"mcp_servers": {_SERVER: entry}}
        config_text = (f"[mcp_servers.{_SERVER}]\ncommand = {json.dumps(command, ensure_ascii=False)}\n"
                       f"args = {json.dumps(args, ensure_ascii=False)}\n")
        registration = shlex.join(["codex", "mcp", "add", _SERVER, "--", command, *args])
    else:
        if client == "claude-code":
            entry = {"type": "stdio", **entry}
        config = {"mcpServers": {_SERVER: entry}}
        config_text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        registration = shlex.join(["claude", "mcp", "add", "--transport", "stdio", _SERVER,
                                   "--", command, *args]) if client == "claude-code" else None
    return {"client": client, "server_name": _SERVER, "transport": "stdio", "command": command,
            "args": args, "config": config, "config_text": config_text,
            "registration_command": registration, "launch_command": shlex.join([command, *args]),
            "skill_source": _skill_source(), "account_apply_enabled": allow_writes,
            "settings_written": False}


def add_setup_arguments(parser: argparse.ArgumentParser, *, include_profile: bool = True) -> None:
    """Share flags with the main CLI; it may already have a global profile flag."""
    parser.add_argument("--client", choices=CLIENTS, default="generic")
    if include_profile:
        parser.add_argument("--profile", default="default")
    parser.add_argument("--allow-writes", action="store_true", help="Include the account apply tool in settings")
    parser.add_argument("--python", dest="python_executable", help="Absolute Python path; defaults to this environment")
    parser.add_argument("--format", choices=("json", "config", "command"), default="json")


def print_setup(data: dict[str, Any], output_format: str = "json") -> None:
    if output_format == "config":
        print(data["config_text"], end="")
    elif output_format == "command":
        print(data["registration_command"] or data["launch_command"])
    else:
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = _SetupParser(description=__doc__)
    add_setup_arguments(parser)
    try:
        args = parser.parse_args(argv)
        data = render_setup(args.client, python_executable=args.python_executable,
                            profile=args.profile, allow_writes=args.allow_writes)
    except (ValueError, OSError):
        print(json.dumps({"ok": False, "error": {"code": "invalid_setup", "message": "Invalid setup arguments, client, profile or Python interpreter path."}}))
        return 1
    print_setup(data, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
