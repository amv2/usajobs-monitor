"""HTML digest and CSV export: written per report.formats, validated, published and attached."""

import csv
import io
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from usajobs_monitor import api, delivery, runner
from usajobs_monitor.config import ConfigError, load_settings, validate_config
from usajobs_monitor.reports import CSV_NAME, EXCEL_NAME, HTML_NAME, WORD_NAME, report_files

PROFILE = """
version = 1
[search]
locations = ["Denver, Colorado"]
target_role_keywords = ["budget analyst"]
[filters]
salary_upper_must_exceed = 100000
[eligibility]
presets = ["public"]
[report]
formats = {formats}
[delivery.email]
smtp_host = "smtp.example.com"
username = "me@example.invalid"
to_addresses = ["me@example.invalid"]
"""


def posting(number, title, pay, paths=("public",)):
    return {"PositionTitle": title, "OrganizationName": "Department of Example",
            "PositionURI": f"https://www.usajobs.gov/job/{number}",
            "PositionRemuneration": [{"MinimumRange": "90000", "MaximumRange": str(pay)}],
            "PositionOfferingType": [{"Code": "15317", "Name": "Permanent"}],
            "JobGrade": [{"Code": "GS"}], "ApplicationCloseDate": "2099-01-31T23:59:59.9970",
            "UserArea": {"Details": {"HiringPath": list(paths), "LowGrade": "12", "HighGrade": "13"}}}


