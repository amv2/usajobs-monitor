"""Offline cases for result paging, the senior-executive filter, and near-miss order."""

import unittest
from datetime import date
from unittest import mock

from usajobs_monitor import api, reports, review, tracking
from usajobs_monitor.config import DEFAULT_SCORING as S
from usajobs_monitor.reasons import Code, Reason


def page(ids, total):
    return {"SearchResult": {"SearchResultCountAll": total,
                             "SearchResultItems": [{"MatchedObjectDescriptor": {"PositionID": i}} for i in ids]}}


class PagingTests(unittest.TestCase):
    def test_fetches_until_count_reached(self):
        pages = [page(range(500), 1200), page(range(500, 1000), 1200), page(range(1000, 1200), 1200)]
        with mock.patch.object(api, "fetch_usajobs", side_effect=pages) as fetch:
            items, calls, warning = api.fetch_all_pages({"Keyword": "x"}, "e", "k")
        self.assertEqual((len(items), calls, warning), (1200, 3, ""))
        self.assertEqual([c.args[0]["Page"] for c in fetch.call_args_list], [1, 2, 3])
        self.assertTrue(all(c.args[0]["ResultsPerPage"] == 500 for c in fetch.call_args_list))

    def test_single_page_makes_one_call(self):
        with mock.patch.object(api, "fetch_usajobs", return_value=page(range(39), 39)) as fetch:
            items, calls, warning = api.fetch_all_pages({"Keyword": "x"}, "e", "k")
        self.assertEqual((len(items), calls, warning, fetch.call_count), (39, 1, "", 1))

    def test_empty_page_stops_even_if_count_is_higher(self):
        with mock.patch.object(api, "fetch_usajobs", side_effect=[page(range(500), 900), page([], 900)]):
            items, calls, warning = api.fetch_all_pages({"Keyword": "x"}, "e", "k")
        self.assertEqual((len(items), calls, warning), (500, 2, ""))

    def test_page_cap_reports_truncation(self):
        with mock.patch.object(api, "fetch_usajobs", side_effect=[page(range(500), 5000), page(range(500, 1000), 5000)]):
            items, calls, warning = api.fetch_all_pages({"Keyword": "x", "LocationName": "Bethesda"}, "e", "k", max_pages=2)
        self.assertEqual((len(items), calls), (1000, 2))
        self.assertIn("fetched 1000 of 5000", warning)

    def test_search_dedupes_across_searches_and_counts_pages(self):
        config = {"credentials": {"email": "e", "api_key": "k"},
                  "search": {"radius_miles": 35, "date_posted_days": 14, "salary_query_floor": 1,
                             "target_role_keywords": ["a", "b"], "locations": ["Bethesda"],
                             "include_remote_jobs": True}}
        with mock.patch.object(api, "fetch_usajobs", side_effect=[page([1, 2], 2), page([2, 3], 2),
                                                                page([3, 4], 2), page([], 0)]):
            jobs, calls, warnings = api.search_all_jobs(config)
        self.assertEqual(([j["PositionID"] for j in jobs], calls, warnings), ([1, 2, 3, 4], 4, []))

    SEARCH = {"credentials": {"email": "e", "api_key": "k"},
              "search": {"target_role_keywords": ["a", "b", "c"], "locations": ["Bethesda"]}}

    def test_failed_search_is_skipped_and_named_when_failures_collected(self):
        effects = [page([1], 1), RuntimeError("USAJOBS API returned HTTP 500: secret body"), page([3], 1)]
        failures = []
        with mock.patch.object(api, "fetch_usajobs", side_effect=effects):
            jobs, calls, _ = api.search_all_jobs(self.SEARCH, failures=failures)
        self.assertEqual(([j["PositionID"] for j in jobs], calls), ([1, 3], 2))
        self.assertEqual(failures, ["'b' (Bethesda)"])

    def test_failed_search_raises_without_failures_list(self):
        effects = [page([1], 1), RuntimeError("down"), page([3], 1)]
        with mock.patch.object(api, "fetch_usajobs", side_effect=effects), self.assertRaises(RuntimeError):
            api.search_all_jobs(self.SEARCH)

    def test_more_than_half_failed_fails_the_run(self):
        effects = [RuntimeError("down"), RuntimeError("down"), page([3], 1)]
        with mock.patch.object(api, "fetch_usajobs", side_effect=effects), self.assertRaises(api.SearchFailed):
            api.search_all_jobs(self.SEARCH, failures=[])


