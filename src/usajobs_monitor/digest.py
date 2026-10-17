"""Turn a validated run into the messages each delivery channel sends.

Everything here reads only the run's manifest and summary. A failed run's
message uses the manifest's fixed failure text, never the summary's contents,
so exception text cannot reach a delivered message.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .reports import CSV_NAME, EXCEL_NAME, HTML_NAME, HTML_STYLE, REPORT_LABELS, WORD_NAME
from .tracking import CLOSING_SOON_DAYS

# File name -> (MIME type, the ZIP member an Office file must contain, or "").
ATTACHMENT_TYPES = {
    WORD_NAME: ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "word/document.xml"),
    EXCEL_NAME: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xl/workbook.xml"),
    HTML_NAME: ("text/html", ""),
    CSV_NAME: ("text/csv", ""),
}
PREVIEW_FROM = "usajobs-monitor@example.invalid"
PREVIEW_TO = "recipient@example.invalid"


@dataclass
class Item:
    title: str
    details: list[str]
    url: str = ""


@dataclass
class Section:
    heading: str
    items: list[Item] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


@dataclass
class Digest:
    subject: str
    ok: bool
    run_id: str
    finished: datetime
    paragraphs: list[str]
    sections: list[Section]


def text(value: object, default: str = "Not listed") -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return default
    value = " ".join(str(value).split())[:1000]
    return value or default


def count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def job_url(value: object) -> str:
    """Only plain USAJOBS announcement links, with query and fragment removed."""
    if not isinstance(value, str):
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "www.usajobs.gov"
                or url.username or url.password or url.port not in (None, 443)
                or not re.fullmatch(r"/(?:job/|GetJob/ViewDetails/)\d+/?", url.path)):
            return ""
        return urlunsplit(("https", "www.usajobs.gov", url.path, "", ""))
    except ValueError:
        return ""


def close_date(value: object) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        # USAJOBS can return four fractional-second digits, which older Python
        # releases reject. The announcement's own calendar date needs no conversion.
        day = datetime.fromisoformat(value.partition("T")[0])
        return f"{day:%b} {day.day}, {day.year}"
    except (TypeError, ValueError):
        return text(value)


def grade(entry: dict) -> str:
    value = text(entry.get("grade"))
    pay_plan = text(entry.get("pay_plan"), "")
    if pay_plan and re.fullmatch(r"[\d\s–-]+", value):
        return f"{pay_plan} {value}"
    return value


def _entries(summary: dict, key: str) -> list[dict]:
    value = summary.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _item(entry: dict, *, eligibility: bool = True) -> Item:
    title = text(entry.get("title"))
    flags = [f for f in entry.get("flags", []) if isinstance(f, str)] if isinstance(entry.get("flags"), list) else []
    if flags:
        title += f" [{', '.join(text(f) for f in flags)}]"
    details = [
        f"Agency: {text(entry.get('agency'))}",
        f"Grade: {grade(entry)}",
        f"Promotion potential / full performance level: {text(entry.get('promotion_potential'))}",
        f"Closes: {close_date(entry.get('close_date'))}",
    ]
    if eligibility:
        details.append(f"Eligibility: {text(entry.get('eligibility_basis'))}")
    if entry.get("your_status"):
        details.append(f"Your status: {text(entry.get('your_status'))}")
    return Item(title, details, job_url(entry.get("url")))


def build_digest(manifest: dict, summary: dict, finished: datetime, *, max_items: int = 5,
                 preview: bool = False) -> Digest:
    day = finished.date().isoformat()
    sections: list[Section] = []
    if manifest["status"] == "error":
        subject = f"USAJOBS — {day} — run failed"
        failure = text(manifest.get("failure_message"), "The search or report generation did not complete.")
        paragraphs = [failure, "Review the local run and rerun the search. No reports are attached."]
    else:
        matches = summary["matches_after_final_filters"]
        watch_count = count(summary.get("watch_list_count"))
        watch_label = text(summary.get("watch_list_label"), "Watch list")
        new = summary.get("new_since_last_run") if isinstance(summary.get("new_since_last_run"), dict) else {}
        new_total = sum(count(new.get(key)) for key in ("matches", "watch_list", "near_misses"))
        subject = f"USAJOBS — {day} — {matches} {'match' if matches == 1 else 'matches'}"
        if watch_count:
            subject += f" · {watch_count} watch"
        if new_total:
            subject += f" · {new_total} new"
        paragraphs = [
            f"Reviewed {summary['jobs_reviewed']} jobs across {summary['usajobs_searches']} searches.",
            f"Matches: {matches}. Target-agency matches: {summary['target_agency_matches']}. "
            + (f"{watch_label}: {watch_count}. " if watch_count or watch_label != "Watch list" else "")
            + f"Near misses reported: {summary['near_misses_reported']}.",
        ]
        if summary.get("history_baseline") is True:
            paragraphs.append("New since last run: this is the baseline run, so nothing is flagged NEW yet.")
        elif new:
            dropped = summary.get("dropped_since_last_run")
            paragraphs.append(
                f"New since last run: {count(new.get('matches'))} matches, {count(new.get('watch_list'))} watch list, "
                f"{count(new.get('near_misses'))} near misses. Closing within {CLOSING_SOON_DAYS} days: "
                f"{count(summary.get('closing_soon'))}. "
                f"Dropped since last run: {len(dropped) if isinstance(dropped, list) else 0}."
            )
        if matches == 0:
            paragraphs.append("No jobs met the final filters in this run.")
        else:
            paragraphs.append("Review eligibility and deadlines before applying.")

        entries = _entries(summary, "matches")
        if entries:
            section = Section("Matches", [_item(e) for e in entries[:max_items]])
            if len(entries) > max_items:
                section.lines.append(f"{len(entries) - max_items} more in the reports.")
            sections.append(section)
        watch = _entries(summary, "watch_list")
        if watch:
            sections.append(Section(f"{watch_label} (fails only the pay rule)",
                                    [_item(e, eligibility=False) for e in watch[:max_items]]))
        new_near = [e for e in _entries(summary, "top_near_misses")
                    if isinstance(e.get("flags"), list) and "NEW" in e["flags"]]
        if new_near:
            sections.append(Section("New near misses", [_item(e, eligibility=False) for e in new_near[:max_items]]))
        dropped = _entries(summary, "dropped_since_last_run")
        if dropped:
            sections.append(Section("Dropped since last run", lines=[
                f"{text(d.get('title'))} ({text(d.get('agency'))}): {text(d.get('reason'))}" for d in dropped[:10]]))
    if preview:
        subject = f"PREVIEW — {subject}"
        paragraphs.insert(0, "Local preview only. This message has not been sent.")
    return Digest(subject, manifest["status"] == "ok", manifest["run_id"], finished, paragraphs, sections)


def plain_text(digest: Digest, note: str = "") -> str:
    lines = [digest.subject, "", *digest.paragraphs, *([note] if note else [])]
    for section in digest.sections:
        lines.extend(["", section.heading.upper()])
        for item in section.items:
            lines.extend(["", item.title, *item.details])
            if item.url:
                lines.append(item.url)
        lines.extend(section.lines)
    lines.extend(["", f"Run: {digest.run_id}"])
    return "\n".join(lines) + "\n"


def html_text(digest: Digest, note: str = "") -> str:
    """The email's HTML part: the same digest, styled inline so mail clients keep the look."""
    style = HTML_STYLE
    esc = html.escape
    parts = ["<!doctype html><html><body style=\"" + style["body"] + "\">", f'<div style="{style["wrap"]}">',
             f'<h1 style="{style["h1"]}">{esc(digest.subject)}</h1>']
    parts.extend(f'<p style="{style["fact"]}">{esc(p)}</p>' for p in [*digest.paragraphs, *([note] if note else [])])
    for section in digest.sections:
        parts.append(f'<h2 style="{style["h2"]}">{esc(section.heading)}</h2>')
        for item in section.items:
            parts.append(f'<div style="{style["card"]}"><h3 style="{style["title"]}">{esc(item.title)}</h3><ul style="margin:0;padding-left:18px">')
            parts.extend(f'<li style="{style["fact"]}">{esc(d)}</li>' for d in item.details)
            parts.append("</ul>")
            if item.url:
                parts.append(f'<p style="{style["fact"]}"><a style="{style["link"]}" '
                             f'href="{esc(item.url, quote=True)}">View announcement</a></p>')
            parts.append("</div>")
        parts.extend(f'<p style="{style["fact"]}">{esc(line)}</p>' for line in section.lines)
    parts.append(f'<p style="{style["muted"]}">Run: {esc(digest.run_id)}</p></div></body></html>')
    return "".join(parts)


