"""Applicant-type presets: what they fill in, what they leave alone, and their effect on screening."""

import copy
import unittest

from usajobs_monitor.config import ConfigError, validate_config
from usajobs_monitor.presets import HIRING_PATHS, PRESETS
from usajobs_monitor.reports import active_filters_text
from usajobs_monitor.review import eligibility_screen, hiring_paths, review_job

MINIMAL = {"search": {"locations": ["Denver, Colorado"], "target_role_keywords": ["budget analyst"]}}


def config(**eligibility):
    return validate_config({**MINIMAL, "eligibility": eligibility})[0]


def job(*paths, display=None):
    details = {"HiringPath": list(paths)} if paths else {}
    if display:
        details["HiringPathDisplay"] = display
    return {"PositionTitle": "Budget Analyst", "OrganizationName": "Department of Example",
            "PositionRemuneration": [{"MaximumRange": "150000"}], "UserArea": {"Details": details}}


class PresetConfigTests(unittest.TestCase):
    def test_no_presets_leaves_eligibility_exactly_as_before(self):
        cfg = config(service="competitive", has_competitive_status=True)
        self.assertEqual(cfg["eligibility"], {"service": "competitive", "has_competitive_status": True,
                                              "interchange_agency": "", "additional_hiring_paths": []})

    def test_each_preset_validates_and_uses_known_hiring_paths(self):
        for name, preset in PRESETS.items():
            with self.subTest(name):
                cfg = config(presets=[name])
                self.assertTrue(set(preset.hiring_paths) <= set(HIRING_PATHS))
                self.assertTrue(preset.sources)
                self.assertEqual(cfg["eligibility"]["presets"], [name])

    def test_presets_combine(self):
        cfg = config(presets=["excepted-fed", "veteran", "military-spouse"])["eligibility"]
        self.assertEqual(cfg["service"], "excepted")
        self.assertFalse(cfg["has_competitive_status"])
        self.assertEqual(cfg["additional_hiring_paths"], ["vet", "mspouse"])

    def test_explicit_settings_win_and_paths_are_added(self):
        cfg = config(presets=["reinstatement", "veteran"], has_competitive_status=False,
                     additional_hiring_paths=["nguard"])["eligibility"]
        self.assertFalse(cfg["has_competitive_status"])
        self.assertEqual(cfg["additional_hiring_paths"], ["nguard", "vet"])
        self.assertEqual(cfg["resolved_presets"], {"service": "none", "additional_hiring_paths": ["vet"]})

    def test_names_are_forgiving_but_unknown_ones_fail(self):
        self.assertEqual(config(presets="Military_Spouse")["eligibility"]["presets"], ["military-spouse"])
        with self.assertRaises(ConfigError) as caught:
            config(presets=["veterans"])
        self.assertIn("eligibility.presets[0]", str(caught.exception))

    def test_unknown_hiring_path_code_warns(self):
        _, warnings = validate_config({**MINIMAL, "eligibility": {"additional_hiring_paths": ["veteran"]}})
        self.assertTrue(any("additional_hiring_paths[0]" in w for w in warnings))

    def test_preset_named_in_active_filters(self):
        lines = active_filters_text(config(presets=["military-spouse"]), "d.csv")
        self.assertIn("Applicant type: Military spouse.", "\n".join(lines))


class PresetScreeningTests(unittest.TestCase):
    def screen(self, posting, *presets, **extra):
        return eligibility_screen(posting, config(presets=list(presets), **extra))

    def test_public_only_takes_public_postings(self):
        self.assertEqual(self.screen(job("public"), "public")[0], "Open to the public")
        self.assertIsNotNone(self.screen(job("fed-competitive"), "public")[1])
        self.assertIsNotNone(self.screen(job("vet"), "public")[1])

    def test_competitive_fed_and_reinstatement_take_merit_promotion(self):
        for preset in ("competitive-fed", "reinstatement"):
            basis, reason = self.screen(job("fed-competitive"), preset)
            self.assertIsNone(reason, preset)
            self.assertIn("Competitive status", basis)

    def test_excepted_fed_takes_excepted_but_not_merit_promotion_without_interchange(self):
        self.assertIsNone(self.screen(job("fed-excepted"), "excepted-fed")[1])
        self.assertIsNotNone(self.screen(job("fed-competitive"), "excepted-fed")[1])
        basis, reason = self.screen(job("fed-competitive", "special-authorities"), "excepted-fed",
                                    interchange_agency="Example Commission")
        self.assertIsNone(reason)
        self.assertIn("interchange", basis)

    def test_special_path_presets_match_their_codes_only(self):
        cases = {"veteran": "vet", "military-spouse": "mspouse", "student": "student",
                 "recent-graduate": "graduates", "disability": "disability"}
        for preset, code in cases.items():
            with self.subTest(preset):
                self.assertIsNone(self.screen(job(code), preset)[1])
                self.assertIsNotNone(self.screen(job("fed-competitive"), preset)[1])
                other = next(c for c in cases.values() if c != code)
                self.assertIsNotNone(self.screen(job(other), preset)[1])

    def test_display_names_map_to_codes_when_codes_are_missing(self):
        self.assertEqual(hiring_paths(job(display=["Veterans", "Military spouses"])), {"vet", "mspouse"})
        self.assertIsNone(self.screen(job(display=["Military spouses"]), "military-spouse")[1])

    def test_preset_and_hand_written_eligibility_give_identical_reviews(self):
        postings = [job(*paths) for paths in (["public"], ["fed-competitive"], ["vet"], ["fed-excepted"],
                                              ["fed-internal-search"], [])]
        by_preset = config(presets=["competitive-fed", "veteran"])
        by_hand = config(service="competitive", has_competitive_status=True, additional_hiring_paths=["vet"])
        for posting in postings:
            a, b = review_job(copy.deepcopy(posting), by_preset), review_job(copy.deepcopy(posting), by_hand)
            self.assertEqual((a.kept, [str(r) for r in a.reasons], a.score, a.eligibility_basis),
                             (b.kept, [str(r) for r in b.reasons], b.score, b.eligibility_basis))


if __name__ == "__main__":
    unittest.main()
