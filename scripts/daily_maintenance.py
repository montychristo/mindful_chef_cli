"""Generate the public daily record from anonymous checks and official sources."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from scripts.check_public_tree import scan_text, scan_tree


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "HEALTH.md"
CHECKS = (
    ("Offline tests", ("-m", "unittest", "discover", "-s", "tests", "-q")),
    ("Ruff", ("-m", "ruff", "check", "src", "tests", "scripts")),
    ("Dependency audit", ("-m", "pip_audit", "--skip-editable", "--progress-spinner", "off")),
)
CONTRACTS = {
    "login_form": "Login form",
    "public_menu": "Public menu",
    "recipe_detail": "Sample recipe details",
}


def run_checks() -> list[tuple[str, str]]:
    results = []
    for label, args in CHECKS:
        try:
            result = subprocess.run(  # noqa: S603 - fixed Python modules and arguments
                [sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            results.append((label, "Unavailable"))
            continue
        status = "Passed" if result.returncode == 0 else "Failed"
        if label == "Offline tests" and result.returncode == 0:
            match = re.search(r"^Ran (\d{1,5}) tests?\b", result.stderr, re.MULTILINE)
            if match:
                status += f" ({int(match.group(1))} tests)"
        results.append((label, status))
    return results


def check_contracts() -> list[tuple[str, str]]:
    from mindful_chef_cli.storage import SecureStore
    from scripts.healthcheck import run_healthcheck

    with TemporaryDirectory(prefix="mindful-chef-public-") as directory:
        store = SecureStore(state_dir=Path(directory).resolve())
        result = run_healthcheck(store=store, public_only=True)["data"]["public"]
    outcomes = {check["name"]: check["ok"] for check in result["checks"]}
    return [(label, {True: "Passed", False: "Failed", None: "Unavailable"}[outcomes.get(key)])
            for key, label in CONTRACTS.items()]


def previous_fingerprints(report: str) -> dict[str, str]:
    match = re.search(r"^<!-- source-fingerprints: (.+) -->$", report, re.MULTILINE)
    if not match:
        return {}
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("Invalid source fingerprints in the previous record")
    return {key: value for key, value in data.items()
            if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key)
            and isinstance(value, str) and re.fullmatch(r"sha256[0-9a-f]{64}", value)}


def render_report(day: str, checks: list[tuple[str, str]], sources: list[dict], previous: str) -> str:
    saved = previous_fingerprints(previous)
    fingerprints = {}
    lines = ["# Daily maintenance", "", f"**Checked on {day}**", "",
             "GitHub Actions checks the anonymous website and runs the repository's offline tests.", "",
             "| Check | Result |", "| --- | --- |"]
    lines.extend(f"| {label} | {status} |" for label, status in checks)
    lines.extend(["", "## Mindful Chef sources", "",
                  "Changes flag public guidance for review. Source history is in this file's Git history.", "",
                  "| Official source | Result |", "| --- | --- |"])
    for source in sources:
        key = source["key"]
        prior = saved.get(key)
        digest = source.get("digest")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            fingerprints[key] = "sha256" + digest
            status = "Baseline recorded" if prior is None else "Unchanged"
            if prior is not None and fingerprints[key] != prior:
                status = "Changed since last successful check"
        else:
            status = "Unavailable"
            if prior is not None:
                fingerprints[key] = prior
        lines.append(f"| [{source['label']}]({source['url']}) | {status} |")
    if not sources:
        lines.append("| Official source checks | Unavailable |")
        fingerprints = saved
    metadata = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"))
    lines.extend(["", f"<!-- source-fingerprints: {metadata} -->", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-failed", action="store_true")
    args = parser.parse_args(argv)
    day = datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    if REPORT.is_symlink() or REPORT.parent.is_symlink():
        print("The maintenance report path must not use symlinks.", file=sys.stderr)
        return 1
    previous = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
    if f"**Checked on {day}**" in previous.splitlines():
        print(f"Daily maintenance is already recorded for {day}.")
        return 0

    if args.bootstrap_failed:
        checks = [("Dependency installation", "Failed")]
        sources = []
    else:
        from scripts.provider_updates import check_sources

        checks = check_contracts() + run_checks()
        sources = check_sources()
    findings = scan_tree(ROOT)
    if findings:
        print("Public-tree privacy scan failed. Review the affected files before publishing.", file=sys.stderr)
        return 1
    checks.append(("Public-tree privacy scan", "Passed"))
    report = render_report(day, checks, sources, previous)
    if scan_text("docs/HEALTH.md", report):
        print("Generated report failed its privacy scan.", file=sys.stderr)
        return 1
    REPORT.write_text(report, encoding="utf-8")
    print(f"Generated public maintenance record for {day}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
