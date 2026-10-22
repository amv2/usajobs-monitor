"""Tiers (matches, watch list, near misses), decisions, and run-to-run history."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import CLOSING_SOON_DAYS
from .jobs import GS_SCALE_PAY_PLANS, close_date, grade_numbers, job_id, job_pay_plans, job_url, salary_upper
from .reasons import Code, code_of, is_hard_no
from .review import VERIFY_ELIGIBILITY_FLAG, ReviewResult, needs_eligibility_check

HISTORY_NAME = "usajobs_job_history.json"
DECISION_STATUSES = {"applied", "interview", "offer", "rejected", "withdrawn", "not_interested", "watching"}
# Postings with these statuses are hidden from later runs: you passed on them, or the application is over.
HIDDEN_STATUSES = {"not_interested", "rejected", "withdrawn"}
# Applications still in progress: flagged on every run and listed by ``decide`` with days waiting.
ACTIVE_STATUSES = {"applied", "interview", "offer"}
STALE_DAYS = 30
HISTORY_KEEP_DAYS = 45
NEAR_MISS_LIMIT = 15
FIT_LABELS = {"strong": "Strong", "possible": "Possible", "stretch": "Stretch", "unlikely": "Unlikely"}
# Ranking order: postings the AI did not rate sit between "possible" and "stretch",
# so a missing note neither lifts a posting nor buries it.
FIT_RANK = {"strong": 0, "possible": 1, "": 2, "stretch": 3, "unlikely": 4}


def near_misses(results: list[ReviewResult], limit: int = NEAR_MISS_LIMIT) -> list[ReviewResult]:
    # Failures that can never become a fit are not near misses.
    excluded = [r for r in results if not r.kept and not any(is_hard_no(reason) for reason in r.reasons)]
    candidates = [r for r in excluded if r.score >= 5 or len(r.reasons) == 1]
    # Closest calls first: fewest failed checks, then strongest relevance score.
    return sorted(
        candidates,
        key=lambda r: (len(r.reasons), -r.score, str(r.job.get("PositionTitle", ""))),
    )[:limit]


def near_miss_reason_text(result: ReviewResult) -> str:
    if len(result.reasons) == 1:
        return f"Only failed check: {result.reasons[0]}"
    return f"{len(result.reasons)} failed checks: " + "; ".join(result.reasons)


def clears_near_miss_floor(job: dict[str, Any], pay_floor: float, min_gs_grade: int | None) -> bool:
    """GS-scale postings must reach ``min_gs_grade`` when it is set (special rates can
    push a lower grade past the pay floor); other pay systems are judged by upper pay alone. Unposted pay passes: the posting
    is already excluded for it, and a near miss is where a reader can check it."""
    if min_gs_grade and {plan.upper() for plan in job_pay_plans(job)} & GS_SCALE_PAY_PLANS:
        numbers = grade_numbers(job)
        if numbers and max(numbers) < int(min_gs_grade):
            return False
    upper = salary_upper(job)
    return pay_floor <= 0 or upper is None or upper > pay_floor


def watch_list_label(config: dict[str, Any]) -> str:
    """'GS/GG-15 watch list' from filters.watch_list, or a plain 'Watch list'."""
    watch = (config.get("filters") or {}).get("watch_list") or {}
    plans = [str(p).upper() for p in watch.get("pay_plans", [])]
    grades = [str(g).strip() for g in watch.get("grades", [])]
    if plans and grades:
        return f"{'/'.join(plans)}-{'/'.join(grades)} watch list"
    return "Watch list"


def is_watch_candidate(result: ReviewResult, config: dict[str, Any]) -> bool:
    """Relevant postings at a watch-list pay plan and grade that failed only the salary rule."""
    watch = config["filters"].get("watch_list") or {}
    pay_plans = {str(plan).upper() for plan in watch.get("pay_plans", [])}
    grades = {int(grade) for grade in watch.get("grades", [])}
    if result.kept or len(result.reasons) != 1 or code_of(result.reasons[0]) is not Code.SALARY:
        return False
    if not pay_plans & {plan.upper() for plan in job_pay_plans(result.job)}:
        return False
    numbers = grade_numbers(result.job)
    return bool(numbers) and any(min(numbers) <= grade <= max(numbers) for grade in grades)


def _iso_date(value: Any) -> str:
    """A ``YYYY-MM-DD`` date from a hand-edited cell, or "" when it isn't one."""
    text = str(value or "").strip().partition("T")[0]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _applied_on(row: dict[str, Any]) -> str:
    """The date you applied. Files written before ``applied_on`` existed only have
    ``decided_on``, which for a row still marked applied is the day you applied."""
    applied = _iso_date(row.get("applied_on"))
    if not applied and str(row.get("status") or "").strip().lower() == "applied":
        applied = _iso_date(row.get("decided_on"))
    return applied


