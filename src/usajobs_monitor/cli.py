"""Command line: ``usajobs-monitor init | run | deliver | search | decide | fit | mcp | check-config |
profiles | credentials | pay-table``."""

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
    from .config import target_label
    target = target_label(settings.config)
    if target:
        resolved = settings.config["target"]["resolved"]
        print(f"  Pay target: {target}")
        for key, value in resolved["applied"].items():
            shown = f"${value:,}" if isinstance(value, int) and "salary" in key else json.dumps(value)
            print(f"    filters.{key} = {shown}")
        for key in resolved["overridden"]:
            print(f"    filters.{key}: set in your config, so the target value is not used")
    eligibility = settings.config["eligibility"]
    from .presets import preset_labels
    if preset_labels(settings.config):
        print(f"  Applicant type: {'; '.join(preset_labels(settings.config))}")
        for key, value in eligibility.get("resolved_presets", {}).items():
            print(f"    eligibility.{key} = {json.dumps(value)}")
    from .reports import REPORT_LABELS, report_files
    print("  Reports: " + ", ".join(REPORT_LABELS[name] for name in report_files(settings.config)))
    print("  " + _ai_status(settings))
    from .delivery import check_channels
    channels = check_channels(settings.config)
    if not channels:
        print("  Delivery: none configured (reports stay in the reports folder)")
    for name, state in channels:
        print(f"  Delivery {name}: {state}")
    return 0


def _ai_status(settings) -> str:
    """One line on the optional AI add-on. Reads the resume's name, never its text."""
    ai = settings.config["ai"]
    if not ai["fit_notes"]:
        return "AI fit notes: off"
    from .resume_text import ResumeError, find_resumes
    try:
        resume = ", ".join(path.name for path in find_resumes(settings.ai_resume, settings.resume_folder))
    except ResumeError as exc:
        resume = f"not found ({exc})"
    provider = f"{ai['provider']} {ai['model']}" if ai["provider"] == "anthropic" else ai["provider"]
    sending = "allowed" if ai["send_resume"] else "not allowed (set ai.send_resume = true)"
    line = f"AI fit notes: on ({provider}); resume: {resume}; sending it to the provider: {sending}"
    if ai["provider"] == "anthropic":
        from . import keychain
        line += f"; {ai['api_key_env']}: " + ("found" if keychain.environment().source(ai["api_key_env"]) else "missing")
    return line


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
    from .reports import REPORT_LABELS
    for name in summary["report_files"]:
        print(f"{REPORT_LABELS[name]}: {output / name}")
    fit_notes = summary.get("fit_notes") or {}
    if fit_notes.get("file"):
        print(f"Fit notes ({fit_notes['notes']}, {fit_notes['from_cache']} reused): {output / fit_notes['file']}")
    return 0


def _deliverer(settings, preview: bool):
    from .delivery import deliver
    return lambda run_dir: deliver(run_dir, settings, dry_run=preview)


def cmd_run(args: argparse.Namespace) -> int:
    if args.all:
        return _run_all(args)
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


def _run_all(args: argparse.Namespace) -> int:
    """Run every saved profile in turn; the exit code is the worst one."""
    from . import profiles

    saved = profiles.list_profiles()
    if not saved:
        print(f"No saved profiles in {profiles.profiles_dir()}. Create one with: usajobs-monitor init", file=sys.stderr)
        return 2
    folders: dict[Path, str] = {}
    for name, path in saved:
        reports = load_settings(path, require_credentials=False).reports_dir
        if reports in folders:
            print(f"Profiles {folders[reports]} and {name} share the reports folder {reports}; give each its own "
                  "paths.reports_dir so their history and decisions stay separate.", file=sys.stderr)
            return 2
        folders[reports] = name
    worst = 0
    for name, path in saved:
        print(f"== {name} ==")
        try:
            code = cmd_run(argparse.Namespace(**{**vars(args), "all": False, "config": path}))
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            code = 2
        worst = max(worst, code)
    return worst


