"""Your applications: listed on every run from the decisions file, in messages and every report."""

import json
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from openpyxl import load_workbook

from usajobs_monitor import api, delivery, runner, tracking
from usajobs_monitor.config import ConfigError, load_settings, validate_config
from usajobs_monitor.digest import build_digest, chat_text, plain_text
from usajobs_monitor.reports import EXCEL_NAME, HTML_NAME, WORD_NAME

from test_outputs import JOBS, PROFILE
from test_search_filters import WATCH_CONFIG, result

MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}
TODAY = date(2026, 11, 23)


def decision(status, applied_on="", decided_on="", title="", agency="", note=""):
    return {"status": status, "note": note, "title": title, "agency": agency,
            "applied_on": applied_on, "decided_on": decided_on}


DECISIONS = {
    "100": decision("applied", "2026-11-13", "2026-11-13", "Branch Chief", "Example Commission"),
    "200": decision("interview", "2026-10-01", "2026-10-10", "Budget Analyst", "Example Agency", "panel 10/9"),
    "300": decision("cancelled", "2026-10-24", "2026-11-04", "Team Lead", "Example Commission"),
    "400": decision("rejected", "2026-09-01", "2026-09-20", "Auditor"),
    "500": decision("watching", decided_on="2026-11-01"),
    "600": decision("not_interested", decided_on="2026-11-01"),
    "700": decision("offer", "", "2026-11-20"),
}


class ApplicationListTests(unittest.TestCase):
    def test_in_progress_first_longest_wait_first_then_ended_latest_first(self):
        apps = tracking.applications(DECISIONS, TODAY)
        self.assertEqual([a["control_number"] for a in apps], ["200", "100", "700", "300", "400"])
        self.assertEqual([a["active"] for a in apps], [True, True, True, False, False])

    def test_days_waiting_and_the_no_update_mark(self):
        apps = {a["control_number"]: a for a in tracking.applications(DECISIONS, TODAY)}
        self.assertEqual((apps["100"]["days_since_applied"], apps["100"]["days_since_update"]), (10, 10))
        self.assertFalse(apps["100"]["stale"])
        self.assertEqual((apps["200"]["days_since_applied"], apps["200"]["days_since_update"]), (53, 44))
        self.assertTrue(apps["200"]["stale"])
        self.assertFalse(apps["300"]["stale"])  # ended applications are never stale
        self.assertEqual((apps["700"]["applied_on"], apps["700"]["days_since_applied"]), ("", None))
        self.assertEqual(apps["100"]["url"], "https://www.usajobs.gov/job/100")

    def test_a_future_date_has_no_day_count(self):
        apps = tracking.applications({"1": decision("applied", "2026-12-01", "2026-12-01")}, TODAY)
        self.assertEqual((apps[0]["days_since_applied"], apps[0]["days_since_update"], apps[0]["stale"]),
                         (None, None, False))
        self.assertEqual(tracking.application_text(apps[0])[1], ["applied Dec 1, 2026"])

    def test_titles_come_from_history_then_the_control_number(self):
        jobs = {"1": {"title": "Management &amp; Program Analyst", "agency": "Example Agency"}}
        apps = tracking.applications({"1": decision("applied"), "2": decision("applied")}, TODAY, jobs,
                                     {"1": "match"})
        self.assertEqual([(a["title"], a["agency"], a["list"]) for a in apps],
                         [("Management & Program Analyst", "Example Agency", "match"), ("Control number 2", "", "")])

    def test_wording(self):
        apps = {a["control_number"]: a for a in tracking.applications(DECISIONS, TODAY, in_run={"100": "watch"})}
        self.assertEqual(tracking.application_text(apps["100"]),
                         (["APPLIED"], ["Example Commission", "applied Nov 13 (10 days ago)", "still on the watch list"]))
        self.assertEqual(tracking.application_text(apps["200"]),
                         (["INTERVIEW", "NO UPDATE IN 44 DAYS"],
                          ["Example Agency", "applied Oct 1 (53 days ago)", "last update Oct 10 (44 days ago)"]))
        self.assertEqual(tracking.application_text(apps["700"])[1],
                         ["applied date unknown (record it with decide --applied-on)", "last update Nov 20 (3 days ago)"])
        today = tracking.applications({"1": decision("applied", "2026-11-23", "2026-11-23")}, TODAY)[0]
        self.assertEqual(tracking.application_text(today)[1], ["applied Nov 23 (today)"])
        yesterday = tracking.applications({"1": decision("applied", "2026-11-22", "2026-11-22")}, TODAY)[0]
        self.assertEqual(tracking.application_text(yesterday)[1], ["applied Nov 22 (1 day ago)"])
        last_year = tracking.applications({"1": decision("applied", "2025-12-30", "2025-12-30")}, date(2026, 1, 2))[0]
        self.assertEqual(tracking.application_text(last_year)[1], ["applied Dec 30, 2025 (3 days ago)"])

    def test_decide_list_shows_a_future_date_without_negative_days(self):
        lines = tracking.decision_lines({"1": decision("applied", "2026-12-01", "2026-12-01", "Analyst")}, TODAY)
        self.assertIn("applied 2026-12-01", lines[-1])
        self.assertNotIn("days ago", lines[-1])