def email_message(digest: Digest, attachments: list[tuple[str, bytes]], *, sender: str,
                  recipients: list[str]) -> tuple[EmailMessage, str]:
    labels = [REPORT_LABELS[name] for name, _ in attachments]
    note = ""
    if labels:
        listed = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        note = f"The {listed} {'is' if len(labels) == 1 else 'are'} attached."
    body = plain_text(digest, note)
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = digest.subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = format_datetime(digest.finished)
    run_digest = hashlib.sha256(digest.run_id.encode()).hexdigest()
    domain = sender.rpartition("@")[2] or "usajobs-monitor.invalid"
    # One fixed Message-ID per run lets mail servers and clients spot a repeat.
    message["Message-ID"] = f"<usajobs-monitor.{run_digest}@{domain}>"
    message.set_content(body)
    message.add_alternative(html_text(digest, note), subtype="html")
    day = digest.finished.date().isoformat()
    for name, data in attachments:
        stem, suffix = os.path.splitext(name)
        maintype, subtype = ATTACHMENT_TYPES[name][0].split("/")
        if maintype == "text":
            message.add_attachment(data.decode("utf-8"), subtype=subtype, filename=f"{stem}_{day}{suffix}")
        else:
            message.add_attachment(data, maintype=maintype, subtype=subtype, filename=f"{stem}_{day}{suffix}")
    return message, body


def chat_text(digest: Digest, *, limit: int, link: str = "{url}", bold: str = "{}", heading: str = "{}",
              escape=lambda s: s) -> str:
    """A short digest for chat: headline, counts, and one entry per listed job.

    ``link``, ``bold`` and ``heading`` are format strings for the target's markup
    (Discord wraps links in ``<>`` to skip previews). Sections that don't fit
    under ``limit`` characters are counted at the end instead.
    """
    out = "\n".join([bold.format(escape(digest.subject)), *(escape(p) for p in digest.paragraphs)])
    footer = f"\nRun: {escape(digest.run_id)}"
    budget = limit - len(footer) - 60  # room for the "more entries" line
    skipped = 0
    for section in digest.sections:
        entries = []
        for item in section.items:
            facts = [d for d in item.details if not d.startswith(("Promotion", "Your status"))]
            line = f"• {escape(item.title)} — " + "; ".join(escape(f) for f in facts)
            if item.url:
                line += "\n  " + link.format(url=item.url)
            entries.append(line)
        entries.extend(escape(line) for line in section.lines)
        added = 0
        for entry in entries:
            head = "" if added else "\n\n" + heading.format(escape(section.heading))
            if len(out) + len(head) + len(entry) + 1 > budget:
                skipped += 1
                continue
            out += head + "\n" + entry
            added += 1
    if skipped:
        out += f"\n\n{skipped} more entries are in the reports."
    return (out + footer)[:limit]
