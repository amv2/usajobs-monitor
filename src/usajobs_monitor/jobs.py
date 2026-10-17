"""Field accessors and display helpers for USAJOBS ``MatchedObjectDescriptor`` records."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# USAJOBS position offering type codes (data.usajobs.gov/api/codelist/positionofferingtypes)
PERMANENT_OFFERING_CODE = "15317"
NON_PERMANENT_OFFERING_CODES = {
    "15318": "Temporary", "15319": "Term", "15320": "Detail", "15321": "Temporary promotion",
    "15322": "Seasonal", "15323": "Summer", "15328": "Internship", "15522": "Intermittent",
}
GS_SCALE_PAY_PLANS = {"GS", "GG", "GM", "GL"}


def first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def details(job: dict[str, Any]) -> dict[str, Any]:
    return first_mapping(first_mapping(job.get("UserArea")).get("Details"))


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(as_text(v) for v in value.values())
    return str(value)


def mapping_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def not_listed(value: str) -> str:
    return value if value else "Not listed"


def joined_unique(values: list[str], separator: str = ", ") -> str:
    unique = list(dict.fromkeys(value for value in (v.strip() for v in values) if value))
    return separator.join(unique)


def collect_text(job: dict[str, Any]) -> str:
    relevant = [
        job.get("PositionTitle"),
        job.get("OrganizationName"),
        job.get("DepartmentName"),
        job.get("QualificationSummary"),
        job.get("PositionSchedule"),
        job.get("PositionOfferingType"),
        job.get("JobGrade"),
        job.get("PositionLocationDisplay"),
        details(job),
    ]
    return re.sub(r"\s+", " ", as_text(relevant)).strip()


# Hours, days and pay periods per year, to compare any posted rate with an annual floor.
# 2,087 hours is OPM's standard for converting hourly to annual pay (5 U.S.C. 5504).
ANNUAL_MULTIPLIERS = {"PH": 2087.0, "PD": 261.0, "PW": 52.0, "BW": 26.0, "PM": 12.0}


def _amount(raw: Any) -> float | None:
    try:
        value = float(str(raw).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def salary_upper(job: dict[str, Any]) -> float | None:
    """The highest posted pay as an annual amount, or None when no pay is posted.

    Hourly, daily, weekly, biweekly and monthly bands are annualized so they can be
    compared with the annual salary floors. Missing pay is not $0: callers must decide
    what an unknown salary means."""
    maximums: list[float] = []
    for band in job.get("PositionRemuneration", []) or []:
        if not isinstance(band, dict):
            continue
        amount = _amount(band.get("MaximumRange"))
        if amount is None:
            continue
        rate = str(band.get("RateIntervalCode") or "").strip().upper()
        maximums.append(amount * ANNUAL_MULTIPLIERS.get(rate, 1.0))
    return max(maximums) if maximums else None


def salary_display(job: dict[str, Any]) -> str:
    bands = []
    for band in job.get("PositionRemuneration", []) or []:
        if not isinstance(band, dict):
            continue
        minimum = _amount(band.get("MinimumRange"))
        maximum = _amount(band.get("MaximumRange"))
        rate = band.get("RateIntervalCode", "")
        if minimum or maximum:
            bands.append(f"${minimum or 0:,.0f} - ${maximum or 0:,.0f} {rate}".strip())
    return "; ".join(bands)


def job_category_codes(job: dict[str, Any]) -> list[str]:
    codes = []
    for category in mapping_list(job.get("JobCategory")):
        code = as_text(category.get("Code", "")).strip()
        if code:
            codes.append(code)
    return codes


def job_pay_plans(job: dict[str, Any]) -> list[str]:
    pay_plans: list[str] = []
    for category in mapping_list(job.get("JobGrade")):
        code = as_text(category.get("Code", "")).strip()
        if code:
            pay_plans.append(code)
    return pay_plans


def pay_plan_display(job: dict[str, Any]) -> str:
    return not_listed(joined_unique(job_pay_plans(job)))


def grade_display(job: dict[str, Any]) -> str:
    detail = details(job)
    low_grade = as_text(detail.get("LowGrade", "")).strip()
    high_grade = as_text(detail.get("HighGrade", "")).strip()
    if low_grade and high_grade:
        if low_grade == high_grade:
            return low_grade
        return f"{low_grade} - {high_grade}"
    return not_listed(low_grade or high_grade)


def grade_numbers(job: dict[str, Any]) -> list[int]:
    detail = details(job)
    numbers = []
    for key in ("LowGrade", "HighGrade"):
        try:
            numbers.append(int(as_text(detail.get(key)).strip()))
        except ValueError:
            continue
    return numbers


def promotion_potential_display(job: dict[str, Any]) -> str:
    """Use the advertised ceiling, never infer it from the hiring grade."""
    value = as_text(details(job).get("PromotionPotential")).strip()
    if not value:
        return "Not listed"
    if value.casefold() == "none":
        return "None (no promotion potential advertised)"
    pay_plan = pay_plan_display(job)
    if re.fullmatch(r"\d+(?:\.\d+)?|[IVX]+", value) and pay_plan != "Not listed" and "," not in pay_plan:
        return f"{pay_plan}-{value}"
    return value


def occupational_series_display(job: dict[str, Any]) -> str:
    series = []
    for category in mapping_list(job.get("JobCategory")):
        code = as_text(category.get("Code", "")).strip()
        name = as_text(category.get("Name", "")).strip()
        if code and name:
            series.append(f"{code} ({name})")
        elif code or name:
            series.append(code or name)
    return not_listed(joined_unique(series, "; "))


def series_label(job: dict[str, Any], code: str) -> str:
    for category in mapping_list(job.get("JobCategory")):
        if str(category.get("Code", "")).strip() == code:
            return str(
                category.get("Value")
                or category.get("Name")
                or category.get("Title")
                or category.get("Description")
                or ""
            ).strip()
    return ""


def job_url(job: dict[str, Any]) -> str:
    return str(job.get("PositionURI") or "")


def job_id(job: dict[str, Any]) -> str:
    """USAJOBS control number (the number in the job URL), else the agency position ID."""
    match = re.search(r"/(\d+)/?$", str(job.get("PositionURI") or ""))
    if match:
        return match.group(1)
    return str(job.get("PositionID") or job.get("PositionTitle") or "")


def close_date(job: dict[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(job.get("ApplicationCloseDate") or "").partition("T")[0])
    except ValueError:
        return None


def location_display(job: dict[str, Any]) -> str:
    value = job.get("PositionLocationDisplay")
    if value:
        return as_text(value)
    parts = []
    for location in job.get("PositionLocation", []) or []:
        city = location.get("CityName", "")
        state = location.get("CountrySubDivisionCode", "")
        parts.append(", ".join(p for p in [city, state] if p))
    return "; ".join(parts)


def agency_display(job: dict[str, Any]) -> str:
    return str(job.get("OrganizationName") or job.get("DepartmentName") or "Unknown")
