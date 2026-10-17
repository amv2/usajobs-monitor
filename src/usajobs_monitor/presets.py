"""Applicant-type presets: starting ``[eligibility]`` settings for common hiring routes.

A preset only says which USAJOBS hiring paths ("This job is open to") an
applicant type can use. It never proves that a person is eligible: every
route has conditions (service length, time limits, documents) that the
announcement and the applicant's own records decide. The sources listed
with each preset are the rules it reflects.

Hiring path codes are the ones the USAJOBS API returns in
``UserArea.Details.HiringPath``: https://developer.usajobs.gov/General/Hiring-Paths
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Every hiring path code USAJOBS uses, with its display name. "ses" and
# "special-authorities" appear in announcements but not in the API code list.
HIRING_PATHS = {
    "public": "The public",
    "fed-competitive": "Federal employees - Competitive service",
    "fed-excepted": "Federal employees - Excepted service",
    "fed-internal-search": "Internal to an agency",
    "fed-transition": "Career transition (CTAP, ICTAP, RPL)",
    "vet": "Veterans",
    "mspouse": "Military spouses",
    "student": "Students",
    "graduates": "Recent graduates",
    "disability": "Individuals with disabilities",
    "nguard": "National Guard & Reserves",
    "native": "Native Americans",
    "overseas": "Family of overseas employees",
    "peace": "Peace Corps & AmeriCorps Vista",
    "land": "Land & base management",
    "special-authorities": "Special authorities",
    "ses": "Senior executives",
}

USAJOBS_PATHS_HELP = "https://help.usajobs.gov/working-in-government/unique-hiring-paths"
USAJOBS_FED_HELP = "https://help.usajobs.gov/working-in-government/unique-hiring-paths/federal-employees"


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    summary: str
    service: str | None = None  # "competitive" | "excepted"; None leaves it to other presets
    has_competitive_status: bool = False
    hiring_paths: tuple[str, ...] = ()
    caveat: str = ""
    sources: tuple[str, ...] = ()


PRESETS: dict[str, Preset] = {p.name: p for p in (
    Preset(
        "public", "General public",
        "Announcements open to the public (U.S. citizens and nationals).",
        caveat="Federal-only (merit promotion) announcements are not open to you.",
        sources=("https://help.usajobs.gov/working-in-government/unique-hiring-paths/public",),
    ),
    Preset(
        "competitive-fed", "Current federal employee, competitive service",
        "Public announcements plus merit promotion announcements for competitive-service employees "
        "(\"Federal employees - Competitive service\").",
        service="competitive", has_competitive_status=True,
        caveat="Competitive status comes from a career or career-conditional appointment; check block 34 "
               "of your SF-50. Add your agency under [profile] so its internal postings count.",
        sources=("5 CFR 212.301, https://www.ecfr.gov/current/title-5/section-212.301",
                 USAJOBS_FED_HELP),
    ),
    Preset(
        "excepted-fed", "Current federal employee, excepted service",
        "Public announcements plus those open to excepted-service employees. With eligibility.interchange_agency "
        "set, competitive-service announcements that invite OPM interchange candidates or advertise special "
        "authorities are kept too.",
        service="excepted",
        caveat="Excepted-service employees generally cannot apply to merit promotion announcements unless "
               "their agency has an OPM interchange agreement; the agreement sets the conditions.",
        sources=("5 CFR 6.7, https://www.ecfr.gov/current/title-5/section-6.7",
                 USAJOBS_FED_HELP),
    ),
    Preset(
        "veteran", "Veteran (VEOA, VRA, 30% or more disabled)",
        "Public announcements plus those that list the Veterans hiring path.",
        hiring_paths=("vet",),
        caveat="VEOA covers preference eligibles and veterans separated after 3 or more years of continuous "
               "active service under honorable conditions, and applies to merit promotion announcements "
               "open outside the agency's workforce, not to excepted-service positions. VRA and the "
               "30% disabled authority have their own rules.",
        sources=("5 U.S.C. 3304(f) (VEOA)",
                 "https://www.opm.gov/fedshirevets/hiring-officials/strategic-recruitment-and-hiring/"
                 "military-spouses-family-members/tabs/veterans-employment-opportunities-act/",
                 "https://help.usajobs.gov/working-in-government/unique-hiring-paths/veterans"),
    ),
    Preset(
        "military-spouse", "Military spouse",
        "Public announcements plus those that list the Military spouses hiring path.",
        hiring_paths=("mspouse",),
        caveat="Covers spouses of active-duty members, spouses of members retired or separated with a "
               "100 percent disability rating, and un-remarried widows or widowers of members killed on "
               "active duty. From January 1, 2029, active-duty spouses are limited to one appointment per "
               "permanent change of station.",
        sources=("5 CFR 315.612, https://www.ecfr.gov/current/title-5/section-315.612",
                 "https://help.usajobs.gov/working-in-government/unique-hiring-paths/military-spouses"),
    ),
    Preset(
        "student", "Student (Pathways Internship)",
        "Public announcements plus those that list the Students hiring path.",
        hiring_paths=("student",),
        caveat="You must be enrolled or accepted in a qualifying degree, certificate or career and technical "
               "program, at least half time, for the whole internship.",
        sources=("5 CFR 362.202 and 362.203, https://www.ecfr.gov/current/title-5/part-362",
                 "https://help.usajobs.gov/working-in-government/unique-hiring-paths/students"),
    ),
    Preset(
        "recent-graduate", "Recent graduate (Pathways Recent Graduates)",
        "Public announcements plus those that list the Recent graduates hiring path.",
        hiring_paths=("graduates",),
        caveat="Apply within 2 years of completing your qualifying program. Veterans kept from applying by "
               "military service get 2 years from discharge, up to 6 years after completion.",
        sources=("5 CFR 362.302, https://www.ecfr.gov/current/title-5/part-362",
                 "https://help.usajobs.gov/working-in-government/unique-hiring-paths/recent-graduates"),
    ),
    Preset(
        "reinstatement", "Former federal employee (reinstatement)",
        "Public announcements plus merit promotion announcements for competitive-service employees, "
        "which accept reinstatement eligibles.",
        has_competitive_status=True,
        caveat="You need an earlier career or career-conditional appointment. Career tenure and veterans' "
               "preference mean no time limit; otherwise reinstatement is limited to 3 years after separation.",
        sources=("5 CFR 315.401, https://www.ecfr.gov/current/title-5/section-315.401",
                 USAJOBS_FED_HELP),
    ),
    Preset(
        "disability", "Individual with a disability (Schedule A)",
        "Public announcements plus those that list the Individuals with disabilities hiring path.",
        hiring_paths=("disability",),
        caveat="Schedule A appointments need proof of disability, such as a letter from a doctor or a "
               "state vocational rehabilitation agency.",
        sources=("5 CFR 213.3102(u), https://www.ecfr.gov/current/title-5/section-213.3102",
                 "https://help.usajobs.gov/working-in-government/unique-hiring-paths/individuals-with-disabilities"),
    ),
)}


def apply_presets(names: list[str], eligibility: dict[str, Any]) -> dict[str, Any]:
    """Fill ``eligibility`` from ``names``. Keys already set there win; hiring
    paths are combined. Returns what the presets set, for reports and check-config."""
    presets = [PRESETS[name] for name in names]
    services = {p.service for p in presets}
    derived = {
        "service": "excepted" if "excepted" in services else "competitive" if "competitive" in services else "none",
        "has_competitive_status": any(p.has_competitive_status for p in presets),
    }
    applied: dict[str, Any] = {}
    for key, value in derived.items():
        if eligibility.get(key) is None:
            eligibility[key] = value
            applied[key] = value
    paths = list(eligibility.get("additional_hiring_paths") or [])
    added = [path for p in presets for path in p.hiring_paths if path not in paths]
    added = list(dict.fromkeys(added))
    if added:
        eligibility["additional_hiring_paths"] = paths + added
        applied["additional_hiring_paths"] = added
    return applied


def preset_labels(config: dict[str, Any]) -> list[str]:
    names = (config.get("eligibility") or {}).get("presets") or []
    return [PRESETS[name].label for name in names if name in PRESETS]
