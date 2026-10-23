"""Offline checks for the MCP tools, the server wrapper, run integration of fit notes, and the fit command."""

import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from usajobs_monitor import api, cli, fit, runner, tools
from usajobs_monitor.config import load_settings

PROFILE = {"search": {"locations": ["Denver, Colorado"], "target_role_keywords": ["budget analyst"],
                      "role_title_terms": ["budget"]},
           "filters": {"salary_upper_must_exceed": 100000},
           "eligibility": {"presets": ["public"]}}
ENV = {"USAJOBS_EMAIL": "me@example.com", "USAJOBS_API_KEY": "key"}


def job(number, title="Budget Analyst", top="150000", paths=("public",)):
    return {"PositionTitle": title, "OrganizationName": "Example Agency",
            "PositionURI": f"https://www.usajobs.gov/job/{number}", "JobGrade": [{"Code": "GS"}],
            "PositionRemuneration": [{"MinimumRange": "80000", "MaximumRange": top}],
            "UserArea": {"Details": {"HiringPath": list(paths), "WhoMayApply": {"Name": "Open"}}}}


def payload(*jobs):
    return {"SearchResult": {"SearchResultCountAll": len(jobs),
                             "SearchResultItems": [{"MatchedObjectDescriptor": j} for j in jobs]}}


class ToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.settings = load_settings(self.base / "p.toml", env=ENV, raw=PROFILE)

    def fetch(self, *jobs):
        return mock.patch.object(api, "fetch_usajobs", return_value=payload(*jobs))

    def test_search_jobs_sorts_by_list_and_hides_hard_fails(self):
        jobs = [job("1", top="90000"), job("2"), job("3", title="IT Specialist")]
        with self.fetch(*jobs) as fetch:
            out = tools.search_jobs(self.settings, keyword="accountant", location="Boise, Idaho")
        params = fetch.call_args.args[0]
        self.assertEqual((params["Keyword"], params["LocationName"]), ("accountant", "Boise, Idaho"))
        self.assertEqual([r["control_number"] for r in out["results"]], ["2", "1"])
        self.assertEqual([r["tier"] for r in out["results"]], ["match", "near_miss"])
        self.assertEqual(out["counts"], {"match": 1, "near_miss": 1, "excluded": 1})

    def test_search_keyword_counts_as_role_signal(self):
        with self.fetch(job("4", title="Accountant")):
            out = tools.search_jobs(self.settings, keyword="accountant")
        self.assertEqual(out["results"][0]["tier"], "match")

    def test_search_without_keyword_runs_profile_searches_and_limits(self):
        with self.fetch(job("1"), job("2")) as fetch:
            out = tools.search_jobs(self.settings, include_excluded=True, limit=1)
        self.assertEqual(fetch.call_args.args[0]["Keyword"], "budget analyst")
        self.assertEqual((len(out["results"]), out["truncated"]), (1, True))

    def test_hidden_decisions_are_respected(self):
        self.settings.decisions_path.write_text("control_number,status,note\n2,not_interested,\n")
        with self.fetch(job("2")):
            out = tools.search_jobs(self.settings)
        self.assertEqual((out["results"], out["counts"]), ([], {"hidden": 1}))

    def test_posting_lookup_keeps_only_that_control_number(self):
        # A keyword search for the number can bring back other postings too.
        with self.fetch(job("7"), job("812345600"), job("8")):
            out = tools.screen_eligibility(self.settings, "812345600")
        self.assertEqual(out["eligibility_basis"], "Open to the public")
        with self.fetch(job("7"), job("8")), self.assertRaises(tools.ToolInputError) as caught:
            tools.screen_eligibility(self.settings, "812345600")
        self.assertIn("No open USAJOBS posting has control number 812345600", str(caught.exception))

    def test_screen_eligibility(self):
        with self.fetch(job("5")) as fetch:
            out = tools.screen_eligibility(self.settings, "5")
        self.assertEqual(fetch.call_args.args[0], {"Keyword": "5", "ResultsPerPage": 500})
        self.assertEqual((out["verdict"], out["eligibility_basis"]), ("route_found", "Open to the public"))
        with self.fetch(job("6", paths=("fed-competitive",))):
            out = tools.screen_eligibility(self.settings, "6")
        self.assertEqual(out["verdict"], "no_hiring_route")
        self.assertIn("competitive status", out["reason"])

    def test_explain_match_lists_checks_and_score(self):
        with self.fetch(job("7", top="90000")):
            out = tools.explain_match(self.settings, "7")
        self.assertEqual(out["tier"], "near_miss")
        self.assertTrue(out["headline"].startswith("Near miss. Only failed check: posted upper salary"))
        self.assertEqual(out["failed_checks"][0]["code"], "salary")
        self.assertFalse(out["failed_checks"][0]["hard_no"])
        self.assertEqual(sum(p["points"] for p in out["score_parts"]), out["score"])

    def test_unknown_control_number(self):
        with self.fetch(job("8")):
            with self.assertRaises(tools.ToolInputError):
                tools.explain_match(self.settings, "9")
        with self.assertRaises(tools.ToolInputError):
            tools.explain_match(self.settings, "not-a-number")


