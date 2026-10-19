"""Delivery boundaries using local fixtures and mocked SMTP and HTTPS. All offline."""

import hashlib
import io
import json
from pathlib import Path
import smtplib
import socket
import ssl
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch
import urllib.error
import zipfile

from usajobs_monitor import delivery, runner
from usajobs_monitor.config import ConfigError, Settings, validate_config
from usajobs_monitor.digest import ATTACHMENT_TYPES

MINIMAL = {"search": {"locations": ["Arlington, Virginia"], "target_role_keywords": ["budget analyst"]}}
SECRET = "app-password-secret"
DISCORD = "https://discord.com/api/webhooks/123/discord-token-secret"
SLACK = "https://hooks.slack.com/services/T0/B0/slack-token-secret"
ENV = {"USAJOBS_SMTP_PASSWORD": SECRET, "USAJOBS_DISCORD_WEBHOOK_URL": DISCORD,
       "USAJOBS_SLACK_WEBHOOK_URL": SLACK, "USAJOBS_NTFY_TOPIC": "topic-secret"}


def office_document(member):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr(member, "<document/>")
    return output.getvalue()


def fake_smtp():
    smtp = Mock()
    smtp.mail.return_value = (250, b"OK")
    smtp.rcpt.return_value = (250, b"OK")
    smtp.data.return_value = (250, b"accepted")
    return smtp


