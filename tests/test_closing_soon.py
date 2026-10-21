"""CLOSING SOON: the configurable window, applied jobs, urgency wording, and the digest's lead section."""

import unittest
from datetime import date, datetime, timezone

from usajobs_monitor import runner, tracking
from usajobs_monitor.config import ConfigError, validate_config
from usajobs_monitor.digest import build_digest, chat_text

from test_search_filters import SALARY, WATCH_CONFIG, result

MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}
FRIDAY = date(2026, 10, 16)


def view(results, days=None, decisions=None):
    config = {**WATCH_CONFIG, "report": {"closing_soon_days": days}} if days else WATCH_CONFIG
    return tracking.build_view(results, config, None, decisions or {}, FRIDAY, "run")


class WindowTests(unittest.TestCase):
    def test_default_window_boundaries(self):
        jobs = [result(f"closes +{n}", [], number=n + 1, close=f"2026-10-{16 + n}") for n in (0, 1, 3, 4)]
        found = view(jobs)
        self.assertEqual(found.closing_days, {"1": 0, "2": 1, "4": 3})
        self.assertNotIn("CLOSING SOON", found.flags["5"])
        self.assertEqual(found.closing_soon_count(), 3)
        self.assertEqual(found.closing_soon_days, 3)

    def test_already_closed_missing_or_unparsable_dates_are_not_flagged(self):
        jobs = [result("closed", [], number=1, close="2026-10-15"), result("blank", [], number=2),
                result("garbage", [], number=3)]
        jobs[1].job["ApplicationCloseDate"] = ""
        jobs[2].job["ApplicationCloseDate"] = "next week"
        self.assertEqual(view(jobs).closing_days, {})

    def test_custom_window(self):
        jobs = [result("closes +4", [], number=1, close="2026-10-20"), result("closes +6", [], number=2, close="2026-10-22")]
        self.assertEqual(view(jobs, days=5).closing_days, {"1": 4})
        self.assertEqual(view(jobs, days=5).closing_soon_days, 5)

    def test_applied_jobs_are_not_flagged_but_watching_jobs_are(self):
        decisions = {"1": {"status": "applied", "note": ""}, "2": {"status": "watching", "note": ""}}
        jobs = [result("applied", [], number=1, close="2026-10-17"), result("watching", [], number=2, close="2026-10-17")]
        found = view(jobs, decisions=decisions)
        self.assertEqual(found.flags["1"], ["APPLIED"])
        self.assertEqual(found.flags["2"], ["CLOSING SOON", "WATCHING"])
        self.assertEqual(found.closing_soon_count(), 1)

    def test_soonest_first_across_lists_once_each(self):
        jobs = [result("match +3", [], number=1, close="2026-10-19", score=9),
                result("watch +0", [SALARY], number=2, close="2026-10-16"),
                result("match +1", [], number=3, close="2026-10-17", score=1)]
        found = view(jobs)
        self.assertEqual([(r.job["PositionTitle"], name) for r, name in found.closing_soon()],
                         [("watch +0", "watch"), ("match +1", "match"), ("match +3", "match")])

    def test_urgency_wording(self):
        self.assertEqual(tracking.closes_in_text(0, FRIDAY), "closes today")
        self.assertEqual(tracking.closes_in_text(1, date(2026, 10, 17)), "closes tomorrow")
        self.assertEqual(tracking.closes_in_text(3, date(2026, 10, 19)), "closes Mon Oct 19")


class ConfigTests(unittest.TestCase):
    def test_default_and_range(self):
        config, _ = validate_config(MINIMAL)
        self.assertEqual(config["report"]["closing_soon_days"], 3)
        config, _ = validate_config({**MINIMAL, "report": {"closing_soon_days": 14}})
        self.assertEqual(config["report"]["closing_soon_days"], 14)
        for bad in (0, 15, 2.5, "3", True):
            with self.assertRaises(ConfigError) as caught:
                validate_config({**MINIMAL, "report": {"closing_soon_days": bad}})
            self.assertTrue(any("report.closing_soon_days" in e for e in caught.exception.errors), bad)


class DigestTests(unittest.TestCase):
    def digest(self, found, **summary):
        base = {"jobs_reviewed": 10, "usajobs_searches": 2, "matches_after_final_filters": len(found.matches),
                "target_agency_matches": 0, "near_misses_reported": 0, "watch_list_label": "GS-15 watch list",
                "new_since_last_run": {"matches": 0}, "closing_soon_days": found.closing_soon_days,
                "closing_soon": found.closing_soon_count(), "closing_soon_jobs": runner.closing_soon_entries(found),
                "matches": []}
        manifest = {"status": "ok", "run_id": "run"}
        return build_digest(manifest, {**base, **summary}, datetime(2026, 10, 16, 17, tzinfo=timezone.utc), max_items=1)

    def test_closing_section_leads_and_ignores_max_items(self):
        jobs = [result(f"job {n}", [], number=n, close=f"2026-10-{16 + n}", score=10 - n) for n in (1, 2, 3)]
        found = view(jobs, decisions={"9": {"status": "applied", "note": ""}})
        digest = self.digest(found)
        self.assertIn("3 closing soon", digest.subject)
        first = digest.sections[0]
        self.assertEqual(first.heading, "Closing soon (not yet applied)")
        self.assertEqual(len(first.items), 3)
        self.assertEqual(first.items[0].details[0].split(" · ")[:2], ["closes tomorrow (Oct 17)", "Match"])
        self.assertTrue(first.items[1].details[0].startswith("closes Sun Oct 18 · Match"))
        self.assertTrue(first.items[2].details[0].startswith("closes Mon Oct 19 · Match"))
        self.assertNotIn("CLOSING SOON", first.items[0].title)
        self.assertIn("closing soon", chat_text(digest, limit=2000).lower())

    def test_no_closing_jobs_no_section(self):
        digest = self.digest(view([result("later", [], number=1)]))
        self.assertNotIn("closing soon", digest.subject)
        self.assertFalse(any(s.heading.startswith("Closing soon") for s in digest.sections))

    def test_old_summaries_still_build(self):
        manifest = {"status": "ok", "run_id": "run"}
        summary = {"jobs_reviewed": 1, "usajobs_searches": 1, "matches_after_final_filters": 0,
                   "target_agency_matches": 0, "near_misses_reported": 0, "new_since_last_run": {"matches": 0},
                   "closing_soon": 2}
        digest = build_digest(manifest, summary, datetime(2026, 10, 16, 17, tzinfo=timezone.utc))
        self.assertTrue(any("Closing within 3 days: 2" in p for p in digest.paragraphs))


if __name__ == "__main__":
    unittest.main()
