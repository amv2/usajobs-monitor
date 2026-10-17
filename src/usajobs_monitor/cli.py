"""Command line: ``usajobs-monitor run | deliver | search | check-config | credentials``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG_NAME, ConfigError, load_settings


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, require_credentials=False)
    _print_warnings(settings.warnings)
    try:
        from .config import resolve_credentials
        _print_warnings(resolve_credentials(dict(settings.config)))
        credentials = "found"
    except ConfigError as exc:
        credentials = "missing (" + "; ".join(exc.errors) + ")"
    search = settings.config["search"]
    keywords = len(search["target_role_keywords"]) + len(search["search_only_keywords"])
    per_keyword = len(search["locations"]) + (1 if search["include_remote_jobs"] else 0)
    print(f"Config OK: {settings.config_path}")
    print(f"  Credentials: {credentials}")
    print(f"  Searches per run: {keywords * per_keyword} ({keywords} keywords x {per_keyword} locations)")
    print(f"  Reports folder: {settings.reports_dir}")
    print(f"  Decisions file: {settings.decisions_path}"
          + ("" if settings.decisions_path.is_file() else " (not created yet)"))
    from .delivery import check_channels
    channels = check_channels(settings.config)
    if not channels:
        print("  Delivery: none configured (reports stay in the reports folder)")
    for name, state in channels:
        print(f"  Delivery {name}: {state}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .runner import run_search

    settings = load_settings(args.config)
    _print_warnings(settings.warnings)
    output = (args.output_dir or settings.reports_dir).resolve()
    try:
        summary = run_search(settings, output)
    except Exception as exc:  # noqa: BLE001
        # Name the failure without echoing request headers or config values.
        print(f"Search failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    print(f"Reviewed {summary['jobs_reviewed']} jobs across {summary['usajobs_searches']} USAJOBS searches.")
    print(f"Matches: {summary['matches_after_final_filters']}  Watch list: {summary['watch_list_count']}  "
          f"Near misses: {summary['near_misses_reported']}")
    for warning in summary["search_warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Word briefing: {summary['word_briefing']}")
    print(f"Excel tracker: {summary['excel_tracker']}")
    return 0


def _deliverer(settings, preview: bool):
    from .delivery import deliver
    return lambda run_dir: deliver(run_dir, settings, dry_run=preview)


def cmd_run(args: argparse.Namespace) -> int:
    from .runner import run

    settings = load_settings(args.config)
    _print_warnings(settings.warnings)
    try:
        code, outcome = run(settings, None if args.no_deliver else _deliverer(settings, args.preview_delivery))
    except Exception as exc:  # noqa: BLE001
        # Do not echo config, credentials, remote errors, or raw exception text.
        print(f"usajobs-monitor run stopped ({type(exc).__name__}). Check local configuration and run artifacts.",
              file=sys.stderr)
        return 2
    print(json.dumps(outcome, indent=2))
    return code


def cmd_deliver(args: argparse.Namespace) -> int:
    from .runner import SUMMARY, run

    settings = load_settings(args.config)
    _print_warnings(settings.warnings)
    run_id = args.run_id
    try:
        if run_id == "latest":
            run_id = json.loads((settings.reports_dir / SUMMARY).read_text()).get("run_id") or ""
        code, outcome = run(settings, _deliverer(settings, args.preview), deliver_run=run_id)
    except Exception as exc:  # noqa: BLE001
        print(f"usajobs-monitor deliver stopped ({type(exc).__name__}). Check the run identifier and run folder.",
              file=sys.stderr)
        return 2
    print(json.dumps(outcome, indent=2))
    return code


CREDENTIAL_NAMES = ("USAJOBS_EMAIL", "USAJOBS_API_KEY")


def _credential_names(config_path: Path) -> list[tuple[str, bool]]:
    """``(variable, required)`` for the API credentials and each enabled channel's secrets."""
    from .config import DELIVERY_CHANNELS, DELIVERY_SECRET_ENV

    names = [(name, True) for name in CREDENTIAL_NAMES]
    if not config_path.is_file():
        return names
    delivery = load_settings(config_path, require_credentials=False).config.get("delivery") or {}
    for channel in DELIVERY_CHANNELS:
        settings = delivery.get(channel)
        if isinstance(settings, dict) and settings.get("enabled", True):
            names += [(settings[key], key != "token_env") for key in DELIVERY_SECRET_ENV[channel]]
    return names


