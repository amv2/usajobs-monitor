"""Keep promotion ceilings distinct from the grades open for hiring."""

import unittest

from usajobs_monitor.jobs import promotion_potential_display


class PromotionPotentialTests(unittest.TestCase):
    def job(self, **detail):
        return {"JobGrade": [{"Code": "CG"}],
                "UserArea": {"Details": {"LowGrade": "11", "HighGrade": "13", **detail}}}

    def test_ceiling_can_exceed_hiring_grade(self):
        self.assertEqual(promotion_potential_display(self.job(PromotionPotential="14")), "CG-14")

    def test_missing_ceiling_is_not_inferred(self):
        for value in [None, "", "   "]:
            with self.subTest(value=value):
                self.assertEqual(promotion_potential_display(self.job(PromotionPotential=value)), "Not listed")
        self.assertEqual(promotion_potential_display(self.job()), "Not listed")

    def test_explicit_none_is_distinct_from_missing(self):
        self.assertEqual(promotion_potential_display(self.job(PromotionPotential="None")),
                         "None (no promotion potential advertised)")

    def test_preserves_zero_and_descriptive_values(self):
        for value, expected in [(0, "CG-0"), ("00", "CG-00"),
                                ("14 - Full performance level", "14 - Full performance level")]:
            with self.subTest(value=value):
                self.assertEqual(promotion_potential_display(self.job(PromotionPotential=value)), expected)

    def test_band_and_list_shaped_details(self):
        job = {"JobGrade": {"Code": "NB"},
               "UserArea": [{"Details": [{"PromotionPotential": "VII"}]}]}
        self.assertEqual(promotion_potential_display(job), "NB-VII")

    def test_no_pay_plan_is_not_assumed_gs(self):
        job = self.job(PromotionPotential="14")
        del job["JobGrade"]
        self.assertEqual(promotion_potential_display(job), "14")


if __name__ == "__main__":
    unittest.main()
