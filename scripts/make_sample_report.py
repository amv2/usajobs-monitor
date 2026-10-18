"""Regenerate the README's sample report from made-up postings.

Runs the real pipeline offline against the sample profile, with invented
agencies and control numbers, so nothing personal or real goes into the repo:

    python scripts/make_sample_report.py
    # then, to refresh the picture (needs Chrome or Chromium):
    chromium --headless --hide-scrollbars --window-size=900,1215 \
        --screenshot=docs/sample/digest.png docs/sample/usajobs_digest.html
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from usajobs_monitor import api, runner
from usajobs_monitor.config import load_settings
from usajobs_monitor.reports import HTML_NAME

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "sample"


def posting(number: int, title: str, agency: str, *, upper: int, plan: str = "GS", grade: str = "15",
            paths: tuple[str, ...] = ("public",), series: str = "0501", closes: str = "2099-01-30") -> dict:
    return {
        "PositionID": f"SAMPLE-{number}", "PositionURI": f"https://www.usajobs.gov/job/{number:09d}",
        "PositionTitle": title, "OrganizationName": agency, "DepartmentName": agency,
        "PositionLocationDisplay": "Washington, District of Columbia",
        "PositionRemuneration": [{"MinimumRange": str(upper - 40000), "MaximumRange": str(upper),
                                  "RateIntervalCode": "PA"}],
        "JobGrade": [{"Code": plan}], "JobCategory": [{"Code": series, "Name": "Sample series"}],
        "PositionOfferingType": [{"Code": "15317", "Name": "Permanent"}],
        "ApplicationCloseDate": f"{closes}T23:59:59",
        "UserArea": {"Details": {"HiringPath": list(paths), "LowGrade": grade, "HighGrade": grade,
                                 "PromotionPotential": grade}},
    }


JOBS = [
    posting(101, "Supervisory Budget Analyst", "Example Regulatory Commission", upper=221000, plan="SK", grade="16"),
    posting(102, "Financial Management Analyst", "Department of Example", upper=210500, plan="CT", grade="15",
            paths=("special-authorities",)),
    posting(103, "Budget Analyst", "Example Bureau of Statistics", upper=197200),
    posting(104, "Program Analyst (Term)", "Department of Example", upper=205000),
    posting(105, "Registered Nurse", "Example Health Service", upper=205000, series="0610"),
]


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        profile = Path(folder) / "usajobs-monitor.toml"
        text = (ROOT / "profiles" / "financial-management-dc.toml").read_text(encoding="utf-8")
        profile.write_text(text, encoding="utf-8")
        settings = load_settings(profile, {"USAJOBS_EMAIL": "sample@example.invalid", "USAJOBS_API_KEY": "sample"})
        settings.config["report"]["formats"] = ["html"]
        settings.config["report"]["title"] = "Sample report (made-up postings)"
        settings.config["eligibility"]["interchange_agency"] = "Example Agency"
        with mock.patch.object(api, "search_all_jobs", return_value=(JOBS, 1, [])):
            code, outcome = runner.run(settings)
        if code != 0:
            raise SystemExit(f"sample run failed: {outcome}")
        OUT.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings.reports_dir / HTML_NAME, OUT / HTML_NAME)
    print(f"wrote {OUT / HTML_NAME}")


if __name__ == "__main__":
    main()