class WholeWordTermTests(unittest.TestCase):
    def test_terms_match_whole_words(self):
        cases = [("Processes Clerk", " ses ", False), ("Director, SES Programs", " ses ", True),
                 ("Refinance Analyst", "finance", False), ("Finance Officer", "finance", True),
                 ("Budget Analysts", "budget analyst", True), ("Budget  Analyst", "budget analyst", True),
                 ("Determination Specialist", "term ", False), ("Program Analyst (Term)", "term ", True),
                 ("Financial Analyst", "financ*", True), ("Financial Analyst", "finance", False)]
        for text, term, expected in cases:
            with self.subTest(text=text, term=term):
                self.assertIs(review.contains_any(f" {text.lower()} ", [term]), expected)

    def test_keywords_count_whole_words(self):
        self.assertEqual(review.keyword_score("Refinance the budgets; budgetary review", ["finance", "budget"]), 1)


class SeniorExecutiveTests(unittest.TestCase):
    config = {
        "search": {"target_role_keywords": ["budget analyst"], "target_agencies": []},
        "filters": {"salary_upper_must_exceed": 100, "current_agency_terms": ["ERC"],
                    "temporary_terms": [], "mismatch_terms": [],
                    "senior_executive_terms": ["senior executive service", " ses ", " sl ", " st ",
                                               "executive director", "deputy director", "senior authority"]},
        "eligibility": {"service": "excepted", "has_competitive_status": False,
                        "interchange_agency": "Example Regulatory Commission", "additional_hiring_paths": []},
    }

    def job(self, title="Budget Analyst", pay_plan="GS", qualification=""):
        return {"PositionTitle": title, "OrganizationName": "Smithsonian Institution",
                "JobGrade": [{"Code": pay_plan}], "QualificationSummary": qualification,
                "PositionRemuneration": [{"MaximumRange": "200000"}],
                "UserArea": {"Details": {"HiringPath": ["public"]}}}

    def senior_reasons(self, job):
        return [r for r in review.review_job(job, self.config).reasons
                if r.code in (Code.SENIOR_EXEC_PAY_PLAN, Code.SENIOR_EXEC_TITLE)]

    def test_boilerplate_mention_does_not_exclude(self):
        job = self.job(qualification="Reports to the Deputy Director and the Executive Director for Operations.")
        self.assertEqual(self.senior_reasons(job), [])
        self.assertTrue(review.review_job(job, self.config).kept)

    def test_senior_pay_plans_excluded(self):
        for plan in ["ES", "SL", "ST", "es", "IE", "EP"]:
            with self.subTest(plan=plan):
                self.assertEqual(self.senior_reasons(self.job(pay_plan=plan)),
                                 [f"senior executive pay plan ({plan.upper()})"])

    def test_senior_title_excluded(self):
        self.assertEqual(self.senior_reasons(self.job(title="Deputy Director, Office of Budget")),
                         ["senior executive or director-level title"])

    def test_agency_executive_plans_need_config(self):
        for plan in ["EM", "EO", "LL"]:
            with self.subTest(plan=plan):
                self.assertEqual(self.senior_reasons(self.job(pay_plan=plan)), [])
                config = {**self.config, "filters": {**self.config["filters"], "excluded_pay_plans": ["EM", "EO", "LL"]}}
                reasons = review.review_job(self.job(pay_plan=plan), config).reasons
                self.assertEqual(reasons, [f"excluded pay plan ({plan})"])
                self.assertIs(reasons[0].code, Code.EXCLUDED_PAY_PLAN)

    def test_exclude_senior_executive_can_be_turned_off(self):
        config = {**self.config, "filters": {**self.config["filters"], "exclude_senior_executive": False}}
        self.assertTrue(review.review_job(self.job(pay_plan="ES"), config).kept)
        self.assertTrue(review.review_job(self.job(title="Deputy Director, Budget Analyst Staff"), config).kept)

    def test_non_executive_regulator_plans_pass(self):
        for plan in ["CG", "CM", "SK", "EL"]:
            with self.subTest(plan=plan):
                self.assertEqual(self.senior_reasons(self.job(pay_plan=plan)), [])

    def test_assistant_director_passes_through(self):
        job = self.job(title="Assistant Director, Budget Analyst", pay_plan="SK")
        self.assertEqual(self.senior_reasons(job), [])