def load_decisions(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Read your decisions (applied, interview, not interested, ...), keyed by control number."""
    decisions: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    if not path.is_file():
        return decisions, warnings
    with path.open(newline="", encoding="utf-8-sig") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            number = str(row.get("control_number") or "").strip()
            status = re.sub(r"[\s-]+", "_", str(row.get("status") or "").strip().lower())
            if not number and not status:
                continue
            if not number.isdigit() or status not in DECISION_STATUSES:
                warnings.append(f"{path.name} line {line}: needs a numeric control_number and a status of "
                                + ", ".join(sorted(DECISION_STATUSES)))
                continue
            decisions[number] = {"status": status, "note": str(row.get("note") or "").strip(),
                                 "title": str(row.get("title") or "").strip(),
                                 "agency": str(row.get("agency") or "").strip(),
                                 "applied_on": _applied_on(row), "decided_on": _iso_date(row.get("decided_on"))}
    return decisions, warnings


DECISION_ALIASES = {"not_interested": "not_interested", "not": "not_interested", "no": "not_interested",
                    "ni": "not_interested", "hide": "not_interested", "watch": "watching", "watching": "watching",
                    "applied": "applied", "apply": "applied",
                    "interview": "interview", "interviewing": "interview", "interviewed": "interview",
                    "offer": "offer", "offered": "offer",
                    "rejected": "rejected", "reject": "rejected", "not_referred": "rejected",
                    "not_selected": "rejected",
                    "withdrawn": "withdrawn", "withdraw": "withdrawn", "withdrew": "withdrawn",
                    "clear": "clear", "remove": "clear", "undo": "clear"}
STATUS_HELP = "applied, interview, offer, rejected, withdrawn, watching, not_interested or clear"
DECISION_COLUMNS = ["control_number", "status", "note", "title", "agency", "applied_on", "decided_on"]


def normalize_status(value: str) -> str:
    """'not interested', 'not-interested' and 'NI' all mean not_interested; 'clear' removes a decision."""
    key = re.sub(r"[\s-]+", "_", value.strip().lower())
    if key not in DECISION_ALIASES:
        raise ValueError(f"unknown status \"{value}\"; use {STATUS_HELP}")
    return DECISION_ALIASES[key]


def save_decision(path: Path, number: str, status: str, note: str = "", title: str = "",
                  today: date | None = None, agency: str = "", applied_on: date | None = None) -> dict[str, str] | None:
    """Add, replace or (with status "clear") remove one decision row, keeping the
    file's other rows and columns. Returns the row it replaced.

    ``decided_on`` is the day of the latest change. ``applied_on`` is set the day a job
    is marked applied and carried over through later changes (interview, rejected, ...).
    Pass ``applied_on`` for an application made before you started tracking it. With no
    new note, the previous note is kept."""
    number = number.strip()
    if not number.isdigit():
        raise ValueError("the control number is the number in the job's USAJOBS link, e.g. 812345600")
    status = normalize_status(status)
    rows: list[dict[str, str]] = []
    columns = list(DECISION_COLUMNS)
    if path.is_file():
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = list(reader.fieldnames or columns)
            rows = [dict(row) for row in reader]
        for required in DECISION_COLUMNS:
            if required not in columns:
                columns.append(required)
    previous = None
    kept = []
    for row in rows:
        if str(row.get("control_number") or "").strip() == number:
            previous = row
        else:
            kept.append(row)
    if status != "clear":
        today = today or date.today()
        applied = applied_on.isoformat() if applied_on else _applied_on(previous or {})
        if not applied and status == "applied":
            applied = today.isoformat()
        row = {"control_number": number, "status": status,
               "note": note.strip() or str((previous or {}).get("note") or ""),
               "title": title or str((previous or {}).get("title") or ""),
               "agency": agency or str((previous or {}).get("agency") or ""),
               "applied_on": applied, "decided_on": today.isoformat()}
        kept.append({key: value for key, value in row.items() if key in columns})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(kept)
    tmp.replace(path)
    return previous


UNREADABLE_SUFFIX = ".unreadable"


def read_history(path: Path) -> tuple[dict[str, Any] | None, str]:
    """``(history, state)`` where state is "found", "missing" or "unreadable".

    A missing file is a normal first run (or an evicted Actions cache). An unreadable
    one is not, so runs say so instead of quietly starting over."""
    if not path.exists():
        return None, "missing"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(history, dict) or not isinstance(history.get("jobs"), dict):
        return None, "unreadable"
    return history, "found"


def load_history(path: Path) -> dict[str, Any] | None:
    return read_history(path)[0]


def baseline_text(state: str, history_name: str = HISTORY_NAME) -> str:
    """Why a run is a baseline, for reports and the digest."""
    if state == "unreadable":
        return (f"the earlier history file couldn't be read, so this run starts over and nothing is flagged NEW "
                f"(the unreadable copy is kept as {history_name}{UNREADABLE_SUFFIX})")
    return "there is no earlier history yet, so nothing is flagged NEW"


def incomplete_text(failed: list[str]) -> str:
    """Why an incomplete run's lists may be short."""
    shown = ", ".join(failed[:5]) + (f" and {len(failed) - 5} more" if len(failed) > 5 else "")
    return (f"Incomplete run: {len(failed)} {'search' if len(failed) == 1 else 'searches'} failed after retries "
            f"({shown}). Postings only those searches find are missing from this run, and nothing is "
            "reported as dropped. The next run searches them again.")


@dataclass
class RunView:
    matches: list[ReviewResult]
    watch: list[ReviewResult]
    near: list[ReviewResult]
    hidden: list[ReviewResult]
    dropped: list[dict[str, Any]]
    flags: dict[str, list[str]]
    decisions: dict[str, dict[str, str]]
    baseline: bool
    history: dict[str, Any]
    history_state: str = "missing"
    # AI fit level by control number, filled in by the optional fit notes.
    fits: dict[str, str] = field(default_factory=dict)
    # With several resume versions, the one the AI judged each posting by.
    fit_resumes: dict[str, str] = field(default_factory=dict)
    # The AI's two- or three-sentence summary, for the Word and HTML reports.
    fit_summaries: dict[str, str] = field(default_factory=dict)
    # Requirements the resume does not show or shows unclearly, as one line.
    fit_gaps: dict[str, str] = field(default_factory=dict)
    # The first requirement the resume does not show (or shows unclearly), for short messages.
    fit_main_gaps: dict[str, str] = field(default_factory=dict)
    # The AI's own level when its requirement checklist lowered it.
    fit_lowered: dict[str, str] = field(default_factory=dict)
    # Days until each CLOSING SOON posting closes (0 = closes today).
    closing_days: dict[str, int] = field(default_factory=dict)
    closing_soon_days: int = CLOSING_SOON_DAYS
    # Searches that failed after their retries; non-empty means an incomplete run.
    failed_searches: list[str] = field(default_factory=list)

    def fit_text(self, result: ReviewResult) -> str:
        number = job_id(result.job)
        label = FIT_LABELS.get(self.fits.get(number, ""), "")
        if not label:
            return ""
        notes = []
        if self.fit_lowered.get(number):
            notes.append(f"AI said {FIT_LABELS.get(self.fit_lowered[number], self.fit_lowered[number])}")
        if self.fit_resumes.get(number):
            notes.append(f"resume: {self.fit_resumes[number]}")
        return f"{label} ({'; '.join(notes)})" if notes else label

    def fit_gap_text(self, result: ReviewResult) -> str:
        return self.fit_gaps.get(job_id(result.job), "")

    def rank_by_fit(self) -> None:
        """Sort each list by AI fit level, keeping the current order within a level.
        Nothing is removed: low fits move to the bottom of their list."""
        def key(result: ReviewResult) -> int:
            return FIT_RANK.get(self.fits.get(job_id(result.job), ""), FIT_RANK[""])

        self.matches = sorted(self.matches, key=key)
        self.watch = sorted(self.watch, key=key)
        self.near = sorted(self.near, key=key)

    def flag_text(self, result: ReviewResult) -> str:
        return ", ".join(self.flags.get(job_id(result.job), []))

    def your_status(self, result: ReviewResult) -> str:
        decision = self.decisions.get(job_id(result.job))
        if not decision:
            return ""
        label = decision["status"].replace("_", " ")
        details = [decision["note"]] if decision["note"] else []
        if decision["status"] in ACTIVE_STATUSES and decision.get("applied_on"):
            details.append(f"applied {decision['applied_on']}")
        return f"{label} ({'; '.join(details)})" if details else label

    def new_count(self, items: list[ReviewResult]) -> int:
        return sum("NEW" in self.flags.get(job_id(r.job), []) for r in items)

    def closing_soon(self) -> list[tuple[ReviewResult, str]]:
        """Listed postings flagged CLOSING SOON with the list each is on, soonest first."""
        seen: set[str] = set()
        found = []
        for name, items in (("match", self.matches), ("watch", self.watch), ("near_miss", self.near)):
            for r in items:
                number = job_id(r.job)
                if number in self.closing_days and number not in seen:
                    seen.add(number)
                    found.append((r, name))
        return sorted(found, key=lambda pair: self.closing_days[job_id(pair[0].job)])

    def closing_soon_count(self) -> int:
        return len(self.closing_soon())


def build_view(results: list[ReviewResult], config: dict[str, Any], history: dict[str, Any] | None,
               decisions: dict[str, dict[str, str]], today: date, run_marker: str,
               failed_searches: list[str] | None = None) -> RunView:
    """Sort a run's results into lists and compare them with the last run's history.

    ``failed_searches`` marks an incomplete run: postings those searches would have
    found are missing, so nothing is reported dropped, and the last run's postings
    carry its marker forward so the next full run can still report them dropped.
    """
    partial = bool(failed_searches)
    hidden_ids = {number for number, d in decisions.items() if d["status"] in HIDDEN_STATUSES}
    hidden = [r for r in results if job_id(r.job) in hidden_ids]
    visible = [r for r in results if job_id(r.job) not in hidden_ids]
    matches = sorted((r for r in visible if r.kept), key=lambda r: r.score, reverse=True)
    watch = sorted((r for r in visible if is_watch_candidate(r, config)), key=lambda r: -r.score)
    watch_ids = {id(r) for r in watch}
    filters = config["filters"]
    near_floor = float(filters.get("near_miss_upper_salary_must_exceed") or 0)
    min_grade = filters.get("near_miss_min_gs_grade")
    near = near_misses([r for r in visible if id(r) not in watch_ids
                        and clears_near_miss_floor(r.job, near_floor, min_grade)])

    lists: dict[str, str] = {}
    for name, items in (("match", matches), ("watch", watch), ("near_miss", near)):
        for r in items:
            lists.setdefault(job_id(r.job), name)

    previous = (history or {}).get("jobs", {})
    baseline = history is None
    window = (config.get("report") or {}).get("closing_soon_days") or CLOSING_SOON_DAYS
    flags: dict[str, list[str]] = {}
    closing_days: dict[str, int] = {}
    for r in results:
        number = job_id(r.job)
        tags = []
        if not baseline and number not in previous:
            tags.append("NEW")
        if needs_eligibility_check(r):
            tags.append(VERIFY_ELIGIBILITY_FLAG)
        closes = close_date(r.job)
        decision = decisions.get(number)
        # A posting with an application in progress needs no deadline reminder.
        if (closes is not None and 0 <= (closes - today).days <= window
                and not (decision and decision["status"] in ACTIVE_STATUSES)):
            tags.append("CLOSING SOON")
            closing_days[number] = (closes - today).days
        if decision and decision["status"] not in HIDDEN_STATUSES:
            tags.append(decision["status"].upper())
        flags[number] = tags

    current_ids = {job_id(r.job) for r in results}
    previous_marker = (history or {}).get("run_marker")
    dropped = []
    for number, entry in previous.items():
        if (not partial and number not in current_ids and entry.get("list") in ("match", "watch", "near_miss")
                and entry.get("last_seen_run") == previous_marker and number not in hidden_ids):
            closes = None
            try:
                closes = date.fromisoformat(str(entry.get("close_date") or "").partition("T")[0])
            except ValueError:
                pass
            dropped.append({**{k: entry.get(k, "") for k in ("title", "agency", "list", "close_date", "url")},
                            "control_number": number,
                            "reason": "closed" if closes is not None and closes < today else "no longer listed"})

    jobs: dict[str, Any] = {}
    for number, entry in previous.items():
        if number in current_ids:
            continue
        try:
            last_seen = date.fromisoformat(str(entry.get("last_seen", "")))
        except ValueError:
            continue
        if (today - last_seen).days <= HISTORY_KEEP_DAYS:
            if partial and entry.get("last_seen_run") == previous_marker:
                entry = {**entry, "last_seen_run": run_marker}
            jobs[number] = entry
    for r in results:
        number = job_id(r.job)
        jobs[number] = {
            "first_seen": previous.get(number, {}).get("first_seen", today.isoformat()),
            "last_seen": today.isoformat(),
            "last_seen_run": run_marker,
            "list": "hidden" if number in hidden_ids else lists.get(number, "excluded"),
            "title": str(r.job.get("PositionTitle", "")),
            "agency": str(r.job.get("OrganizationName", "")),
            "close_date": str(r.job.get("ApplicationCloseDate", "")),
            "url": job_url(r.job),
        }
    new_history = {"version": 1, "run_marker": run_marker, "updated": today.isoformat(), "jobs": jobs}
    return RunView(matches, watch, near, hidden, dropped, flags, decisions, baseline, new_history,
                   closing_days=closing_days, closing_soon_days=window,
                   failed_searches=list(failed_searches or []))


def closes_in_text(days: int, closes: date) -> str:
    """How soon a CLOSING SOON posting closes: today, tomorrow, or the weekday and date."""
    if days <= 0:
        return "closes today"
    if days == 1:
        return "closes tomorrow"
    return f"closes {closes:%a %b} {closes.day}"


def _days_since(value: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(value)).days
    except ValueError:
        return None


def decision_lines(decisions: dict[str, dict[str, str]], today: date, stale_days: int = STALE_DAYS) -> list[str]:
    """``decide`` with no arguments: counts by status, then active applications with
    days since applying and since the last update (marked when there's been none in
    ``stale_days``), then everything else."""
    if not decisions:
        return []
    counts = {status: 0 for status in ("applied", "interview", "offer", "rejected", "withdrawn",
                                       "watching", "not_interested")}
    for decision in decisions.values():
        counts[decision["status"]] += 1
    lines = [", ".join(f"{count} {status.replace('_', ' ')}" for status, count in counts.items() if count)]

    def label(number: str, decision: dict[str, str]) -> str:
        what = ", ".join(part for part in (decision.get("title", ""), decision.get("agency", "")) if part)
        return f"{number:<12} {decision['status']:<15}" + (what or "(no title saved)")

    active = [(n, d) for n, d in decisions.items() if d["status"] in ACTIVE_STATUSES]
    # Longest wait first; unknown application dates last.
    active.sort(key=lambda item: (item[1].get("applied_on") or "9999", item[0]))
    if active:
        lines += ["", "Active applications:"]
    for number, decision in active:
        applied = _days_since(decision.get("applied_on", ""), today)
        updated = _days_since(decision.get("decided_on", ""), today)
        facts = [f"applied {decision['applied_on']} ({applied} days ago)" if applied is not None
                 else "applied date unknown (record it with --applied-on)"]
        if updated is not None and decision.get("decided_on") != decision.get("applied_on"):
            facts.append(f"last update {decision['decided_on']} ({updated} days ago)")
        if updated is not None and updated >= stale_days:
            facts.append(f"NO UPDATE IN {updated} DAYS")
        if decision["note"]:
            facts.append(decision["note"])
        lines += [label(number, decision), " " * 13 + "; ".join(facts)]
    others = [(n, d) for n, d in decisions.items() if d["status"] not in ACTIVE_STATUSES]
    if others:
        lines += ["", "Other decisions:"]
    for number, decision in others:
        lines.append(label(number, decision) + (f"  {decision['note']}" if decision["note"] else ""))
    return lines
