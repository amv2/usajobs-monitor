"""``usajobs-monitor init``: ask a few questions and write a profile.

Credentials are never written to the profile. They go to the OS keychain when
the optional ``keyring`` extra is installed and the user agrees, otherwise the
wizard prints the environment variables to set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .presets import PRESETS
from .series import COMMON_SERIES, find_series

API_KEY_URL = "https://developer.usajobs.gov/apirequest/"
CHANNEL_CHOICES = ("email", "discord", "slack", "ntfy")
CHANNEL_SECRETS = {
    "email": ["USAJOBS_SMTP_PASSWORD"],
    "discord": ["USAJOBS_DISCORD_WEBHOOK_URL"],
    "slack": ["USAJOBS_SLACK_WEBHOOK_URL"],
    "ntfy": ["USAJOBS_NTFY_TOPIC"],
}


class Cancelled(Exception):
    pass


@dataclass
class Answers:
    presets: list[str] = field(default_factory=list)
    current_agency: str = ""
    current_agency_terms: list[str] = field(default_factory=list)
    interchange_agency: str = ""
    locations: list[str] = field(default_factory=list)
    radius_miles: int = 25
    include_remote_jobs: bool = False
    grade: int | None = None
    locality: str = ""
    match: str = "at_grade"
    keywords: list[str] = field(default_factory=list)
    series: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    email: dict[str, Any] = field(default_factory=dict)
    formats: list[str] = field(default_factory=lambda: ["html", "csv"])
    title: str = "USAJOBS Job Monitor"


def _split(value: str, sep: str = ",") -> list[str]:
    return [part.strip() for part in value.split(sep) if part.strip()]


class Wizard:
    def __init__(self, ask: Callable[[str], str] | None = None, say: Callable[[str], None] = print,
                 secret: Callable[[str], str] | None = None, store=None) -> None:
        self._ask = ask or (lambda prompt: input(prompt))
        self.say = say
        self.secret = secret
        self.store = store  # a keychain.Store, or None when no keychain is usable

    def ask(self, prompt: str, default: str = "") -> str:
        try:
            value = self._ask(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        return value or default

    def yes(self, prompt: str, default: bool = False) -> bool:
        while True:
            value = self.ask(f"{prompt} (y/n)", "y" if default else "n").lower()
            if value in ("y", "yes", "n", "no"):
                return value.startswith("y")
            self.say("  Please answer y or n.")

    def number(self, prompt: str, default: int | None, low: int, high: int, allow_blank: bool = False) -> int | None:
        while True:
            value = self.ask(prompt, "" if default is None else str(default))
            if not value and allow_blank:
                return None
            if value.isdigit() and low <= int(value) <= high:
                return int(value)
            self.say(f"  Enter a whole number from {low} to {high}" + (", or leave it blank." if allow_blank else "."))

    def choose(self, prompt: str, options: list[tuple[str, str]], default: str, many: bool = True) -> list[str]:
        """Numbered menu; returns the chosen keys."""
        for index, (_, label) in enumerate(options, start=1):
            self.say(f"  {index}. {label}")
        while True:
            raw = self.ask(prompt, default)
            picks = _split(raw.replace(" ", ","))
            if picks and all(p.isdigit() and 1 <= int(p) <= len(options) for p in picks) and (many or len(picks) == 1):
                return list(dict.fromkeys(options[int(p) - 1][0] for p in picks))
            self.say(f"  Enter {'one or more numbers like 1,3' if many else 'one number'} from the list.")

    # -- steps -------------------------------------------------------------

    def credentials(self) -> None:
        self.say("\nStep 1 of 6: USAJOBS API key")
        self.say(f"  The search needs a free API key. Request one at {API_KEY_URL}")
        self.say("  using the email address you want to search with.")
        if self.store is None or self.secret is None:
            self.say("  When it arrives, set these environment variables (they are never stored in the profile):")
            self.say('    USAJOBS_EMAIL="you@example.com"   USAJOBS_API_KEY="your key"')
            return
        if not self.yes("  Save your USAJOBS email and API key in the system keychain now?", True):
            self.say("  Later: usajobs-monitor credentials set USAJOBS_EMAIL (and USAJOBS_API_KEY), "
                     "or set those environment variables.")
            return
        email = self.ask("  USAJOBS email")
        key = self.secret("  API key (input hidden): ").strip()
        for name, value in (("USAJOBS_EMAIL", email), ("USAJOBS_API_KEY", key)):
            if value:
                self.store.set(name, value)
                self.say(f"  Saved {name} to the keychain.")
            else:
                self.say(f"  Skipped {name}; set it later with: usajobs-monitor credentials set {name}")

    def applicant(self, a: Answers) -> None:
        self.say("\nStep 2 of 6: Who are you applying as?")
        self.say("  Each choice adds the USAJOBS hiring paths that applicant type can use. Pick all that apply.")
        a.presets = self.choose("  Your choices", [(p.name, p.label) for p in PRESETS.values()], "1")
        if "public" in a.presets and len(a.presets) > 1:
            a.presets.remove("public")  # every preset already includes public announcements
        for name in a.presets:
            caveat = PRESETS[name].caveat
            if caveat:
                self.say(f"  Note ({PRESETS[name].label}): {caveat}")
        if {"competitive-fed", "excepted-fed"} & set(a.presets):
            a.current_agency = self.ask("  Your current agency's name, as USAJOBS shows it")
            terms = self.ask("  Other names or acronyms postings use for it (comma-separated)", "")
            a.current_agency_terms = list(dict.fromkeys(([a.current_agency] if a.current_agency else []) + _split(terms)))
        if "excepted-fed" in a.presets:
            self.say("  If your agency has an OPM interchange agreement, competitive-service postings that invite")
            self.say("  interchange candidates can be kept too.")
            if self.yes("  Does your agency have an interchange agreement with OPM?", False):
                a.interchange_agency = self.ask("  Agency name to show in reports", a.current_agency)

    def location(self, a: Answers) -> None:
        self.say("\nStep 3 of 6: Where do you want to work?")
        while not a.locations:
            a.locations = _split(self.ask('  Locations as "City, State"; separate several with ;'), ";")
            if not a.locations:
                self.say('  Enter at least one, like "Denver, Colorado".')
        a.radius_miles = self.number("  Search radius in miles", 25, 0, 500)
        a.include_remote_jobs = self.yes("  Also include remote jobs?", False)

    def grade(self, a: Answers) -> None:
        from .paytables import available_years, load_table

        self.say("\nStep 4 of 6: Target grade")
        self.say("  Pay floors come from OPM's General Schedule table for your locality, so non-GS postings")
        self.say("  that pay the same are found too. Leave it blank to skip pay filtering.")
        a.grade = self.number("  Target GS grade (1-15)", None, 1, 15, allow_blank=True)
        if a.grade is None:
            return
        table = load_table(available_years()[-1])
        guess = "RUS"
        for location in a.locations:
            try:
                guess = table.find_locality(location.split(",")[0])
                break
            except LookupError:
                continue
        while True:
            value = self.ask("  OPM locality pay area (code or name; `usajobs-monitor pay-table` lists them)",
                             table.localities[guess].name)
            try:
                code = table.find_locality(value)
            except LookupError as exc:
                self.say(f"  {exc}")
                continue
            a.locality = table.localities[code].name
            break
        picks = self.choose("  Which jobs count as matches?", [
            ("at_grade", f"GS-{a.grade} jobs, or other pay plans paying more than GS-{a.grade - 1} can"
             if a.grade > 1 else "GS-1 jobs and anything paying more"),
            ("above_grade", f"Only jobs paying more than GS-{a.grade} can (a promotion)"),
        ], "1", many=False)
        a.match = picks[0]

    def occupation(self, a: Answers) -> None:
        self.say("\nStep 5 of 6: What kind of work?")
        a.keywords = _split(self.ask('  Job titles or keywords to search, comma-separated (e.g. "budget analyst, '
                                     'program analyst")'))
        self.say("  Job series narrow results to an occupation. Type codes like 0343 or words like \"budget\";")
        self.say("  leave it blank when you are done.")
        while True:
            raw = self.ask("  Add job series")
            if not raw:
                break
            for token in _split(raw):
                hits = find_series(token)
                if not hits:
                    self.say(f'  No common series matches "{token}". Enter its 4-digit code instead.')
                    continue
                if len(hits) > 1:
                    self.say(f'  "{token}" matches several series:')
                    chosen = self.choose("  Which ones?", [(c, f"{c} {t}") for c, t in hits], "1")
                    hits = [h for h in hits if h[0] in chosen]
                for code, title in hits:
                    if code not in a.series:
                        a.series.append(code)
                        self.say(f"  Added {code} {title}.")
            if a.series:
                self.say("  Series so far: " + ", ".join(a.series))
        if not a.keywords and not a.series:
            self.say("  A search needs at least one keyword.")
            return self.occupation(a)
        if not a.keywords:
            a.keywords = [COMMON_SERIES.get(code, code) for code in a.series if code in COMMON_SERIES] or a.series
            self.say("  Searching by the series titles: " + ", ".join(a.keywords))

    def alerts(self, a: Answers) -> None:
        self.say("\nStep 6 of 6: Results")
        picks = self.choose("  How should results reach you?", [
            ("none", "Just write reports to the reports folder"),
            ("email", "Email (any SMTP server)"),
            ("discord", "Discord webhook"),
            ("slack", "Slack webhook"),
            ("ntfy", "ntfy push notification"),
        ], "1")
        a.channels = [p for p in picks if p != "none"]
        if "email" in a.channels:
            host = self.ask("  SMTP server, e.g. smtp.gmail.com or smtp.fastmail.com")
            security = self.choose("  Connection security", [("ssl", "SSL/TLS (port 465)"),
                                                              ("starttls", "STARTTLS (port 587)")], "1", many=False)[0]
            username = self.ask("  SMTP username (usually your address)")
            to = _split(self.ask("  Send to (comma-separated)", username))
            a.email = {"smtp_host": host, "security": security, "username": username, "to_addresses": to}
        if a.channels:
            names = [name for c in a.channels for name in CHANNEL_SECRETS[c]]
            self.say("  Secrets stay out of the profile. Set " + ", ".join(names) + " as environment variables,")
            self.say("  or save each with: usajobs-monitor credentials set NAME")
        if self.yes("  Also write Word and Excel reports? (HTML and CSV are always written)", False):
            a.formats = ["docx", "xlsx", "html", "csv"]

    def run(self) -> Answers:
        answers = Answers()
        self.credentials()
        self.applicant(answers)
        self.location(answers)
        self.grade(answers)
        self.occupation(answers)
        self.alerts(answers)
        return answers


def _toml(value: Any) -> str:
    # JSON strings, numbers, booleans and string arrays are valid TOML values.
    return json.dumps(value, ensure_ascii=False)


def render_profile(a: Answers, *, reports_dir: str = "reports", decisions_file: str = "job_decisions.csv") -> str:
    lines = [
        "# usajobs-monitor profile written by `usajobs-monitor init`.",
        "# Edit freely; field notes are in docs/CONFIG_REFERENCE.md. Check it with:",
        "#   usajobs-monitor check-config",
        "# Credentials are never stored here: USAJOBS_EMAIL and USAJOBS_API_KEY come from",
        "# the environment or the system keychain.",
        "",
        "version = 1",
        "",
        "[paths]",
        f"reports_dir    = {_toml(reports_dir)}",
        f"decisions_file = {_toml(decisions_file)}   # usajobs-monitor decide <control number> <status>",
        "",
        "[profile]",
        f"current_agency       = {_toml(a.current_agency)}",
        f"current_agency_terms = {_toml(a.current_agency_terms)}",
        "",
        "[search]",
        f"locations           = {_toml(a.locations)}",
        f"radius_miles        = {a.radius_miles}",
        f"include_remote_jobs = {_toml(a.include_remote_jobs)}",
        "date_posted_days    = 14",
        f"target_role_keywords = {_toml(a.keywords)}",
        f"role_title_terms     = {_toml(list(dict.fromkeys(k.lower() for k in a.keywords)))}",
        f"target_job_series    = {_toml(a.series)}",
        "",
    ]
    if a.grade is not None:
        lines += [
            "[target]",
            f"grade    = {a.grade}",
            f"locality = {_toml(a.locality)}",
            f"match    = {_toml(a.match)}   # \"at_grade\" or \"above_grade\"",
            "",
        ]
    lines += [
        "[filters]",
        "exclude_senior_executive = true",
        "",
        "[eligibility]",
        f"presets            = {_toml(a.presets)}   # see docs/CONFIG_REFERENCE.md for each type's rules",
    ]
    if a.interchange_agency:
        lines.append(f"interchange_agency = {_toml(a.interchange_agency)}")
    lines += [
        "",
        "[report]",
        f"title   = {_toml(a.title)}",
        f"formats = {_toml(a.formats)}   # any of \"html\", \"csv\", \"docx\", \"xlsx\"",
        "",
    ]
    if a.channels:
        lines.append("[delivery]")
        lines.append("")
        for channel in a.channels:
            lines.append(f"[delivery.{channel}]")
            if channel == "email":
                for key in ("smtp_host", "security", "username", "to_addresses"):
                    lines.append(f"{key} = {_toml(a.email[key])}")
                lines.append("# password from USAJOBS_SMTP_PASSWORD")
            else:
                lines.append(f"# secret from {CHANNEL_SECRETS[channel][0]}")
            lines.append("")
    return "\n".join(lines)


def write_profile(path: Path, text: str, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