class RoleSignalTests(unittest.TestCase):
    config = {
        "search": {"target_role_keywords": ["budget analyst"], "role_title_terms": ["accountant", "financial"],
                   "target_agencies": ["Commodity Futures Trading Commission"]},
        "filters": {"salary_upper_must_exceed": 100, "current_agency_terms": ["ERC"],
                    "temporary_terms": [], "mismatch_terms": [], "senior_executive_terms": []},
        "eligibility": {"service": "excepted", "has_competitive_status": False,
                        "interchange_agency": "Example Regulatory Commission", "additional_hiring_paths": []},
    }

    def job(self, title):
        return {"PositionTitle": title, "OrganizationName": "Commodity Futures Trading Commission",
                "JobGrade": [{"Code": "CT"}], "PositionRemuneration": [{"MaximumRange": "200000"}],
                "UserArea": {"Details": {"HiringPath": ["public"]}}}

    def test_target_agency_alone_is_not_a_role_signal(self):
        result = review.review_job(self.job("Supervisory IT Cybersecurity Specialist"), self.config)
        self.assertFalse(result.kept)
        self.assertEqual(result.reasons, ["no target role signal (title term, job series, or keyword)"])
        self.assertIs(result.reasons[0].code, Code.NO_ROLE_SIGNAL)

    def test_title_term_counts_as_role_signal(self):
        result = review.review_job(self.job("Accountant"), self.config)
        self.assertTrue(result.kept)
        self.assertEqual(result.score, S["title"] + S["target_agency"] + S["above_salary_rule"])

    def test_target_series_counts_as_role_signal(self):
        config = {**self.config, "search": {**self.config["search"], "target_job_series": ["0560"]}}
        job = self.job("Resource Specialist")
        job["JobCategory"] = [{"Code": "0560"}]
        result = review.review_job(job, config)
        self.assertTrue(result.kept)
        self.assertEqual(result.score, S["series"] + S["target_agency"] + S["above_salary_rule"])

    def test_pay_tiers_rank_senior_postings_higher(self):
        config = {**self.config, "search": {**self.config["search"], "target_agencies": []},
                  "filters": {**self.config["filters"], "salary_upper_must_exceed": 197200}}
        scores = {}
        for label, upper in [("gs12", "133142"), ("gs13", "158322"), ("gs15", "197200")]:
            job = self.job("Budget Analyst")
            job["PositionRemuneration"] = [{"MaximumRange": upper}]
            scores[label] = review.review_job(job, config).score
        self.assertEqual(scores["gs13"] - scores["gs12"], S["pay_mid"])
        self.assertEqual(scores["gs15"] - scores["gs12"], S["pay_high"])

    def test_keyword_counts_as_role_signal(self):
        self.assertTrue(review.review_job(self.job("Budget Analyst"), self.config).kept)

    def test_search_only_keyword_in_body_is_not_a_role_signal(self):
        config = {**self.config, "search": {**self.config["search"], "search_only_keywords": ["Human capital"]}}
        job = self.job("Program Manager (Outreach)")
        job["QualificationSummary"] = "Request an alternative application from the Human Capital Office."
        self.assertFalse(review.review_job(job, config).kept)

    def test_search_only_keywords_are_searched(self):
        config = {"credentials": {"email": "e", "api_key": "k"},
                  "search": {"radius_miles": 35, "date_posted_days": 14, "salary_query_floor": 1,
                             "target_role_keywords": ["a"], "search_only_keywords": ["hc"],
                             "locations": ["Bethesda"], "include_remote_jobs": False}}
        with mock.patch.object(api, "fetch_usajobs", side_effect=[page([1], 1), page([2], 1)]) as fetch:
            jobs, calls, _ = api.search_all_jobs(config)
        self.assertEqual([c.args[0]["Keyword"] for c in fetch.call_args_list], ["a", "hc"])
        self.assertEqual((len(jobs), calls), (2, 2))


