"""Offline checks for isolated report runs, publishing, the lock, and the delivery hook."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from usajobs_monitor import runner
from usajobs_monitor.config import Settings


def make_reports(folder, *, when=None):
    when = when or datetime.now(timezone.utc)
    for name, member in zip(runner.REPORT_NAMES, ("word/document.xml", "xl/workbook.xml")):
        with zipfile.ZipFile(folder / name, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr(member, "<document />")
    summary = {"status": "ok", "started_at": when.isoformat(), "finished_at": when.isoformat(),
               "jobs_reviewed": 1, "usajobs_searches": 1, "matches_after_final_filters": 0,
               "target_agency_matches": 0, "near_misses_reported": 1, "matches": [], "resume_count": 0}
    (folder / runner.SUMMARY).write_text(json.dumps(summary))
    return summary


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.reports = self.base / "reports"
        self.reports.mkdir()
        self.settings = Settings(config={}, config_path=self.base / "usajobs-monitor.toml",
                                 reports_dir=self.reports, decisions_path=self.base / "job_decisions.csv",
                                 resume_folder=None)

    def search(self, side_effect):
        return patch.object(runner, "run_search", side_effect=side_effect)

    def test_success_publishes_complete_run_and_manifest_hashes(self):
        with self.search(lambda settings, out: make_reports(out)):
            folder, success = runner.execute_search(self.settings)
        self.assertTrue(success)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["summary_sha256"], runner.sha256(folder / runner.SUMMARY))
        self.assertEqual(len(manifest["attachments"]), 2)
        self.assertEqual(json.loads((self.reports / runner.SUMMARY).read_text())["run_id"], folder.name)
        for name in runner.REPORT_NAMES:
            self.assertEqual((self.reports / name).read_bytes(), (folder / name).read_bytes())

    def test_history_published_only_with_successful_run(self):
        def child(settings, out):
            make_reports(out)
            (out / runner.HISTORY).write_text('{"jobs": {"1": {}}}')
        with self.search(child):
            runner.execute_search(self.settings)
        self.assertEqual(json.loads((self.reports / runner.HISTORY).read_text()), {"jobs": {"1": {}}})

        def failing(settings, out):
            (out / runner.HISTORY).write_text('{"jobs": {}}')
            raise RuntimeError("search failed")
        with self.search(failing):
            _, success = runner.execute_search(self.settings)
        self.assertFalse(success)
        self.assertEqual(json.loads((self.reports / runner.HISTORY).read_text()), {"jobs": {"1": {}}})

    def test_failure_does_not_reuse_successful_old_files_or_leak_error_text(self):
        make_reports(self.reports)
        original = (self.reports / runner.REPORT_NAMES[0]).read_bytes()
        with self.search(RuntimeError("secret API body")):
            folder, success = runner.execute_search(self.settings)
        self.assertFalse(success)
        self.assertEqual((self.reports / runner.REPORT_NAMES[0]).read_bytes(), original)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["attachments"], [])
        self.assertEqual(json.loads((self.reports / runner.SUMMARY).read_text())["status"], "error")
        for path in (folder / runner.SUMMARY, folder / "manifest.json", self.reports / runner.SUMMARY):
            self.assertNotIn("secret", path.read_text())

    def test_failed_search_summary_with_traceback_is_replaced(self):
        def child(settings, out):
            (out / runner.SUMMARY).write_text(json.dumps({"status": "error", "traceback": "secret frame"}))
            raise RuntimeError("boom")
        with self.search(child):
            folder, success = runner.execute_search(self.settings)
        self.assertFalse(success)
        self.assertNotIn("secret", (folder / runner.SUMMARY).read_text())

    def test_partial_generation_is_failure(self):
        def child(settings, out):
            make_reports(out)
            (out / runner.REPORT_NAMES[1]).unlink()
        with self.search(child):
            folder, success = runner.execute_search(self.settings)
        self.assertFalse(success)
        self.assertEqual(json.loads((folder / "manifest.json").read_text())["attachments"], [])

    def test_stale_success_summary_rejected(self):
        make_reports(self.reports, when=datetime.now(timezone.utc) - timedelta(days=1))
        with self.assertRaises(ValueError):
            runner.validate_reports(self.reports, datetime.now(timezone.utc))

    def test_corrupt_docx_rejected(self):
        make_reports(self.reports)
        (self.reports / runner.REPORT_NAMES[0]).write_bytes(b"not a zip")
        with self.assertRaises(zipfile.BadZipFile):
            runner.validate_reports(self.reports)

    def test_preview_uses_copies_and_cannot_be_selected_for_send(self):
        make_reports(self.reports)
        original = (self.reports / runner.SUMMARY).read_bytes()
        folder = runner.preview_current(self.reports)
        self.assertEqual((self.reports / runner.SUMMARY).read_bytes(), original)
        self.assertTrue(json.loads((folder / "manifest.json").read_text())["preview_only"])
        with self.assertRaises(ValueError):
            runner.select_run(self.reports, folder.name)

    def test_run_selection_rejects_path_escape_and_symlink(self):
        with self.assertRaises(ValueError):
            runner.select_run(self.reports, "../elsewhere")
        folder = runner.new_run_dir(self.reports)
        target = self.base / "outside"
        target.mkdir()
        folder.rmdir()
        folder.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            runner.select_run(self.reports, folder.name)

    def test_overlap_rejected(self):
        with runner.run_lock(self.reports):
            with self.assertRaises(RuntimeError):
                with runner.run_lock(self.reports):
                    pass
        with runner.run_lock(self.reports):
            pass  # released after the first holder exits

    def test_malformed_summary_becomes_safe_failure(self):
        for bad in ([], {"status": "ok", "started_at": None}):
            def child(settings, out):
                (out / runner.SUMMARY).write_text(json.dumps(bad))
            with self.subTest(summary=bad), self.search(child):
                folder, success = runner.execute_search(self.settings)
            self.assertFalse(success)
            self.assertEqual(json.loads((folder / "manifest.json").read_text())["attachments"], [])

    def test_run_without_delivery_hook_reports_disabled(self):
        with self.search(lambda settings, out: make_reports(out)):
            code, outcome = runner.run(self.settings)
        self.assertEqual(code, 0)
        self.assertEqual((outcome["search_status"], outcome["delivery_status"]), ("ok", "disabled"))
        self.assertEqual(json.loads((self.reports / runner.DELIVERY_LAST_RUN).read_text()), outcome)

    def test_failed_search_exit_code(self):
        with self.search(RuntimeError("x")):
            code, outcome = runner.run(self.settings)
        self.assertEqual((code, outcome["search_status"]), (1, "error"))

    def test_delivery_exception_records_new_outcome_without_losing_search_success(self):
        folder = runner.new_run_dir(self.reports)

        def deliver(run_dir):
            raise RuntimeError("sensitive remote data")
        with patch.object(runner, "execute_search", return_value=(folder, True)):
            code, _ = runner.run(self.settings, deliver)
        self.assertEqual(code, 2)
        outcome = json.loads((self.reports / runner.DELIVERY_LAST_RUN).read_text())
        self.assertEqual(outcome["search_status"], "ok")
        self.assertEqual(outcome["delivery_status"], "unknown")
        self.assertEqual(outcome["run_id"], folder.name)
        self.assertNotIn("sensitive", json.dumps(outcome))

    def test_retry_older_run_preserves_latest_search_delivery_pointer(self):
        folder = runner.new_run_dir(self.reports)
        runner.prepare_manifest(folder, make_reports(folder))
        (self.reports / runner.SUMMARY).write_text(json.dumps({"run_id": "newer-run"}))
        latest = self.reports / runner.DELIVERY_LAST_RUN
        latest.write_text('{"run_id": "newer-run"}')
        before = latest.read_bytes()
        code, _ = runner.run(self.settings, lambda run_dir: {"status": "accepted"}, deliver_run=folder.name)
        self.assertEqual(code, 0)
        self.assertEqual(latest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
