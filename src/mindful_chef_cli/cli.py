"""JSON CLI and shared operations for the optional local MCP adapter."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
import warnings

from .planner import plan_away_weeks, recommend_meals
from .preferences import default_preferences, validate_preferences
from .storage import SecureStore, StorageError


class CLIError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's usual error includes rejected arguments, which could be secrets.
        raise CLIError("invalid_arguments", "Invalid arguments. Run mindful_chef_cli --help or the command's --help.")


def error_envelope(error: Exception) -> dict[str, Any]:
    """Return safe errors, never repr(), HTTP bodies, credentials, or tracebacks."""
    if isinstance(error, CLIError):
        code, message = error.code, str(error)
    elif isinstance(error, StorageError):
        code, message = "storage_error", str(error)
    else:
        # Only the owning client's audited exception is allowed to supply details.
        try:
            from .client import ClientError
        except ImportError:
            ClientError = ()  # type: ignore[assignment]
        if isinstance(error, ClientError):
            code, message = error.code, str(error)
        elif isinstance(error, ValueError):
            code, message = "invalid_input", "Invalid input data. Check the documented JSON schema and command options."
        elif isinstance(error, (OSError, UnicodeError)):
            code, message = "file_error", "Cannot read the requested input file safely."
        else:
            code, message = "operation_failed", "The operation failed; sensitive diagnostic details were withheld."
    return {"ok": False, "error": {"code": code, "message": message}}


def _read_json_file(path: str) -> Any:
    """Read an explicitly named CLI input, bounded to the private-state size cap."""
    with Path(path).expanduser().open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise CLIError("invalid_input", "Input JSON exceeds the one-megabyte size limit.")
    return json.loads(raw)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Invalid recipe identifier")
    return value.strip()


def _slug(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return urlsplit(value).path.rstrip("/").rsplit("/", 1)[-1] or None


class AgentOperations:
    """Bounded operations for one local profile; no raw HTTP or secret access API."""

    def __init__(self, store: SecureStore, client_factory: Callable[..., Any] | None = None):
        self.store = store
        self.client_factory = client_factory

    @contextmanager
    def client(self) -> Iterator[Any]:
        factory = self.client_factory
        if factory is None:
            from .client import MindfulChefClient
            factory = MindfulChefClient
        with factory(store=self.store) as client:
            yield client

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self.client() as client:
            return getattr(client, method)(*args, **kwargs)

    def preferences(self) -> dict[str, Any]:
        with self.store.lock():
            prefs = validate_preferences(self.store.read_private_json("preferences.json"))
            annotations = self.store.read_private_json("annotations.json", {})
            ratings = self.store.read_private_json("ratings.json", [])
        if not isinstance(annotations, dict) or not isinstance(ratings, list):
            raise ValueError("Invalid private preferences state")
        return {"preferences": prefs, "annotations": annotations, "ratings": ratings}

    def init_preferences(self) -> dict[str, Any]:
        with self.store.lock():
            current = self.store.read_private_json("preferences.json")
            if current is not None:
                return {"created": False, "preferences": validate_preferences(current)}
            prefs = default_preferences()
            self.store.write_private_json("preferences.json", prefs)
        return {"created": True, "preferences": prefs}

    def set_preferences(self, data: dict[str, Any]) -> dict[str, Any]:
        prefs = validate_preferences(data)
        with self.store.lock():
            self.store.write_private_json("preferences.json", prefs)
        return {"saved": True, "preferences": prefs}

    def annotate(self, recipe_id: str, *, reheat: str | None = None, finishing: str | None = None) -> dict[str, Any]:
        identifier = _identifier(recipe_id)
        if reheat not in (None, "good", "fair", "poor") or finishing not in (None, "low", "medium", "high"):
            raise ValueError("Invalid assessment")
        if reheat is None and finishing is None:
            raise CLIError("invalid_input", "Supply --reheat, --finishing, or both.")
        with self.store.lock():
            annotations = self.store.read_private_json("annotations.json", {})
            if not isinstance(annotations, dict):
                raise ValueError("Invalid annotation state")
            item = dict(annotations.get(identifier, {}))
            if reheat is not None:
                item["reheat_suitability"] = reheat
            if finishing is not None:
                item["finishing_effort"] = finishing
            item["assessment_source"] = "household"
            annotations[identifier] = item
            self.store.write_private_json("annotations.json", annotations)
        return {"saved": True, "recipe_id": identifier, "assessment": item}

    def rate(self, recipe_id: str, rating: int) -> dict[str, Any]:
        identifier = _identifier(recipe_id)
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ValueError("Rating must be 1..5")
        with self.store.lock():
            ratings = self.store.read_private_json("ratings.json", [])
            if not isinstance(ratings, list) or any(not isinstance(item, dict) for item in ratings):
                raise ValueError("Invalid rating state")
            ratings = [item for item in ratings if str(item.get("recipe_id")) != identifier]
            item = {"recipe_id": identifier, "rating": rating}
            ratings.append(item)
            self.store.write_private_json("ratings.json", ratings)
        return {"saved": True, **item}

    def recommend(self, delivery_date: str, count: int = 3) -> dict[str, Any]:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12:
            raise ValueError("count must be 1..12")
        private = self.preferences()
        history = self.store.read_private_json("history.json", [])
        if not isinstance(history, list):
            raise ValueError("Invalid history state")
        history = list(history) + private["ratings"]
        with self.client() as client:
            menu = client.menu(delivery_date, details=True)
            recipes = menu.get("recipes") if isinstance(menu, dict) else None
            if not isinstance(recipes, list):
                raise CLIError("schema_changed", "Menu recipe data has an unexpected shape.")
            source = {"past_orders_page": 1, "complete": False, "customer_selections_matched": 0,
                      "status": "checked", "note": "One page only; provider-selected meals are not preference signals."}
            by_slug = {_slug(item.get("url")): str(item.get("id", item.get("recipe_id")))
                       for item in recipes if isinstance(item, dict) and _slug(item.get("url"))}
            try:
                discovered = []
                past = client.orders(past=True, page=1)
                if not isinstance(past, list):
                    raise ValueError("Unexpected orders shape")
                for order in past:
                    if not isinstance(order, dict):
                        continue
                    items = order.get("recipes", order.get("items", []))
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        recipe = item.get("recipe", item)
                        chosen_by = item.get("chosen_by", order.get("chosen_by"))
                        if isinstance(recipe, dict) and chosen_by == "customer":
                            matched = by_slug.get(_slug(recipe.get("url")))
                            if matched is not None:
                                discovered.append({"recipe_id": matched, "selected": True})
                history.extend(discovered)
                source["customer_selections_matched"] = len(discovered)
            except Exception:
                # History is optional evidence. Surface the coverage loss without
                # leaking an HTTP body or turning it into proof of no history.
                source["status"] = "unavailable"
                source["note"] = "Past orders unavailable; using private history and explicit ratings only."
        annotated = []
        for recipe in recipes:
            if not isinstance(recipe, dict):
                raise CLIError("schema_changed", "Menu recipe data has an unexpected shape.")
            identifier = str(recipe.get("id", recipe.get("recipe_id")))
            assessment = private["annotations"].get(identifier, {})
            if not isinstance(assessment, dict):
                raise ValueError("Invalid annotation state")
            overlay = {key: assessment[key] for key in ("reheat_suitability", "finishing_effort") if key in assessment}
            annotated.append({**recipe, **overlay})
        result = recommend_meals(annotated, private["preferences"], history=history, count=count)
        result["delivery_date"] = delivery_date
        if isinstance(menu.get("detail_coverage"), dict):
            result["detail_coverage"] = menu["detail_coverage"]
        result["warnings"].extend(w for w in menu.get("warnings", []) if isinstance(w, str))
        result["history_source"] = source
        result["assessment_source"] = "Recipe facts plus explicit private household annotations; unknown assessments remain unknown."
        if source["status"] != "checked":
            result["warnings"].append(source["note"])
        return result


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="mindful_chef_cli", description="Mindful Chef CLI: an unofficial, local Mindful Chef assistant. Outputs JSON.")
    parser.add_argument("--profile", default="default", help="Private local profile name (default: default)")
    try:
        package_version = version("mindful_chef_cli")
    except PackageNotFoundError:
        package_version = "0.1.0"
    parser.add_argument("--version", action="version", version=f"mindful_chef_cli {package_version}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    auth = commands.add_parser("auth", help="Manage a session in the OS credential store")
    auth_commands = auth.add_subparsers(dest="action", required=True, parser_class=_Parser)
    login = auth_commands.add_parser("login", help="Prompt for email and password in a real terminal")
    login.add_argument("--reauth", action="store_true", help="Replace the saved session with a fresh login")
    for name in ("status", "refresh", "logout"):
        auth_commands.add_parser(name)
    orders = commands.add_parser("orders")
    order_commands = orders.add_subparsers(dest="action", required=True, parser_class=_Parser)
    orders_list = order_commands.add_parser("list")
    orders_list.add_argument("--past", action="store_true")
    orders_list.add_argument("--page", type=int, default=1)
    menu = commands.add_parser("menu")
    menu.add_argument("date", metavar="DATE")
    menu.add_argument("--details", action="store_true")
    recipe = commands.add_parser("recipe")
    recipe.add_argument("slug", metavar="SLUG")
    prefs = commands.add_parser("prefs", help="Private household preferences and recipe assessments")
    pref_commands = prefs.add_subparsers(dest="action", required=True, parser_class=_Parser)
    for name in ("init", "show"):
        pref_commands.add_parser(name)
    pref_set = pref_commands.add_parser("set")
    pref_set.add_argument("--file", required=True)
    annotate = pref_commands.add_parser("annotate")
    annotate.add_argument("recipe_id", metavar="RECIPE_ID")
    annotate.add_argument("--reheat", choices=("good", "fair", "poor"))
    annotate.add_argument("--finishing", choices=("low", "medium", "high"))
    rate = pref_commands.add_parser("rate")
    rate.add_argument("recipe_id", metavar="RECIPE_ID")
    rate.add_argument("rating", type=int, choices=range(1, 6))
    recommend = commands.add_parser("recommend")
    recommend.add_argument("date", metavar="DATE")
    recommend.add_argument("--count", type=int, default=3)
    away = commands.add_parser("away", help="Create proposals from explicitly supplied minimal calendar data")
    away.add_argument("--deliveries", required=True, metavar="FILE")
    away.add_argument("--events", required=True, metavar="FILE")
    away.add_argument("--days", type=int, default=7)
    changes = commands.add_parser("changes", help="Review plans before applying exact approved changes")
    change_commands = changes.add_subparsers(dest="action", required=True, parser_class=_Parser)
    select = change_commands.add_parser("select")
    select.add_argument("date", metavar="DATE")
    select.add_argument("recipe_ids", nargs="+", metavar="ID")
    select.add_argument("--max-total-cents", type=int)
    for name in ("skip", "resume"):
        item = change_commands.add_parser(name)
        item.add_argument("date", metavar="DATE")
        if name == "resume":
            item.add_argument("--max-total-cents", type=int)
    for name in ("show", "apply", "rollback", "reconcile"):
        item = change_commands.add_parser(name)
        item.add_argument("plan_id", metavar="ID")
        if name == "apply":
            item.add_argument("--approve", required=True, metavar="HASH", help="Exact reviewed plan hash; human approval remains required")
    doctor = commands.add_parser("doctor", help="Read-only endpoint and schema checks")
    doctor.add_argument("--authenticated", action="store_true")
    mcp = commands.add_parser("mcp", help="Run an optional stdio-only MCP server")
    mcp.add_argument("--allow-writes", action="store_true", help="Expose apply tools; each plan still requires exact human approval")
    from .setup import add_setup_arguments
    setup = commands.add_parser("setup", help="Print ready-to-use agent configuration without changing settings")
    add_setup_arguments(setup, include_profile=False)
    return parser


def _login(operations: AgentOperations) -> dict[str, Any]:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CLIError("interactive_required", "Run auth login in an interactive terminal. Password flags, environment variables and piped input are unsupported.")
    print("Email: ", end="", file=sys.stderr, flush=True)
    email = input().strip()
    if not email or "@" not in email:
        raise CLIError("invalid_input", "Enter a valid account email address.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("Password: ", stream=sys.stderr)
        except getpass.GetPassWarning:
            raise CLIError("interactive_required", "A terminal with password echo disabled is required.") from None
    if not password:
        raise CLIError("invalid_input", "A password is required.")
    try:
        return operations.call("login", email, password)
    finally:
        del password


def _dispatch(args: argparse.Namespace, operations: AgentOperations) -> Any:
    if args.command == "auth":
        if args.action == "login":
            return _login(operations)
        return operations.call({"status": "auth_status", "refresh": "refresh", "logout": "logout"}[args.action])
    if args.command == "orders":
        if args.page < 1:
            raise ValueError("Invalid page")
        return operations.call("orders", past=args.past, page=args.page)
    if args.command == "menu":
        return operations.call("menu", args.date, details=args.details)
    if args.command == "recipe":
        return operations.call("recipe", args.slug)
    if args.command == "prefs":
        if args.action == "init":
            return operations.init_preferences()
        if args.action == "show":
            return operations.preferences()
        if args.action == "set":
            return operations.set_preferences(_read_json_file(args.file))
        if args.action == "annotate":
            return operations.annotate(args.recipe_id, reheat=args.reheat, finishing=args.finishing)
        return operations.rate(args.recipe_id, args.rating)
    if args.command == "recommend":
        return operations.recommend(args.date, args.count)
    if args.command == "away":
        return plan_away_weeks(_read_json_file(args.deliveries), _read_json_file(args.events), days=args.days)
    if args.command == "changes":
        if args.action == "select":
            return operations.call("propose_selection", args.date, args.recipe_ids, max_total_cents=args.max_total_cents)
        if args.action in ("skip", "resume"):
            kwargs = {"max_total_cents": args.max_total_cents} if args.action == "resume" else {}
            return operations.call("propose_" + args.action, args.date, **kwargs)
        if args.action == "apply":
            return operations.call("apply_plan", args.plan_id, approval=args.approve)
        return operations.call({"show": "show_plan", "rollback": "rollback_plan", "reconcile": "reconcile_plan"}[args.action], args.plan_id)
    if args.command == "doctor":
        return operations.call("doctor", authenticated=args.authenticated)
    raise CLIError("invalid_arguments", "Unsupported command.")


def main(argv: list[str] | None = None, *, store_factory: Callable[..., Any] = SecureStore,
         client_factory: Callable[..., Any] | None = None) -> int:
    args = None
    try:
        args = _parser().parse_args(argv)
        if args.command == "setup":
            from .setup import print_setup, render_setup
            data = render_setup(args.client, python_executable=args.python_executable,
                                profile=args.profile, allow_writes=args.allow_writes)
            print_setup(data, args.format)
            return 0
        store = store_factory(profile=args.profile)
        if args.command == "mcp":
            from .mcp_server import run_server
            run_server(store=store, allow_writes=args.allow_writes, client_factory=client_factory)
            return 0
        data = _dispatch(args, AgentOperations(store, client_factory))
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, allow_nan=False))
        return 0
    except (KeyboardInterrupt, EOFError):
        envelope = {"ok": False, "error": {"code": "cancelled", "message": "Operation cancelled."}}
    except Exception as error:
        envelope = error_envelope(error)
    output = sys.stderr if args is not None and args.command == "mcp" else sys.stdout
    print(json.dumps(envelope, ensure_ascii=False, allow_nan=False), file=output)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