class NearMissOrderTests(unittest.TestCase):
    def test_hard_no_failures_are_not_near_misses(self):
        hard = [Reason(Code.OCCUPATION_MISMATCH, "obvious non-target occupational mismatch"),
                Reason(Code.SENIOR_EXEC_PAY_PLAN, "senior executive pay plan (ES)"),
                Reason(Code.EXCLUDED_PAY_PLAN, "excluded pay plan (EM)"),
                Reason(Code.SENIOR_EXEC_TITLE, "senior executive or director-level title"),
                Reason(Code.EXCLUDED_SERIES, "excluded occupational/job series 0905 (Attorney)"),
                Reason(Code.NO_ROLE_SIGNAL, "no target role signal (title term, job series, or keyword)")]
        results = [self.result(f"hard {i}", 9, [reason]) for i, reason in enumerate(hard)]
        results.append(self.result("salary only", 1, [Reason(Code.SALARY, "posted upper salary $187,093 does not exceed $197,200")]))
        results.append(self.result("internal only", 7, [Reason(Code.INTERNAL_OTHER_AGENCY, "internal-to-agency posting outside current agency")]))
        results.append(self.result("no route", 6, [Reason(Code.NO_HIRING_ROUTE, "no supported hiring route for excepted-service profile")]))
        results.append(self.result("temporary", 6, [Reason(Code.TEMPORARY, "temporary, term, NTE, or detail appointment (Term)")]))
        self.assertEqual([r.job["PositionTitle"] for r in tracking.near_misses(results)], ["no route", "temporary", "salary only"])

    def result(self, title, score, reasons):
        return review.ReviewResult(job={"PositionTitle": title}, kept=False, reasons=reasons, score=score)

    def test_fewest_failed_checks_first(self):
        results = [self.result("agency hit, three failures", 9, ["a", "b", "c"]),
                   self.result("salary only", 1, ["salary"]),
                   self.result("salary only, higher score", 3, ["salary"])]
        self.assertEqual([r.job["PositionTitle"] for r in tracking.near_misses(results)],
                         ["salary only, higher score", "salary only", "agency hit, three failures"])

    def test_reason_text(self):
        self.assertEqual(tracking.near_miss_reason_text(self.result("t", 1, ["salary"])), "Only failed check: salary")
        self.assertEqual(tracking.near_miss_reason_text(self.result("t", 1, ["a", "b"])), "2 failed checks: a; b")



class ExcludedSeriesTests(unittest.TestCase):
    config = {
        "search": {"target_role_keywords": [], "role_title_terms": ["accountant", "budget"], "target_agencies": []},
        "filters": {"salary_upper_must_exceed": 100, "current_agency_terms": ["ERC"], "temporary_terms": [],
                    "mismatch_terms": [], "senior_executive_terms": [], "excluded_job_series": ["0905", "0510", "0511"]},
        "eligibility": {"service": "excepted", "has_competitive_status": False,
                        "interchange_agency": "Example Regulatory Commission", "additional_hiring_paths": []},
    }

    def review(self, title, *series):
        job = {"PositionTitle": title, "OrganizationName": "Agency", "JobCategory": [{"Code": c} for c in series],
               "PositionRemuneration": [{"MaximumRange": "200000"}], "UserArea": {"Details": {"HiringPath": ["public"]}}}
        return review.review_job(job, self.config)

    def test_accounting_series_excluded(self):
        for series in ["0510", "0511"]:
            with self.subTest(series=series):
                result = self.review("Accountant", series)
                self.assertFalse(result.kept)
                self.assertIn(f"excluded occupational/job series {series}", result.reasons)

    def test_mixed_series_posting_kept(self):
        self.assertTrue(self.review("Budget and Accounting Analyst", "0501", "0510").kept)


