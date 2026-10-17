"""The GitHub Actions workflow passes every secret the package reads, and nothing else."""

from pathlib import Path
import re
import unittest

from usajobs_monitor.config import DELIVERY_SECRET_ENV
from usajobs_monitor.delivery import EMAIL_ENV

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "usajobs-monitor.yml"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_secrets_match_the_package(self):
        expected = {"USAJOBS_EMAIL", "USAJOBS_API_KEY", *EMAIL_ENV.values()}
        for names in DELIVERY_SECRET_ENV.values():
            expected.update(names.values())
        passed = set(re.findall(r"^\s+(\w+): \$\{\{ secrets\.(\w+) \}\}$", self.text, re.M))
        self.assertEqual({env for env, _ in passed}, expected)
        self.assertTrue(all(env == secret for env, secret in passed), "env names must equal secret names")

    def test_secrets_reach_only_the_run_step(self):
        steps = self.text.split("\n      - ")
        with_secrets = [step.splitlines()[0] for step in steps if "secrets." in step]
        self.assertEqual(with_secrets, ["name: Search and deliver"])

    def test_refuses_public_repositories(self):
        self.assertIn("if: github.event.repository.private", self.text)


if __name__ == "__main__":
    unittest.main()