def cmd_credentials(args: argparse.Namespace) -> int:
    from . import keychain

    try:
        if args.action == "status":
            env = keychain.environment()
            print(f"Keychain: {keychain.backend_description()}")
            for name, required in _credential_names(args.config):
                source = env.source(name)
                if source == "environment":
                    label = "set in the environment"
                    if env.store is not None and env.store.get(name):
                        label += " (also in the keychain; the environment wins)"
                elif source == "keychain":
                    label = "stored in the keychain"
                else:
                    label = "missing" if required else "not set (optional)"
                print(f"  {name}: {label}")
            return 0
        name = keychain.check_name(args.name)
        store = keychain.default_store()
        if store is None:
            print(f"No keychain available: {keychain.backend_description()}.", file=sys.stderr)
            return 2
        if args.action == "delete":
            print(f"Removed {name} from the keychain." if store.delete(name) else f"{name} was not in the keychain.")
            return 0
        if args.stdin:
            value = sys.stdin.readline().strip()
        else:
            import getpass
            value = getpass.getpass(f"{name} (input hidden): ").strip()
        if not value:
            print("Nothing entered; the keychain was not changed.", file=sys.stderr)
            return 2
        store.set(name, value)
        print(f"Saved {name} to the keychain.")
        if str(os.environ.get(name) or "").strip():
            print(f"warning: {name} is also set in the environment, which wins over the keychain.", file=sys.stderr)
        return 0
    except keychain.KeychainError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usajobs-monitor", description="Scheduled, filtered USAJOBS search.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_NAME),
                       help=f"config file, TOML or JSON (default: ./{DEFAULT_CONFIG_NAME})")
        p.set_defaults(func=func)
        return p

    run = add("run", "Search, validate the reports in an isolated run folder, publish them, then deliver them.",
              cmd_run)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--preview-delivery", action="store_true",
                      help="write the messages into the run folder instead of sending them")
    mode.add_argument("--no-deliver", action="store_true", help="skip delivery for this run")
    deliver = add("deliver", "Send (or preview) an earlier run again. Channels that already accepted it are skipped.",
                  cmd_deliver)
    deliver.add_argument("run_id", help='a run folder name from reports/runs, or "latest"')
    deliver.add_argument("--preview", action="store_true", help="write the messages instead of sending them")
    search = add("search", "Search and write reports directly, without run isolation.", cmd_search)
    search.add_argument("--output-dir", type=Path, help="write reports here instead of paths.reports_dir")
    add("check-config", "Validate the config and report what a run would do. Never prints credentials.", cmd_check_config)
    credentials = sub.add_parser(
        "credentials", help="Store or check credentials in the OS keychain. Never prints a secret.",
        description="Store or check credentials in the OS keychain (macOS Keychain, Windows Credential "
                    "Manager, Linux Secret Service). Needs the keyring extra. Environment variables win "
                    "over the keychain. Never prints a secret.")
    credentials.set_defaults(func=cmd_credentials)
    actions = credentials.add_subparsers(dest="action", required=True)
    status = actions.add_parser("status", help="show where each credential comes from, without values")
    status.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_NAME),
                        help="also list the delivery secrets this config needs")
    store = actions.add_parser("set", help="save a secret; prompts without echoing")
    store.add_argument("name", help="the variable name, for example USAJOBS_API_KEY")
    store.add_argument("--stdin", action="store_true", help="read the value from the first line of standard input")
    remove = actions.add_parser("delete", help="remove a saved secret")
    remove.add_argument("name", help="the variable name, for example USAJOBS_API_KEY")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