class MismatchTests(unittest.TestCase):
    terms = ["physician", "nurse", "pilot", "clinical", "law enforcement"]

    def job(self, title, series="0501", body=""):
        return {"PositionTitle": title, "JobCategory": [{"Code": series}], "QualificationSummary": body}

    def test_boilerplate_does_not_trigger(self):
        body = ("Use of ChatGPT or Copilot is prohibited. Careers range from doctors to nurses. "
                "Excepted service pilot review. Documentation from a physician. Federal law enforcement agency.")
        self.assertFalse(review.occupational_mismatch(self.job("Financial Management Analyst", body=body), self.terms, ["0610"]))

    def test_title_whole_words_and_series(self):
        self.assertTrue(review.occupational_mismatch(self.job("Registered Nurse - Transplant"), self.terms, []))
        self.assertTrue(review.occupational_mismatch(self.job("Law Enforcement Specialist (Instructor)"), self.terms, []))
        self.assertFalse(review.occupational_mismatch(self.job("Copilot Program Analyst"), self.terms, []))
        self.assertTrue(review.occupational_mismatch(self.job("Senior Operational Medicine Advisor", series="0602"), self.terms, ["0602"]))


class AppointmentTypeTests(unittest.TestCase):
    terms = ["temporary appointment", "not to exceed", "not-to-exceed", " nte ", "time-limited", "detail assignment"]

    def job(self, title="Budget Analyst", codes=("15317",), name=""):
        return {"PositionTitle": title,
                "PositionOfferingType": [{"Code": code, "Name": name} for code in codes],
                "QualificationSummary": "Resume not to exceed 2 pages; latest non-temporary appointment; time-limited appointment."}

    def test_permanent_code_ignores_boilerplate(self):
        self.assertIsNone(review.appointment_type_reason(self.job(), self.terms))

    def test_non_permanent_codes_excluded(self):
        self.assertEqual(review.appointment_type_reason(self.job(codes=("15319",)), self.terms),
                         "temporary, term, NTE, or detail appointment (Term)")
        self.assertIn("Detail", review.appointment_type_reason(self.job(codes=("15320",)), self.terms))

    def test_title_marks_temporary_even_if_permanent_code(self):
        self.assertIn("(title)", review.appointment_type_reason(self.job(title="Budget Analyst (Temporary NTE 1 Year)"), self.terms))

    def test_unknown_code_falls_back_to_label_not_body(self):
        self.assertIsNone(review.appointment_type_reason(self.job(codes=("15327",)), self.terms))
        self.assertIn("temporary", review.appointment_type_reason(self.job(codes=("",), name="Temporary NTE 1 year"), self.terms))


def result(title, reasons, *, number, plan="GS", low="15", high="15", close="2026-12-31", score=3, kept=None):
    job = {"PositionTitle": title, "OrganizationName": "Agency", "PositionURI": f"https://www.usajobs.gov/job/{number}",
           "JobGrade": [{"Code": plan}], "ApplicationCloseDate": f"{close}T23:59:59.9970",
           "UserArea": {"Details": {"LowGrade": low, "HighGrade": high}}}
    return review.ReviewResult(job=job, kept=not reasons if kept is None else kept, reasons=reasons, score=score)


SALARY = Reason(Code.SALARY, "posted upper salary $197,200 does not exceed $197,200")
WATCH_CONFIG = {"filters": {"watch_list": {"pay_plans": ["GS", "GG"], "grades": ["15"]}}}


