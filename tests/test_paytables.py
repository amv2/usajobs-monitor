"""OPM pay tables, [target] floors, the GS grade rule, and the pay-table command."""

import contextlib
import copy
import io
from pathlib import Path
import tomllib
import unittest

from usajobs_monitor import cli, reports, review
from usajobs_monitor.config import ConfigError, target_label, validate_config
from usajobs_monitor.paytables import available_years, load_table
from usajobs_monitor.reasons import Code

ROOT = Path(__file__).resolve().parent.parent
MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}
# Floors the original script used, typed by hand: the 2026 pay cap and DC GS-14 step 10.
HAND_TYPED = {"salary_upper_must_exceed": 197200, "near_miss_upper_salary_must_exceed": 187093,
              "near_miss_min_gs_grade": 15, "watch_list": {"pay_plans": ["GS", "GG"], "grades": [15]}}


def with_target(**target):
    return {**copy.deepcopy(MINIMAL), "target": {"grade": 15, "locality": "DCB", "year": 2026, **target}}


class PayTableTests(unittest.TestCase):
    table = load_table(2026)

    def test_rates_match_published_opm_tables(self):
        # Rows copied from OPM's published 2026 locality tables (SF, DCB, RUS).
        published = {
            ("SF", 1): [33049, 34157, 35256, 36351, 37447, 38089, 39177, 40271, 40317, 41338],
            ("SF", 14): [157236, 162478, 167720, 172962, 178204, 183446, 188688, 193930, 197200, 197200],
            ("SF", 15): [184950, 191116] + [197200] * 8,
            ("DCB", 2): [34011, 34820, 35948, 36900, 37313, 38410, 39507, 40604, 41701, 42798],
            ("DCB", 12): [102415, 105829, 109243, 112657, 116071, 119485, 122899, 126313, 129728, 133142],
            ("DCB", 15): [169279, 174922, 180565, 186207, 191850] + [197200] * 5,
            ("RUS", 1): [26437, 27323, 28202, 29078, 29954, 30468, 31338, 32214, 32250, 33067],
            ("RUS", 15): [147945, 152877, 157809, 162740, 167672, 172604, 177536, 182467, 187399, 192331],
        }
        for (code, grade), rates in published.items():
            with self.subTest(locality=code, grade=grade):
                self.assertEqual([self.table.rate(grade, step, code) for step in range(1, 11)], rates)

    def test_table_contents(self):
        self.assertIn(2026, available_years())
        self.assertEqual(len(self.table.localities), 58)
        self.assertEqual(self.table.pay_cap, 197200)
        self.assertEqual(sorted(self.table.base), list(range(1, 16)))
        self.assertTrue(any("2025-23844" in source for source in self.table.sources))
        self.assertEqual(self.table.top_of_grade(0, "DCB"), 0)

    def test_find_locality(self):
        for text, code in [("DCB", "DCB"), ("dcb", "DCB"), ("Washington, DC", "DCB"), ("Baltimore", "DCB"),
                           ("Rest of U.S.", "RUS"), ("rest of us", "RUS"), ("Alaska", "AK"),
                           ("seattle", "SEA"), ("San Francisco", "SF")]:
            with self.subTest(text=text):
                self.assertEqual(self.table.find_locality(text), code)
        with self.assertRaisesRegex(LookupError, "several areas.*SD.*SF"):
            self.table.find_locality("San")
        with self.assertRaisesRegex(LookupError, "no OPM locality"):
            self.table.find_locality("Atlantis")


