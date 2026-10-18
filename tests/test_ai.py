"""Offline checks for the optional AI add-on: config, resume reading, fit notes and providers."""

from datetime import date
import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from usajobs_monitor import fit, resume_text
from usajobs_monitor.config import ConfigError, Settings, load_settings, validate_config
from usajobs_monitor.fit import FitError
from usajobs_monitor.review import review_job
from usajobs_monitor.tracking import build_view

BASE = {"search": {"locations": ["Denver, Colorado"], "target_role_keywords": ["budget analyst"],
                   "role_title_terms": ["budget"]},
        "eligibility": {"presets": ["public"]}}
GOOD_ANSWER = {"fit": "possible", "summary": "Close; say budget formulation outright.",
               "strengths": ["Five years of budget execution"], "gaps": ["No formulation wording"],
               "resume_tips": ["Add hours per week"], "questions_to_verify": []}


def job(number="812345600", title="Budget Analyst"):
    return {"PositionTitle": title, "OrganizationName": "Example Agency",
            "PositionURI": f"https://www.usajobs.gov/job/{number}",
            "JobGrade": [{"Code": "GS"}], "UserArea": {"Details": {"HiringPath": ["public"],
                                                                     "MajorDuties": ["Formulate the budget."]}},
            "PositionRemuneration": [{"MinimumRange": "90000", "MaximumRange": "120000"}],
            "QualificationSummary": "One year of specialized experience at GS-11."}


class FakeProvider:
    label = "fake"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer or GOOD_ANSWER, error, []

    def complete(self, system, prompt):
        self.calls.append((system, prompt))
        if self.error:
            raise self.error
        return "Here you go:\n" + json.dumps(self.answer)


