"""Structured exclusion reasons.

Near-miss and watch-list logic branch on a reason's code; reports and JSON
show its text. ``Reason`` subclasses ``str`` so it serializes, joins and
compares as its readable text.
"""

from __future__ import annotations

from enum import Enum


class Code(str, Enum):
    SALARY = "salary"
    ELIGIBILITY_UNKNOWN = "eligibility_unknown"
    NO_HIRING_ROUTE = "no_hiring_route"
    INTERNAL_OTHER_AGENCY = "internal_other_agency"
    TEMPORARY = "temporary"
    SENIOR_EXEC_PAY_PLAN = "senior_exec_pay_plan"
    EXCLUDED_PAY_PLAN = "excluded_pay_plan"
    SENIOR_EXEC_TITLE = "senior_exec_title"
    OCCUPATION_MISMATCH = "occupation_mismatch"
    EXCLUDED_SERIES = "excluded_series"
    NO_ROLE_SIGNAL = "no_role_signal"


# Failures that can never become a fit, so they are never near misses.
# Eligibility and appointment-type failures are deliberately not hard-no.
HARD_NO = frozenset({
    Code.INTERNAL_OTHER_AGENCY,
    Code.SENIOR_EXEC_PAY_PLAN,
    Code.EXCLUDED_PAY_PLAN,
    Code.SENIOR_EXEC_TITLE,
    Code.OCCUPATION_MISMATCH,
    Code.EXCLUDED_SERIES,
    Code.NO_ROLE_SIGNAL,
})


class Reason(str):
    """Readable reason text carrying a machine-checkable ``code``."""

    code: Code

    def __new__(cls, code: Code, text: str) -> "Reason":
        obj = super().__new__(cls, text)
        obj.code = Code(code)
        return obj

    def __repr__(self) -> str:
        return f"Reason({self.code.name}, {str.__repr__(self)})"


def code_of(reason: object) -> Code | None:
    return getattr(reason, "code", None)


def is_hard_no(reason: object) -> bool:
    return code_of(reason) in HARD_NO
