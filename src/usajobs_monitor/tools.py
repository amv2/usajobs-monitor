"""Search, eligibility and match explanations as plain data, for the MCP server.

Each function takes loaded ``Settings`` and returns JSON-ready dicts, so the
MCP layer (mcp_server.py) stays thin and these work without any AI installed.
Nothing here reads or returns your resume.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import api
from .jobs import (
    agency_display,
    as_text,
    close_date,
    grade_display,
    job_id,
    job_url,
    location_display,
    occupational_series_display,
    pay_plan_display,
    promotion_potential_display,
    salary_display,
)
from .reasons import code_of, is_hard_no
from .review import ReviewResult, applicant_label, eligibility_screen, hiring_paths, is_internal_other_agency, review_job
from .config import Settings, current_agency_terms
from .tracking import build_view, load_decisions, near_miss_reason_text, watch_list_label

MAX_RESULTS = 50


class ToolInputError(ValueError):
    """A problem with what the caller asked for; its message is safe to show."""
TIER_ORDER = {"match": 0, "watch_list": 1, "near_miss": 2, "excluded": 3, "hidden": 4}


def _tiers(results: list[ReviewResult], settings: Settings) -> dict[int, str]:
    """``id(result) -> tier`` using the same lists a run reports."""
    decisions, _ = load_decisions(settings.decisions_path)
    view = build_view(results, settings.config, None, decisions, date.today(), "mcp")
    tiers = {id(r): "excluded" for r in results}
    for name, items in (("hidden", view.hidden), ("near_miss", view.near), ("watch_list", view.watch),
                        ("match", view.matches)):
        for r in items:
            tiers[id(r)] = name
    return tiers


def _decision(settings: Settings, number: str) -> dict[str, str] | None:
    decisions, _ = load_decisions(settings.decisions_path)
    return decisions.get(number)


def posting(job: dict[str, Any]) -> dict[str, Any]:
    closes = close_date(job)
    return {
        "control_number": job_id(job),
        "title": str(job.get("PositionTitle") or ""),
        "agency": agency_display(job),
        "url": job_url(job),
        "location": location_display(job),
        "salary": salary_display(job),
        "pay_plan": pay_plan_display(job),
        "grade": grade_display(job),
        "promotion_potential": promotion_potential_display(job),
        "series": occupational_series_display(job),
        "close_date": closes.isoformat() if closes else "",
    }


def _reasons(result: ReviewResult) -> list[dict[str, Any]]:
    return [{"code": code.value if (code := code_of(r)) else "", "text": str(r), "hard_no": is_hard_no(r)}
            for r in result.reasons]


def search_jobs(settings: Settings, keyword: str = "", location: str = "", remote: bool = False,
                days: int | None = None, include_excluded: bool = False, limit: int = 25) -> dict[str, Any]:
    """Search USAJOBS and screen every result against the profile.

    With no keyword, this runs the profile's own searches (every keyword in
    every location), which can take many requests.
    """
    config = settings.config
    search = dict(config["search"])
    if keyword.strip():
        search["target_role_keywords"] = [keyword.strip()]
        search["search_only_keywords"] = []
        if location.strip() or remote:
            search["locations"] = [location.strip()] if location.strip() else []
            search["include_remote_jobs"] = bool(remote)
    if days is not None:
        search["date_posted_days"] = int(days)
    jobs, calls, warnings = api.search_all_jobs({**config, "search": search})
    # The profile's filters and eligibility apply as in a scheduled run; an
    # ad hoc keyword also counts as a role signal, alongside the profile's own.
    review_config = config
    if keyword.strip():
        review_search = {**config["search"],
                         "target_role_keywords": [*config["search"]["target_role_keywords"], keyword.strip()]}
        review_config = {**config, "search": review_search}
    results = [review_job(job, review_config) for job in jobs]
    tiers = _tiers(results, settings)
    shown = [r for r in results if include_excluded or tiers[id(r)] in ("match", "watch_list", "near_miss")]
    shown.sort(key=lambda r: (TIER_ORDER[tiers[id(r)]], -r.score, str(r.job.get("PositionTitle") or "")))
    limit = max(1, min(int(limit), MAX_RESULTS))
    counts: dict[str, int] = {}
    for r in results:
        counts[tiers[id(r)]] = counts.get(tiers[id(r)], 0) + 1
    return {
        "jobs_reviewed": len(results),
        "usajobs_requests": calls,
        "counts": counts,
        "watch_list_label": watch_list_label(config),
        "warnings": warnings,
        "results": [{**posting(r.job), "tier": tiers[id(r)], "score": r.score,
                     "eligibility_basis": r.eligibility_basis, "failed_checks": [str(x) for x in r.reasons]}
                    for r in shown[:limit]],
        "truncated": len(shown) > limit,
    }


def _fetch(settings: Settings, control_number: str) -> dict[str, Any]:
    try:
        job = api.fetch_job(control_number, settings.config["credentials"])
    except ValueError as exc:
        raise ToolInputError(str(exc)) from None
    if job is None:
        raise ToolInputError(f"No open USAJOBS posting has control number {str(control_number).strip()}. "
                          "Closed postings drop out of the search API.")
    return job


def screen_eligibility(settings: Settings, control_number: str) -> dict[str, Any]:
    """Who may apply, and whether the profile's applicant type has a route in."""
    job = _fetch(settings, control_number)
    config = settings.config
    basis, reason = eligibility_screen(job, config)
    internal_other = not basis and is_internal_other_agency(job, current_agency_terms(config))
    detail = (job.get("UserArea") or {}).get("Details") or {}
    if basis:
        verdict = "route_found"
    elif internal_other:
        verdict = "internal_other_agency"
    else:
        verdict = code_of(reason).value if reason is not None and code_of(reason) else "no_route"
    return {
        **posting(job),
        "applicant_profile": applicant_label(config),
        "hiring_paths": sorted(hiring_paths(job)),
        "who_may_apply": as_text(detail.get("WhoMayApply")).strip()[:1500],
        "verdict": verdict,
        "eligibility_basis": basis,
        "reason": ("internal-to-agency posting outside current agency" if internal_other
                   else str(reason) if reason is not None else ""),
        "note": "A screening aid, not a determination. The announcement and the hiring agency decide eligibility.",
    }


def review_posting(settings: Settings, control_number: str) -> tuple[ReviewResult, str]:
    """Fetch one posting and review it; returns the result and its tier."""
    result = review_job(_fetch(settings, control_number), settings.config)
    return result, _tiers([result], settings)[id(result)]


def explain_match(settings: Settings, control_number: str) -> dict[str, Any]:
    """Why a posting is a match, watch-list item, near miss or excluded, check by check."""
    result, tier = review_posting(settings, control_number)
    job = result.job
    number = job_id(job)
    decision = _decision(settings, number)
    if tier == "match":
        headline = "Passes every check."
    elif tier == "hidden":
        headline = "You marked this not interested, so runs hide it."
    elif tier == "watch_list":
        headline = f"On the {watch_list_label(settings.config)}: relevant, failing only the salary rule."
    elif tier == "near_miss":
        headline = "Near miss. " + near_miss_reason_text(result)
    elif any(is_hard_no(r) for r in result.reasons):
        headline = "Excluded by a check that cannot become a fit."
    else:
        headline = "Excluded; it is not close enough to list as a near miss."
    return {
        **posting(job),
        "tier": tier,
        "headline": headline,
        "score": result.score,
        "score_parts": [{"signal": signal, "points": points} for signal, points in result.score_parts],
        "eligibility_basis": result.eligibility_basis,
        "failed_checks": _reasons(result),
        "your_decision": decision or {},
    }
