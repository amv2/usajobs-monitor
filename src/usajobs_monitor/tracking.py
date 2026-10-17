"""Tiers (matches, watch list, near misses), decisions, and run-to-run history."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .jobs import GS_SCALE_PAY_PLANS, close_date, grade_numbers, job_id, job_pay_plans, job_url, salary_upper
from .reasons import Code, code_of, is_hard_no
from .review import ReviewResult

HISTORY_NAME = "usajobs_job_history.json"
DECISION_STATUSES = {"applied", "not_interested", "watching"}
CLOSING_SOON_DAYS = 3
HISTORY_KEEP_DAYS = 45
NEAR_MISS_LIMIT = 15


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
    push a lower grade past the pay floor); other pay systems are judged by upper pay alone."""
    if min_gs_grade and {plan.upper() for plan in job_pay_plans(job)} & GS_SCALE_PAY_PLANS:
        numbers = grade_numbers(job)
        if numbers and max(numbers) < int(min_gs_grade):
            return False
    return pay_floor <= 0 or salary_upper(job) > pay_floor


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


def load_decisions(path: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Read applied / not-interested / watching decisions, keyed by control number."""
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
            decisions[number] = {"status": status, "note": str(row.get("note") or "").strip()}
    return decisions, warnings


def load_history(path: Path) -> dict[str, Any] | None:
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(history, dict) or not isinstance(history.get("jobs"), dict):
        return None
    return history


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

    def flag_text(self, result: ReviewResult) -> str:
        return ", ".join(self.flags.get(job_id(result.job), []))

    def your_status(self, result: ReviewResult) -> str:
        decision = self.decisions.get(job_id(result.job))
        if not decision:
            return ""
        label = decision["status"].replace("_", " ")
        return f"{label} ({decision['note']})" if decision["note"] else label

    def new_count(self, items: list[ReviewResult]) -> int:
        return sum("NEW" in self.flags.get(job_id(r.job), []) for r in items)

    def closing_soon_count(self) -> int:
        return sum("CLOSING SOON" in self.flags.get(job_id(r.job), []) for r in self.matches + self.watch + self.near)


def build_view(results: list[ReviewResult], config: dict[str, Any], history: dict[str, Any] | None,
               decisions: dict[str, dict[str, str]], today: date, run_marker: str) -> RunView:
    hidden_ids = {number for number, d in decisions.items() if d["status"] == "not_interested"}
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
    flags: dict[str, list[str]] = {}
    for r in results:
        number = job_id(r.job)
        tags = []
        if not baseline and number not in previous:
            tags.append("NEW")
        closes = close_date(r.job)
        if closes is not None and 0 <= (closes - today).days <= CLOSING_SOON_DAYS:
            tags.append("CLOSING SOON")
        decision = decisions.get(number)
        if decision and decision["status"] != "not_interested":
            tags.append(decision["status"].upper())
        flags[number] = tags

    current_ids = {job_id(r.job) for r in results}
    previous_marker = (history or {}).get("run_marker")
    dropped = []
    for number, entry in previous.items():
        if (number not in current_ids and entry.get("list") in ("match", "watch", "near_miss")
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
    return RunView(matches, watch, near, hidden, dropped, flags, decisions, baseline, new_history)
