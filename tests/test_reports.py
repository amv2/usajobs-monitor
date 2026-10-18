"""Generated report text, plus an offline end-to-end run that must pass validation."""

from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from usajobs_monitor import api, runner
from usajobs_monitor.config import load_settings
from usajobs_monitor.reasons import Reason, Code
from usajobs_monitor.reports import active_filters_text, format_timestamp
from usajobs_monitor.tracking import watch_list_label

ROOT = Path(__file__).resolve().parent.parent
ENV = {"USAJOBS_EMAIL": "e@example.com", "USAJOBS_API_KEY": "key"}


def sample_settings(folder: Path):
    raw = (ROOT / "profiles" / "financial-management-dc.toml").read_text()
    path = folder / "usajobs-monitor.toml"
    path.write_text(raw)
    return load_settings(path, ENV)


class FilterTextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = sample_settings(Path(temp.name))
        self.config = self.settings.config

    def test_text_comes_from_config(self):
        text = "\n".join(active_filters_text(self.config, "job_decisions.csv"))
        self.assertIn("must exceed $197,200", text)
        self.assertIn("GS/GG-15 watch list", text)
        self.assertIn("competitive-service profile", text)
        self.assertIn("0905, 0510, 0511", text)
        self.assertIn("EM, EO", text)
        self.assertIn("grade 15", text)
        self.assertIn("job_decisions.csv", text)
        self.assertNotIn("Regulatory Commission", text)
        self.assertNotIn("accounting", text)

    def test_text_follows_a_different_profile(self):
        config = {**self.config,
                  "filters": {**self.config["filters"], "excluded_job_series": [], "watch_list": {},
                              "exclude_senior_executive": False, "excluded_pay_plans": [],
                              "near_miss_min_gs_grade": None, "salary_upper_must_exceed": 0},
                  "eligibility": {"service": "excepted", "has_competitive_status": False,
                                  "interchange_agency": "Example Agency", "additional_hiring_paths": ["vet"]}}
        text = "\n".join(active_filters_text(config, "d.csv"))
        self.assertIn("no minimum posted salary", text)
        self.assertIn("Example Agency excepted-service profile", text)
        self.assertIn("additional hiring paths: vet", text)
        for absent in ("watch list", "Occupational series", "Pay plans excluded", "grade 15"):
            self.assertNotIn(absent, text)

    def test_watch_list_label(self):
        self.assertEqual(watch_list_label(self.config), "GS/GG-15 watch list")
        self.assertEqual(watch_list_label({"filters": {"watch_list": {"pay_plans": ["NH"], "grades": [3, 4]}}}),
                         "NH-3/4 watch list")
        self.assertEqual(watch_list_label({"filters": {}}), "Watch list")

    def test_portable_timestamp(self):
        self.assertEqual(format_timestamp(datetime(2026, 10, 7, 9, 5)), "Refreshed October 7, 2026 at 9:05 AM")
        self.assertEqual(format_timestamp(datetime(2026, 1, 1, 0, 30)), "Refreshed January 1, 2026 at 12:30 AM")
        self.assertEqual(format_timestamp(datetime(2026, 1, 1, 12, 0)), "Refreshed January 1, 2026 at 12:00 PM")


def posting(number, title, *, upper="210000", plan="GS", grade="15", paths=("public",), series="0501"):
    return {"MatchedObjectDescriptor": {
        "PositionID": f"P-{number}", "PositionURI": f"https://www.usajobs.gov/job/{number}",
        "PositionTitle": title, "OrganizationName": "Department of the Treasury",
        "PositionRemuneration": [{"MinimumRange": "150000", "MaximumRange": upper, "RateIntervalCode": "PA"}],
        "JobGrade": [{"Code": plan}], "JobCategory": [{"Code": series, "Name": "Series"}],
        "PositionOfferingType": [{"Code": "15317", "Name": "Permanent"}],
        "ApplicationCloseDate": "2099-01-01T23:59:59",
        "UserArea": {"Details": {"HiringPath": list(paths), "LowGrade": grade, "HighGrade": grade}},
    }}