class WatchListTests(unittest.TestCase):
    def test_grade_15_salary_only_is_watch(self):
        for plan, low, high in [("GS", "15", "15"), ("GG", "14", "15")]:
            with self.subTest(plan=plan, low=low):
                self.assertTrue(tracking.is_watch_candidate(result("t", [SALARY], number=1, plan=plan, low=low, high=high), WATCH_CONFIG))

    def test_not_watch(self):
        cases = [result("gs14", [SALARY], number=1, low="14", high="14"),
                 result("two reasons", [SALARY, "internal-to-agency posting outside current agency"], number=2),
                 result("other plan", [SALARY], number=3, plan="NH"),
                 result("kept", [], number=4)]
        for case in cases:
            with self.subTest(case=case.job["PositionTitle"]):
                self.assertFalse(tracking.is_watch_candidate(case, WATCH_CONFIG))

    def test_watch_items_are_not_near_misses(self):
        watch = result("watch", [SALARY], number=1)
        near = result("near", [Reason(Code.SALARY, "posted upper salary $150,000 does not exceed $197,200")], number=2, low="13", high="13")
        view = tracking.build_view([watch, near], WATCH_CONFIG, None, {}, date(2026, 10, 16), "run-1")
        self.assertEqual([r.job["PositionTitle"] for r in view.watch], ["watch"])
        self.assertEqual([r.job["PositionTitle"] for r in view.near], ["near"])


class NearMissPayFloorTests(unittest.TestCase):
    def test_gs14_and_below_not_near_misses(self):
        config = {"filters": {"watch_list": {"pay_plans": ["GS", "GG"], "grades": ["15"]},
                              "near_miss_upper_salary_must_exceed": 187093, "near_miss_min_gs_grade": 15}}
        def near(title, upper, reason):
            code = Code.SALARY if reason.startswith("posted upper salary") else Code.NO_HIRING_ROUTE
            r = result(title, [Reason(code, reason)], number=abs(hash(title)) % 10**9, low="14", high="14")
            r.job["PositionRemuneration"] = [{"MaximumRange": upper}]
            return r
        results = [near("gs14 lateral", "187093", "posted upper salary $187,093 does not exceed $197,200"),
                   near("gs12", "133142", "posted upper salary $133,142 does not exceed $197,200"),
                   near("nh4 band", "197200", "posted upper salary $197,200 does not exceed $197,200"),
                   near("above cap, no route", "220000", "no supported hiring route for ERC excepted-service profile"),
                   near("gg14 special rate", "196689", "posted upper salary $196,689 does not exceed $197,200")]
        results[2].job["JobGrade"] = [{"Code": "NH"}]
        results[2].job["UserArea"]["Details"].update(LowGrade="4", HighGrade="4")
        results[3].job["JobGrade"] = [{"Code": "SK"}]
        results[4].job["JobGrade"] = [{"Code": "GG"}]
        view = tracking.build_view(results, config, None, {}, date(2026, 10, 17), "run")
        self.assertEqual(sorted(r.job["PositionTitle"] for r in view.near), ["above cap, no route", "nh4 band"])