class TargetConfigTests(unittest.TestCase):
    def test_no_target_leaves_floors_alone(self):
        config, _ = validate_config({**MINIMAL, "filters": dict(HAND_TYPED)})
        self.assertNotIn("target", config)
        self.assertIsNone(config["filters"]["min_gs_grade"])
        for key, value in HAND_TYPED.items():
            self.assertEqual(config["filters"][key], value)

    def test_above_grade_reproduces_hand_typed_floors(self):
        config, warnings = validate_config(with_target(match="above_grade"))
        self.assertEqual(warnings, [])
        for key, value in HAND_TYPED.items():
            self.assertEqual(config["filters"][key], value, key)
        self.assertIsNone(config["filters"]["min_gs_grade"])
        self.assertEqual(target_label(config),
                         "GS-15 in Washington-Baltimore-Arlington, DC-MD-VA-WV-PA (2026 OPM pay table)")

    def test_at_grade_is_the_default(self):
        filters = validate_config(with_target(grade=14, locality="Seattle"))[0]["filters"]
        table = load_table(2026)
        self.assertEqual(filters["salary_upper_must_exceed"], table.rate(13, 10, "SEA"))
        self.assertEqual(filters["near_miss_upper_salary_must_exceed"], table.rate(12, 10, "SEA"))
        self.assertEqual((filters["min_gs_grade"], filters["near_miss_min_gs_grade"]), (14, 13))
        self.assertEqual(filters["watch_list"], {})  # at_grade leaves the watch list to you

    def test_floors_stay_below_the_cap(self):
        # In SF GS-14 step 10 is capped, so "above GS-14" would shut out capped GS-15 postings.
        filters = validate_config(with_target(locality="SF"))[0]["filters"]
        self.assertEqual(filters["salary_upper_must_exceed"], 197199)

    def test_grade_one_has_no_lower_grade(self):
        filters = validate_config(with_target(grade=1))[0]["filters"]
        self.assertEqual((filters["salary_upper_must_exceed"], filters["near_miss_upper_salary_must_exceed"]), (0, 0))
        self.assertIsNone(filters["near_miss_min_gs_grade"])

    def test_explicit_filters_override_target(self):
        raw = with_target(match="above_grade")
        raw["filters"] = {"salary_upper_must_exceed": 150000, "watch_list": {}}
        config, _ = validate_config(raw)
        self.assertEqual(config["filters"]["salary_upper_must_exceed"], 150000)
        self.assertEqual(config["filters"]["watch_list"]["grades"], [])
        self.assertEqual(config["filters"]["near_miss_upper_salary_must_exceed"], 187093)
        self.assertEqual(config["target"]["resolved"]["overridden"], ["salary_upper_must_exceed", "watch_list"])

    def test_target_errors_name_the_setting(self):
        cases = [
            ({"grade": 16}, "target.grade: expected a GS grade from 1 to 15"),
            ({"grade": None}, "target.grade: required"),
            ({"grade": True}, "target.grade: expected a GS grade"),
            ({"year": 1999}, "target.year: expected a bundled pay table year"),
            ({"locality": "Atlantis"}, "target.locality: no OPM locality"),
            ({"locality": "San"}, "target.locality: matches several areas"),
            ({"match": "best"}, "target.match: expected one of"),
            ({"pay_plan": "NH"}, "target.pay_plan: expected one of \"GS\""),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                raw = with_target(**change)
                raw["target"] = {k: v for k, v in raw["target"].items() if v is not None}
                with self.assertRaises(ConfigError) as caught:
                    validate_config(raw)
                self.assertTrue(any(e.startswith(message) for e in caught.exception.errors), caught.exception.errors)

    def test_missing_locality_is_required(self):
        raw = with_target()
        del raw["target"]["locality"]
        with self.assertRaises(ConfigError) as caught:
            validate_config(raw)
        self.assertIn("target.locality: required", caught.exception.errors)

    def test_sample_profile_floors_equal_the_old_hand_typed_ones(self):
        raw = tomllib.loads((ROOT / "profiles" / "financial-management-dc.toml").read_text())
        filters = validate_config(raw)[0]["filters"]
        for key, value in HAND_TYPED.items():
            self.assertEqual(filters[key], value, key)


def job(plan="GS", low="14", high="14", upper="190000"):
    return {"PositionTitle": "Budget Analyst", "OrganizationName": "Example Agency",
            "JobGrade": [{"Code": plan}], "PositionRemuneration": [{"MaximumRange": upper}],
            "UserArea": {"Details": {"HiringPath": ["public"], "LowGrade": low, "HighGrade": high}}}


class GradeRuleTests(unittest.TestCase):
    config = validate_config(with_target())[0]  # at_grade GS-15 in DC: above $187,093 and GS-15

    def test_gs_posting_below_target_grade_fails_pay_rule(self):
        result = review.review_job(job(), self.config)
        self.assertEqual([r.code for r in result.reasons], [Code.SALARY])
        self.assertEqual(str(result.reasons[0]), "top grade GS-14 is below GS-15")

    def test_target_grade_and_equivalent_pay_plans_pass(self):
        for case in (job(high="15", upper="197200"), job(plan="SK", low="", high="", upper="190000"),
                     job(plan="GG", low="13", high="15", upper="197200")):
            with self.subTest(plan=case["JobGrade"][0]["Code"]):
                self.assertTrue(review.review_job(case, self.config).kept)

    def test_low_pay_gives_one_salary_reason(self):
        result = review.review_job(job(upper="150000"), self.config)
        self.assertEqual(len(result.reasons), 1)
        self.assertTrue(str(result.reasons[0]).startswith("posted upper salary"))

    def test_above_grade_target_reviews_like_hand_typed_floors(self):
        target = validate_config(with_target(match="above_grade"))[0]
        typed = validate_config({**MINIMAL, "filters": dict(HAND_TYPED)})[0]
        jobs = [job(), job(high="15", upper="197200"), job(plan="SK", low="", high="", upper="230000"),
                job(plan="GG", low="13", high="13", upper="158322")]
        for case in jobs:
            a, b = review.review_job(case, target), review.review_job(case, typed)
            self.assertEqual((a.kept, list(map(str, a.reasons)), a.score), (b.kept, list(map(str, b.reasons)), b.score))


class ReportTextTests(unittest.TestCase):
    def test_active_filters_name_the_target(self):
        config = validate_config(with_target())[0]
        line = reports.active_filters_text(config, "job_decisions.csv")[0]
        self.assertIn("must exceed $187,093", line)
        self.assertIn("GS-scale postings must also reach grade 15", line)
        self.assertIn("target, GS-15 in Washington-Baltimore-Arlington", line)

    def test_hand_typed_floors_keep_their_wording(self):
        config = validate_config({**MINIMAL, "filters": dict(HAND_TYPED)})[0]
        line = reports.active_filters_text(config, "job_decisions.csv")[0]
        self.assertEqual(line, "Compensation: posted upper salary must exceed $197,200. "
                               "Postings that fail only this rule are shown on the GS/GG-15 watch list.")


class PayTableCommandTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["pay-table", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_lists_areas(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("DCB", out)
        self.assertIn("33.94%", out)

    def test_shows_one_area(self):
        code, out, _ = self.run_cli("Washington", "--year", "2026")
        self.assertEqual(code, 0)
        self.assertIn("187,093", out)
        self.assertIn("Sources:", out)

    def test_unknown_area(self):
        code, _, err = self.run_cli("Atlantis")
        self.assertEqual(code, 2)
        self.assertIn("no OPM locality", err)


if __name__ == "__main__":
    unittest.main()
