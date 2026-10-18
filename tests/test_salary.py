"""Missing and non-annual pay: unposted pay is unknown, not $0."""

import unittest

from usajobs_monitor import review, tracking
from usajobs_monitor.jobs import salary_display, salary_upper
from usajobs_monitor.reasons import Code


def job(bands, plan="GS", low="14", high="14"):
    return {"PositionTitle": "Budget Analyst", "OrganizationName": "Smithsonian Institution",
            "JobGrade": [{"Code": plan}], "UserArea": {"Details": {"HiringPath": ["public"],
                                                                     "LowGrade": low, "HighGrade": high}},
            "PositionOfferingType": [{"Code": "15317"}], "PositionRemuneration": bands}


def config(floor, min_grade=None):
    return {"search": {"target_role_keywords": ["budget"], "target_agencies": []},
            "filters": {"salary_upper_must_exceed": floor, "min_gs_grade": min_grade, "current_agency_terms": [],
                        "temporary_terms": [], "mismatch_terms": [], "senior_executive_terms": []},
            "eligibility": {"service": "competitive", "has_competitive_status": True}}


class SalaryUpperTests(unittest.TestCase):
    def test_missing_or_blank_pay_is_none(self):
        for bands in ([], None, [{"MaximumRange": ""}], [{"MaximumRange": "N/A"}], [{"MaximumRange": "0"}]):
            self.assertIsNone(salary_upper({"PositionRemuneration": bands}), bands)
        self.assertIsNone(salary_upper({}))

    def test_rates_are_annualized(self):
        self.assertEqual(salary_upper(job([{"MaximumRange": "50.00", "RateIntervalCode": "PH"}])), 50 * 2087)
        self.assertEqual(salary_upper(job([{"MaximumRange": "5,000", "RateIntervalCode": "BW"}])), 130000)
        self.assertEqual(salary_upper(job([{"MaximumRange": "150000", "RateIntervalCode": "PA"}])), 150000)

    def test_display_tolerates_text(self):
        self.assertEqual(salary_display(job([{"MinimumRange": "N/A", "MaximumRange": "100", "RateIntervalCode": "PA"}])),
                         "$0 - $100 PA")


class MissingSalaryReviewTests(unittest.TestCase):
    def test_no_floor_keeps_unposted_pay(self):
        result = review.review_job(job([]), config(0))
        self.assertNotIn(Code.SALARY, [r.code for r in result.reasons])
        self.assertTrue(result.kept, result.reasons)

    def test_floor_marks_unposted_pay_unconfirmed(self):
        result = review.review_job(job([]), config(150000))
        self.assertEqual([r.code for r in result.reasons], [Code.SALARY])
        self.assertIn("salary not posted", result.reasons[0])
        self.assertNotIn("$0", result.reasons[0])
        self.assertTrue(tracking.clears_near_miss_floor(result.job, 140000, None))

    def test_grade_rule_still_applies_without_pay(self):
        result = review.review_job(job([], low="11", high="12"), config(150000, min_grade=14))
        self.assertEqual(len(result.reasons), 1)
        self.assertIn("top grade GS-12 is below GS-14", result.reasons[0])

    def test_grade_rule_applies_with_no_floor(self):
        result = review.review_job(job([], low="11", high="12"), config(0, min_grade=14))
        self.assertIn("below GS-14", result.reasons[0])

    def test_hourly_pay_compared_as_annual(self):
        result = review.review_job(job([{"MaximumRange": "80", "RateIntervalCode": "PH"}], plan="WG"), config(150000))
        self.assertTrue(result.kept, result.reasons)


if __name__ == "__main__":
    unittest.main()