class ViewTests(unittest.TestCase):
    def view(self, config=WATCH_CONFIG, decisions=DECISIONS):
        jobs = [result("Branch Chief", [], number=100), result("Other", [], number=999)]
        return tracking.build_view(jobs, config, None, decisions, TODAY, "run")

    def test_view_lists_applications_with_the_list_they_are_on(self):
        found = self.view()
        self.assertEqual([a["control_number"] for a in found.active_applications()], ["200", "100", "700"])
        self.assertEqual({a["control_number"]: a["list"] for a in found.applications}["100"], "match")
        self.assertEqual({a["control_number"]: a["list"] for a in found.applications}["200"], "")

    def test_switch_off_and_no_applications(self):
        off = validate_config({**MINIMAL, "report": {"applications": False}})[0]
        self.assertEqual(self.view(off).applications, [])
        self.assertEqual(self.view(decisions={"600": decision("not_interested")}).applications, [])

    def test_config_default_and_type(self):
        self.assertIs(validate_config(MINIMAL)[0]["report"]["applications"], True)
        with self.assertRaises(ConfigError) as caught:
            validate_config({**MINIMAL, "report": {"applications": "yes"}})
        self.assertTrue(any("report.applications" in e for e in caught.exception.errors))


class DigestTests(unittest.TestCase):
    def digest(self, apps, **extra):
        summary = {"jobs_reviewed": 10, "usajobs_searches": 2, "matches_after_final_filters": 0,
                   "target_agency_matches": 0, "near_misses_reported": 0, "new_since_last_run": {"matches": 0},
                   "dropped_since_last_run": [{"title": "Gone", "agency": "A", "reason": "closed"}],
                   "applications_in_progress": apps, **extra}
        return build_digest({"status": "ok", "run_id": "run"}, summary,
                            datetime(2026, 11, 23, 17, tzinfo=timezone.utc), max_items=1)

    def active(self):
        return [a for a in tracking.applications(DECISIONS, TODAY) if a["active"]]

    def test_count_line_and_a_last_section(self):
        digest = self.digest(self.active())
        self.assertIn("Applications in progress: 3 (1 with no update in 30 days or more).", digest.paragraphs)
        last = digest.sections[-1]
        self.assertEqual(last.heading, "Your applications")
        self.assertEqual(len(last.items), 3)  # not cut by max_items
        self.assertEqual(last.items[0].title, "Budget Analyst [INTERVIEW, NO UPDATE IN 44 DAYS]")
        self.assertEqual(last.items[0].details, ["Example Agency · applied Oct 1 (53 days ago) · last update Oct 10 "
                                                 "(44 days ago)", "Your note: panel 10/9"])
        self.assertEqual(last.items[0].url, "https://www.usajobs.gov/job/200")
        self.assertNotIn("Your applications", digest.subject)

    def test_notes_go_to_email_but_never_to_chat(self):
        digest = self.digest(self.active())
        self.assertIn("Your note: panel 10/9", plain_text(digest))
        chat = chat_text(digest, limit=2000)
        self.assertIn("Budget Analyst [INTERVIEW, NO UPDATE IN 44 DAYS]", chat)
        self.assertNotIn("panel", chat)

    def test_long_lists_are_capped(self):
        apps = [{**self.active()[1], "control_number": str(n), "url": f"https://www.usajobs.gov/job/{n}"}
                for n in range(30)]
        section = self.digest(apps).sections[-1]
        self.assertEqual((len(section.items), section.lines), (25, ["5 more in the reports."]))

    def test_no_applications_changes_nothing(self):
        without = self.digest([])
        self.assertFalse(any(s.heading == "Your applications" for s in without.sections))
        self.assertFalse(any("Applications in progress" in p for p in without.paragraphs))
        summary = {"jobs_reviewed": 10, "usajobs_searches": 2, "matches_after_final_filters": 0,
                   "target_agency_matches": 0, "near_misses_reported": 0, "new_since_last_run": {"matches": 0},
                   "dropped_since_last_run": [{"title": "Gone", "agency": "A", "reason": "closed"}]}
        old = build_digest({"status": "ok", "run_id": "run"}, summary, datetime(2026, 11, 23, 17, tzinfo=timezone.utc),
                           max_items=1)
        self.assertEqual(plain_text(old), plain_text(without))

    def test_odd_entries_are_shown_safely(self):
        apps = [{"title": "<b>x</b>", "status": "rejected", "applied_on": "soon", "days_since_applied": -4,
                 "last_update": 5, "stale": "yes", "list": "hidden", "url": "https://evil.example/job/1", "note": 7}]
        item = self.digest(apps).sections[-1].items[0]
        self.assertEqual((item.title, item.url), ("<b>x</b>", ""))
        self.assertEqual(item.details, ["applied date unknown (record it with decide --applied-on)", "Your note: 7"])

    def test_failed_runs_stay_the_fixed_notice(self):
        digest = build_digest({"status": "error", "run_id": "run"}, {"applications_in_progress": self.active()},
                              datetime(2026, 11, 23, 17, tzinfo=timezone.utc))
        self.assertEqual(digest.sections, [])
        self.assertNotIn("Branch Chief", plain_text(digest))


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        patch.object(api, "search_all_jobs", return_value=(JOBS, 1, [])).start()
        self.addCleanup(patch.stopall)
        today = date.today()
        applied = (today - timedelta(days=40)).isoformat()
        (self.base / "job_decisions.csv").write_text(
            "control_number,status,note,title,agency,applied_on,decided_on\n"
            f"111111111,applied,sent,Budget Analyst,Department of Example,{applied},{applied}\n"
            f"812345600,interview,phone screen,Closed Posting,Other Agency,{applied},{today.isoformat()}\n"
            f"812345700,rejected,,Old Posting,Other Agency,{applied},{today.isoformat()}\n", encoding="utf-8")

    def run_all(self, report=""):
        path = self.base / "profile.toml"
        path.write_text(PROFILE.format(formats=json.dumps(["docx", "xlsx", "html", "csv"]))
                        .replace("[report]\n", f"[report]\n{report}"))
        settings = load_settings(path, {"USAJOBS_EMAIL": "me@example.invalid", "USAJOBS_API_KEY": "k"})
        code, outcome = runner.run(settings, lambda run_dir: delivery.deliver(run_dir, settings, dry_run=True))
        self.assertEqual(code, 0, outcome)
        return Path(outcome["run_directory"])

    def email_text(self, folder):
        message = BytesParser(policy=policy.default).parsebytes(next(folder.rglob("email.eml")).read_bytes())
        return message.get_body(preferencelist=("plain",)).get_content()

    def test_every_output_lists_them(self):
        folder = self.run_all()
        summary = json.loads((folder / runner.SUMMARY).read_text())
        self.assertEqual([a["control_number"] for a in summary["applications_in_progress"]],
                         ["111111111", "812345600"])
        body = self.email_text(folder)
        self.assertIn("Applications in progress: 2 (1 with no update in 30 days or more).", body)
        self.assertIn("YOUR APPLICATIONS", body)
        self.assertIn("Closed Posting [INTERVIEW]", body)
        self.assertIn("still listed as a match", body)
        page = (folder / HTML_NAME).read_text()
        self.assertIn("Your applications", page)
        self.assertIn("Closed Posting", page)
        self.assertNotIn("Old Posting", page)
        with zipfile.ZipFile(folder / WORD_NAME) as archive:
            document = archive.read("word/document.xml").decode()
        self.assertIn("Your Applications", document)
        self.assertIn("Your note: phone screen", document)
        book = load_workbook(folder / EXCEL_NAME)
        rows = list(book["Applications"].iter_rows(values_only=True))
        self.assertEqual([r[0] for r in rows], ["Status", "applied", "interview", "rejected"])
        self.assertEqual(rows[1][8], "Match")
        self.assertEqual(rows[2][8], "Not in results")
        self.assertIn(("Applications in progress", 2), list(book["Summary"].iter_rows(values_only=True)))

    def test_switched_off_nothing_is_listed(self):
        folder = self.run_all("applications = false\n")
        self.assertEqual(json.loads((folder / runner.SUMMARY).read_text())["applications_in_progress"], [])
        self.assertNotIn("Your applications", (folder / HTML_NAME).read_text())
        self.assertNotIn("YOUR APPLICATIONS", self.email_text(folder))
        self.assertNotIn("Applications in progress", self.email_text(folder))
        self.assertNotIn("Applications", load_workbook(folder / EXCEL_NAME).sheetnames)
        with zipfile.ZipFile(folder / WORD_NAME) as archive:
            self.assertNotIn("Your Applications", archive.read("word/document.xml").decode())


if __name__ == "__main__":
    unittest.main()
