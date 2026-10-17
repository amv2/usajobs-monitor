"""Eligibility screening, exclusion filters and relevance scoring for one posting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import current_agency_terms, scoring
from .jobs import (
    GS_SCALE_PAY_PLANS,
    NON_PERMANENT_OFFERING_CODES,
    PERMANENT_OFFERING_CODE,
    as_text,
    collect_text,
    details,
    first_mapping,
    grade_numbers,
    job_category_codes,
    job_pay_plans,
    mapping_list,
    salary_upper,
    series_label,
)
from .presets import HIRING_PATHS
from .reasons import Code, Reason

# SES and its equivalents (USAJOBS pay plan code list). Agency-specific
# executive plans such as FDIC EM/EO or FHFA LL go in filters.excluded_pay_plans.
SENIOR_EXECUTIVE_PAY_PLANS = {
    "ES", "SL", "ST",  # SES, Senior Level, Scientific and Professional
    "IE", "EP",  # Senior Intelligence Executive Service, Defense Intelligence SES
    "EX", "SN", "FT", "FB", "FX", "FJ",  # Executive Schedule and senior-level / Foreign Service equivalents
    "EV", "SW", "RX",  # FAA, TSA, and FDA executive plans
}


@dataclass
class ReviewResult:
    job: dict[str, Any]
    kept: bool
    reasons: list[Reason]
    score: int
    eligibility_basis: str = ""
    # (signal, points) in the order they were added, for explanations.
    score_parts: list[tuple[str, float]] = field(default_factory=list)


def contains_any(text: str, terms: list[str]) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in terms)


def keyword_score(text: str, keywords: list[str]) -> int:
    lower = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lower)


def text_values(value: Any) -> set[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return set()
    return {item.strip().casefold() for item in value if isinstance(item, str) and item.strip()}


def target_agency_hit(job: dict[str, Any], agencies: list[str]) -> bool:
    agency_text = " ".join(
        [
            as_text(job.get("OrganizationName")),
            as_text(job.get("DepartmentName")),
            as_text(details(job).get("AgencyMarketingStatement")),
        ]
    )
    lower = agency_text.lower()
    for agency in agencies:
        term = str(agency or "").strip()
        if not term:
            continue
        lowered = term.lower()
        if term.isalpha() and term.isupper() and len(term) <= 5:
            if re.search(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", lower):
                return True
            continue
        if lowered in lower:
            return True
    return False


def hiring_paths(job: dict[str, Any]) -> set[str]:
    detail = details(job)
    paths = text_values(detail.get("HiringPath"))
    if paths:
        return paths
    labels = text_values(detail.get("HiringPathDisplay"))
    if not labels:
        labels = text_values(first_mapping(detail.get("WhoMayApply")).get("Name"))
    codes = {
        "the public": "public",
        "competitive service": "fed-competitive",
        "federal employees - competitive service": "fed-competitive",
        "excepted service": "fed-excepted",
        "federal employees - excepted service": "fed-excepted",
        "special authorities": "special-authorities",
        "internal to an agency": "fed-internal-search",
        "agency employees only": "fed-internal-search",
        "career transition (ctap, ictap, rpl)": "fed-transition",
        **{name.casefold(): code for code, name in HIRING_PATHS.items() if code not in ("fed-internal-search",)},
    }
    return {codes.get(label, "unrecognized:" + label) for label in labels}


def current_agency_hit(job: dict[str, Any], terms: list[str]) -> bool:
    employer = " ".join(as_text(job.get(key)) for key in ("OrganizationName", "DepartmentName"))
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term.strip())}(?![a-z0-9])", employer, re.IGNORECASE)
        for term in terms if term.strip()
    )


def is_internal_other_agency(job: dict[str, Any], current_agency_terms: list[str]) -> bool:
    """Exclude explicit internal hiring paths, not general federal eligibility.

    Competitive service and special authorities can provide an interchange
    route. Keeping a posting for review does not certify personal eligibility.
    Missing/ambiguous hiring metadata is not evidence of an internal restriction.
    """
    paths = hiring_paths(job)
    internal_only = "fed-internal-search" in paths and paths <= {"fed-internal-search", "fed-transition"}
    if not internal_only:
        return False
    # Agency identity must come from the employer fields. A mention of the
    # applicant's agency in boilerplate elsewhere must not bypass an internal-only restriction.
    employer = " ".join(as_text(job.get(key)) for key in ("OrganizationName", "DepartmentName"))
    if not employer.strip():
        return False
    return not current_agency_hit(job, current_agency_terms)


def applicant_label(config: dict[str, Any]) -> str:
    """'Example Agency excepted-service profile', or just 'excepted-service profile'."""
    profile = config.get("eligibility") or {}
    agency = str(profile.get("interchange_agency") or (config.get("profile") or {}).get("current_agency") or "").strip()
    service = str(profile.get("service") or "").strip()
    service_text = f"{service}-service" if service in ("competitive", "excepted") else "applicant"
    return f"{agency} {service_text} profile" if agency else f"{service_text} profile"


INTERCHANGE_DENIAL = re.compile(
    r"interchange.{0,100}\b(?:not accepted|not eligible|not considered|ineligible|excluded)\b"
    r"|(?:do not|does not|will not|cannot) (?:accept|consider).{0,80}interchange"
)
INTERCHANGE_INVITATIONS = (
    r"if you are in the excepted service,? your current agency must have an interchange agreement with opm",
    r"(?:applicants|employees|candidates|individuals) (?:eligible|covered) under (?:an? |the )?(?:opm )?interchange agreements? (?:may apply|may be considered|are eligible)",
)


def eligibility_screen(job: dict[str, Any], config: dict[str, Any]) -> tuple[str, Reason | None]:
    """Return an evidenced screening route, or a reason to withhold a match.

    A badge alone is not proof of individual eligibility. In particular, an
    excepted-service employee does not automatically have competitive status.
    Explicit announcement instructions may establish interchange consideration
    even when the special-authorities badge is absent (e.g. SEC 884631100).
    """
    profile = config.get("eligibility", {})
    if not profile:
        return "", Reason(Code.ELIGIBILITY_UNKNOWN, "eligibility review required: applicant hiring profile is missing")
    paths = hiring_paths(job)
    if "public" in paths:
        return "Open to the public", None
    if "fed-internal-search" in paths and current_agency_hit(job, current_agency_terms(config)):
        return "Internal opening at current agency; check any narrower organizational restrictions", None
    if "fed-excepted" in paths and profile.get("service") == "excepted":
        return "Excepted-service employees invited; verify appointment-specific requirements", None
    if "fed-competitive" in paths and profile.get("has_competitive_status") is True:
        return "Competitive status confirmed in applicant profile", None

    interchange_agency = str(profile.get("interchange_agency") or "").strip()
    if interchange_agency:
        # Restrict affirmative evidence to applicant instructions. Do not match
        # a generic mention, an outgoing-transfer statement, or a denial.
        interchange_basis = ""
        interchange_denied = False
        for field in ("WhoMayApply", "ClarificationFromAgency", "RequiredDocuments", "HowToApply", "OtherInformation"):
            instruction = re.sub(r"\s+", " ", as_text(details(job).get(field))).casefold()
            for statement in re.split(r"(?<=[.!?;])\s+", instruction):
                if INTERCHANGE_DENIAL.search(statement):
                    interchange_denied = True
                if re.search(r"\b(?:not|no|cannot|ineligible|excluded)\b", statement):
                    continue
                if any(re.search(pattern, statement) for pattern in INTERCHANGE_INVITATIONS):
                    interchange_basis = (f"Announcement explicitly permits OPM interchange consideration ({field}); "
                                         "verify service and SF-50 requirements")
        if interchange_basis and not interchange_denied:
            return interchange_basis, None
        if "special-authorities" in paths and not interchange_denied:
            return (f"Special authorities advertised; potential {interchange_agency} interchange route, "
                    "subject to announcement-specific coverage and SF-50 requirements"), None

    other_paths = paths & text_values(profile.get("additional_hiring_paths"))
    if other_paths:
        return "Confirmed additional applicant hiring path: " + ", ".join(sorted(other_paths)), None
    if not paths or any(path.startswith("unrecognized:") for path in paths):
        return "", Reason(Code.ELIGIBILITY_UNKNOWN, "eligibility review required: missing or unrecognized hiring "
                                                    "paths; no supported applicant route established")
    return "", Reason(Code.NO_HIRING_ROUTE, f"no supported hiring route for {applicant_label(config)}: "
                                            "competitive status and other special eligibilities are not assumed")


def occupational_mismatch(job: dict[str, Any], terms: list[str], series: list[str]) -> bool:
    """Title words and job series only; announcement boilerplate ("ChatGPT or Copilot",
    "doctors to nurses", "a physician or other medical professional") is ignored."""
    title = as_text(job.get("PositionTitle")).lower()
    if any(re.search(rf"\b{re.escape(term.strip().lower())}s?\b", title) for term in terms if term.strip()):
        return True
    return bool(set(job_category_codes(job)) & {str(code).strip() for code in series})


def appointment_type_reason(job: dict[str, Any], temporary_terms: list[str]) -> Reason | None:
    """Decide permanence from the appointment-type code, not announcement boilerplate.

    Phrases such as "resume not to exceed 2 pages" or "non-temporary appointment"
    appear in permanent postings, so free-text matching is limited to the title
    and the appointment-type label.
    """
    offerings = mapping_list(job.get("PositionOfferingType"))
    codes = {as_text(o.get("Code")).strip() for o in offerings}
    title = f" {as_text(job.get('PositionTitle')).lower()} "
    if contains_any(title, temporary_terms) or re.search(r"\((?:temporary|term|nte|detail)\b", title):
        return Reason(Code.TEMPORARY, "temporary, term, NTE, or detail appointment (title)")
    if PERMANENT_OFFERING_CODE in codes:
        return None
    non_permanent = sorted(NON_PERMANENT_OFFERING_CODES[c] for c in codes if c in NON_PERMANENT_OFFERING_CODES)
    if non_permanent:
        return Reason(Code.TEMPORARY, f"temporary, term, NTE, or detail appointment ({', '.join(non_permanent)})")
    label = f" {as_text([o.get('Name') for o in offerings]).lower()} "
    if contains_any(label, temporary_terms + ["temporary", "term "]):
        return Reason(Code.TEMPORARY, "temporary, term, NTE, or detail appointment")
    return None


def pay_plan_reason(job: dict[str, Any], filters: dict[str, Any]) -> Reason | None:
    """One reason for executive pay plans: built-in SES equivalents plus configured plans."""
    plans = {plan.upper() for plan in job_pay_plans(job)}
    senior = SENIOR_EXECUTIVE_PAY_PLANS if filters.get("exclude_senior_executive", True) else set()
    extra = {str(plan).strip().upper() for plan in filters.get("excluded_pay_plans", []) if str(plan).strip()}
    hits = sorted(plans & (senior | extra))
    if not hits:
        return None
    if plans & senior:
        return Reason(Code.SENIOR_EXEC_PAY_PLAN, f"senior executive pay plan ({', '.join(hits)})")
    return Reason(Code.EXCLUDED_PAY_PLAN, f"excluded pay plan ({', '.join(hits)})")


def gs_grade_reason(job: dict[str, Any], min_grade: int | None) -> Reason | None:
    """GS-scale postings whose top grade is below ``min_grade`` fail the pay rule.
    Other pay plans are judged by dollars alone, so "GS-15 or equivalent" works."""
    if not min_grade:
        return None
    plans = [plan.upper() for plan in job_pay_plans(job) if plan.upper() in GS_SCALE_PAY_PLANS]
    numbers = grade_numbers(job)
    if not plans or not numbers or max(numbers) >= int(min_grade):
        return None
    return Reason(Code.SALARY, f"top grade {plans[0]}-{max(numbers)} is below {plans[0]}-{int(min_grade)}")


def pay_reason(job: dict[str, Any], filters: dict[str, Any]) -> Reason | None:
    """One salary-rule reason, or None. A posting with no pay listed is not "$0": with no
    floor it passes, and with a floor it fails only as unconfirmed, so it can still be a
    near miss or watch-list entry. The GS grade rule applies either way."""
    upper = salary_upper(job)
    salary_floor = float(filters.get("salary_upper_must_exceed") or 0)
    if upper is not None and salary_floor > 0 and upper <= salary_floor:
        return Reason(Code.SALARY, f"posted upper salary ${upper:,.0f} does not exceed ${salary_floor:,.0f}")
    grade_reason = gs_grade_reason(job, filters.get("min_gs_grade"))
    if grade_reason:
        return grade_reason
    if upper is None and salary_floor > 0:
        return Reason(Code.SALARY, f"salary not posted, so it can't be confirmed above ${salary_floor:,.0f}; "
                                   "check the announcement")
    return None


def review_job(job: dict[str, Any], config: dict[str, Any]) -> ReviewResult:
    filters = config["filters"]
    search = config["search"]
    weights = scoring(config)
    text = collect_text(job)
    reasons: list[Reason] = []

    salary_reason = pay_reason(job, filters)
    if salary_reason:
        reasons.append(salary_reason)

    eligibility_basis, eligibility_reason = eligibility_screen(job, config)
    internal_other = not eligibility_basis and is_internal_other_agency(job, current_agency_terms(config))
    if internal_other:
        reasons.append(Reason(Code.INTERNAL_OTHER_AGENCY, "internal-to-agency posting outside current agency"))
    elif eligibility_reason:
        reasons.append(eligibility_reason)

    temporary_reason = appointment_type_reason(job, filters.get("temporary_terms", []))
    if temporary_reason:
        reasons.append(temporary_reason)

    plan_reason = pay_plan_reason(job, filters)
    if plan_reason:
        reasons.append(plan_reason)
    elif filters.get("exclude_senior_executive", True) and contains_any(
            f" {as_text(job.get('PositionTitle')).lower()} ", filters.get("senior_executive_terms", [])):
        # Title only: reporting-line boilerplate ("reports to the Deputy Director")
        # must not exclude ordinary analyst postings.
        reasons.append(Reason(Code.SENIOR_EXEC_TITLE, "senior executive or director-level title"))

    if occupational_mismatch(job, filters.get("mismatch_terms", []), filters.get("mismatch_job_series", [])):
        reasons.append(Reason(Code.OCCUPATION_MISMATCH, "obvious non-target occupational mismatch"))

    excluded_job_series = {str(code).strip() for code in filters.get("excluded_job_series", []) if str(code).strip()}
    job_series = job_category_codes(job)
    # Exclude only when every listed series is excluded: a posting that also
    # recruits under a target series can still be applied to under that series.
    if job_series and set(job_series) <= excluded_job_series:
        code = job_series[0]
        label = series_label(job, code)
        text_label = f"excluded occupational/job series {code} ({label})" if label else f"excluded occupational/job series {code}"
        reasons.append(Reason(Code.EXCLUDED_SERIES, text_label))

    # Role signals: the title is the strongest evidence, then the job series,
    # then keyword phrases found anywhere in the announcement.
    parts: list[tuple[str, float]] = []
    keyword_hits = keyword_score(text, search.get("target_role_keywords", []))
    if keyword_hits:
        parts.append((f"{keyword_hits} target keyword{'s' if keyword_hits != 1 else ''} in the announcement",
                      keyword_hits * weights["keyword"]))
    title = f" {as_text(job.get('PositionTitle')).lower()} "
    if contains_any(title, search.get("role_title_terms", [])):
        parts.append(("role title term in the title", weights["title"]))
    target_series = {str(code).strip() for code in search.get("target_job_series", [])}
    if target_series & set(job_series):
        parts.append(("target job series", weights["series"]))
    # A target agency raises the score but is not a role signal on its own;
    # otherwise any IT or facilities posting at a target agency would match.
    if sum(points for _, points in parts) == 0:
        reasons.append(Reason(Code.NO_ROLE_SIGNAL, "no target role signal (title term, job series, or keyword)"))

    if target_agency_hit(job, search.get("target_agencies", [])):
        parts.append(("target agency", weights["target_agency"]))
    # Pay level ranks senior postings above junior ones among near misses and the watch list.
    # Unposted pay earns nothing here.
    upper = salary_upper(job)
    salary_floor = float(filters.get("salary_upper_must_exceed") or 0)
    if upper is None:
        pass
    elif upper > salary_floor:
        parts.append(("upper pay above the salary rule", weights["above_salary_rule"]))
    elif upper >= weights["pay_tier_high"]:
        parts.append(("upper pay in the high tier", weights["pay_high"]))
    elif upper >= weights["pay_tier_mid"]:
        parts.append(("upper pay in the middle tier", weights["pay_mid"]))
    score = sum(points for _, points in parts)

    return ReviewResult(job=job, kept=not reasons, reasons=reasons, score=score, eligibility_basis=eligibility_basis,
                        score_parts=parts)
