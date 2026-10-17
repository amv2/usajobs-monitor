"""Config validation, defaults, old-key compatibility, and credentials from the environment."""

import json
from pathlib import Path
import tempfile
import unittest

from usajobs_monitor.config import (
    DEFAULT_SCORING,
    DEFAULT_TEMPORARY_TERMS,
    ConfigError,
    load_settings,
    resolve_credentials,
    validate_config,
)

ROOT = Path(__file__).resolve().parent.parent
MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}
KEY = "k3y-value-that-must-not-leak"


class ValidationTests(unittest.TestCase):
    def errors(self, raw):
        with self.assertRaises(ConfigError) as caught:
            validate_config(raw)
        return caught.exception.errors

    def test_minimal_config_gets_defaults(self):
        config, warnings = validate_config(MINIMAL)
        self.assertEqual(warnings, [])
        self.assertEqual(config["search"]["radius_miles"], 25)
        self.assertEqual(config["search"]["search_only_keywords"], [])
        self.assertEqual(config["filters"]["watch_list"], {})
        self.assertEqual(config["filters"]["near_miss_upper_salary_must_exceed"], 0)
        self.assertIsNone(config["filters"]["near_miss_min_gs_grade"])
        self.assertEqual(config["filters"]["excluded_pay_plans"], [])
        self.assertTrue(config["filters"]["exclude_senior_executive"])
        self.assertEqual(config["filters"]["temporary_terms"], DEFAULT_TEMPORARY_TERMS)
        self.assertEqual(config["eligibility"]["service"], "none")
        self.assertEqual(config["scoring"], DEFAULT_SCORING)
        self.assertEqual(config["paths"]["reports_dir"], "reports")
        self.assertEqual(config["report"]["title"], "USAJOBS Job Monitor")

    def test_input_is_not_mutated(self):
        raw = json.loads(json.dumps(MINIMAL))
        validate_config(raw)
        self.assertEqual(raw, MINIMAL)

    def test_required_keys_named_by_path(self):
        self.assertEqual(self.errors({}), ["search.locations: required: a non-empty list of strings",
                                           "search.target_role_keywords: required: a non-empty list of strings"])

    def test_type_errors_name_the_exact_setting(self):
        raw = {"search": {**MINIMAL["search"], "radius_miles": "35", "target_job_series": ["0501", "510"],
                          "locations": "Arlington, Virginia"},
               "filters": {"watch_list": {"pay_plans": ["GS"], "grades": ["fifteen"]},
                           "exclude_senior_executive": "yes"},
               "eligibility": {"service": "public"}}
        self.assertEqual(sorted(self.errors(raw)), sorted([
            "search.locations: expected a list of strings, not a single string",
            "search.radius_miles: expected an integer",
            'search.target_job_series[1]: expected a 4-digit series like "0510"',
            "filters.exclude_senior_executive: expected true or false",
            "filters.watch_list.grades[0]: expected an integer",
            'eligibility.service: expected one of "competitive", "excepted", "none"',
        ]))

    def test_unknown_keys_warn(self):
        _, warnings = validate_config({**MINIMAL, "filtres": {}, "search": {**MINIMAL["search"], "radius": 5}})
        self.assertIn("filtres: unknown setting, ignored", warnings)
        self.assertIn("search.radius: unknown setting, ignored", warnings)

    def test_old_json_layout_still_loads(self):
        raw = {
            "credentials": {"email": "PUT_YOUR_EMAIL_HERE", "api_key": "PUT_YOUR_USAJOBS_API_KEY_HERE"},
            "resume_folder": "/tmp/resumes",
            "user_profile": {"home_zip": "00000", "current_agency": "Example Agency", "rounded_salary_benchmark": 1},
            "search": {**MINIMAL["search"], "radius_miles": 35, "include_remote_jobs": False, "date_posted_days": 14,
                       "salary_query_floor": 120000, "target_agencies": []},
            "filters": {"salary_upper_must_exceed": 197200, "current_agency_terms": ["EXA"],
                        "temporary_terms": [" nte "], "senior_executive_terms": [" ses "], "mismatch_terms": []},
            "eligibility": {"service": "excepted", "has_competitive_status": False,
                            "interchange_agency": "Example Agency", "additional_hiring_paths": []},
        }
        config, warnings = validate_config(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(config["paths"]["resume_folder"], "/tmp/resumes")
        self.assertEqual(config["profile"], {"current_agency": "Example Agency", "current_agency_terms": ["EXA"]})
        self.assertNotIn("current_agency_terms", config["filters"])
        self.assertEqual(config["filters"]["temporary_terms"], [" nte "])

    def test_shipped_examples_validate(self):
        for path in [ROOT / "examples" / "usajobs-monitor.example.toml", ROOT / "profiles" / "financial-management-dc.toml"]:
            with self.subTest(path=path.name):
                settings = load_settings(path, require_credentials=False)
                self.assertEqual(settings.warnings, [])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def test_toml_and_relative_paths(self):
        path = self.folder / "usajobs-monitor.toml"
        path.write_text('[paths]\nreports_dir = "out"\n\n[search]\nlocations = ["Arlington, Virginia"]\n'
                        'target_role_keywords = ["budget"]\n')
        settings = load_settings(path, {"USAJOBS_EMAIL": "me@example.com", "USAJOBS_API_KEY": KEY})
        self.assertEqual(settings.reports_dir, self.folder.resolve() / "out")
        self.assertEqual(settings.decisions_path, self.folder.resolve() / "job_decisions.csv")
        self.assertEqual(settings.history_path, self.folder.resolve() / "out" / "usajobs_job_history.json")
        self.assertIsNone(settings.resume_folder)
        self.assertEqual(settings.config["credentials"], {"email": "me@example.com", "api_key": KEY})

    def test_json_config_loads(self):
        path = self.folder / "config.json"
        path.write_text(json.dumps(MINIMAL))
        settings = load_settings(path, require_credentials=False)
        self.assertEqual(settings.config["search"]["locations"], ["Arlington, Virginia"])

    def test_missing_and_unparseable_files(self):
        with self.assertRaises(ConfigError):
            load_settings(self.folder / "nope.toml", require_credentials=False)
        bad = self.folder / "bad.toml"
        bad.write_text(f'api_key = "{KEY}"\n[search\n')
        with self.assertRaises(ConfigError) as caught:
            load_settings(bad, require_credentials=False)
        self.assertNotIn(KEY, str(caught.exception))


class CredentialTests(unittest.TestCase):
    def test_environment_wins_over_file(self):
        config = {"credentials": {"email": "file@example.com", "api_key": "file-key"}}
        warnings = resolve_credentials(config, {"USAJOBS_EMAIL": "env@example.com", "USAJOBS_API_KEY": KEY})
        self.assertEqual(config["credentials"], {"email": "env@example.com", "api_key": KEY})
        self.assertTrue(any("found in the config file" in w for w in warnings))

    def test_file_credentials_still_work_with_warning(self):
        config = {"credentials": {"email": "file@example.com", "api_key": KEY}}
        warnings = resolve_credentials(config, {})
        self.assertEqual(config["credentials"]["api_key"], KEY)
        self.assertEqual(len(warnings), 1)
        self.assertNotIn(KEY, warnings[0])

    def test_environment_only_has_no_warning(self):
        config = {}
        self.assertEqual(resolve_credentials(config, {"USAJOBS_EMAIL": "e@example.com", "USAJOBS_API_KEY": KEY}), [])

    def test_missing_or_placeholder_credentials_fail_without_values(self):
        config = {"credentials": {"email": "PUT_YOUR_EMAIL_HERE", "api_key": ""}}
        with self.assertRaises(ConfigError) as caught:
            resolve_credentials(config, {"USAJOBS_EMAIL": " "})
        self.assertEqual(caught.exception.errors, [
            "credentials.email: missing; set the USAJOBS_EMAIL environment variable",
            "credentials.api_key: missing; set the USAJOBS_API_KEY environment variable",
        ])


if __name__ == "__main__":
    unittest.main()