class AiConfigTests(unittest.TestCase):
    def test_defaults_keep_ai_off_and_resume_unsent(self):
        config, warnings = validate_config(BASE)
        ai = config["ai"]
        self.assertEqual((ai["fit_notes"], ai["send_resume"], ai["provider"], ai["model"]),
                         (False, False, "anthropic", "claude-opus-5-5"))
        self.assertEqual(warnings, [])

    def test_api_key_in_file_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config({**BASE, "ai": {"api_key": "sk-secret"}})
        self.assertIn("ai.api_key", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_bad_values_name_the_setting(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config({**BASE, "ai": {"provider": "openai!", "effort": "huge", "lists": ["all"],
                                            "max_jobs": 0}})
        text = str(caught.exception)
        for path in ("ai.provider", "ai.effort", "ai.lists[0]", "ai.max_jobs"):
            self.assertIn(path, text)

    def test_command_provider_needs_a_command(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config({**BASE, "ai": {"provider": "command"}})
        self.assertIn("ai.command", str(caught.exception))

    def test_custom_provider_path_accepted(self):
        config, _ = validate_config({**BASE, "ai": {"provider": "my_pkg.llm:make"}})
        self.assertEqual(config["ai"]["provider"], "my_pkg.llm:make")

    def test_fit_notes_without_consent_warns(self):
        _, warnings = validate_config({**BASE, "ai": {"fit_notes": True}})
        self.assertTrue(any("ai.send_resume" in w for w in warnings))

    def test_resume_path_is_relative_to_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.toml"
            settings = load_settings(path, require_credentials=False, raw={**BASE, "ai": {"resume": "me.md"}})
        self.assertEqual(settings.ai_resume, path.parent.resolve() / "me.md")


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def test_markdown_and_text(self):
        path = self.folder / "resume.md"
        path.write_text("# Alex Example\n\nBudget analyst   \n", encoding="utf-8")
        self.assertEqual(resume_text.read_resume(path), "# Alex Example\n\nBudget analyst")

    def test_docx_paragraphs_and_tables(self):
        from docx import Document
        document = Document()
        document.add_paragraph("Budget Analyst, 2019-2024")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Skills", "Formulation"
        path = self.folder / "resume.docx"
        document.save(str(path))
        text = resume_text.read_resume(path)
        self.assertIn("Budget Analyst, 2019-2024", text)
        self.assertIn("Skills | Formulation", text)

    def test_pdf_without_markitdown_explains_the_extra(self):
        path = self.folder / "resume.pdf"
        path.write_bytes(b"%PDF-1.4")
        with mock.patch.dict(sys.modules, {"markitdown": None}):
            with self.assertRaises(resume_text.ResumeError) as caught:
                resume_text.read_resume(path)
        self.assertIn("usajobs-monitor[ai]", str(caught.exception))

    def test_empty_and_huge_files_refused(self):
        empty, huge = self.folder / "a.txt", self.folder / "b.txt"
        empty.write_text("  \n")
        huge.write_text("x" * (resume_text.MAX_RESUME_CHARS + 1))
        for path in (empty, huge):
            with self.subTest(path=path.name), self.assertRaises(resume_text.ResumeError):
                resume_text.read_resume(path)

    def test_find_resumes_in_folder(self):
        with self.assertRaises(resume_text.ResumeError):
            resume_text.find_resumes(None, None)
        (self.folder / "one.md").write_text("x")
        (self.folder / "notes.xlsx").write_text("x")
        self.assertEqual([p.name for p in resume_text.find_resumes(None, self.folder)], ["one.md"])
        (self.folder / "two.docx").write_text("x")
        self.assertEqual([p.name for p in resume_text.find_resumes(None, self.folder)], ["one.md", "two.docx"])
        self.assertEqual([p.name for p in resume_text.find_resumes(self.folder / "two.docx", self.folder)],
                         ["two.docx"])
        for index in range(resume_text.MAX_RESUMES):
            path = self.folder / f"extra{index}.txt"
            path.write_text("x")
            os.utime(path, (2_000_000_000 + index, 2_000_000_000 + index))  # newer than one.md and two.docx
        picked, left_out = resume_text.pick_resumes(None, self.folder)
        self.assertEqual(left_out, 2)
        self.assertEqual([p.name for p in picked], [f"extra{index}.txt" for index in range(resume_text.MAX_RESUMES)])

    def test_resume_names_drop_the_shared_start(self):
        names = resume_text.resume_names([Path("Pat Lee - Resume - Budget Director.pdf"),
                                          Path("Pat Lee - Resume - FDIC Analyst (18Sept2026).pdf")])
        self.assertEqual(names, ["Budget Director", "FDIC Analyst (18Sept2026)"])
        self.assertEqual(resume_text.resume_names([Path("Resume v1.md"), Path("Resume v2.md")]), ["v1", "v2"])
        self.assertEqual(resume_text.resume_names([Path("a.md"), Path("a.txt")]), ["a", "a"])
        self.assertEqual(resume_text.resume_names([Path("Only.md")]), ["Only"])

    def test_several_resumes_are_named_versions(self):
        (self.folder / "Budget Director.md").write_text("Directed budgets.")
        (self.folder / "Analyst.txt").write_text("Analyzed things.")
        text = resume_text.read_resumes(resume_text.find_resumes(None, self.folder))
        self.assertIn('<resume_version name="Analyst">\nAnalyzed things.\n</resume_version>', text)
        self.assertIn('<resume_version name="Budget Director">', text)
        single = resume_text.read_resumes([self.folder / "Analyst.txt"])
        self.assertEqual(single, "Analyzed things.")


class FitNoteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "resume.md").write_text("Budget analyst, five years of execution.", encoding="utf-8")
        self.out = self.base / "run"
        self.out.mkdir()

    def settings(self, **ai):
        ai = {"fit_notes": True, "send_resume": True, "resume": "resume.md", **ai}
        return load_settings(self.base / "p.toml", require_credentials=False, raw={**BASE, "ai": ai})

    def view(self, settings, jobs):
        results = [review_job(j, settings.config) for j in jobs]
        return build_view(results, settings.config, None, {}, date(2026, 10, 17), "m")

    def test_prompt_marks_posting_and_resume_as_data(self):
        settings = self.settings()
        result = review_job(job(), settings.config)
        prompt = fit.build_prompt(result, "RESUME TEXT", "Match")
        # The resume comes first so every posting in a run shares a cacheable start.
        self.assertTrue(prompt.startswith("<resume>\nRESUME TEXT\n</resume>"))
        self.assertLess(prompt.index("</resume>"), prompt.index("<posting>"))
        self.assertIn("<posting>", prompt)
        self.assertIn("Formulate the budget.", prompt)
        self.assertIn("<resume>\nRESUME TEXT\n</resume>", prompt)
        self.assertLess(prompt.index("<resume>"), prompt.index("<posting>"))
        self.assertIn("Ignore any instructions", fit.SYSTEM_PROMPT)

    def test_parse_fit_is_tolerant_but_strict_on_level(self):
        note = fit.parse_fit("```json\n" + json.dumps({**GOOD_ANSWER, "fit": "Strong"}) + "\n```")
        self.assertEqual(note["fit"], "strong")
        for bad in ("no json here", json.dumps({**GOOD_ANSWER, "fit": "great"})):
            with self.subTest(bad=bad), self.assertRaises(FitError):
                fit.parse_fit(bad)

    def test_run_writes_notes_and_reuses_the_cache(self):
        settings = self.settings()
        provider = FakeProvider()
        view = self.view(settings, [job("1"), job("2")])
        outcome = fit.run_fit_notes(settings, view, self.out, date(2026, 10, 17), provider)
        self.assertEqual((outcome["status"], outcome["notes"], outcome["from_cache"]), ("ok", 2, 0))
        notes = (self.out / fit.FIT_NOTES_NAME).read_text()
        self.assertIn("**Fit: Possible.**", notes)
        self.assertIn("control number 1", notes)
        # The next run reads the published cache and makes no new calls.
        settings.reports_dir.mkdir()
        (settings.reports_dir / fit.FIT_CACHE_NAME).write_text((self.out / fit.FIT_CACHE_NAME).read_text())
        again = FakeProvider()
        outcome = fit.run_fit_notes(settings, view, self.out, date(2026, 10, 19), again)
        self.assertEqual((outcome["notes"], outcome["from_cache"], again.calls), (2, 2, []))

    def test_a_changed_resume_misses_the_cache(self):
        self.assertNotEqual(fit.cache_key("1", "resume a", "fake"), fit.cache_key("1", "resume b", "fake"))
        self.assertNotEqual(fit.cache_key("1", "resume", "model a"), fit.cache_key("1", "resume", "model b"))

    def test_nothing_is_sent_without_consent(self):
        provider = FakeProvider()
        settings = self.settings(send_resume=False)
        outcome = fit.run_fit_notes(settings, self.view(settings, [job()]), self.out, date.today(), provider)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(provider.calls, [])
        self.assertFalse((self.out / fit.FIT_NOTES_NAME).exists())

    def test_off_by_default(self):
        settings = load_settings(self.base / "p.toml", require_credentials=False, raw=BASE)
        self.assertEqual(fit.run_fit_notes(settings, None, self.out, date.today()), {"status": "off"})

    def test_fatal_provider_error_stops_after_one_call(self):
        provider = FakeProvider(error=FitError("the Claude API rejected the API key", fatal=True))
        settings = self.settings()
        outcome = fit.run_fit_notes(settings, self.view(settings, [job("1"), job("2")]), self.out, date.today(),
                                    provider)
        self.assertEqual((outcome["status"], outcome["failed"], len(provider.calls)), ("error", 1, 1))
        self.assertEqual(outcome["message"], "the Claude API rejected the API key")

    def test_unexpected_provider_error_is_contained(self):
        provider = FakeProvider(error=KeyError("resume text that must not leak"))
        settings = self.settings()
        outcome = fit.run_fit_notes(settings, self.view(settings, [job()]), self.out, date.today(), provider)
        self.assertEqual(outcome["status"], "error")
        self.assertNotIn("must not leak", json.dumps(outcome) + (self.out / fit.FIT_NOTES_NAME).read_text())

    def test_max_jobs_caps_the_calls(self):
        provider = FakeProvider()
        settings = self.settings(max_jobs=1)
        fit.run_fit_notes(settings, self.view(settings, [job("1"), job("2")]), self.out, date.today(), provider)
        self.assertEqual(len(provider.calls), 1)

    def test_requirement_checklist_caps_the_level(self):
        met = {"requirement": "One year at GS-11", "status": "met", "evidence": "five years of execution"}
        unclear = {**met, "status": "unclear"}
        missing = {"requirement": "CPA license", "status": "missing", "evidence": "no license listed"}
        for level, requirements, expected in (("strong", [met], "strong"), ("strong", [met, unclear], "possible"),
                                              ("possible", [met, missing], "unlikely"),
                                              ("stretch", [met], "stretch")):
            with self.subTest(level=level, expected=expected):
                note = fit.parse_fit(json.dumps({**GOOD_ANSWER, "fit": level, "requirements": requirements}))
                self.assertEqual(note["fit"], expected)

    def test_requirements_are_kept_and_shown(self):
        answer = {**GOOD_ANSWER, "requirements": [
            {"requirement": "One year at GS-11", "status": "Met", "evidence": "five years of execution"},
            {"requirement": "ignored", "status": "maybe", "evidence": ""}]}
        note = fit.parse_fit(json.dumps(answer))
        self.assertEqual(note["requirements"], [{"requirement": "One year at GS-11", "status": "met",
                                                 "evidence": "five years of execution"}])
        self.assertIn("- Met: One year at GS-11 (five years of execution)", fit.render_note(note))
        self.assertIn("requirements", fit.FIT_SCHEMA["required"])

    def test_run_records_fit_levels_on_the_view(self):
        settings = self.settings()
        view = self.view(settings, [job("1"), job("2")])
        fit.run_fit_notes(settings, view, self.out, date.today(), FakeProvider())
        self.assertEqual(view.fits, {"1": "possible", "2": "possible"})
        self.assertEqual(view.fit_text(view.matches[0]), "Possible")

    def test_best_resume_is_recorded_and_shown(self):
        folder = self.base / "resumes"
        folder.mkdir()
        (folder / "Budget.md").write_text("Budget work.")
        (folder / "Policy.md").write_text("Policy work.")
        settings = self.settings(resume="resumes")
        provider = FakeProvider({**GOOD_ANSWER, "resume_used": "Budget"})
        view = self.view(settings, [job("1")])
        fit.run_fit_notes(settings, view, self.out, date.today(), provider)
        self.assertIn('<resume_version name="Policy">', provider.calls[0][1])
        self.assertEqual(view.fit_text(view.matches[0]), "Possible (resume: Budget)")
        self.assertIn("Best resume for this posting: Budget", (self.out / fit.FIT_NOTES_NAME).read_text())
        # A name that is not one of the versions is dropped rather than shown.
        note = fit.parse_fit(json.dumps({**GOOD_ANSWER, "resume_used": "Made Up"}), ["Budget", "Policy"])
        self.assertEqual(note["resume_used"], "")

    def test_ranked_notes_file_is_best_first(self):
        settings = self.settings(rank=True)
        answers = iter([{**GOOD_ANSWER, "fit": "unlikely", "summary": "First seen."},
                        {**GOOD_ANSWER, "fit": "strong", "summary": "Second seen."}])

        class InOrder(FakeProvider):
            def complete(self, system, prompt):
                return json.dumps(next(answers))

        fit.run_fit_notes(settings, self.view(settings, [job("1"), job("2")]), self.out, date.today(), InOrder())
        notes = (self.out / fit.FIT_NOTES_NAME).read_text()
        self.assertLess(notes.index("Second seen."), notes.index("First seen."))

    def test_rank_by_fit_reorders_without_hiding(self):
        settings = self.settings()
        view = self.view(settings, [job("1"), job("2"), job("3"), job("4")])
        view.fits = {"1": "unlikely", "2": "strong", "4": "stretch"}
        view.rank_by_fit()
        self.assertEqual([r.job["PositionURI"][-1] for r in view.matches], ["2", "3", "4", "1"])

    def test_missing_resume_reports_without_raising(self):
        settings = self.settings(resume="gone.md")
        outcome = fit.run_fit_notes(settings, self.view(settings, [job()]), self.out, date.today(), FakeProvider())
        self.assertEqual(outcome["status"], "error")
        self.assertIn("gone.md", outcome["message"])


class RankedRunTests(unittest.TestCase):
    """ai.rank reorders every report and the digest summary by fit level."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "resume.md").write_text("Budget analyst, five years of execution.", encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        jobs = [job("1"), job("2"), job("3")]
        for posting in jobs:
            posting["PositionRemuneration"] = [{"MinimumRange": "90000", "MaximumRange": "150000"}]
        from usajobs_monitor import api
        mock.patch.object(api, "search_all_jobs", return_value=(jobs, 1, [])).start()
        levels = {"1": "unlikely", "2": "strong", "3": "stretch"}

        class ByNumber(FakeProvider):
            def complete(self, system, prompt):
                number = prompt.split("usajobs.gov/job/")[1][0] if "usajobs.gov/job/" in prompt else "1"
                return json.dumps({**GOOD_ANSWER, "fit": levels[number]})

        mock.patch.object(fit, "make_provider", return_value=ByNumber()).start()
        mock.patch.object(fit, "posting_text", side_effect=lambda result, tier="": result.job["PositionURI"]).start()

    def run_search(self, **ai):
        from usajobs_monitor import runner
        ai = {"fit_notes": True, "send_resume": True, "resume": "resume.md", **ai}
        raw = {**BASE, "ai": ai, "report": {"formats": ["docx", "xlsx", "html", "csv"]}}
        settings = load_settings(self.base / "p.toml", require_credentials=False, raw=raw)
        out = self.base / "out"
        return runner.run_search(settings, out), out

    def test_rank_orders_matches_and_reports(self):
        summary, out = self.run_search(rank=True)
        self.assertTrue(summary["fit_notes"]["ranked"])
        self.assertEqual([m["control_number"] for m in summary["matches"]], ["2", "3", "1"])
        self.assertEqual([m["ai_fit"] for m in summary["matches"]], ["Strong", "Stretch", "Unlikely"])
        page = (out / "usajobs_digest.html").read_text()
        self.assertLess(page.index("job/2"), page.index("job/1"))
        self.assertIn("AI fit:</b> Strong. Close; say budget formulation outright.", page)
        self.assertIn("AI ranking:", page)
        from docx import Document
        briefing = "\n".join(p.text for p in Document(str(out / "usajobs_briefing.docx")).paragraphs)
        self.assertIn("the AI compared your resume with 3 postings", briefing)
        import csv
        rows = list(csv.DictReader((out / "usajobs_jobs.csv").read_text(encoding="utf-8-sig").splitlines()))
        self.assertEqual({row["Control Number"]: row["AI Fit"] for row in rows},
                         {"1": "Unlikely", "2": "Strong", "3": "Stretch"})

    def test_without_rank_order_is_unchanged_but_fit_is_shown(self):
        summary, _ = self.run_search()
        self.assertNotIn("ranked", summary["fit_notes"])
        self.assertEqual([m["control_number"] for m in summary["matches"]], ["1", "2", "3"])
        self.assertEqual(summary["matches"][1]["ai_fit"], "Strong")

    def test_rank_config_defaults_off_and_warns_without_fit_notes(self):
        config, warnings = validate_config(BASE)
        self.assertFalse(config["ai"]["rank"])
        _, warnings = validate_config({**BASE, "ai": {"rank": True}})
        self.assertTrue(any("ai.rank" in w for w in warnings))


class ProviderTests(unittest.TestCase):
    AI = {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "medium", "api_key_env": "ANTHROPIC_API_KEY",
          "command": [], "timeout_seconds": 30}

    def test_anthropic_needs_a_key(self):
        with self.assertRaises(FitError) as caught:
            fit.make_provider(self.AI, env={})
        self.assertIn("ANTHROPIC_API_KEY", str(caught.exception))

    def test_command_provider_round_trip(self):
        script = "import sys, json; sys.stdin.read(); print(json.dumps(%r))" % (GOOD_ANSWER,)
        command = [sys.executable, "-c", script]
        provider = fit.make_provider({**self.AI, "provider": "command", "command": command},
                                     env={"USAJOBS_ALLOW_AI_PROVIDER": shlex.join(command)})
        self.assertEqual(fit.parse_fit(provider.complete("system", "prompt"))["fit"], "possible")

    def test_command_provider_failures_are_fixed_text(self):
        provider = fit.CommandProvider([sys.executable, "-c", "import sys; print('secret'); sys.exit(3)"], 30)
        with self.assertRaises(FitError) as caught:
            provider.complete("s", "p")
        self.assertEqual(str(caught.exception), "ai.command exited with status 3")
        with self.assertRaises(FitError) as caught:
            fit.CommandProvider(["/no/such/program"], 30).complete("s", "p")
        self.assertTrue(caught.exception.fatal)

    def test_custom_provider_factory(self):
        module = type(sys)("fake_llm")
        module.make = lambda ai, env: FakeProvider()
        with mock.patch.dict(sys.modules, {"fake_llm": module}):
            provider = fit.make_provider({**self.AI, "provider": "fake_llm:make"},
                                         env={"USAJOBS_ALLOW_AI_PROVIDER": "other:x, fake_llm:make"})
        self.assertIsInstance(provider, FakeProvider)
        with self.assertRaises(FitError):
            fit.make_provider({**self.AI, "provider": "no_such_module_xyz:make"},
                              env={"USAJOBS_ALLOW_AI_PROVIDER": "no_such_module_xyz:make"})

    def test_profile_alone_cannot_run_code(self):
        module = type(sys)("fake_llm")
        module.make = mock.Mock(return_value=FakeProvider())
        cases = [({**self.AI, "provider": "command", "command": [sys.executable, "-c", "pass"]},
                  shlex.quote(shlex.join([sys.executable, "-c", "pass"]))),
                 ({**self.AI, "provider": "fake_llm:make"}, "fake_llm:make")]
        with mock.patch.dict(sys.modules, {"fake_llm": module}), mock.patch.object(fit.subprocess, "run") as run:
            for ai, needed in cases:
                for env in ({}, {"USAJOBS_ALLOW_AI_PROVIDER": "command"}, {"USAJOBS_ALLOW_AI_PROVIDER": "fake_llm"},
                            {"USAJOBS_ALLOW_AI_PROVIDER": sys.executable}):
                    with self.assertRaises(FitError) as caught:
                        fit.make_provider(ai, env=env).complete("s", "p")
                    self.assertTrue(caught.exception.fatal)
                    self.assertIn(f"USAJOBS_ALLOW_AI_PROVIDER={needed}", str(caught.exception))
        module.make.assert_not_called()
        run.assert_not_called()

    def test_check_config_warns_about_code_providers(self):
        raw = {**BASE, "ai": {"provider": "command", "command": ["ollama", "run", "x"]}}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USAJOBS_ALLOW_AI_PROVIDER", None)
            _, warnings = validate_config(raw)
            self.assertTrue(any("USAJOBS_ALLOW_AI_PROVIDER='ollama run x'" in w for w in warnings), warnings)
        with mock.patch.dict(os.environ, {"USAJOBS_ALLOW_AI_PROVIDER": "ollama run x"}):
            _, warnings = validate_config(raw)
            self.assertFalse(any("USAJOBS_ALLOW_AI_PROVIDER" in w for w in warnings), warnings)

    def test_allowing_an_interpreter_does_not_allow_other_scripts(self):
        mine = {**self.AI, "provider": "command", "command": ["python3", "my_model.py"]}
        theirs = {**self.AI, "provider": "command", "command": ["python3", "-c", "import os"]}
        env = {"USAJOBS_ALLOW_AI_PROVIDER": "python3 my_model.py, ollama run llama3.1"}
        self.assertIsInstance(fit.make_provider(mine, env=env), fit.CommandProvider)
        for allowed in ("python3", "python3 my_model.py"):
            with self.assertRaises(FitError):
                fit.make_provider(theirs, env={"USAJOBS_ALLOW_AI_PROVIDER": allowed})

    def test_single_command_with_a_comma_can_be_allowed(self):
        ai = {**self.AI, "provider": "command", "command": ["runner", "--models=a,b"]}
        self.assertIsInstance(fit.make_provider(ai, env={"USAJOBS_ALLOW_AI_PROVIDER": "runner --models=a,b"}),
                              fit.CommandProvider)

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
    def test_anthropic_request_shape(self):
        provider = fit.make_provider(self.AI, env={"ANTHROPIC_API_KEY": "test-key"})
        response = mock.Mock(stop_reason="end_turn", content=[mock.Mock(type="text", text=json.dumps(GOOD_ANSWER))])
        with mock.patch.object(provider.client.beta.messages, "create", return_value=response) as create:
            self.assertEqual(fit.parse_fit(provider.complete("sys", "prompt"))["fit"], "possible")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["output_config"]["effort"], "medium")
        self.assertEqual(kwargs["output_config"]["format"]["schema"], fit.FIT_SCHEMA)
        self.assertEqual((kwargs["fallbacks"], kwargs["betas"]), ("default", [fit.FALLBACK_BETA]))

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "anthropic not installed")
    def test_anthropic_refusal_and_errors(self):
        import anthropic
        provider = fit.make_provider({**self.AI, "model": "claude-haiku-5-5"}, env={"ANTHROPIC_API_KEY": "k"})
        refusal = mock.Mock(stop_reason="refusal", content=[])
        with mock.patch.object(provider.client.beta.messages, "create", return_value=refusal) as create:
            with self.assertRaises(FitError):
                provider.complete("s", "p")
        self.assertNotIn("fallbacks", create.call_args.kwargs)  # only for models that take it
        error = anthropic.APIConnectionError(request=mock.Mock())
        with mock.patch.object(provider.client.beta.messages, "create", side_effect=error):
            with self.assertRaises(FitError) as caught:
                provider.complete("s", "p")
        self.assertTrue(caught.exception.fatal)


class CoreWithoutAiTests(unittest.TestCase):
    def test_core_imports_without_ai_packages(self):
        code = ("import sys\n"
                "for name in ('anthropic', 'markitdown', 'mcp'): sys.modules[name] = None\n"
                "import usajobs_monitor.cli, usajobs_monitor.runner, usajobs_monitor.fit, usajobs_monitor.tools\n"
                "usajobs_monitor.cli.build_parser().parse_args(['fit', '1', '--preview'])\n")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
