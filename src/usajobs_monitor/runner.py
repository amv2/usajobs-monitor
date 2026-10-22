"""Run a search into an isolated folder, validate it, then publish it.

A failed run never publishes, never overwrites the last good reports, and
never puts raw errors or tracebacks into anything meant for delivery.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from . import api
from .config import Settings
from .jobs import job_id
from .locking import exclusive_lock
from .reports import (
    CSV_NAME,
    EXCEL_NAME,
    FORMAT_FILES,
    HTML_NAME,
    REPORT_NAMES,
    SUMMARY_NAME,
    WORD_NAME,
    report_files,
    resume_pdfs,
    summary_entry,
    write_csv,
    write_excel,
    write_html,
    write_word,
)
from .review import review_job, target_agency_hit
from .tracking import (HISTORY_NAME, UNREADABLE_SUFFIX, RunView, build_view, incomplete_text, load_decisions,
                       read_history, watch_list_label)

SUMMARY = SUMMARY_NAME
HISTORY = HISTORY_NAME
LOCK_NAME = ".usajobs-run.lock"
DELIVERY_LAST_RUN = "usajobs_delivery_last_run.json"
FAILURE_MESSAGE = (
    "The USAJOBS search or report validation did not complete successfully. "
    "Review the local run and verify USAJOBS credentials, network access, and report write access. "
    "No previous reports are attached."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".usajobs-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())


@contextmanager
def run_lock(reports: Path) -> Iterator[None]:
    with exclusive_lock(reports / LOCK_NAME, "Another usajobs-monitor run is active."):
        yield


def new_run_dir(reports: Path, preview: bool = False) -> Path:
    root = reports / ("previews" if preview else "runs")
    root.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
    result = root / run_id
    result.mkdir(mode=0o700)
    return result


def run_search(settings: Settings, output_dir: Path, *, today: date | None = None) -> dict[str, Any]:
    """Search, review, and write reports, summary and new history into ``output_dir``.

    Reads the last published history and the decisions file but writes nothing
    outside ``output_dir``. Raises on any failure; callers decide what to record.
    """
    config = settings.config
    started_at = datetime.now().astimezone()
    output_dir.mkdir(parents=True, exist_ok=True)
    resumes = resume_pdfs(settings.resume_folder)
    failed_searches: list[str] = []
    jobs, calls, search_warnings = api.search_all_jobs(config, failures=failed_searches)
    results = [review_job(job, config) for job in jobs]
    decisions, decision_warnings = load_decisions(settings.decisions_path)
    history, history_state = read_history(settings.history_path)
    view = build_view(results, config, history, decisions, today or date.today(),
                      started_at.isoformat(timespec="seconds"), failed_searches)
    view.history_state = history_state
    if history_state == "unreadable":
        decision_warnings.append(f"Job history: {settings.history_path.name} could not be read. This run is a new "
                                 f"baseline; the file is kept as {settings.history_path.name}{UNREADABLE_SUFFIX} "
                                 "when this run publishes.")
    # Fit notes come before the reports so their levels can show in them and, with
    # ai.rank, reorder each list.
    fit_notes = {"status": "off"}
    if (config.get("ai") or {}).get("fit_notes"):
        from .fit import run_fit_notes  # the optional AI add-on; never fails the search
        fit_notes = run_fit_notes(settings, view, output_dir, today or date.today())
        if config["ai"].get("rank") and view.fits:
            view.rank_by_fit()
            fit_notes["ranked"] = True
    fit_warnings = [f"Fit notes: {fit_notes['message']}"] if fit_notes.get("message") else []
    files = report_files(config)
    word, excel = output_dir / WORD_NAME, output_dir / EXCEL_NAME
    if WORD_NAME in files:
        write_word(word, results, config, resumes, calls, view, settings.decisions_path.name)
    if EXCEL_NAME in files:
        write_excel(excel, results, config, resumes, calls, view, settings.decisions_path)
    if HTML_NAME in files:
        write_html(output_dir / HTML_NAME, results, config, calls, view, settings.decisions_path.name)
    if CSV_NAME in files:
        write_csv(output_dir / CSV_NAME, results, view)
    (output_dir / HISTORY).write_text(json.dumps(view.history, indent=2, sort_keys=True), encoding="utf-8")

    target_agencies = config["search"].get("target_agencies", [])
    summary = {
        "status": "ok",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "report_title": config.get("report", {}).get("title", ""),
        "jobs_reviewed": len(results),
        "usajobs_searches": calls,
        "matches_after_final_filters": len(view.matches),
        "target_agency_matches": sum(target_agency_hit(r.job, target_agencies) for r in view.matches),
        "near_misses_reported": len(view.near),
        "watch_list_count": len(view.watch),
        "watch_list_label": watch_list_label(config),
        "search_warnings": ([incomplete_text(failed_searches)] if failed_searches else [])
                           + search_warnings + decision_warnings + fit_warnings,
        "incomplete": bool(failed_searches),
        "failed_searches": failed_searches,
        "fit_notes": fit_notes,
        "history_baseline": view.baseline,
        "history_status": history_state,
        "new_since_last_run": {
            "matches": view.new_count(view.matches),
            "watch_list": view.new_count(view.watch),
            "near_misses": view.new_count(view.near),
        },
        "closing_soon": view.closing_soon_count(),
        "closing_soon_days": view.closing_soon_days,
        "closing_soon_jobs": closing_soon_entries(view),
        "dropped_since_last_run": view.dropped,
        "hidden_not_interested": len(view.hidden),
        "matches": [summary_entry(r, view) for r in view.matches],
        "watch_list": [summary_entry(r, view, with_reasons=True) for r in view.watch],
        "top_near_misses": [summary_entry(r, view, with_reasons=True) for r in view.near[:5]],
        "resume_count": len(resumes),
        "report_files": files,
    }
    _add_report_paths(summary, output_dir)
    (output_dir / SUMMARY).write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def closing_soon_entries(view: RunView) -> list[dict]:
    """Every listed posting that closes within the window, soonest first. Delivery
    leads with these, so a deadline is not hidden by the per-section limit."""
    return [{**summary_entry(r, view), "list": name, "days_left": view.closing_days[job_id(r.job)]}
            for r, name in view.closing_soon()]


def _add_report_paths(summary: dict, folder: Path) -> None:
    """Point ``word_briefing`` / ``excel_tracker`` (and their newer siblings) at ``folder``."""
    for key, name in (("word_briefing", WORD_NAME), ("excel_tracker", EXCEL_NAME),
                      ("html_digest", HTML_NAME), ("csv_export", CSV_NAME)):
        if name in summary_report_files(summary):
            summary[key] = str(folder / name)
        else:
            summary.pop(key, None)


def summary_report_files(summary: dict) -> list[str]:
    """The reports a run wrote. Summaries from before report.formats list none: they had the Office pair."""
    files = summary.get("report_files", list(REPORT_NAMES))
    if (not isinstance(files, list) or not files or len(set(files)) != len(files)
            or any(name not in FORMAT_FILES.values() for name in files)):
        raise ValueError("The run's report list is invalid.")
    return files


ZIP_MEMBERS = {WORD_NAME: "word/document.xml", EXCEL_NAME: "xl/workbook.xml"}


def validate_report_file(path: Path) -> None:
    """Raise ``ValueError`` unless ``path`` is a readable report of its kind."""
    if path.name in ZIP_MEMBERS:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Report ZIP integrity check failed.")
            ET.fromstring(archive.read("[Content_Types].xml"))
            ET.fromstring(archive.read(ZIP_MEMBERS[path.name]))
    elif path.name == HTML_NAME:
        if not path.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html>"):
            raise ValueError("The HTML digest is not an HTML document.")
    elif path.name == CSV_NAME:
        header = next(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))), [])
        if header[:2] != ["List", "Score"]:
            raise ValueError("The CSV export has an unexpected header.")
    else:
        raise ValueError("Unknown report file.")


def validate_reports(run_dir: Path, started_at: datetime | None = None) -> dict:
    summary_path = run_dir / SUMMARY
    if summary_path.is_symlink():
        raise ValueError("Summary must be a regular file.")
    summary = json.loads(summary_path.read_text())
    if not isinstance(summary, dict) or summary.get("status") != "ok":
        raise ValueError("Search did not report success.")
    try:
        start = datetime.fromisoformat(summary["started_at"]).astimezone()
        finish = datetime.fromisoformat(summary["finished_at"]).astimezone()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Run timestamps are missing or invalid.") from None
    for key in ("jobs_reviewed", "usajobs_searches", "matches_after_final_filters", "target_agency_matches"):
        if type(summary.get(key)) is not int or summary[key] < 0:
            raise ValueError("Run counts are missing or invalid.")
    if not isinstance(summary.get("matches"), list) or any(not isinstance(m, dict) for m in summary["matches"]):
        raise ValueError("Run matches are invalid.")
    if len(summary["matches"]) != summary["matches_after_final_filters"]:
        raise ValueError("Match count does not agree with report summary.")
    now = datetime.now(timezone.utc)
    if finish < start or finish.timestamp() > now.timestamp() + 2:
        raise ValueError("Invalid run timestamps.")
    if started_at is not None and start.timestamp() < started_at.timestamp() - 2:
        raise ValueError("Summary belongs to an earlier run.")
    for name in summary_report_files(summary):
        p = run_dir / name
        if p.is_symlink() or not p.is_file() or not p.stat().st_size:
            raise ValueError("A report is missing or is not a regular file.")
        if started_at is not None and p.stat().st_mtime < started_at.timestamp() - 2:
            raise ValueError("A report belongs to an earlier run.")
        validate_report_file(p)
    summary["started_at"] = start.isoformat(timespec="seconds")
    summary["finished_at"] = finish.isoformat(timespec="seconds")
    return summary


def prepare_manifest(run_dir: Path, summary: dict, *, failure: bool = False, preview: bool = False) -> dict:
    summary["run_id"] = run_dir.name
    files = [] if failure else summary_report_files(summary)
    if not failure:
        _add_report_paths(summary, run_dir)
    write_json(run_dir / SUMMARY, summary)
    manifest = {
        "version": 1, "run_id": run_dir.name, "status": summary["status"],
        "finished_at": summary["finished_at"], "summary_sha256": sha256(run_dir / SUMMARY),
        "attachments": [{"name": n, "sha256": sha256(run_dir / n)} for n in files],
        "failure_message": FAILURE_MESSAGE if failure else "", "preview_only": preview,
    }
    write_json(run_dir / "manifest.json", manifest)
    return manifest


def publish_latest(run_dir: Path, reports: Path, summary: dict) -> None:
    # Publish JSON last, so its run_id marks a complete set of copied reports.
    for name in summary_report_files(summary):
        atomic_write(reports / name, (run_dir / name).read_bytes())
    # Job history drives NEW/dropped flags; it advances only with a validated run.
    history = run_dir / HISTORY
    if history.is_file() and not history.is_symlink():
        published = reports / HISTORY
        if summary.get("history_status") == "unreadable" and published.is_file() and not published.is_symlink():
            atomic_write(reports / (HISTORY + UNREADABLE_SUFFIX), published.read_bytes())
        atomic_write(published, history.read_bytes())
    # Fit notes (optional AI add-on) and their cache advance the same way.
    from .fit import FIT_CACHE_NAME, FIT_NOTES_NAME
    for name in (FIT_NOTES_NAME, FIT_CACHE_NAME):
        source = run_dir / name
        if source.is_file() and not source.is_symlink():
            atomic_write(reports / name, source.read_bytes())
    latest = dict(summary)
    _add_report_paths(latest, reports)
    write_json(reports / SUMMARY, latest)


def execute_search(settings: Settings) -> tuple[Path, bool]:
    reports = settings.reports_dir
    run_dir = new_run_dir(reports)
    started = datetime.now(timezone.utc)
    try:
        run_search(settings, run_dir)
        summary = validate_reports(run_dir, started)
        prepare_manifest(run_dir, summary)
        publish_latest(run_dir, reports, summary)
        return run_dir, True
    except Exception:  # noqa: BLE001
        # Raw API responses and tracebacks may contain sensitive data. The run
        # records a fixed failure message, never the exception text.
        summary = {
            "status": "error", "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": FAILURE_MESSAGE,
        }
        prepare_manifest(run_dir, summary, failure=True)
        write_json(reports / SUMMARY, summary)
        return run_dir, False


def preview_current(reports: Path) -> Path:
    # Copies only allowlisted outputs and cannot be selected by select_run.
    latest = reports / SUMMARY
    if latest.is_symlink():
        raise ValueError("Preview source must be a regular file.")
    files = summary_report_files(json.loads(latest.read_text()))
    run_dir = new_run_dir(reports, preview=True)
    for name in (*files, SUMMARY):
        source = reports / name
        if source.is_symlink():
            raise ValueError("Preview source must be a regular file.")
        shutil.copyfile(source, run_dir / name)
    summary = validate_reports(run_dir)
    prepare_manifest(run_dir, summary, preview=True)
    return run_dir


def select_run(reports: Path, run_id: str) -> Path:
    if not re.fullmatch(r"\d{8}T\d{6}Z-[a-f0-9]{12}", run_id):
        raise ValueError("Invalid run identifier.")
    root = (reports / "runs").resolve()
    candidate = root / run_id
    if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent != root:
        raise ValueError("Unknown run identifier.")
    return candidate


Deliver = Callable[[Path], dict[str, Any]]
DELIVERED_OK = ("disabled", "preview", "accepted", "already_accepted")


def run(settings: Settings, deliver: Deliver | None = None, *, deliver_run: str | None = None) -> tuple[int, dict]:
    """Full run under the lock. Returns ``(exit_code, outcome)``.

    ``deliver`` sends or previews a run (see ``delivery.deliver``); without one,
    delivery is "disabled".
    Exit codes: 0 ok, 1 search failed, 2 delivery not confirmed.
    """
    reports = settings.reports_dir
    with run_lock(reports):
        if deliver_run:
            run_dir = select_run(reports, deliver_run)
            success = json.loads((run_dir / "manifest.json").read_text())["status"] == "ok"
        else:
            run_dir, success = execute_search(settings)
        channels: dict[str, Any] = {}
        if deliver is None:
            delivery_status, delivery_message = "disabled", ""
        else:
            try:
                delivery = deliver(run_dir)
                delivery_status = delivery.get("status", "unknown")
                delivery_message = delivery.get("message", "")
                channels = delivery.get("channels", {})
            except Exception:  # noqa: BLE001
                # Delivery may have reached the server before failing. Preserve
                # search success and require review before another send.
                delivery_status = "unknown"
                delivery_message = "Delivery status could not be confirmed. Review this run's delivery record before retrying."
        outcome = {"run_id": run_dir.name, "search_status": "ok" if success else "error",
                   "delivery_status": delivery_status, "delivery_message": delivery_message,
                   "delivery_channels": channels,
                   "run_directory": str(run_dir)}
        write_json(run_dir / "outcome.json", outcome)
        update_latest = not deliver_run
        if deliver_run:
            try:
                update_latest = (delivery_status != "preview"
                                 and json.loads((reports / SUMMARY).read_text()).get("run_id") == run_dir.name)
            except (OSError, ValueError, AttributeError):
                update_latest = False
        if update_latest:
            write_json(reports / DELIVERY_LAST_RUN, outcome)
    if not success:
        return 1, outcome
    return (0 if delivery_status in DELIVERED_OK else 2), outcome