def cmd_profiles(args: argparse.Namespace) -> int:
    from . import profiles

    saved = profiles.list_profiles()
    print(f"Profiles folder: {profiles.profiles_dir()}"
          + ("" if os.environ.get(profiles.HOME_VAR) else f" (set {profiles.HOME_VAR} to move it)"))
    if not saved:
        print("  No profiles yet. Create one with: usajobs-monitor init")
    for name, path in saved:
        try:
            settings = load_settings(path, require_credentials=False)
            search = settings.config["search"]
            detail = f"{len(search['locations'])} locations, {len(search['target_role_keywords'])} keywords"
        except ConfigError as exc:
            detail = f"{len(exc.errors)} config problems; run check-config --profile {name}"
        print(f"  {name:<20} {detail}  ({path.name})")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    from .tracking import load_decisions, load_history, save_decision

    settings = load_settings(args.config, require_credentials=False)
    path = settings.decisions_path
    if args.control_number is None:
        decisions, warnings = load_decisions(path)
        _print_warnings(warnings)
        if not decisions:
            print(f"No decisions yet in {path}.")
        for number, decision in decisions.items():
            note = f"  {decision['note']}" if decision["note"] else ""
            print(f"{number:<12} {decision['status']:<15}{note}")
        return 0
    if args.status is None:
        print("decide: give a status: applied, watching, not_interested or clear", file=sys.stderr)
        return 2
    number = args.control_number.strip()
    seen = ((load_history(settings.history_path) or {}).get("jobs") or {}).get(number) or {}
    title = str(seen.get("title") or "")
    try:
        previous = save_decision(path, number, args.status, " ".join(args.note), title)
    except ValueError as exc:
        print(f"decide: {exc}", file=sys.stderr)
        return 2
    from .tracking import normalize_status
    status = normalize_status(args.status)
    label = f"{number} ({title}, {seen.get('agency', '')})" if title else number
    if status == "clear":
        print(f"Cleared the decision for {label}." if previous else f"{label} had no decision; nothing changed.")
    else:
        print(f"Marked {label} as {status.replace('_', ' ')}"
              + (f" (was {previous.get('status')})" if previous else "") + f". Saved in {path}.")
        if status == "not_interested":
            print("It is hidden from the next run's lists.")
    if not title:
        print("warning: this control number is not in the last run's history; check it against the USAJOBS link.",
              file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from . import keychain, profiles
    from .wizard import Cancelled, Wizard, render_profile, write_profile

    print("usajobs-monitor setup. Press Enter to accept the [default]; Ctrl-C cancels without saving.")
    try:
        if args.config is not None:
            path, reports_dir, decisions = Path(args.config), "reports", "job_decisions.csv"
        else:
            name = args.profile
            while not name:
                try:
                    name = profiles.check_name(input("Profile name [default]: ").strip() or "default")
                except ValueError as exc:
                    print(f"  {exc}")
            name = profiles.check_name(name)
            path = profiles.profiles_dir() / f"{name}.toml"
            reports_dir, decisions = f"{name}/reports", f"{name}/job_decisions.csv"
        if path.exists() and not args.force:
            print(f"{path} already exists. Pass --force to replace it, or pick another name.", file=sys.stderr)
            return 2
        import getpass
        wizard = Wizard(secret=getpass.getpass, store=keychain.default_store())
        answers = wizard.run()
    except (Cancelled, KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing was saved.", file=sys.stderr)
        return 130
    except (ValueError, keychain.KeychainError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    write_profile(path, render_profile(answers, reports_dir=reports_dir, decisions_file=decisions), force=args.force)
    print(f"\nSaved {path}")
    settings = load_settings(path, require_credentials=False)
    _print_warnings(settings.warnings)
    flag = f"--config {path}" if args.config is not None else f"--profile {path.stem}"
    print("Next:")
    print(f"  usajobs-monitor check-config {flag}          # confirm credentials and see the pay floors")
    print(f"  usajobs-monitor run --preview-delivery {flag}  # search now; alerts are written, not sent")
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    """Fit notes for one posting, printed as Markdown."""
    from .fit import SYSTEM_PROMPT, FitError, build_prompt, fit_note, make_provider, render_note
    from .resume_text import ResumeError, find_resumes, read_resumes, resume_name
    from .tools import review_posting

    settings = load_settings(args.config)
    _print_warnings(settings.warnings)
    ai = settings.config["ai"]
    try:
        resume_paths = find_resumes(args.resume or settings.ai_resume, settings.resume_folder)
        resume = read_resumes(resume_paths)
        result, tier = review_posting(settings, args.control_number)
    except (ResumeError, ValueError) as exc:  # includes tools.ToolInputError
        print(f"fit: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"fit: {exc}", file=sys.stderr)
        return 1
    label = tier.replace("_", " ")
    if args.preview:
        print(f"Nothing was sent. This is what ai.provider = \"{ai['provider']}\" would receive "
              f"for {', '.join(path.name for path in resume_paths)}:\n")
        print(f"--- system ---\n{SYSTEM_PROMPT}\n\n--- user ---\n{build_prompt(result, resume, label)}")
        return 0
    if ai["send_resume"] is not True:
        print("fit: your resume is only sent to the AI provider when the config sets ai.send_resume = true. "
              "Run with --preview to see exactly what would be sent.", file=sys.stderr)
        return 2
    try:
        names = [resume_name(path) for path in resume_paths] if len(resume_paths) > 1 else []
        note = fit_note(result, resume, make_provider(ai), label, names)
    except FitError as exc:
        print(f"fit: {exc}", file=sys.stderr)
        return 1
    job = result.job
    print(f"# {job.get('PositionTitle', '')}, {job.get('OrganizationName', '')}\n")
    print(f"{label.capitalize()} · control number {args.control_number.strip()} · {job.get('PositionURI', '')}\n")
    print(render_note(note))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    try:
        serve(args.config)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


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
    ai = load_settings(config_path, require_credentials=False).config["ai"]
    if ai["provider"] == "anthropic" and (ai["fit_notes"] or ai["send_resume"]):
        names.append((ai["api_key_env"], False))
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


def cmd_pay_table(args: argparse.Namespace) -> int:
    from .paytables import MAX_GRADE, STEPS, available_years, load_table

    try:
        table = load_table(args.year or available_years()[-1])
        code = table.find_locality(args.locality) if args.locality else None
    except LookupError as exc:
        print(f"pay-table: {exc}", file=sys.stderr)
        return 2
    if code is None:
        print(f"OPM locality pay areas, {table.year} (use the code or name as target.locality):")
        for area in sorted(table.localities.values(), key=lambda a: a.name):
            print(f"  {area.code:<4} {area.percent:>6}%  {area.name}")
    else:
        area = table.localities[code]
        print(f"{table.year} GS pay, {area.name} ({code}, locality {area.percent}%), "
              f"limited to ${table.pay_cap:,}")
        print("Grade " + "".join(f"{'Step ' + str(step):>10}" for step in range(1, STEPS + 1)))
        for grade in range(1, MAX_GRADE + 1):
            print(f"GS-{grade:<3}" + "".join(f"{table.rate(grade, step, code):>10,}" for step in range(1, STEPS + 1)))
    print("Sources: " + "; ".join(table.sources))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usajobs-monitor", description="Scheduled, filtered USAJOBS search.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def config_options(p: argparse.ArgumentParser) -> None:
        which = p.add_mutually_exclusive_group()
        which.add_argument("--config", type=Path, default=None,
                           help=f"config file, TOML or JSON (default: ./{DEFAULT_CONFIG_NAME}, "
                                "or your only saved profile)")
        which.add_argument("--profile", help="a saved profile's name (see usajobs-monitor profiles)")

    def add(name: str, help_text: str, func) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        config_options(p)
        p.set_defaults(func=func)
        return p

    init = sub.add_parser("init", help="Answer a few questions and write a search profile.",
                          description="Answer a few questions and write a search profile. Without --config the "
                                      "profile is saved in the profiles folder (see usajobs-monitor profiles).")
    init_to = init.add_mutually_exclusive_group()
    init_to.add_argument("--profile", help="name for the new profile (asked if left out)")
    init_to.add_argument("--config", type=Path, default=None, help="write the profile to this file instead")
    init.add_argument("--force", action="store_true", help="replace an existing profile")
    init.set_defaults(func=cmd_init, resolve_config=False)

    run = add("run", "Search, validate the reports in an isolated run folder, publish them, then deliver them.",
              cmd_run)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--preview-delivery", action="store_true",
                      help="write the messages into the run folder instead of sending them")
    mode.add_argument("--no-deliver", action="store_true", help="skip delivery for this run")
    run.add_argument("--all", action="store_true", help="run every saved profile, one after another")
    deliver = add("deliver", "Send (or preview) an earlier run again. Channels that already accepted it are skipped.",
                  cmd_deliver)
    deliver.add_argument("run_id", help='a run folder name from reports/runs, or "latest"')
    deliver.add_argument("--preview", action="store_true", help="write the messages instead of sending them")
    search = add("search", "Search and write reports directly, without run isolation.", cmd_search)
    search.add_argument("--output-dir", type=Path, help="write reports here instead of paths.reports_dir")
    add("check-config", "Validate the config and report what a run would do. Never prints credentials.", cmd_check_config)
    decide = add("decide", "Mark a job applied, watching or not interested (hidden from later runs), with an "
                           "optional note. With no arguments, list your decisions.", cmd_decide)
    decide.add_argument("control_number", nargs="?", help="the number in the job's USAJOBS link")
    decide.add_argument("status", nargs="?", help="applied, watching, not_interested, or clear to remove it")
    decide.add_argument("note", nargs="*", help="optional note, e.g. \"applied 10/17, referred\"")
    fit = add("fit", "AI fit notes comparing your resume with one posting (needs the ai extra and "
                     "ai.send_resume = true; --preview shows what would be sent, without sending).", cmd_fit)
    fit.add_argument("control_number", help="the number in the job's USAJOBS link")
    fit.add_argument("--resume", type=Path, help="this resume instead of ai.resume")
    fit.add_argument("--preview", action="store_true", help="print the prompt instead of sending it")
    add("mcp", "Serve search_jobs, screen_eligibility and explain_match to an MCP client over standard "
               "input/output (needs the mcp extra).", cmd_mcp)
    profiles = sub.add_parser("profiles", help="List saved profiles.", description="List saved profiles.")
    profiles.set_defaults(func=cmd_profiles)
    credentials = sub.add_parser(
        "credentials", help="Store or check credentials in the OS keychain. Never prints a secret.",
        description="Store or check credentials in the OS keychain (macOS Keychain, Windows Credential "
                    "Manager, Linux Secret Service). Needs the keyring extra. Environment variables win "
                    "over the keychain. Never prints a secret.")
    credentials.set_defaults(func=cmd_credentials)
    actions = credentials.add_subparsers(dest="action", required=True)
    status = actions.add_parser("status", help="show where each credential comes from, without values")
    config_options(status)
    store = actions.add_parser("set", help="save a secret; prompts without echoing")
    store.add_argument("name", help="the variable name, for example USAJOBS_API_KEY")
    store.add_argument("--stdin", action="store_true", help="read the value from the first line of standard input")
    remove = actions.add_parser("delete", help="remove a saved secret")
    remove.add_argument("name", help="the variable name, for example USAJOBS_API_KEY")
    pay = sub.add_parser("pay-table", help="List OPM locality areas, or show one area's GS pay table.",
                         description="List OPM locality areas, or show one area's GS pay table.")
    pay.add_argument("locality", nargs="?", help='an OPM locality code or name, e.g. "DCB" or "Seattle"')
    pay.add_argument("--year", type=int, help="pay table year (default: the newest bundled)")
    pay.set_defaults(func=cmd_pay_table)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if hasattr(args, "profile") and getattr(args, "resolve_config", True):
            from . import profiles
            try:
                args.config = profiles.resolve(args.config, args.profile, Path(DEFAULT_CONFIG_NAME))
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
        return args.func(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