@unittest.skipUnless(importlib.util.find_spec("mcp"), "mcp not installed")
class ServerTests(unittest.TestCase):
    def setUp(self):
        from usajobs_monitor import mcp_server
        self.server = mcp_server.build_server(lambda profile: f"settings:{profile}")

    def test_lists_three_read_only_tools(self):
        listed = asyncio.run(self.server.list_tools())
        self.assertEqual(sorted(t.name for t in listed), ["explain_match", "screen_eligibility", "search_jobs"])

    def test_calls_reach_the_tool_functions(self):
        with mock.patch.object(tools, "explain_match", return_value={"tier": "match"}) as explain:
            asyncio.run(self.server.call_tool("explain_match", {"control_number": "1", "profile": "alex"}))
        self.assertEqual(explain.call_args.args, ("settings:alex", "1"))

    def test_errors_become_tool_errors_without_internal_details(self):
        with mock.patch.object(tools, "screen_eligibility", side_effect=KeyError("api key xyz")):
            with self.assertRaises(Exception) as caught:
                asyncio.run(self.server.call_tool("screen_eligibility", {"control_number": "1"}))
        self.assertIn("KeyError", str(caught.exception))
        self.assertNotIn("xyz", str(caught.exception))


class RunIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "resume.md").write_text("Budget analyst.")

    def settings(self, **ai):
        raw = {**PROFILE, "report": {"formats": ["csv"]},
               "ai": {"fit_notes": True, "send_resume": True, "resume": "resume.md", **ai}}
        return load_settings(self.base / "p.toml", env=ENV, raw=raw)

    def test_run_publishes_fit_notes_and_cache(self):
        settings = self.settings()
        answer = json.dumps({"fit": "strong", "summary": "Yes.", "strengths": [], "gaps": [], "resume_tips": [],
                             "questions_to_verify": []})
        provider = mock.Mock(complete=mock.Mock(return_value=answer), label="fake")
        with mock.patch.object(api, "fetch_usajobs", return_value=payload(job("1"))), \
                mock.patch.object(fit, "make_provider", return_value=provider):
            code, outcome = runner.run(settings)
        self.assertEqual(code, 0)
        summary = json.loads((settings.reports_dir / runner.SUMMARY).read_text())
        self.assertEqual(summary["fit_notes"]["status"], "ok")
        self.assertIn("**Fit: Strong.**", (settings.reports_dir / fit.FIT_NOTES_NAME).read_text())
        self.assertTrue((settings.reports_dir / fit.FIT_CACHE_NAME).is_file())

    def test_fit_failure_never_fails_the_run(self):
        settings = self.settings(resume="missing.md")
        with mock.patch.object(api, "fetch_usajobs", return_value=payload(job("1"))):
            code, _ = runner.run(settings)
        self.assertEqual(code, 0)
        summary = json.loads((settings.reports_dir / runner.SUMMARY).read_text())
        self.assertEqual(summary["fit_notes"]["status"], "error")
        self.assertTrue(any(w.startswith("Fit notes:") for w in summary["search_warnings"]))

    def test_no_ai_config_means_no_fit_files(self):
        settings = load_settings(self.base / "q.toml", env=ENV, raw={**PROFILE, "report": {"formats": ["csv"]}})
        with mock.patch.object(api, "fetch_usajobs", return_value=payload(job("1"))), \
                mock.patch.object(fit, "run_fit_notes") as fit_run:
            runner.run(settings)
        fit_run.assert_not_called()
        self.assertFalse((settings.reports_dir / fit.FIT_NOTES_NAME).exists())


class FitCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "resume.md").write_text("RESUME BODY")
        self.env = mock.patch.dict("os.environ", {**ENV, "USAJOBS_KEYRING": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, ai, *args):
        path = self.base / "p.json"
        path.write_text(json.dumps({**PROFILE, "ai": {"resume": "resume.md", **ai}}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(api, "fetch_usajobs", return_value=payload(job("1"))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["fit", "1", "--config", str(path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_preview_prints_prompt_and_sends_nothing(self):
        with mock.patch.object(fit, "make_provider") as make:
            code, out, _ = self.run_cli({}, "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent", out)
        self.assertIn("RESUME BODY", out)
        make.assert_not_called()

    def test_refuses_to_send_without_consent(self):
        with mock.patch.object(fit, "make_provider") as make:
            code, _, err = self.run_cli({})
        self.assertEqual(code, 2)
        self.assertIn("ai.send_resume = true", err)
        make.assert_not_called()

    def test_prints_the_note(self):
        answer = json.dumps({"fit": "stretch", "summary": "Thin.", "strengths": [], "gaps": ["No formulation"],
                             "resume_tips": [], "questions_to_verify": []})
        provider = mock.Mock(complete=mock.Mock(return_value=answer))
        with mock.patch.object(fit, "make_provider", return_value=provider):
            code, out, _ = self.run_cli({"send_resume": True})
        self.assertEqual(code, 0)
        self.assertIn("**Fit: Stretch.** Thin.", out)
        self.assertIn("- No formulation", out)


    def test_match_sends_one_version_and_names_it(self):
        folder = self.base / "resumes"
        folder.mkdir()
        (folder / "Budget.md").write_text("BUDGET BODY")
        (folder / "Policy.md").write_text("POLICY BODY")
        ai = {"resume": "resumes", "resume_choice": "match", "resume_rules": {"Policy": ["budget"]}}
        code, out, _ = self.run_cli(ai, "--preview")
        self.assertEqual(code, 0)
        self.assertIn('for the Policy version (your rule: "budget" in the title)', out)
        self.assertIn("POLICY BODY", out)
        self.assertNotIn("BUDGET BODY", out)
        answer = json.dumps({"fit": "possible", "summary": "Close.", "resume_used": "", "strengths": [], "gaps": [],
                             "resume_tips": [], "questions_to_verify": []})
        provider = mock.Mock(complete=mock.Mock(return_value=answer))
        with mock.patch.object(fit, "make_provider", return_value=provider):
            code, out, _ = self.run_cli({**ai, "send_resume": True})
        self.assertEqual(code, 0)
        self.assertIn('Best resume for this posting: Policy (picked before sending: your rule: "budget" in the title)', out)
        self.assertNotIn("BUDGET BODY", provider.complete.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
