#!/usr/bin/env python3
"""Read-only endpoint checks with private, sanitized change detection.

Run with the interpreter in which Mindful Chef CLI is installed. This script never
logs in, applies plans, repairs state, or retains provider response bodies.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Callable

from mindful_chef_cli.client import ClientError, MindfulChefClient
from mindful_chef_cli.storage import SecureStore, StorageError


_CHECK_NAMES = {"login_form", "public_menu", "recipe_detail", "authenticated_orders", "account_cart_contract", "request", "schema"}
_SAFE_CODES = {
    "auth_expired", "auth_required", "invalid_session", "invalid_credentials", "login_failed",
    "network_error", "http_error", "rate_limited", "redirect_limit", "ambiguous_redirect",
    "schema_changed", "recipe_detail_unavailable", "unsafe_destination", "not_editable", "unknown_delivery",
    "storage_unavailable", "invalid_state", "operation_failed",
}
_LOGIN_CODES = {"auth_expired", "auth_required", "invalid_session", "invalid_credentials", "login_failed"}
_STATE_FILE = "health-last.json"


def _safe_code(value: Any) -> str:
    return value if isinstance(value, str) and value in _SAFE_CODES else "operation_failed"


def _section(*, status: str, checked: bool, healthy: bool | None,
             checks: list[dict[str, Any]] | None = None, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "checked": checked, "healthy": healthy, "checks": checks or []}
    if code is not None:
        result["code"] = _safe_code(code)
    return result


def _sanitize_doctor(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("checks"), list):
        return _section(status="unhealthy", checked=True, healthy=False, code="schema_changed")
    checks = []
    for check in raw["checks"]:
        if not isinstance(check, dict):
            return _section(status="unhealthy", checked=True, healthy=False, code="schema_changed")
        name = check.get("name")
        name = name if isinstance(name, str) and name in _CHECK_NAMES else "unknown_check"
        ok = check.get("ok")
        item: dict[str, Any] = {"name": name, "ok": ok if isinstance(ok, bool) else None}
        if check.get("code") is not None:
            item["code"] = _safe_code(check["code"])
        checks.append(item)
    checks.sort(key=lambda item: (item["name"], str(item.get("code", "")), str(item["ok"])))
    healthy = raw.get("healthy") is True and bool(checks) and all(check["ok"] is True for check in checks)
    code = next((check["code"] for check in checks if check.get("code") in _LOGIN_CODES), None)
    return _section(status="needs_login" if code else "healthy" if healthy else "unhealthy",
                    checked=True, healthy=healthy, checks=checks, code=code)


def _doctor(client: Any, *, authenticated: bool) -> dict[str, Any]:
    try:
        return _sanitize_doctor(client.doctor(authenticated=authenticated))
    except StorageError:
        return _section(status="storage_unavailable", checked=True, healthy=False, code="storage_unavailable")
    except ClientError as error:
        code = _safe_code(error.code)
        return _section(status="needs_login" if code in _LOGIN_CODES else "unhealthy",
                        checked=True, healthy=False, code=code)
    except Exception:
        return _section(status="unhealthy", checked=True, healthy=False, code="operation_failed")


def _fingerprint(summary: dict[str, Any]) -> str:
    return sha256(json.dumps(summary, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _valid_previous(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"schema_version", "fingerprint", "summary", "checked_at"}
        and value.get("schema_version") == 1
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"[a-f0-9]{64}", value["fingerprint"]) is not None
        and isinstance(value.get("summary"), dict)
        and set(value["summary"]) == {"healthy", "requires_login", "public", "authenticated"}
        and isinstance(value.get("checked_at"), str)
        and _fingerprint(value["summary"]) == value["fingerprint"]
    )


def run_healthcheck(*, store: SecureStore, client_factory: Callable[..., Any] = MindfulChefClient,
                    public_only: bool = False) -> dict[str, Any]:
    """Check public contracts, then a saved session, without performing any login.

    Missing authentication is an expected setup state: public health can remain
    true and the process can exit successfully while ``requires_login`` is true.
    An expired saved session, blocked store, or failed endpoint is unhealthy.
    Only safe status/check names/codes enter output and the private receipt.
    """
    public = _section(status="not_checked", checked=False, healthy=None)
    authenticated = _section(status="disabled" if public_only else "not_checked", checked=False, healthy=None)
    requires_login = False
    try:
        with client_factory(store=store) as client:
            public = _doctor(client, authenticated=False)
            if not public_only:
                try:
                    session = store.load_session()
                except StorageError:
                    authenticated = _section(status="storage_unavailable", checked=False, healthy=False, code="storage_unavailable")
                except Exception:
                    authenticated = _section(status="storage_unavailable", checked=False, healthy=False, code="storage_unavailable")
                else:
                    if session is None:
                        requires_login = True
                        authenticated = _section(status="missing", checked=False, healthy=None)
                    else:
                        authenticated = _doctor(client, authenticated=True)
                        requires_login = authenticated["status"] == "needs_login"
                    # The session is only inspected for presence, never copied
                    # into a result, diagnostic, fingerprint, or ordinary file.
                    del session
    except StorageError:
        if not public["checked"]:
            public = _section(status="storage_unavailable", checked=False, healthy=False, code="storage_unavailable")
    except Exception:
        if not public["checked"]:
            public = _section(status="unhealthy", checked=False, healthy=False, code="operation_failed")
    healthy = public["healthy"] is True and authenticated["healthy"] is not False
    summary = {"healthy": healthy, "requires_login": requires_login, "public": public, "authenticated": authenticated}
    digest = _fingerprint(summary)
    data = {**summary, "authenticated_checked": authenticated["checked"], "changed": True,
            "checked_at": datetime.now(timezone.utc).isoformat(), "writes_tested": False}
    try:
        with store.lock():
            previous = store.read_private_json(_STATE_FILE)
            if previous is not None and not _valid_previous(previous):
                data["healthy"] = False
                data["state"] = {"status": "invalid", "code": "invalid_state"}
                # Never silently repair, overwrite, or delete an invalid receipt.
                return {"ok": True, "data": data}
            data["changed"] = previous is None or previous["fingerprint"] != digest
            store.write_private_json(_STATE_FILE, {"schema_version": 1, "fingerprint": digest,
                                                  "summary": summary, "checked_at": data["checked_at"]})
    except Exception:
        data["healthy"] = False
        data["changed"] = True
        data["state"] = {"status": "unavailable", "code": "storage_unavailable"}
    return {"ok": True, "data": data}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Mindful Chef endpoint checks with private change detection.")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--public-only", action="store_true", help="Do not read the credential store or check account endpoints")
    args = parser.parse_args(argv)
    try:
        result = run_healthcheck(store=SecureStore(profile=args.profile), public_only=args.public_only)
    except Exception:
        result = {"ok": True, "data": {"healthy": False, "requires_login": False, "changed": True,
                  "authenticated_checked": False, "writes_tested": False,
                  "public": _section(status="not_checked", checked=False, healthy=None),
                  "authenticated": _section(status="not_checked", checked=False, healthy=None),
                  "state": {"status": "unavailable", "code": "storage_unavailable"}}}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["data"]["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