class EndToEndTests(unittest.TestCase):
    def test_offline_run_publishes_valid_reports(self):
        items = [
            posting(1, "Supervisory Budget Analyst", plan="SK", grade="16"),  # match
            posting(2, "Budget Analyst", upper="197200"),  # watch: GS-15, salary only
            posting(3, "Financial Analyst", upper="220000", plan="EM", grade="00"),  # excluded pay plan
            posting(4, "Registered Nurse", series="0610"),  # mismatch
            posting(5, "Budget Officer", upper="220000", plan="CT", paths=("fed-competitive",)),  # match via status
        ]
        page = {"SearchResult": {"SearchResultCountAll": len(items), "SearchResultItems": items}}
        with tempfile.TemporaryDirectory() as folder:
            settings = sample_settings(Path(folder))
            with mock.patch.object(api, "fetch_usajobs", return_value=page) as fetch:
                code, outcome = runner.run(settings)
            self.assertEqual(code, 0, outcome)
            searches = len(settings.config["search"]["locations"]) * (
                len(settings.config["search"]["target_role_keywords"]) + len(settings.config["search"]["search_only_keywords"]))
            self.assertEqual(fetch.call_count, searches)

            reports = settings.reports_dir
            summary = json.loads((reports / runner.SUMMARY).read_text())
            self.assertEqual(summary["jobs_reviewed"], 5)
            self.assertEqual(sorted(m["control_number"] for m in summary["matches"]), ["1", "5"])
            self.assertEqual([w["control_number"] for w in summary["watch_list"]], ["2"])
            self.assertEqual(summary["watch_list"][0]["exclusion_codes"], ["salary"])
            self.assertTrue(summary["history_baseline"])
            self.assertTrue((reports / runner.HISTORY).is_file())
            with zipfile.ZipFile(reports / "usajobs_briefing.docx") as docx:
                body = docx.read("word/document.xml").decode()
            self.assertIn("USAJOBS Financial Management Monitor", body)
            self.assertIn("GS/GG-15 Watch List", body)
            self.assertNotIn("Regulatory Commission", body)

            # Second run: same postings, so nothing NEW; history carries over.
            with mock.patch.object(api, "fetch_usajobs", return_value=page):
                code, _ = runner.run(settings)
            summary = json.loads((reports / runner.SUMMARY).read_text())
            self.assertEqual((code, summary["history_baseline"], summary["new_since_last_run"]["matches"]),
                             (0, False, 0))
            self.assertEqual(summary["history_status"], "found")

            # A corrupt history file is reported and kept aside, not silently replaced.
            (reports / runner.HISTORY).write_text("{not json", encoding="utf-8")
            with mock.patch.object(api, "fetch_usajobs", return_value=page):
                code, _ = runner.run(settings)
            summary = json.loads((reports / runner.SUMMARY).read_text())
            self.assertEqual((code, summary["history_baseline"], summary["history_status"]), (0, True, "unreadable"))
            self.assertTrue(any("could not be read" in w for w in summary["search_warnings"]))
            self.assertEqual((reports / (runner.HISTORY + ".unreadable")).read_text(), "{not json")
            self.assertIn('"jobs"', (reports / runner.HISTORY).read_text())
            with zipfile.ZipFile(reports / "usajobs_briefing.docx") as docx:
                self.assertIn("couldn", docx.read("word/document.xml").decode())


class ReasonTests(unittest.TestCase):
    def test_reason_is_readable_text_with_a_code(self):
        reason = Reason(Code.SALARY, "posted upper salary $1 does not exceed $2")
        self.assertEqual(reason, "posted upper salary $1 does not exceed $2")
        self.assertIs(reason.code, Code.SALARY)
        self.assertEqual(json.dumps([reason]), '["posted upper salary $1 does not exceed $2"]')
        self.assertEqual("; ".join([reason, reason]).count("posted"), 2)


if __name__ == "__main__":
    unittest.main()
