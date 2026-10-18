"""Offline regression cases for the agency-only exclusion."""

import unittest

from usajobs_monitor.reasons import Code
from usajobs_monitor.review import eligibility_screen, is_internal_other_agency, review_job


class EligibilityTests(unittest.TestCase):
    terms = ["ERC", "Example Regulatory Commission"]
    profile = {"service": "excepted", "has_competitive_status": False,
               "interchange_agency": "Example Regulatory Commission", "additional_hiring_paths": []}

    def screen(self, job, **profile_changes):
        return eligibility_screen(job, {"filters": {"current_agency_terms": self.terms},
                                       "eligibility": {**self.profile, **profile_changes}})

    def job(self, paths=None, agency="Federal Deposit Insurance Corporation", **detail):
        if paths is not None:
            detail["HiringPath"] = paths
        return {
            "PositionTitle": "Senior Financial Analyst, CG-1160-13/14",
            "OrganizationName": agency,
            "PositionRemuneration": [{"MaximumRange": "257500"}],
            "UserArea": {"Details": detail},
        }

    def excluded(self, job):
        return is_internal_other_agency(job, self.terms)

    def test_fdic_regression_passes_all_filters(self):
        job = self.job(
            ["disability", "fed-competitive", "fed-transition", "land", "mspouse",
             "overseas", "peace", "special-authorities", "vet"],
            HiringPathDisplay=["Competitive service", "Career transition (CTAP, ICTAP, RPL)"],
            RequiredDocuments="Competitive service SF-50; interagency career transition assistance.",
        )
        config = {
            "eligibility": self.profile,
            "filters": {"salary_upper_must_exceed": 180000, "current_agency_terms": self.terms,
                        "temporary_terms": [], "senior_executive_terms": [], "mismatch_terms": []},
            "search": {"target_role_keywords": ["financial"], "target_agencies": ["Federal Deposit Insurance Corporation"]},
        }
        result = review_job(job, config)
        self.assertTrue(result.kept)
        self.assertEqual(result.reasons, [])
        self.assertEqual(result.score, 8)

    def test_other_agency_internal_only_excluded(self):
        self.assertTrue(self.excluded(self.job(["fed-internal-search"])))

    def test_internal_with_transition_priority_excluded(self):
        self.assertTrue(self.excluded(self.job(["fed-internal-search", "fed-transition"])))

    def test_current_agency_internal_retained(self):
        for agency in self.terms:
            with self.subTest(agency=agency):
                self.assertFalse(self.excluded(self.job(["fed-internal-search"], agency)))

    def test_alternative_hiring_paths_retained(self):
        for path in ["special-authorities", "fed-competitive", "fed-excepted", "public", "vet", "future-path"]:
            with self.subTest(path=path):
                self.assertFalse(self.excluded(self.job(["fed-internal-search", path])))

    def test_transition_alone_is_not_internal(self):
        self.assertFalse(self.excluded(self.job(["fed-transition"])))

    def test_incidental_agency_mentions_do_not_override_employer(self):
        self.assertTrue(self.excluded(self.job(["fed-internal-search"], RequiredDocuments="ERC employees submit SF-50.")))
        self.assertTrue(self.excluded(self.job(["fed-internal-search"], "AERCB")))

    def test_missing_metadata_does_not_infer_restriction_from_prose(self):
        self.assertFalse(self.excluded(self.job(RequiredDocuments="Internal to an agency; status candidates; competitive service.")))

    def test_label_fallback(self):
        self.assertTrue(self.excluded(self.job(HiringPathDisplay=["Internal to an agency"])))
        self.assertFalse(self.excluded(self.job(HiringPathDisplay=["Internal to an agency", "Special authorities"])))
        self.assertFalse(self.excluded(self.job(HiringPathDisplay=["Competitive service", "Career transition (CTAP, ICTAP, RPL)"])))

    def test_legacy_who_may_apply(self):
        self.assertTrue(self.excluded(self.job(WhoMayApply={"Name": "Agency employees only"})))
        self.assertFalse(self.excluded(self.job(WhoMayApply={"Name": "Status candidates"})))

    def test_codes_take_precedence_over_stale_display(self):
        self.assertFalse(self.excluded(self.job(["special-authorities"], HiringPathDisplay=["Internal to an agency"])))

    def test_normalization_and_string_path(self):
        self.assertTrue(self.excluded(self.job(" FED-INTERNAL-SEARCH ")))

    def test_missing_employer_is_not_known_other_agency(self):
        self.assertFalse(self.excluded(self.job(["fed-internal-search"], "")))

    def test_competitive_service_alone_is_not_interchange_eligibility(self):
        basis, reason = self.screen(self.job(["fed-competitive", "fed-transition"]))
        self.assertFalse(basis)
        self.assertIn("no supported hiring route", reason)
        self.assertIs(reason.code, Code.NO_HIRING_ROUTE)
        self.assertIn("Example Regulatory Commission excepted-service profile", reason)

    def test_dla_mixed_paths_do_not_establish_personal_eligibility(self):
        basis, reason = self.screen(self.job(["fed-internal-search", "fed-transition", "vet", "mspouse", "disability"], "Defense Logistics Agency"))
        self.assertFalse(basis)
        self.assertTrue(reason)

    def test_sec_explicit_interchange_instructions_without_special_badge(self):
        job = self.job(["fed-competitive", "fed-transition", "disability", "land", "mspouse", "peace", "vet"],
                       RequiredDocuments="Note: If you are in the excepted service, your current agency must have an interchange agreement with OPM - for a list of agencies with current interchange agreements please click here.")
        basis, reason = self.screen(job)
        self.assertIn("explicitly permits OPM interchange", basis)
        self.assertIn("RequiredDocuments", basis)
        self.assertFalse(reason)

    def test_generic_interchange_mention_is_not_acceptance(self):
        for wording in ["Learn about interchange agreements.", "Interchange candidates are not eligible.",
                        "Candidates eligible under an interchange agreement may not apply."]:
            with self.subTest(wording=wording):
                basis, reason = self.screen(self.job(["fed-competitive"], RequiredDocuments=wording))
                self.assertFalse(basis)
                self.assertTrue(reason)

    def test_interchange_denial_overrides_special_authorities_badge(self):
        basis, reason = self.screen(self.job(["special-authorities"], OtherInformation="Interchange candidates are not eligible."))
        self.assertFalse(basis)
        self.assertTrue(reason)

    def test_special_authority_has_conditional_interchange_basis(self):
        basis, reason = self.screen(self.job(["special-authorities"]))
        self.assertIn("potential Example Regulatory Commission interchange route", basis)
        self.assertFalse(reason)

    def test_special_authority_match_is_flagged_for_verification(self):
        from datetime import date
        from usajobs_monitor.tracking import build_view
        config = {"eligibility": self.profile,
                  "filters": {"salary_upper_must_exceed": 180000, "current_agency_terms": self.terms,
                              "temporary_terms": [], "senior_executive_terms": [], "mismatch_terms": []},
                  "search": {"target_role_keywords": ["financial"], "target_agencies": []}}
        special = review_job({**self.job(["special-authorities"]), "PositionID": "1"}, config)
        public = review_job({**self.job(["public"]), "PositionID": "2"}, config)
        self.assertTrue(special.kept and public.kept)
        self.assertIn("Schedule A", special.eligibility_basis)
        view = build_view([special, public], config, None, {}, date(2026, 10, 17), "run")
        self.assertIn("VERIFY ELIGIBILITY", view.flags["1"])
        self.assertNotIn("VERIFY ELIGIBILITY", view.flags["2"])

    def test_other_eligibilities_not_assumed(self):
        for path in ["vet", "disability", "mspouse", "land", "peace", "fed-transition", "future-path"]:
            with self.subTest(path=path):
                basis, reason = self.screen(self.job([path]))
                self.assertFalse(basis)
                self.assertTrue(reason)

    def test_supported_routes(self):
        for paths, agency in [(["public"], "FDIC"), (["fed-excepted"], "FDIC"), (["fed-internal-search"], "ERC")]:
            with self.subTest(paths=paths):
                basis, reason = self.screen(self.job(paths, agency))
                self.assertTrue(basis)
                self.assertFalse(reason)

    def test_explicitly_configured_other_status(self):
        self.assertTrue(self.screen(self.job(["vet"]), additional_hiring_paths=["vet"])[0])
        self.assertTrue(self.screen(self.job(["fed-competitive"]), has_competitive_status=True)[0])

    def test_unknown_metadata_requires_review_not_match(self):
        self.assertIn("eligibility review required", self.screen(self.job())[1])
        self.assertIs(self.screen(self.job())[1].code, Code.ELIGIBILITY_UNKNOWN)
        self.assertIn("eligibility review required", self.screen(self.job(HiringPathDisplay=["Unknown label"]))[1])

    def test_agency_terms_read_from_profile_section(self):
        config = {"profile": {"current_agency_terms": self.terms}, "eligibility": self.profile}
        basis, reason = eligibility_screen(self.job(["fed-internal-search"], "ERC"), config)
        self.assertTrue(basis)
        self.assertIsNone(reason)

    def test_internal_other_agency_is_hard_no_code(self):
        config = {
            "eligibility": self.profile,
            "filters": {"salary_upper_must_exceed": 0, "current_agency_terms": self.terms,
                        "temporary_terms": [], "senior_executive_terms": [], "mismatch_terms": []},
            "search": {"target_role_keywords": ["financial"], "target_agencies": []},
        }
        result = review_job(self.job(["fed-internal-search"]), config)
        self.assertEqual([r.code for r in result.reasons], [Code.INTERNAL_OTHER_AGENCY])

    def test_generic_profile_wording_without_agency(self):
        basis, reason = self.screen(self.job(["fed-competitive"]), service="none", interchange_agency="")
        self.assertIn("no supported hiring route for applicant profile", reason)


if __name__ == "__main__":
    unittest.main()