class HistoryAndDecisionTests(unittest.TestCase):
    today = date(2026, 10, 16)

    def view(self, results, history=None, decisions=None, marker="run-2"):
        return tracking.build_view(results, WATCH_CONFIG, history, decisions or {}, self.today, marker)

    def test_baseline_flags_nothing_new(self):
        view = self.view([result("m", [], number=1)])
        self.assertTrue(view.baseline)
        self.assertEqual(view.flags["1"], [])
        self.assertEqual(view.history["jobs"]["1"]["first_seen"], "2026-10-16")

    def test_new_closing_and_dropped(self):
        first = self.view([result("old match", [], number=1), result("old near", [SALARY], number=2, low="13", high="13"),
                           result("closed watch", [SALARY], number=3, close="2026-10-15")], marker="run-1")
        second = self.view([result("old match", [], number=1, close="2026-10-18"), result("new match", [], number=4)],
                           history=first.history)
        self.assertEqual(second.flags["1"], ["CLOSING SOON"])
        self.assertEqual(second.flags["4"], ["NEW"])
        self.assertEqual(second.new_count(second.matches), 1)
        dropped = {d["control_number"]: d["reason"] for d in second.dropped}
        self.assertEqual(dropped, {"2": "no longer listed", "3": "closed"})
        self.assertEqual(second.history["jobs"]["1"]["first_seen"], "2026-10-16")
        third = self.view([result("old match", [], number=1)], history=second.history, marker="run-3")
        self.assertEqual({d["control_number"] for d in third.dropped}, {"4"})

    def test_incomplete_run_reports_nothing_dropped_and_keeps_tracking(self):
        first = self.view([result("a", [], number=1), result("b", [], number=2)], marker="run-1")
        # Job 2 is missing only because its search failed.
        partial = tracking.build_view([result("a", [], number=1)], WATCH_CONFIG, first.history, {}, self.today,
                                      "run-2", ["'b' (Bethesda)"])
        self.assertEqual(partial.dropped, [])
        self.assertEqual(partial.failed_searches, ["'b' (Bethesda)"])
        self.assertEqual(partial.history["jobs"]["2"]["last_seen_run"], "run-2")
        # Back after a full run: not NEW. Gone after a full run: reported dropped.
        back = self.view([result("a", [], number=1), result("b", [], number=2)], history=partial.history,
                         marker="run-3")
        self.assertEqual(back.flags["2"], [])
        gone = self.view([result("a", [], number=1)], history=partial.history, marker="run-3")
        self.assertEqual({d["control_number"] for d in gone.dropped}, {"2"})

    def test_decisions_hide_and_flag(self):
        decisions = {"1": {"status": "not_interested", "note": ""}, "2": {"status": "applied", "note": "10-15"}}
        view = self.view([result("dismissed", [], number=1), result("applied", [], number=2)], decisions=decisions)
        self.assertEqual([r.job["PositionTitle"] for r in view.matches], ["applied"])
        self.assertEqual(len(view.hidden), 1)
        self.assertEqual(view.flags["2"], ["APPLIED"])
        self.assertEqual(view.your_status(view.matches[0]), "applied (10-15)")
        self.assertEqual(view.history["jobs"]["1"]["list"], "hidden")

    def test_rejected_and_withdrawn_hide_and_application_statuses_flag(self):
        decisions = {"1": {"status": "rejected", "note": ""}, "2": {"status": "withdrawn", "note": ""},
                     "3": {"status": "interview", "note": "", "applied_on": "2026-09-01"},
                     "4": {"status": "offer", "note": "GS-14"}}
        view = self.view([result(str(n), [], number=n) for n in (1, 2, 3, 4)], decisions=decisions)
        self.assertEqual(sorted(r.job["PositionTitle"] for r in view.hidden), ["1", "2"])
        self.assertEqual(view.flags["3"], ["INTERVIEW"])
        self.assertEqual(view.flags["4"], ["OFFER"])
        by_title = {r.job["PositionTitle"]: r for r in view.matches}
        self.assertEqual(view.your_status(by_title["3"]), "interview (applied 2026-09-01)")
        self.assertEqual(view.your_status(by_title["4"]), "offer (GS-14)")
        self.assertEqual(view.history["jobs"]["1"]["list"], "hidden")

    def test_load_decisions(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "job_decisions.csv"
            path.write_text("control_number,status,note,updated\n886889900,Not Interested,,2026-10-16\n"
                            "123,applied,sent,\nabc,applied,,\n456,maybe,,\n,,,\n", encoding="utf-8")
            decisions, warnings = tracking.load_decisions(path)
        self.assertEqual({n: (d["status"], d["note"]) for n, d in decisions.items()},
                         {"886889900": ("not_interested", ""), "123": ("applied", "sent")})
        self.assertEqual(len(warnings), 2)
        self.assertEqual(tracking.load_decisions(Path("/nonexistent/x.csv")), ({}, []))



class ResumeNoteTests(unittest.TestCase):
    def test_note_counts_files_and_omits_paths(self):
        from pathlib import Path
        note = reports.resume_note([Path("/private/resumes/a.pdf"), Path("/private/resumes/b.pdf")])
        self.assertIn("2 on file", note)
        self.assertNotIn("/private", note)
        self.assertIn("none found", reports.resume_note([]))

if __name__ == "__main__":
    unittest.main()
