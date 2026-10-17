"""The init wizard, saved profiles, and the decide command. All offline."""

import contextlib
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usajobs_monitor import cli, profiles
from usajobs_monitor.config import load_settings
from usajobs_monitor.series import find_series
from usajobs_monitor.tracking import load_decisions, save_decision
from usajobs_monitor.wizard import Cancelled, Wizard, render_profile


class FakeStore:
    def __init__(self):
        self.saved = {}

    def set(self, name, value):
        self.saved[name] = value


def scripted(answers, store=None, secrets=()):
    replies, said = iter(answers), []
    secret_replies = iter(secrets)
    wizard = Wizard(ask=lambda prompt: next(replies), say=said.append,
                    secret=lambda prompt: next(secret_replies), store=store)
    return wizard, said


# Answers in prompt order for an excepted-service veteran in Seattle.
EXCEPTED_VETERAN = [
    "3,4",                        # applicant types: excepted service + veteran
    "Department of Example",      # current agency
    "DOE-X",                      # other names
    "y",                          # interchange agreement?
    "",                           # interchange agency name (defaults to current agency)
    "Seattle, Washington; Tacoma, Washington",
    "40", "y",                    # radius, remote
    "13", "",                     # grade, locality (accept the guess)
    "2",                          # above_grade
    "budget analyst, program analyst",
    "finance", "3", "0343", "",   # series: pick 0505 from the finance list, then 0343
    "2,3",                        # email + discord
    "smtp.example.com", "2", "me@example.invalid", "",
    "y",                          # Word and Excel too
]


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_full_answers_write_a_valid_profile(self):
        wizard, said = scripted(EXCEPTED_VETERAN)
        answers = wizard.run()
        path = self.base / "p.toml"
        path.write_text(render_profile(answers))
        settings = load_settings(path, require_credentials=False)
        config = settings.config
        self.assertEqual(config["eligibility"]["presets"], ["excepted-fed", "veteran"])
        self.assertEqual(config["eligibility"]["service"], "excepted")
        self.assertEqual(config["eligibility"]["interchange_agency"], "Department of Example")
        self.assertEqual(config["eligibility"]["additional_hiring_paths"], ["vet"])
        self.assertEqual(config["profile"]["current_agency_terms"], ["Department of Example", "DOE-X"])
        self.assertEqual(config["search"]["locations"], ["Seattle, Washington", "Tacoma, Washington"])
        self.assertTrue(config["search"]["include_remote_jobs"])
        self.assertEqual(config["search"]["target_job_series"], ["0505", "0343"])
        self.assertEqual(config["target"]["resolved"]["locality_code"], "SEA")
        self.assertEqual(config["target"]["match"], "above_grade")
        self.assertEqual(config["report"]["formats"], ["docx", "xlsx", "html", "csv"])
        self.assertEqual(config["delivery"]["email"]["security"], "starttls")
        self.assertEqual(config["delivery"]["email"]["to_addresses"], ["me@example.invalid"])
        self.assertIn("discord", config["delivery"])
        self.assertTrue(any("developer.usajobs.gov" in line for line in said))
        self.assertTrue(any("interchange agreement" in line for line in said))

    def test_minimal_answers_and_no_secrets_in_profile(self):
        store = FakeStore()
        wizard, _ = scripted(["y", "me@example.invalid", "", "Denver, Colorado", "", "", "",
                              "", "budget", "", "", ""], store=store, secrets=["secret-key-value"])
        answers = wizard.run()
        text = render_profile(answers)
        self.assertEqual(store.saved, {"USAJOBS_EMAIL": "me@example.invalid", "USAJOBS_API_KEY": "secret-key-value"})
        self.assertNotIn("secret-key-value", text)
        self.assertNotIn("me@example.invalid", text)
        path = self.base / "p.toml"
        path.write_text(text)
        config = load_settings(path, require_credentials=False).config
        self.assertEqual(config["eligibility"]["presets"], ["public"])
        self.assertEqual(config["search"]["target_role_keywords"], ["Budget Analysis"])
        self.assertNotIn("target", config)
        self.assertEqual(config["report"]["formats"], ["html", "csv"])

    def test_invalid_answers_are_asked_again(self):
        wizard, said = scripted(["0", "9,1", "", "Austin, Texas", "x", "10", "maybe", "n", "16", "",
                                 "data", "", "", ""])
        answers = wizard.run()
        self.assertEqual(answers.presets, ["disability"])  # "public" is implied by every other type
        self.assertEqual(answers.radius_miles, 10)
        self.assertIsNone(answers.grade)
        self.assertTrue(any("from the list" in line for line in said))

    def test_ctrl_d_cancels(self):
        def eof(prompt):
            raise EOFError
        with self.assertRaises(Cancelled):
            Wizard(ask=eof, say=lambda s: None).run()

    def test_series_lookup(self):
        self.assertEqual(find_series("0560"), [("0560", "Budget Analysis")])
        self.assertIn(("0505", "Financial Management"), find_series("finance"))
        self.assertEqual(find_series("12"), [])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.env = {profiles.HOME_VAR: str(self.home)}
        patch.dict(os.environ, {**self.env, "USAJOBS_KEYRING": "0"}).start()
        self.addCleanup(patch.stopall)

    def save(self, name, reports):
        folder = profiles.profiles_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.toml").write_text(
            f'[paths]\nreports_dir = "{reports}"\n[search]\nlocations = ["Denver, Colorado"]\n'
            'target_role_keywords = ["budget"]\n')

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_profile_flag_and_single_profile_fallback(self):
        self.save("alex", "alex/reports")
        code, out, _ = self.cli("check-config", "--profile", "alex")
        self.assertEqual(code, 0)
        self.assertIn(str(self.home / "profiles" / "alex" / "reports"), out)
        with patch.object(Path, "cwd", return_value=self.home):
            code, out, _ = self.cli("check-config")  # no ./usajobs-monitor.toml here, one saved profile
        self.assertEqual(code, 0)
        self.assertIn("alex.toml", out)

    def test_unknown_profile_names_the_saved_ones(self):
        self.save("alex", "alex/reports")
        code, _, err = self.cli("check-config", "--profile", "sam")
        self.assertEqual(code, 2)
        self.assertIn("alex", err)

    def test_profiles_lists_each(self):
        self.save("alex", "alex/reports")
        self.save("sam", "sam/reports")
        code, out, _ = self.cli("profiles")
        self.assertEqual(code, 0)
        self.assertIn("alex", out)
        self.assertIn("sam", out)

    def test_run_all_runs_each_profile_and_refuses_a_shared_reports_folder(self):
        self.save("alex", "alex/reports")
        self.save("sam", "sam/reports")
        ran = []

        def fake_run(settings, deliver=None, **kwargs):
            ran.append(settings.config_path.stem)
            return 0, {"run_id": "x"}
        with patch("usajobs_monitor.runner.run", fake_run), \
                patch.dict(os.environ, {"USAJOBS_EMAIL": "a@example.invalid", "USAJOBS_API_KEY": "k"}):
            code, out, _ = self.cli("run", "--all", "--no-deliver")
        self.assertEqual((code, ran), (0, ["alex", "sam"]))
        self.save("sam", "alex/reports")
        code, _, err = self.cli("run", "--all")
        self.assertEqual(code, 2)
        self.assertIn("share the reports folder", err)

    def test_init_writes_into_the_profiles_folder(self):
        answers = iter(["", "Denver, Colorado", "", "", "", "budget analyst", "", "", ""])
        with patch("builtins.input", lambda prompt="": next(answers)):
            code, out, _ = self.cli("init", "--profile", "alex")
        self.assertEqual(code, 0, out)
        path = self.home / "profiles" / "alex.toml"
        settings = load_settings(path, require_credentials=False)
        self.assertEqual(settings.reports_dir, self.home / "profiles" / "alex" / "reports")
        code, _, err = self.cli("init", "--profile", "alex")
        self.assertEqual(code, 2)
        self.assertIn("--force", err)


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "usajobs-monitor.toml"
        self.config.write_text('[search]\nlocations = ["Denver, Colorado"]\ntarget_role_keywords = ["budget"]\n')
        (self.base / "reports").mkdir()
        (self.base / "reports" / "usajobs_job_history.json").write_text(json.dumps(
            {"jobs": {"812345600": {"title": "Budget Analyst", "agency": "Example Agency"}}}))
        self.decisions = self.base / "job_decisions.csv"

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--config", str(self.config)])
        return code, out.getvalue(), err.getvalue()

    def test_mark_change_list_and_clear(self):
        code, out, err = self.cli("decide", "812345600", "not interested", "wrong", "series")
        self.assertEqual(code, 0, err)
        self.assertIn("Budget Analyst", out)
        self.assertEqual(load_decisions(self.decisions)[0],
                         {"812345600": {"status": "not_interested", "note": "wrong series"}})
        code, out, _ = self.cli("decide", "812345600", "applied")
        self.assertIn("was not_interested", out)
        code, out, _ = self.cli("decide")
        self.assertIn("812345600", out)
        self.assertIn("applied", out)
        self.cli("decide", "812345600", "clear")
        self.assertEqual(load_decisions(self.decisions)[0], {})

    def test_unknown_job_warns_and_bad_input_fails(self):
        code, _, err = self.cli("decide", "999999999", "watching")
        self.assertEqual(code, 0)
        self.assertIn("not in the last run", err)
        self.assertEqual(self.cli("decide", "abc", "applied")[0], 2)
        self.assertEqual(self.cli("decide", "999999999", "maybe")[0], 2)

    def test_existing_rows_and_columns_are_kept(self):
        self.decisions.write_text("control_number,status,note,my_column\n111,applied,first,keep me\n"
                                  "222,watching,,\n", encoding="utf-8")
        save_decision(self.decisions, "222", "not-interested", "later")
        with self.decisions.open(newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0], {"control_number": "111", "status": "applied", "note": "first", "my_column": "keep me"})
        self.assertEqual(rows[1]["status"], "not_interested")
        self.assertEqual(rows[1]["note"], "later")


if __name__ == "__main__":
    unittest.main()
