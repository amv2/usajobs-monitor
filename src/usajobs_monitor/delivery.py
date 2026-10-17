"""Send a validated run by email, Discord, Slack or ntfy.

Delivery reads only the run's manifest, summary and the two hashed reports. A
failed run sends a fixed failure notice with no attachments. Secrets come only
from environment variables, and every message this module returns or records
is fixed text that never includes a secret, a server response or an exception.

Each channel keeps a ledger in the run folder (``delivery-<channel>.json``).
Once a channel records "sending", "accepted" or "unknown" for a run, it never
sends that run again, so a retry delivers only to the channels that failed.
"Accepted" means the server took the message; it does not prove receipt.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import smtplib
import socket
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.errors import MessageError
from email.headerregistry import Address
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .config import DELIVERY_CHANNELS, Settings
from .digest import (
    ATTACHMENT_TYPES,
    PREVIEW_FROM,
    PREVIEW_TO,
    Digest,
    build_digest,
    chat_text,
    email_message,
)
from .locking import LockBusy, exclusive_lock
from .reports import SUMMARY_NAME

MAX_EMAIL_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_RECIPIENTS = 10
DEDUPLICATE_STATES = {"sending", "accepted", "unknown"}
PREVIEW_DIR = "delivery-preview"
DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
# Address settings can also come from the environment, so a public config
# (for example in a GitHub fork) needs no personal addresses.
EMAIL_ENV = {"username": "USAJOBS_SMTP_USERNAME", "from_address": "USAJOBS_EMAIL_FROM",
             "to_addresses": "USAJOBS_EMAIL_TO"}


class DeliveryError(Exception):
    """A fixed, safe diagnostic; never wrap server output or credentials."""


# --- reading the run -------------------------------------------------------

def _read_file(path: Path, limit: int) -> bytes:
    """Read a regular file only, without following a final symlink."""
    try:
        if path.is_symlink():
            raise DeliveryError("A required local file is a symbolic link.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise DeliveryError("A required local file is not a regular file or exceeds its size limit.")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise DeliveryError("A required local file exceeds its size limit.")
        return data
    except (OSError, ValueError):
        raise DeliveryError("A required local file is missing or cannot be read safely.") from None


def _json_object(data: bytes) -> dict:
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (UnicodeError, ValueError):
        raise DeliveryError("A local JSON file is invalid.") from None


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise DeliveryError("Refusing to replace a symbolic link.")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":  # Windows cannot open a directory to fsync it.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError:
        raise DeliveryError("Could not safely save the local delivery record or preview.") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)  # type: ignore[arg-type]
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise DeliveryError("Run timestamps must include a time zone.") from None


def _verify_hash(data: bytes, expected: object) -> None:
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise DeliveryError("The run manifest contains an invalid file hash.")
    if hashlib.sha256(data).hexdigest() != expected:
        raise DeliveryError("A report or summary does not match this run's manifest.")


def _validate_zip(data: bytes, required_member: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if (len(members) > 5000 or len(set(names)) != len(names)
                    or sum(member.file_size for member in members) > MAX_ZIP_EXPANDED_BYTES):
                raise ValueError
            for member in members:
                name = member.filename
                if (name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/")
                        or member.flag_bits & 1 or stat.S_ISLNK(member.external_attr >> 16)):
                    raise ValueError
            for member_name in ("[Content_Types].xml", required_member):
                if member_name not in names or archive.getinfo(member_name).file_size > MAX_JSON_BYTES:
                    raise ValueError
                xml = archive.read(member_name)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError
                ElementTree.fromstring(xml)
            if archive.testzip() is not None:
                raise ValueError
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, ElementTree.ParseError):
        raise DeliveryError("An attachment is not a readable Office document.") from None


@dataclass
class Run:
    path: Path
    manifest: dict
    summary: dict
    finished: datetime
    attachments: list[tuple[str, bytes]]

    @property
    def run_id(self) -> str:
        return self.manifest["run_id"]


def validated_run(run_dir: Path) -> Run:
    """Check the manifest, summary hash and both reports before anything is sent."""
    run_dir = Path(run_dir).absolute()
    if run_dir.is_symlink() or not run_dir.is_dir() or run_dir != run_dir.resolve():
        raise DeliveryError("The run directory must be a real directory without symbolic links.")
    manifest = _json_object(_read_file(run_dir / "manifest.json", MAX_JSON_BYTES))
    run_id = manifest.get("run_id")
    if (manifest.get("version") != 1 or not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", run_id)
            or manifest.get("status") not in {"ok", "error"}):
        raise DeliveryError("The run manifest has an invalid version, identifier, or status.")
    finished = _timestamp(manifest.get("finished_at"))
    summary_data = _read_file(run_dir / SUMMARY_NAME, MAX_JSON_BYTES)
    _verify_hash(summary_data, manifest.get("summary_sha256"))
    summary = _json_object(summary_data)
    if _timestamp(summary.get("finished_at")) != finished:
        raise DeliveryError("The summary timestamp does not match this run.")
    if summary.get("run_id", run_id) != run_id or summary.get("status") != manifest["status"]:
        raise DeliveryError("The summary identifier or status does not match this run.")
    entries = manifest.get("attachments")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise DeliveryError("The manifest attachment list is invalid.")
    if manifest["status"] == "error":
        if entries:
            raise DeliveryError("A failed run must not attach reports.")
        return Run(run_dir, manifest, summary, finished, [])
    names = [item.get("name") for item in entries]
    if len(names) != len(ATTACHMENT_TYPES) or set(names) != set(ATTACHMENT_TYPES):
        raise DeliveryError("A successful run must contain exactly the Word briefing and Excel tracker.")
    matches = summary.get("matches")
    if not isinstance(matches, list) or any(not isinstance(match, dict) for match in matches):
        raise DeliveryError("The run summary has an invalid matches list.")
    match_count = summary.get("matches_after_final_filters")
    if type(match_count) is not int or match_count < 0 or match_count != len(matches):
        raise DeliveryError("The run summary match count is inconsistent.")
    for key in ("jobs_reviewed", "usajobs_searches", "target_agency_matches", "near_misses_reported"):
        value = summary.get(key)
        if type(value) is not int or value < 0:
            raise DeliveryError("The run summary contains invalid search counts.")
    attachments = []
    for entry in entries:
        name = entry["name"]
        data = _read_file(run_dir / name, MAX_EMAIL_BYTES)
        _verify_hash(data, entry.get("sha256"))
        _validate_zip(data, ATTACHMENT_TYPES[name][1])
        attachments.append((name, data))
    return Run(run_dir, manifest, summary, finished, attachments)


# --- channels --------------------------------------------------------------

Outcome = tuple[str, str, bool]  # (status, safe message, retry)
MarkSending = Callable[[], None]


def _address(value: object) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise DeliveryError("Email settings need valid sender, recipient, and SMTP username addresses.")
    try:
        parsed = Address(addr_spec=value)
        if not parsed.username or not parsed.domain or parsed.addr_spec != value:
            raise ValueError
        value.encode("ascii")
    except (ValueError, IndexError, UnicodeError, MessageError):
        raise DeliveryError("Email settings need valid sender, recipient, and SMTP username addresses.") from None
    return value


def _env_secret(env: Mapping[str, str], name: str, what: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise DeliveryError(f"{what} is missing; set the {name} environment variable.")
    return value


class Channel:
    name = ""
    label = ""

    def __init__(self, settings: dict[str, Any], timeout: int):
        self.settings = settings
        self.timeout = timeout

    def prepare(self, env: Mapping[str, str]) -> None:
        """Resolve secrets and check settings. Raises ``DeliveryError``."""

    def payload(self, digest: Digest, run: Run) -> bytes:
        raise NotImplementedError

    def preview(self, digest: Digest, run: Run, folder: Path, env: Mapping[str, str]) -> list[Path]:
        """Write what would be sent, with placeholders for anything secret."""
        path = folder / f"{self.name}.json"
        _atomic_write(path, self.payload(digest, run))
        return [path]

    def submit(self, payload: bytes, mark_sending: MarkSending) -> Outcome:
        raise NotImplementedError

    def accepted_message(self) -> str:
        return f"{self.label} accepted the message. Receipt is not verified."


class EmailChannel(Channel):
    name, label = "email", "The SMTP server"

    def addresses(self, env: Mapping[str, str], *, preview: bool = False) -> tuple[str, str, list[str]]:
        s = self.settings
        username = str(env.get(EMAIL_ENV["username"]) or s["username"]).strip()
        sender = str(env.get(EMAIL_ENV["from_address"]) or s["from_address"] or username).strip()
        to_env = str(env.get(EMAIL_ENV["to_addresses"]) or "")
        recipients = [a.strip() for a in to_env.split(",") if a.strip()] if to_env else list(s["to_addresses"])
        if preview:
            def safe(value: str, fallback: str) -> str:
                try:
                    return _address(value)
                except DeliveryError:
                    return fallback
            return username, safe(sender, PREVIEW_FROM), [safe(r, PREVIEW_TO) for r in recipients] or [PREVIEW_TO]
        if username:
            _address(username)
        _address(sender)
        if not recipients:
            raise DeliveryError("Email needs at least one recipient in delivery.email.to_addresses.")
        if len(recipients) > MAX_RECIPIENTS:
            raise DeliveryError(f"Email allows at most {MAX_RECIPIENTS} recipients.")
        return username, sender, [_address(r) for r in recipients]

    def prepare(self, env: Mapping[str, str]) -> None:
        host = self.settings["smtp_host"]
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host or ""):
            raise DeliveryError("Email needs delivery.email.smtp_host, a server name like smtp.example.com.")
        self.username, self.sender, self.recipients = self.addresses(env)
        self.password = (_env_secret(env, self.settings["password_env"], "The SMTP password")
                         if self.username else "")

    def message_bytes(self, digest: Digest, run: Run, sender: str, recipients: list[str]) -> tuple[bytes, str]:
        attachments = run.attachments if self.settings["attach_reports"] else []
        message, body = email_message(digest, attachments, sender=sender, recipients=recipients)
        data = message.as_bytes()
        if len(data) > MAX_EMAIL_BYTES:
            raise DeliveryError("The encoded email exceeds the 20 MiB limit. "
                                "Set delivery.email.attach_reports = false to send the summary only.")
        return data, body

    def payload(self, digest: Digest, run: Run) -> bytes:
        return self.message_bytes(digest, run, self.sender, self.recipients)[0]

    def preview(self, digest: Digest, run: Run, folder: Path, env: Mapping[str, str]) -> list[Path]:
        _, sender, recipients = self.addresses(env, preview=True)
        data, body = self.message_bytes(digest, run, sender, recipients)
        eml, txt = folder / "email.eml", folder / "email.txt"
        _atomic_write(eml, data)
        _atomic_write(txt, body.encode())
        return [eml, txt]

    def _rejection(self, code: int, phase: str) -> tuple[str, bool]:
        return f"The SMTP server rejected the {phase} step (SMTP {code}).", 400 <= code < 500

    def submit(self, payload: bytes, mark_sending: MarkSending) -> Outcome:
        host, port = self.settings["smtp_host"], self.settings["smtp_port"]
        context = ssl.create_default_context()
        smtp = None
        data_started = False
        try:
            if self.settings["security"] == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=self.timeout, context=context)
            else:
                smtp = smtplib.SMTP(host, port, timeout=self.timeout)
                smtp.starttls(context=context)
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            code, _ = smtp.mail(self.sender)
            if code != 250:
                return ("failed", *self._rejection(code, "sender"))
            for recipient in self.recipients:
                code, _ = smtp.rcpt(recipient)
                if code not in (250, 251, 252):
                    return ("failed", *self._rejection(code, "recipient"))
            mark_sending()
            data_started = True
            code, _ = smtp.data(payload)
            if code != 250:
                return ("failed", *self._rejection(code, "message"))
            return "accepted", "The SMTP server accepted the message. Inbox receipt is not verified.", False
        except smtplib.SMTPAuthenticationError:
            return "failed", "SMTP authentication failed. Check the username and the password variable.", False
        except smtplib.SMTPDataError as exc:
            # smtplib raises this when DATA is refused before it transmits the body.
            return ("failed", *self._rejection(exc.smtp_code, "message"))
        except smtplib.SMTPNotSupportedError:
            return "failed", "The SMTP server did not offer a required feature (STARTTLS or authentication).", False
        except smtplib.SMTPResponseException as exc:
            if data_started:
                return "unknown", "SMTP acceptance is uncertain. Review this run before resending.", False
            return ("failed", *self._rejection(exc.smtp_code, "connection"))
        except ssl.SSLCertVerificationError:
            return "failed", "SMTP TLS certificate verification failed.", False
        except (OSError, smtplib.SMTPException):
            if data_started:
                return "unknown", "SMTP acceptance is uncertain. Review this run before resending.", False
            return "failed", "Could not complete the SMTP connection before message submission.", True
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except (OSError, smtplib.SMTPException):
                    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None  # a redirect is reported as an HTTP error, never followed


def _open(request: urllib.request.Request, timeout: int):
    """POST with certificate checks and no redirects. Honors HTTPS_PROXY."""
    opener = urllib.request.build_opener(_NoRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return opener.open(request, timeout=timeout)


class WebhookChannel(Channel):
    """JSON over HTTPS. Only connection failures that prove nothing was sent are retried."""

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        return {}

    def submit(self, payload: bytes, mark_sending: MarkSending) -> Outcome:
        request = urllib.request.Request(self.url(), data=payload, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "usajobs-monitor", **self.headers()})
        mark_sending()
        try:
            with _open(request, self.timeout) as response:
                code = response.status
                response.read(65536)
        except urllib.error.HTTPError as exc:
            code = exc.code
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                return "failed", f"{self.label} TLS certificate verification failed.", False
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
                return "failed", f"Could not connect to {self.label}.", True
            return "unknown", f"{self.label} acceptance is uncertain. Review this run before resending.", False
        except (OSError, ValueError):
            return "unknown", f"{self.label} acceptance is uncertain. Review this run before resending.", False
        if 200 <= code < 300:
            return "accepted", self.accepted_message(), False
        if code == 429 or code >= 500:
            return "failed", f"{self.label} did not accept the message (HTTP {code}).", True
        return "failed", f"{self.label} rejected the message (HTTP {code}). Check the webhook settings.", False


def _https_url(value: str, what: str, hosts: set[str] | None = None, path_prefix: str = "") -> str:
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or (hosts is not None and url.hostname.lower() not in hosts)
                or not url.path.startswith(path_prefix)):
            raise ValueError
    except ValueError:
        raise DeliveryError(f"{what} is not a valid address for this channel.") from None
    return value


def _markdown_escape(value: str) -> str:
    return re.sub(r"([\\*_~`|>#\[\]])", r"\\\1", value)


class DiscordChannel(WebhookChannel):
    name, label = "discord", "Discord"
    LIMIT = 2000

    def prepare(self, env: Mapping[str, str]) -> None:
        var = self.settings["webhook_url_env"]
        self.webhook = _https_url(_env_secret(env, var, "The Discord webhook URL"), f"The URL in {var}",
                                  DISCORD_HOSTS, "/api/webhooks/")

    def url(self) -> str:
        # wait=true makes Discord answer only after the message is created.
        return self.webhook + ("&" if "?" in self.webhook else "?") + "wait=true"

    def payload(self, digest: Digest, run: Run) -> bytes:
        content = chat_text(digest, limit=self.LIMIT, link="<{url}>", bold="**{}**", heading="__{}__",
                            escape=_markdown_escape)
        return json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()


def _slack_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackChannel(WebhookChannel):
    name, label = "slack", "Slack"
    LIMIT = 3500

    def prepare(self, env: Mapping[str, str]) -> None:
        var = self.settings["webhook_url_env"]
        self.webhook = _https_url(_env_secret(env, var, "The Slack webhook URL"), f"The URL in {var}",
                                  {"hooks.slack.com"}, "/services/")

    def url(self) -> str:
        return self.webhook

    def payload(self, digest: Digest, run: Run) -> bytes:
        text = chat_text(digest, limit=self.LIMIT, link="<{url}|View announcement>", bold="*{}*",
                         heading="*{}*", escape=_slack_escape)
        return json.dumps({"text": text, "unfurl_links": False, "unfurl_media": False}).encode()


class NtfyChannel(WebhookChannel):
    name, label = "ntfy", "ntfy"
    LIMIT_BYTES = 4000

    def prepare(self, env: Mapping[str, str]) -> None:
        self.server = _https_url(self.settings["server"], "delivery.ntfy.server").rstrip("/")
        var = self.settings["topic_env"]
        self.topic = _env_secret(env, var, "The ntfy topic")
        if not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", self.topic):
            raise DeliveryError(f"The topic in {var} must be 1-64 letters, digits, - or _.")
        self.token = str(env.get(self.settings["token_env"]) or "").strip()

    def url(self) -> str:
        return self.server + "/"  # JSON publishing posts to the server root

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def payload(self, digest: Digest, run: Run) -> bytes:
        limit = 3800
        while True:
            message = chat_text(digest, limit=limit)
            if len(message.encode()) <= self.LIMIT_BYTES or limit < 500:
                break
            limit -= 300
        body: dict[str, Any] = {"topic": getattr(self, "topic", "preview-topic"), "title": digest.subject,
                                "message": message, "priority": self.settings["priority"],
                                "tags": ["briefcase" if digest.ok else "warning"]}
        first = next((item.url for section in digest.sections for item in section.items if item.url), "")
        if first:
            body["click"] = first
        return json.dumps(body).encode()


CHANNEL_TYPES: dict[str, type[Channel]] = {
    "email": EmailChannel, "discord": DiscordChannel, "slack": SlackChannel, "ntfy": NtfyChannel,
}


def enabled_channels(config: Mapping[str, Any]) -> list[Channel]:
    delivery = config.get("delivery") or {}
    timeout = delivery.get("timeout_seconds", 30)
    return [CHANNEL_TYPES[name](delivery[name], timeout) for name in DELIVERY_CHANNELS
            if isinstance(delivery.get(name), dict) and delivery[name].get("enabled", True)]


def check_channels(config: Mapping[str, Any], env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``(channel, "ready" or a safe problem)`` for each enabled channel, without sending."""
    env = os.environ if env is None else env
    rows = []
    for channel in enabled_channels(config):
        try:
            channel.prepare(env)
            rows.append((channel.name, "ready"))
        except DeliveryError as exc:
            rows.append((channel.name, str(exc)))
    return rows