JOBS = [posting(111111111, "Budget Analyst", 150000), posting(222222222, "=HYPERLINK(\"x\") Budget Analyst", 95000),
        posting(333333333, "Budget Analyst <b>lead</b>", 150000, ("fed-competitive",))]


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        patch.object(api, "search_all_jobs", return_value=(JOBS, 1, [])).start()
        self.addCleanup(patch.stopall)

    def settings(self, formats):
        path = self.base / "profile.toml"
        path.write_text(PROFILE.format(formats=json.dumps(formats)))
        return load_settings(path, {"USAJOBS_EMAIL": "me@example.invalid", "USAJOBS_API_KEY": "k"})

    def test_default_formats_are_the_office_pair(self):
        config, _ = validate_config({"search": {"locations": ["X"], "target_role_keywords": ["y"]}})
        self.assertEqual(report_files(config), [WORD_NAME, EXCEL_NAME])

    def test_formats_are_validated(self):
        for bad in ([], ["pdf"]):
            with self.assertRaises(ConfigError):
                validate_config({"search": {"locations": ["X"], "target_role_keywords": ["y"]},
                                 "report": {"formats": bad}})

    def test_html_and_csv_run_publishes_only_those_files(self):
        settings = self.settings(["html", "csv"])
        folder, ok = runner.execute_search(settings)
        self.assertTrue(ok)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual([a["name"] for a in manifest["attachments"]], [HTML_NAME, CSV_NAME])
        self.assertFalse((folder / WORD_NAME).exists())
        published = json.loads((settings.reports_dir / runner.SUMMARY).read_text())
        self.assertEqual(published["html_digest"], str(settings.reports_dir / HTML_NAME))
        self.assertNotIn("word_briefing", published)
        self.assertTrue((settings.reports_dir / CSV_NAME).is_file())

        page = (folder / HTML_NAME).read_text()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("https://www.usajobs.gov/job/111111111", page)
        self.assertNotIn("<b>lead</b>", page)
        self.assertIn("&lt;b&gt;lead&lt;/b&gt;", page)

        rows = list(csv.DictReader(io.StringIO((folder / CSV_NAME).read_text(encoding="utf-8-sig"))))
        lists = {row["Control Number"]: row["List"] for row in rows}
        self.assertEqual(lists, {"111111111": "Match", "222222222": "Near Miss", "333333333": "Near Miss"})
        injected = next(row for row in rows if row["Control Number"] == "222222222")
        self.assertTrue(injected["Title"].startswith("'="))

    def test_preview_email_attaches_html_and_csv(self):
        settings = self.settings(["html", "csv"])
        code, outcome = runner.run(settings, lambda run_dir: delivery.deliver(run_dir, settings, dry_run=True))
        self.assertEqual(code, 0, outcome)
        eml = next(Path(outcome["run_directory"]).rglob("email.eml"))
        message = BytesParser(policy=policy.default).parsebytes(eml.read_bytes())
        types = sorted(part.get_content_type() for part in message.iter_attachments())
        self.assertEqual(types, ["text/csv", "text/html"])
        self.assertIn("HTML digest and CSV export are attached",
                      message.get_body(preferencelist=("plain",)).get_content())

    def test_all_four_formats_validate_and_deliver(self):
        settings = self.settings(["docx", "xlsx", "html", "csv"])
        code, outcome = runner.run(settings, lambda run_dir: delivery.deliver(run_dir, settings, dry_run=True))
        self.assertEqual(code, 0, outcome)
        self.assertEqual(len(delivery.validated_run(Path(outcome["run_directory"])).attachments), 4)

    def test_verify_eligibility_flag_reaches_every_output(self):
        jobs = [posting(444444444, "Budget Analyst", 150000, ("special-authorities",)),
                posting(555555555, "Budget Analyst", 95000, ("special-authorities",))]
        patch.object(api, "search_all_jobs", return_value=(jobs, 1, [])).start()
        path = self.base / "profile.toml"
        path.write_text(PROFILE.format(formats=json.dumps(["docx", "xlsx", "html", "csv"])).replace(
            'presets = ["public"]', 'presets = ["excepted-fed"]\ninterchange_agency = "Example Agency"'))
        settings = load_settings(path, {"USAJOBS_EMAIL": "me@example.invalid", "USAJOBS_API_KEY": "k"})
        code, outcome = runner.run(settings, lambda run_dir: delivery.deliver(run_dir, settings, dry_run=True))
        self.assertEqual(code, 0, outcome)
        folder = Path(outcome["run_directory"])
        summary = json.loads((folder / runner.SUMMARY).read_text())
        self.assertEqual([m["control_number"] for m in summary["matches"]], ["444444444"])
        flagged = {e["control_number"] for key in ("matches", "watch_list", "top_near_misses") for e in summary[key]
                   if "VERIFY ELIGIBILITY" in e.get("flags", [])}
        self.assertEqual(flagged, {"444444444", "555555555"})
        for name in (WORD_NAME, EXCEL_NAME):
            with zipfile.ZipFile(folder / name) as archive:
                xml = "".join(archive.read(n).decode("utf-8") for n in archive.namelist() if n.endswith(".xml"))
            self.assertIn("VERIFY ELIGIBILITY", xml, name)
        for name in (HTML_NAME, CSV_NAME):
            self.assertIn("VERIFY ELIGIBILITY", (folder / name).read_text(encoding="utf-8-sig"), name)
        eml = next(folder.rglob("email.eml"))
        body = BytesParser(policy=policy.default).parsebytes(eml.read_bytes()).get_body(
            preferencelist=("plain",)).get_content()
        self.assertIn("VERIFY ELIGIBILITY", body)

    def test_tampered_text_report_is_refused(self):
        settings = self.settings(["csv"])
        folder, ok = runner.execute_search(settings)
        self.assertTrue(ok)
        (folder / CSV_NAME).write_text("Title\n=cmd\n")
        with self.assertRaises(delivery.DeliveryError):
            delivery.validated_run(folder)

    def test_summary_from_before_formats_still_previews(self):
        settings = self.settings(["docx", "xlsx"])
        runner.execute_search(settings)
        latest = settings.reports_dir / runner.SUMMARY
        summary = json.loads(latest.read_text())
        del summary["report_files"]
        latest.write_text(json.dumps(summary))
        preview = runner.preview_current(settings.reports_dir)
        self.assertEqual([a["name"] for a in json.loads((preview / "manifest.json").read_text())["attachments"]],
                         [WORD_NAME, EXCEL_NAME])


if __name__ == "__main__":
    unittest.main()