def http_response(status=200):
    response = Mock(status=status)
    response.read.return_value = b"server says secret things"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class DeliveryFixture(unittest.TestCase):
    channels = {"email": {"smtp_host": "smtp.example.com", "username": "sender@example.invalid",
                          "to_addresses": ["recipient@example.invalid"]}}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.run_dir = self.base / "test-run"
        self.run_dir.mkdir()
        self.env = dict(ENV)
        self.set_delivery(json.loads(json.dumps(self.channels)))
        self.summary = {
            "status": "ok", "run_id": "test-run", "finished_at": "2026-10-02T17:00:00-04:00",
            "jobs_reviewed": 270, "usajobs_searches": 105, "matches_after_final_filters": 1,
            "target_agency_matches": 1, "near_misses_reported": 3, "resume_count": 2,
            "watch_list_label": "GS/GG-15 watch list",
            "matches": [{"title": "Budget analyst", "agency": "Example agency", "grade": "GS-13",
                         "promotion_potential": "GS-14", "close_date": "2026-10-15",
                         "eligibility_basis": "Excepted service eligibility requires review",
                         "url": "https://www.usajobs.gov/job/123456789"}],
        }
        self.manifest = {"version": 1, "run_id": "test-run", "status": "ok",
                         "finished_at": self.summary["finished_at"], "attachments": []}
        for name in runner.REPORT_NAMES:
            data = office_document(ATTACHMENT_TYPES[name][1])
            (self.run_dir / name).write_bytes(data)
            self.manifest["attachments"].append({"name": name, "sha256": hashlib.sha256(data).hexdigest()})
        self.write_summary()
        self.smtp_ssl = patch.object(delivery.smtplib, "SMTP_SSL").start()
        self.smtp_plain = patch.object(delivery.smtplib, "SMTP").start()
        self.http = patch.object(delivery, "_open", return_value=http_response()).start()
        self.sleep = patch.object(delivery.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self.smtp = fake_smtp()
        self.smtp_ssl.return_value = self.smtp
        self.smtp_plain.return_value = self.smtp

    def set_delivery(self, delivery_config):
        config, _ = validate_config({**MINIMAL, "delivery": delivery_config})
        self.settings = Settings(config=config, config_path=self.base / "usajobs-monitor.toml",
                                 reports_dir=self.base / "reports", decisions_path=self.base / "d.csv",
                                 resume_folder=None)

    def write_manifest(self):
        (self.run_dir / "manifest.json").write_text(json.dumps(self.manifest))

    def write_summary(self):
        data = json.dumps(self.summary).encode()
        (self.run_dir / runner.SUMMARY).write_bytes(data)
        self.manifest["summary_sha256"] = hashlib.sha256(data).hexdigest()
        self.write_manifest()

    def ledger(self, channel="email"):
        return json.loads((self.run_dir / f"delivery-{channel}.json").read_text())

    def deliver(self, **kwargs):
        return delivery.deliver(self.run_dir, self.settings, env=self.env, **kwargs)


class EmailTests(DeliveryFixture):
    def preview_message(self):
        result = self.deliver(dry_run=True)
        self.assertEqual(result["status"], "preview", result)
        return BytesParser(policy=policy.default).parsebytes((self.run_dir / "delivery-preview" / "email.eml").read_bytes())

    def test_no_channels_is_disabled_without_reading_the_run(self):
        self.set_delivery({})
        self.assertEqual(delivery.deliver(self.base / "missing", self.settings, env=self.env)["status"], "disabled")
        self.set_delivery({"email": {"enabled": False}})
        self.assertEqual(delivery.deliver(self.base / "missing", self.settings, env=self.env)["status"], "disabled")
        self.smtp_ssl.assert_not_called()

    def test_preview_writes_every_format_without_secrets_or_network(self):
        self.set_delivery({})
        self.env = {}
        message = self.preview_message()
        self.assertIn("PREVIEW", str(message["Subject"]))
        self.assertEqual(str(message["From"]), "usajobs-monitor@example.invalid")
        self.assertIn("This message has not been sent", message.get_body(preferencelist=("plain",)).get_content())
        names = {p.name for p in (self.run_dir / "delivery-preview").iterdir()}
        self.assertEqual(names, {"email.eml", "email.txt", "discord.json", "slack.json", "ntfy.json"})
        self.assertFalse(list(self.run_dir.glob("delivery-*.json")))
        self.smtp_ssl.assert_not_called()
        self.http.assert_not_called()

    def test_success_mime_has_exact_two_office_attachments_and_summary(self):
        message = self.preview_message()
        parts = list(message.iter_attachments())
        self.assertEqual({part.get_content_type() for part in parts},
                         {ATTACHMENT_TYPES[name][0] for name in runner.REPORT_NAMES})
        self.assertTrue(all("2026-10-02" in part.get_filename() for part in parts))
        body = message.get_body(preferencelist=("plain",)).get_content()
        for expected in ("270 jobs", "105 searches", "GS-14", "Eligibility:", "Oct 15, 2026",
                         "GS/GG-15 watch list: 0", "are attached"):
            self.assertIn(expected, body)
        self.assertIsNone(message["Cc"])
        self.assertIsNone(message["Bcc"])

    def test_attach_reports_false_sends_summary_only(self):
        self.set_delivery({"email": {**self.channels["email"], "attach_reports": False}})
        message = self.preview_message()
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertNotIn("are attached", message.get_body(preferencelist=("plain",)).get_content())

    def test_zero_matches_still_attaches_both_reports(self):
        self.summary.update(matches=[], matches_after_final_filters=0, target_agency_matches=0)
        self.write_summary()
        message = self.preview_message()
        self.assertIn("0 matches", str(message["Subject"]))
        self.assertEqual(len(list(message.iter_attachments())), 2)

    def test_watch_list_new_and_dropped_sections(self):
        self.summary.update(watch_list_count=1, new_since_last_run={"matches": 1, "watch_list": 0, "near_misses": 1},
                            watch_list=[{"title": "Supervisory budget officer", "flags": ["NEW"]}],
                            top_near_misses=[{"title": "New near", "flags": ["NEW"]}, {"title": "Old near", "flags": []}],
                            dropped_since_last_run=[{"title": "Gone job", "agency": "X", "reason": "closed"}])
        self.write_summary()
        message = self.preview_message()
        self.assertIn("1 watch · 2 new", str(message["Subject"]))
        body = message.get_body(preferencelist=("plain",)).get_content()
        for expected in ("GS/GG-15 WATCH LIST", "Supervisory budget officer [NEW]", "NEW NEAR MISSES", "New near",
                         "DROPPED SINCE LAST RUN", "Gone job (X): closed"):
            self.assertIn(expected, body)
        self.assertNotIn("Old near", body)

    def test_entries_are_compact_with_one_sentence_ai_note(self):
        self.summary["matches"][0].update(ai_level="Possible", ai_fit="Possible (resume: A)",
                                          ai_note="You meet the grade-level time.", ai_gaps="Not shown: X")
        self.write_summary()
        message = self.preview_message()
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Possible fit · Example agency · GS-13 (promotes to GS-14) · closes Oct 15, 2026", body)
        self.assertIn("AI note: You meet the grade-level time.", body)
        for left_to_reports in ("Not shown: X", "resume: A", "Promotion potential"):
            self.assertNotIn(left_to_reports, body)
        markup = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', markup)

    def test_near_misses_all_lists_ranked_entries_every_run(self):
        near = [{"title": f"Near {i}", "flags": [], "ai_level": "Stretch", "ai_note": f"Note {i}.",
                 "ai_gap": f"Not shown on resume: Gap {i}", "exclusion_reasons": [f"Reason {i}"],
                 "pay_plan": "GS", "grade": "15", "promotion_potential": "15"} for i in range(7)]
        self.summary.update(top_near_misses=near[:5], near_misses_reported=7, fit_notes={"ranked": True})
        self.write_summary()
        self.assertNotIn("Near 0", self.preview_message().get_body(preferencelist=("plain",)).get_content())
        self.set_delivery({**self.channels, "near_misses": "all", "max_items": 3})
        message = self.preview_message()
        self.assertIn("· 7 near misses", str(message["Subject"]))
        body = message.get_body(preferencelist=("plain",)).get_content()
        for expected in ("NEAR MISSES (BEST AI FIT FIRST)", "Near 2", "Stretch fit · Not listed · GS-15 · closes",
                         "AI note: Note 2.", "Main gap: Not shown on resume: Gap 2", "Why excluded: Reason 2",
                         "4 more in the reports."):
            self.assertIn(expected, body)
        self.assertNotIn("Near 3", body)
        self.assertNotIn("GS-15 (promotes", body)

    def test_near_misses_setting_is_checked(self):
        with self.assertRaises(ConfigError):
            self.set_delivery({**self.channels, "near_misses": "some"})

    def test_html_escapes_content_and_omits_untrusted_urls(self):
        self.summary["matches"][0].update(title="<script>x</script>", url="https://evil.example/job/1")
        self.write_summary()
        message = self.preview_message()
        markup = message.get_body(preferencelist=("html",)).get_content()
        self.assertNotIn("<script>", markup)
        self.assertNotIn("evil.example", message.as_string())

    def test_failure_email_has_no_attachments_or_raw_exception(self):
        self.summary = {"status": "error", "run_id": "test-run", "finished_at": self.summary["finished_at"],
                        "error": "Traceback: secret detail"}
        self.manifest.update(status="error", attachments=[], failure_message=runner.FAILURE_MESSAGE)
        self.write_summary()
        message = self.preview_message()
        self.assertIn("run failed", str(message["Subject"]))
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertNotIn("secret detail", message.as_string())
        for name in ("discord.json", "slack.json", "ntfy.json"):
            self.assertNotIn("secret detail", (self.run_dir / "delivery-preview" / name).read_text())

    def test_failure_notice_can_be_turned_off(self):
        self.set_delivery({**self.channels, "notify_on_failure": False})
        self.manifest.update(status="error", attachments=[])
        self.summary = {"status": "error", "run_id": "test-run", "finished_at": self.summary["finished_at"]}
        self.write_summary()
        self.assertEqual(self.deliver()["status"], "disabled")
        self.smtp_ssl.assert_not_called()

    def test_failure_manifest_cannot_attach_reports(self):
        self.manifest["status"] = "error"
        self.summary["status"] = "error"
        self.write_summary()
        self.assertEqual(self.deliver()["status"], "failed")

    def test_hash_mismatch_blocks_delivery(self):
        (self.run_dir / runner.REPORT_NAMES[0]).write_bytes(office_document("word/document.xml") + b" ")
        self.assertEqual(self.deliver()["status"], "failed")
        self.smtp_ssl.assert_not_called()

    def test_preview_snapshot_cannot_be_sent(self):
        self.manifest["preview_only"] = True
        self.write_manifest()
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("preview", result["message"])
        self.smtp_ssl.assert_not_called()

    def test_rejects_email_above_encoded_limit(self):
        with patch.object(delivery, "MAX_EMAIL_BYTES", 2000):
            result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("attach_reports", result["channels"]["email"]["message"])
        self.smtp_ssl.assert_not_called()

    def test_invalid_settings_reject_before_connecting(self):
        for key, value in (("smtp_host", ""), ("smtp_host", "bad host"),
                           ("from_address", "sender@example.invalid\r\nBcc: leak@example.invalid"),
                           ("to_addresses", ["a@@example.invalid"]), ("to_addresses", [])):
            with self.subTest(key=key, value=value):
                self.set_delivery({"email": {**self.channels["email"], key: value}})
                self.assertEqual(self.deliver()["status"], "failed")
        self.smtp_ssl.assert_not_called()

    def test_missing_password_names_the_variable_only(self):
        del self.env["USAJOBS_SMTP_PASSWORD"]
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("USAJOBS_SMTP_PASSWORD", result["message"])
        self.smtp_ssl.assert_not_called()

    def test_password_in_config_file_is_an_error(self):
        with self.assertRaises(ConfigError) as caught:
            validate_config({**MINIMAL, "delivery": {"email": {"password": SECRET}}})
        self.assertIn("USAJOBS_SMTP_PASSWORD", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_addresses_can_come_from_environment(self):
        self.set_delivery({"email": {"smtp_host": "smtp.example.com"}})
        self.env.update(USAJOBS_SMTP_USERNAME="env-user@example.invalid",
                        USAJOBS_EMAIL_TO="one@example.invalid, two@example.invalid")
        self.assertEqual(self.deliver()["status"], "accepted")
        self.smtp.login.assert_called_once_with("env-user@example.invalid", SECRET)
        self.smtp.mail.assert_called_once_with("env-user@example.invalid")
        self.assertEqual([c.args[0] for c in self.smtp.rcpt.call_args_list], ["one@example.invalid", "two@example.invalid"])

    def test_no_username_skips_login(self):
        self.set_delivery({"email": {"smtp_host": "smtp.example.com", "from_address": "me@example.invalid",
                                     "to_addresses": ["you@example.invalid"]}})
        del self.env["USAJOBS_SMTP_PASSWORD"]
        self.assertEqual(self.deliver()["status"], "accepted")
        self.smtp.login.assert_not_called()

    def test_accepted_delivery_uses_verified_tls_and_is_deduplicated(self):
        result = self.deliver()
        self.assertEqual(result["status"], "accepted", result)
        self.assertIn("not verified", result["message"])
        args, kwargs = self.smtp_ssl.call_args
        self.assertEqual(args, ("smtp.example.com", 465))
        self.assertTrue(kwargs["context"].check_hostname)
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.smtp.login.assert_called_once_with("sender@example.invalid", SECRET)
        message = BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
        self.assertNotIn("PREVIEW", str(message["Subject"]))
        self.assertTrue(str(message["Message-ID"]).endswith("@example.invalid>"))
        self.assertEqual(self.ledger()["status"], "accepted")
        again = self.deliver()
        self.assertEqual(again["status"], "already_accepted")
        self.assertEqual(self.smtp.data.call_count, 1)
        self.assertNotIn(SECRET, (self.run_dir / "delivery-email.json").read_text() + json.dumps(again))

    def test_starttls_mode(self):
        self.set_delivery({"email": {**self.channels["email"], "security": "starttls"}})
        self.assertEqual(self.settings.config["delivery"]["email"]["smtp_port"], 587)
        self.assertEqual(self.deliver()["status"], "accepted")
        self.assertEqual(self.smtp_plain.call_args.args, ("smtp.example.com", 587))
        context = self.smtp.starttls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.smtp_ssl.assert_not_called()

    def test_starttls_not_offered_stops(self):
        self.set_delivery({"email": {**self.channels["email"], "security": "starttls"}})
        self.smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("no")
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.smtp.login.assert_not_called()
        self.assertEqual(self.smtp_plain.call_count, 1)

    def test_transient_pre_submission_network_failure_retries(self):
        self.smtp_ssl.side_effect = [OSError("sensitive network detail"), self.smtp]
        result = self.deliver()
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(result["channels"]["email"]["attempt"], 2)
        self.sleep.assert_called_once_with(2)

    def test_temporary_recipient_rejection_retries_without_early_data(self):
        first = fake_smtp()
        first.rcpt.return_value = (450, b"private response")
        self.smtp_ssl.side_effect = [first, self.smtp]
        self.assertEqual(self.deliver()["status"], "accepted")
        first.data.assert_not_called()

    def test_temporary_data_rejection_retries(self):
        self.smtp.data.side_effect = [smtplib.SMTPDataError(451, b"private"), (250, b"OK")]
        self.assertEqual(self.deliver()["status"], "accepted")
        self.assertEqual(self.smtp.data.call_count, 2)

    def test_permanent_rejection_stops_and_hides_server_text(self):
        self.smtp.rcpt.return_value = (550, b"sensitive server response")
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.smtp_ssl.call_count, 1)
        self.assertNotIn("sensitive", json.dumps(result))
        self.smtp.data.assert_not_called()

    def test_authentication_failure_stops_and_redacts_response(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, SECRET.encode())
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.smtp_ssl.call_count, 1)
        self.assertNotIn(SECRET, json.dumps(result) + json.dumps(self.ledger()))

    def test_tls_verification_failure_stops(self):
        self.smtp_ssl.side_effect = ssl.SSLCertVerificationError("sensitive hostname")
        result = self.deliver()
        self.assertEqual(self.smtp_ssl.call_count, 1)
        self.assertIn("TLS certificate", result["message"])

    def test_retry_count_is_bounded(self):
        self.smtp_ssl.side_effect = TimeoutError("secret detail")
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.smtp_ssl.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertNotIn("secret detail", json.dumps(result))

    def test_disconnect_during_data_is_unknown_and_never_retried(self):
        def disconnect(_payload):
            self.assertEqual(self.ledger()["status"], "sending")
            raise smtplib.SMTPServerDisconnected("secret server reply")
        self.smtp.data.side_effect = disconnect
        result = self.deliver()
        self.assertEqual(result["status"], "unknown", result)
        self.assertTrue(self.deliver()["channels"]["email"]["duplicate"])
        self.assertEqual(self.smtp_ssl.call_count, 1)
        self.assertNotIn("secret server reply", json.dumps(result) + json.dumps(self.ledger()))

    def test_interrupted_data_leaves_sending_record_that_blocks_resend(self):
        self.smtp.data.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.deliver()
        self.assertEqual(self.ledger()["status"], "sending")
        self.assertEqual(self.deliver()["status"], "unknown")
        self.assertEqual(self.smtp.data.call_count, 1)

    def test_acceptance_record_failure_keeps_sending_record_and_blocks_resend(self):
        original = delivery._record
        def record(*args, **kwargs):
            if args[2] == "accepted":
                raise delivery.DeliveryError("disk unavailable")
            return original(*args, **kwargs)
        with patch.object(delivery, "_record", side_effect=record):
            result = self.deliver()
        self.assertEqual(result["status"], "accepted", result)
        self.assertFalse(result["channels"]["email"]["record_saved"])
        self.assertEqual(self.ledger()["status"], "sending")
        self.assertEqual(self.deliver()["status"], "unknown")
        self.assertEqual(self.smtp.data.call_count, 1)

    def test_held_lock_prevents_parallel_sender(self):
        with delivery.exclusive_lock(self.run_dir / ".delivery.lock", "busy"):
            result = self.deliver()
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["duplicate"])
        self.smtp_ssl.assert_not_called()


