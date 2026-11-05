#!/usr/bin/env python3
"""Check a source tree for private artifacts without printing their contents."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


_BUILD_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", "dist", "build"}
_PRIVATE_DIRS = {"private", ".state", "state", "screenshots", "captures", "plans", "receipts"}
_PRIVATE_FILES = {"preferences.json", "annotations.json", "history.json", "health.json"}
_MAX_SIZE = 2 * 1024 * 1024
_RESERVED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.test"}
_PLACEHOLDERS = {"", "sample", "dummy", "test", "example", "placeholder", "redacted", "password", "token", "secret", "none", "null"}
_SECRET = re.compile(
    r'''(?ix)\b(?:[a-z0-9_-]*(?:token|secret|password|passwd|api_key|apikey)|authorization|cookie|set-cookie)\b
        ["']?\s*[:=]\s*["']([^"'\r\n]+)["']'''
)
_HEADER = re.compile(r"(?i)^\s*(?:cookie|set-cookie|authorization)\s*:\s*(.+)$")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])([a-z0-9][a-z0-9.!#$%&'*+/=?^_`{|}~-]*@([a-z0-9-]+(?:\.[a-z0-9-]+)+))")
_HOME = re.compile(r"(?:/(?:Users|home)/[^/\s\"'<>]+|[A-Za-z]:\\Users\\[^\\\s\"'<>]+)")
_JWT = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
_PHONE = re.compile(r"(?<![\w\d])(?:\+44[\s().-]*|0)[127](?:[\s().-]*\d){9}(?!\d)")
_ADDRESS = re.compile(r"(?i)\b\d{1,4}[A-Z]?\s+(?:[A-Z][a-z'-]+\s+){1,5}(?:Road|Street|Avenue|Lane|Drive|Close|Crescent|Terrace|Way)\b")
_COOKIE_VALUE = re.compile(r'''["']value["']\s*:\s*["']([^"'\r\n]+)["']''')


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str

    def render(self) -> str:
        # Escape control characters in malicious filenames rather than emit them.
        safe = self.path.encode("unicode_escape").decode("ascii")
        return f"{safe}:{self.line}:{self.rule}"


def _placeholder(value: str) -> bool:
    value = value.strip()
    lower = value.lower()
    return (
        lower in _PLACEHOLDERS
        or lower.startswith(("synthetic-", "dummy-", "test-", "example-"))
        or (value.startswith("<") and value.endswith(">"))
        or value.startswith(("${", "os.environ", "process.env"))
    )


def _private_artifact(path: Path) -> bool:
    name = path.name.lower()
    return (
        any(part.lower() in _PRIVATE_DIRS for part in path.parts)
        or name in _PRIVATE_FILES
        or name.endswith((".har", ".log", ".sqlite", ".sqlite3", ".db"))
        or name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or (name.endswith(".json") and any(part in name for part in ("cookie", "session", "credential")))
    )


def scan_text(path: str, content: str) -> list[Finding]:
    """Return locations and rule names only; never return captured values."""
    findings = set()
    lines = content.splitlines()
    cookie_context = 0
    for number, line in enumerate(lines, 1):
        if re.search(r'''["']cookies["']\s*:\s*\[''', line):
            cookie_context = 24
        if _HOME.search(line):
            findings.add(Finding(path, number, "personal-home-path"))
        for match in _EMAIL.finditer(line):
            domain = match.group(2).lower()
            if domain not in _RESERVED_EMAIL_DOMAINS and not domain.endswith((".invalid", ".test")):
                findings.add(Finding(path, number, "personal-email"))
        for match in _SECRET.finditer(line):
            if not _placeholder(match.group(1)):
                findings.add(Finding(path, number, "credential-literal"))
        header = _HEADER.match(line)
        if header and not _placeholder(header.group(1)):
            findings.add(Finding(path, number, "credential-header"))
        if cookie_context:
            for match in _COOKIE_VALUE.finditer(line):
                if not _placeholder(match.group(1)):
                    findings.add(Finding(path, number, "session-cookie-value"))
            cookie_context -= 1
        if _JWT.search(line):
            findings.add(Finding(path, number, "jwt"))
        if _KEY_BLOCK.search(line):
            findings.add(Finding(path, number, "private-key"))
        if _PHONE.search(line):
            findings.add(Finding(path, number, "personal-phone"))
        if _ADDRESS.search(line):
            findings.add(Finding(path, number, "personal-address"))
    return sorted(findings)


def _excluded_build(path: Path) -> bool:
    return any(part in _BUILD_DIRS or part.endswith(".egg-info") for part in path.parts) or path.suffix in {".pyc", ".pyo"}


def _git_files(root: Path) -> tuple[list[str], set[str]] | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        # Fixed Git operations, no shell; root is an absolute argument to -C.
        top = subprocess.run([git, "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, check=True, text=True)  # noqa: S603
        if Path(top.stdout.strip()).resolve() != root:
            return None
        tracked = subprocess.run([git, "-C", str(root), "ls-files", "--cached", "-z"], capture_output=True, check=True)  # noqa: S603
        source = subprocess.run([git, "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, check=True)  # noqa: S603
    except (OSError, subprocess.CalledProcessError):
        return None
    def decode(raw: bytes) -> set[str]:
        return {os.fsdecode(item) for item in raw.split(b"\0") if item}

    return sorted(decode(source.stdout)), decode(tracked.stdout)


def _ignore_patterns(root: Path) -> list[str]:
    ignore = root / ".gitignore"
    if not ignore.is_file() or ignore.is_symlink():
        return []
    # Git performs full ignore matching once initialized. This fallback handles
    # the project's simple root patterns while still finding new source files.
    return [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _ignored(relative: Path, patterns: list[str]) -> bool:
    ignored = False
    name = relative.as_posix()
    for raw in patterns:
        negate = raw.startswith("!")
        pattern = raw[1:] if negate else raw
        pattern = pattern.removeprefix("/")
        if pattern.endswith("/"):
            target = pattern[:-1]
            matches = target in relative.parts[:-1] if "/" not in target else name.startswith(target + "/")
        else:
            matches = fnmatch.fnmatchcase(name, pattern) or ("/" not in pattern and fnmatch.fnmatchcase(relative.name, pattern))
        if matches:
            ignored = not negate
    return ignored


def scan_tree(root: Path) -> list[Finding]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        return [Finding(".", 1, "missing-root")]
    listing = _git_files(root)
    if listing is None:
        patterns = _ignore_patterns(root)
        names = []
        for directory, children, files in os.walk(root, followlinks=False):
            children[:] = sorted(child for child in children if child not in _BUILD_DIRS and not child.endswith(".egg-info"))
            for child in children:
                candidate = Path(directory) / child
                relative = candidate.relative_to(root)
                if candidate.is_symlink() and not _ignored(relative, patterns):
                    names.append(relative.as_posix())
            for name in files:
                relative = (Path(directory) / name).relative_to(root)
                if not _ignored(relative, patterns):
                    names.append(relative.as_posix())
        tracked = set()
    else:
        names, tracked = listing
    findings = []
    for name in sorted(set(names)):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            findings.append(Finding(name, 1, "unsafe-source-path"))
            continue
        if _excluded_build(relative):
            continue
        if _private_artifact(relative):
            # Ignored local files are omitted by enumeration. A tracked private
            # artifact remains a publication failure even if later gitignored.
            findings.append(Finding(name, 1, "tracked-private-artifact" if name in tracked else "private-artifact"))
            continue
        source = root / relative
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != root and root in parent.parents):
            findings.append(Finding(name, 1, "symlink-review"))
            continue
        if not source.is_file():
            if name in tracked:
                continue  # A deleted tracked file is not in the release tree.
            findings.append(Finding(name, 1, "unreadable-source"))
            continue
        try:
            if source.stat().st_size > _MAX_SIZE:
                findings.append(Finding(name, 1, "oversize-review"))
                continue
            raw = source.read_bytes()
            if b"\0" in raw[:8192]:
                continue  # Binary metadata needs separate human review.
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(Finding(name, 1, "unreadable-source"))
            continue
        findings.extend(scan_text(name, content))
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check source files for publication privacy risks; never print matched values.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    findings = scan_tree(args.root)
    for finding in findings:
        print(finding.render())
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