# --- delivering -------------------------------------------------------------

def _record(run: Run, channel: str, status: str, attempt: int, message: str = "") -> dict:
    result = {"run_id": run.run_id, "channel": channel, "status": status, "attempt": attempt,
              "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if message:
        result["message"] = message
    _atomic_write(run.path / f"delivery-{channel}.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def _deliver_channel(channel: Channel, run: Run, digest: Digest, env: Mapping[str, str],
                     max_attempts: int) -> dict:
    ledger_path = run.path / f"delivery-{channel.name}.json"
    if ledger_path.exists() or ledger_path.is_symlink():
        ledger = _json_object(_read_file(ledger_path, MAX_JSON_BYTES))
        if ledger.get("run_id") != run.run_id or ledger.get("status") not in DEDUPLICATE_STATES | {"failed"}:
            raise DeliveryError("The delivery record does not match this run or has an invalid status.")
        if ledger["status"] == "accepted":
            return {"status": "already_accepted", "duplicate": True,
                    "message": "This run was already accepted; nothing was sent again."}
        if ledger["status"] in DEDUPLICATE_STATES:
            return {"status": "unknown", "duplicate": True,
                    "message": "This run may already have been sent and needs review; nothing was sent. "
                               f"Delete {ledger_path.name} to allow a resend."}
    channel.prepare(env)
    payload = channel.payload(digest, run)
    for attempt in range(1, max_attempts + 1):
        status, message, retry = channel.submit(payload, lambda: _record(run, channel.name, "sending", attempt))
        if status == "accepted":
            try:
                return _record(run, channel.name, "accepted", attempt, message)
            except DeliveryError:
                # The durable "sending" record remains and blocks any resend.
                return {"status": "accepted", "attempt": attempt, "record_saved": False,
                        "message": message + " The local record could not be saved; do not resend."}
        result = _record(run, channel.name, status, attempt, message)
        if not retry or attempt == max_attempts:
            return result
        time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")


def _summarize(results: dict[str, dict]) -> tuple[str, str]:
    statuses = {name: r.get("status") for name, r in results.items()}
    if all(s in ("accepted", "already_accepted") for s in statuses.values()):
        status = "already_accepted" if all(s == "already_accepted" for s in statuses.values()) else "accepted"
        return status, "Accepted by " + ", ".join(results) + ". Receipt is not verified."
    status = "unknown" if any(s in ("unknown", "sending") for s in statuses.values()) else "failed"
    problems = [f"{name}: {r.get('message', r.get('status'))}" for name, r in results.items()
                if statuses[name] not in ("accepted", "already_accepted")]
    done = [name for name, s in statuses.items() if s in ("accepted", "already_accepted")]
    return status, " ".join(problems + ([f"Accepted by {', '.join(done)}."] if done else []))


def deliver(run_dir: Path, settings: Settings, *, env: Mapping[str, str] | None = None,
            dry_run: bool = False) -> dict[str, Any]:
    """Send (or, with ``dry_run``, preview) one run. Returns only safe diagnostics.

    ``status`` is disabled, preview, accepted, already_accepted, failed or unknown;
    ``channels`` holds each channel's own result.
    """
    env = os.environ if env is None else env
    config = settings.config
    options = config.get("delivery") or {}
    channels = enabled_channels(config)
    if not channels and not dry_run:
        return {"status": "disabled", "message": "", "channels": {}}
    try:
        run = validated_run(run_dir)
        digest = build_digest(run.manifest, run.summary, run.finished,
                              max_items=options.get("max_items", 5), preview=dry_run)
        if dry_run:
            folder = run.path / PREVIEW_DIR
            folder.mkdir(exist_ok=True)
            # Preview every format, so you can pick channels before configuring them.
            files: list[Path] = []
            for name, kind in CHANNEL_TYPES.items():
                chosen = next((c for c in channels if c.name == name), None)
                defaults = {"email": {"username": "", "from_address": "", "to_addresses": [],
                                      "attach_reports": True}, "ntfy": {"priority": 3}}.get(name, {})
                files += (chosen or kind(defaults, 30)).preview(digest, run, folder, env)
            return {"status": "preview", "message": f"Previews written to {folder}.",
                    "files": [str(f) for f in files], "channels": {}}
        if run.manifest.get("preview_only") is True:
            raise DeliveryError("A snapshot created for preview cannot be delivered as a new run.")
        if run.manifest["status"] == "error" and not options.get("notify_on_failure", True):
            return {"status": "disabled", "message": "Failure notices are turned off.", "channels": {}}
        results: dict[str, dict] = {}
        with exclusive_lock(run.path / ".delivery.lock", "Another sender holds this run's delivery lock."):
            for channel in channels:
                try:
                    results[channel.name] = _deliver_channel(channel, run, digest, env,
                                                             options.get("max_attempts", 3))
                except DeliveryError as exc:
                    results[channel.name] = {"status": "failed", "message": str(exc)}
                except (OSError, ValueError, TypeError):
                    results[channel.name] = {"status": "failed", "message":
                                             "Delivery stopped because a local file or setting could not be "
                                             "processed safely."}
    except LockBusy:
        return {"status": "unknown", "duplicate": True, "channels": {},
                "message": "Another sender holds this run's delivery lock; nothing was sent."}
    except DeliveryError as exc:
        return {"status": "failed", "message": str(exc), "channels": {}}
    except (OSError, ValueError, TypeError):
        return {"status": "failed", "channels": {},
                "message": "Delivery stopped because a local file or setting could not be processed safely."}
    status, message = _summarize(results)
    return {"status": status, "message": message, "channels": results}