class WebhookTests(DeliveryFixture):
    channels = {"discord": {}, "slack": {}, "ntfy": {}}

    def sent(self, index=0):
        request = self.http.call_args_list[index].args[0]
        return request, json.loads(request.data)

    def test_all_three_accepted_and_deduplicated(self):
        result = self.deliver()
        self.assertEqual(result["status"], "accepted", result)
        self.assertEqual(set(result["channels"]), {"discord", "slack", "ntfy"})
        urls = [call.args[0].full_url for call in self.http.call_args_list]
        self.assertEqual(urls, [DISCORD + "?wait=true", SLACK, "https://ntfy.sh/"])
        self.assertEqual(self.deliver()["status"], "already_accepted")
        self.assertEqual(self.http.call_count, 3)
        written = json.dumps(result) + "".join(p.read_text() for p in self.run_dir.glob("delivery-*.json"))
        for secret in ("discord-token-secret", "slack-token-secret", "topic-secret"):
            self.assertNotIn(secret, written)

    def test_discord_payload_blocks_mentions_and_fits(self):
        self.summary["matches"] = [dict(self.summary["matches"][0], title=f"@everyone job {i} " + "x" * 200)
                                   for i in range(30)]
        self.summary["matches_after_final_filters"] = 30
        self.write_summary()
        self.set_delivery({"discord": {}, "max_items": 25})
        self.deliver()
        _, body = self.sent()
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertLessEqual(len(body["content"]), 2000)
        self.assertIn("more entries are in the reports", body["content"])
        self.assertIn("<https://www.usajobs.gov/job/123456789>", body["content"])

    def test_slack_escapes_markup(self):
        self.summary["matches"][0]["title"] = "<!channel> & co"
        self.write_summary()
        self.set_delivery({"slack": {}})
        self.deliver()
        _, body = self.sent()
        self.assertIn("&lt;!channel&gt; &amp; co", body["text"])
        self.assertFalse(body["unfurl_links"])

    def test_ntfy_topic_token_and_click(self):
        self.env["USAJOBS_NTFY_TOKEN"] = "tk_secret"
        self.set_delivery({"ntfy": {"server": "https://ntfy.example.com/", "priority": 4}})
        self.deliver()
        request, body = self.sent()
        self.assertEqual(request.full_url, "https://ntfy.example.com/")
        self.assertEqual(request.get_header("Authorization"), "Bearer tk_secret")
        self.assertEqual((body["topic"], body["priority"]), ("topic-secret", 4))
        self.assertEqual(body["click"], "https://www.usajobs.gov/job/123456789")
        self.assertLessEqual(len(body["message"].encode()), 4000)

    def test_wrong_webhook_host_is_refused_without_echoing_url(self):
        self.env["USAJOBS_DISCORD_WEBHOOK_URL"] = "https://evil.example/api/webhooks/1/token-secret"
        self.set_delivery({"discord": {}})
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("USAJOBS_DISCORD_WEBHOOK_URL", result["message"])
        self.assertNotIn("token-secret", json.dumps(result))
        self.http.assert_not_called()

    def test_webhook_url_in_config_file_is_an_error(self):
        with self.assertRaises(ConfigError):
            validate_config({**MINIMAL, "delivery": {"slack": {"webhook_url": SLACK}}})

    def test_custom_env_variable_name(self):
        self.env["MY_HOOK"] = self.env.pop("USAJOBS_SLACK_WEBHOOK_URL")
        self.set_delivery({"slack": {"webhook_url_env": "MY_HOOK"}})
        self.assertEqual(self.deliver()["status"], "accepted")

    def test_connection_refused_retries_then_succeeds(self):
        self.set_delivery({"discord": {}})
        self.http.side_effect = [urllib.error.URLError(ConnectionRefusedError()), http_response()]
        result = self.deliver()
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.http.call_count, 2)

    def test_rate_limit_retries_but_bad_request_stops(self):
        self.set_delivery({"discord": {}})
        self.http.side_effect = [urllib.error.HTTPError(DISCORD, 429, "secret", {}, None), http_response()]
        self.assertEqual(self.deliver()["status"], "accepted")
        (self.run_dir / "delivery-discord.json").unlink()
        self.http.reset_mock()
        self.http.side_effect = [urllib.error.HTTPError(DISCORD, 404, "secret", {}, None)]
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("HTTP 404", result["message"])
        self.assertEqual(self.http.call_count, 1)

    def test_timeout_after_sending_is_unknown_and_not_resent(self):
        self.set_delivery({"discord": {}})
        self.http.side_effect = urllib.error.URLError(socket.timeout("secret"))
        self.assertEqual(self.deliver()["status"], "unknown")
        self.assertEqual(self.deliver()["status"], "unknown")
        self.assertEqual(self.http.call_count, 1)

    def test_retry_sends_only_to_channels_that_failed(self):
        self.http.side_effect = [http_response(), urllib.error.HTTPError(SLACK, 400, "x", {}, None), http_response()]
        result = self.deliver()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Accepted by discord, ntfy", result["message"])
        self.http.reset_mock(side_effect=True)
        self.http.return_value = http_response()
        result = self.deliver()
        self.assertEqual(result["status"], "accepted")
        self.assertEqual([c.args[0].full_url for c in self.http.call_args_list], [SLACK])

    def test_check_channels_reports_without_values(self):
        rows = dict(delivery.check_channels(self.settings.config, {"USAJOBS_SLACK_WEBHOOK_URL": SLACK}))
        self.assertEqual(rows["slack"], "ready")
        self.assertIn("USAJOBS_DISCORD_WEBHOOK_URL", rows["discord"])
        self.assertIn("USAJOBS_NTFY_TOPIC", rows["ntfy"])


class RunnerDeliveryTests(DeliveryFixture):
    channels = {"discord": {}}

    def test_failed_search_sends_only_the_fixed_notice(self):
        reports = self.base / "reports"
        settings = Settings(config=self.settings.config, config_path=self.settings.config_path,
                            reports_dir=reports, decisions_path=self.base / "d.csv", resume_folder=None)

        def failing(settings, out):
            raise RuntimeError("Traceback with secret detail")
        with patch.object(runner, "run_search", side_effect=failing):
            code, outcome = runner.run(settings, lambda run_dir: delivery.deliver(run_dir, settings, env=self.env))
        self.assertEqual((code, outcome["search_status"], outcome["delivery_status"]), (1, "error", "accepted"))
        body = json.loads(self.http.call_args.args[0].data)
        self.assertIn("run failed", body["content"])
        self.assertNotIn("secret detail", body["content"])
        self.assertFalse((reports / runner.REPORT_NAMES[0]).exists())


if __name__ == "__main__":
    unittest.main()
