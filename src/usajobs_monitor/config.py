"""Load, validate and fill in defaults for a usajobs-monitor config file.

TOML and JSON use the same keys. Validation is hand-rolled so errors name the
exact setting (``filters.watch_list.grades[0]: expected an integer``) instead
of failing with a ``KeyError`` deep in a review. Credentials come from the
``USAJOBS_EMAIL`` / ``USAJOBS_API_KEY`` environment variables; a
``credentials`` block in the file is still accepted, with a warning.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_NAME = "usajobs-monitor.toml"
PLACEHOLDER_VALUES = {"", "PUT_YOUR_USAJOBS_API_KEY_HERE", "PUT_YOUR_EMAIL_HERE"}
SERVICE_VALUES = ("competitive", "excepted", "none")

DEFAULT_TEMPORARY_TERMS = [
    "temporary promotion", "temporary appointment", "not-to-exceed", "not to exceed", " nte ",
    "term appointment", "term-limited", "time-limited", "detail appointment", "detail opportunity",
    "detail assignment",
]
DEFAULT_SENIOR_EXECUTIVE_TERMS = [
    "senior executive service", " ses ", " sl ", " st ", "executive director", "deputy director",
    "senior authority",
]
DEFAULT_SCORING = {
    "keyword": 1,
    "title": 3,
    "series": 2,
    "target_agency": 5,
    "above_salary_rule": 2,
    "pay_high": 2,  # upper pay at GS-14/15 levels
    "pay_mid": 1,  # upper pay at GS-13 levels
    "pay_tier_high": 185000,
    "pay_tier_mid": 155000,
}
DEFAULT_REPORT_TITLE = "USAJOBS Job Monitor"

# Delivery secrets live only in environment variables. Each channel names the
# variables it reads; the defaults below apply when a ``*_env`` key is left out.
DELIVERY_CHANNELS = ("email", "discord", "slack", "ntfy")
DELIVERY_SECRET_ENV = {
    "email": {"password_env": "USAJOBS_SMTP_PASSWORD"},
    "discord": {"webhook_url_env": "USAJOBS_DISCORD_WEBHOOK_URL"},
    "slack": {"webhook_url_env": "USAJOBS_SLACK_WEBHOOK_URL"},
    "ntfy": {"topic_env": "USAJOBS_NTFY_TOPIC", "token_env": "USAJOBS_NTFY_TOKEN"},
}
SMTP_SECURITY = {"ssl": 465, "starttls": 587}
ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,99}"


class ConfigError(Exception):
    """Raised with every problem found, each prefixed by its setting path."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Config problems:\n" + "\n".join(f"  - {e}" for e in errors))


@dataclass(frozen=True)
class Settings:
    """A validated config plus the resolved file locations a run needs."""

    config: dict[str, Any]
    config_path: Path
    reports_dir: Path
    decisions_path: Path
    resume_folder: Path | None
    warnings: list[str] = field(default_factory=list)

    @property
    def history_path(self) -> Path:
        from .tracking import HISTORY_NAME
        return self.reports_dir / HISTORY_NAME


class _Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def section(self, config: dict[str, Any], name: str) -> dict[str, Any]:
        value = config.get(name)
        if value is None:
            config[name] = {}
            return config[name]
        if not isinstance(value, dict):
            self.error(name, "expected a table/object")
            config[name] = {}
        return config[name]

    def unknown(self, section: dict[str, Any], path: str, known: set[str]) -> None:
        for key in section:
            if key not in known:
                self.warnings.append(f"{path}.{key}: unknown setting, ignored" if path else f"{key}: unknown setting, ignored")

    def string(self, section: dict[str, Any], key: str, path: str, default: str | None = None,
               choices: tuple[str, ...] | None = None) -> None:
        if key not in section or section[key] is None:
            if default is None:
                self.error(f"{path}.{key}", "required")
            else:
                section[key] = default
            return
        value = section[key]
        if not isinstance(value, str):
            self.error(f"{path}.{key}", "expected a string")
        elif choices and value not in choices:
            self.error(f"{path}.{key}", "expected one of " + ", ".join(f'"{c}"' for c in choices))

    def boolean(self, section: dict[str, Any], key: str, path: str, default: bool) -> None:
        if key not in section or section[key] is None:
            section[key] = default
        elif not isinstance(section[key], bool):
            self.error(f"{path}.{key}", "expected true or false")

    def number(self, section: dict[str, Any], key: str, path: str, default: float | None,
               integer: bool = False, required: bool = False) -> None:
        if key not in section or section[key] is None:
            if required:
                self.error(f"{path}.{key}", "required")
            section[key] = default
            return
        value = section[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
            self.error(f"{path}.{key}", "expected an integer" if integer else "expected a number")
        elif value < 0:
            self.error(f"{path}.{key}", "must not be negative")

    def strings(self, section: dict[str, Any], key: str, path: str, default: list[str] | None,
                required: bool = False, pattern: str | None = None, hint: str = "") -> None:
        if key not in section or section[key] is None:
            if required:
                self.error(f"{path}.{key}", "required: a non-empty list of strings")
            section[key] = list(default or [])
            return
        value = section[key]
        if isinstance(value, str):
            self.error(f"{path}.{key}", "expected a list of strings, not a single string")
            return
        if not isinstance(value, list):
            self.error(f"{path}.{key}", "expected a list of strings")
            return
        if required and not value:
            self.error(f"{path}.{key}", "must not be empty")
        for index, item in enumerate(value):
            if not isinstance(item, str):
                self.error(f"{path}.{key}[{index}]", "expected a string")
            elif pattern and not re.fullmatch(pattern, item.strip()):
                self.error(f"{path}.{key}[{index}]", hint or f"does not match {pattern}")


def _migrate_old_keys(config: dict[str, Any]) -> None:
    """Read keys from their pre-package locations (the original usajobs_config.json).

    New locations win when both are set. ``user_profile.home_zip`` and
    ``rounded_salary_benchmark`` were never used by the search and are dropped.
    """
    def table(name: str) -> dict[str, Any] | None:
        value = config.setdefault(name, {})
        return value if isinstance(value, dict) else None

    if "resume_folder" in config:
        old = config.pop("resume_folder")
        if (paths := table("paths")) is not None:
            paths.setdefault("resume_folder", old)
    old_profile = config.pop("user_profile", None)
    if isinstance(old_profile, dict) and "current_agency" in old_profile:
        if (profile := table("profile")) is not None:
            profile.setdefault("current_agency", old_profile["current_agency"])
    filters = config.get("filters")
    if isinstance(filters, dict) and "current_agency_terms" in filters:
        old = filters.pop("current_agency_terms")
        if (profile := table("profile")) is not None:
            profile.setdefault("current_agency_terms", old)


def validate_config(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(config_with_defaults, warnings)`` or raise ``ConfigError``.

    Credentials are not checked here; ``resolve_credentials`` does that.
    """
    if not isinstance(raw, Mapping):
        raise ConfigError(["(top level): expected a table/object"])
    config = copy.deepcopy(dict(raw))
    check = _Checker()
    _migrate_old_keys(config)

    check.unknown(config, "", {"version", "credentials", "paths", "profile", "search", "filters",
                               "eligibility", "report", "scoring", "delivery"})
    if "version" in config and config["version"] != 1:
        check.error("version", "only version 1 is supported")

    paths = check.section(config, "paths")
    check.unknown(paths, "paths", {"reports_dir", "decisions_file", "resume_folder"})
    check.string(paths, "reports_dir", "paths", "reports")
    check.string(paths, "decisions_file", "paths", "job_decisions.csv")
    check.string(paths, "resume_folder", "paths", "")

    profile = check.section(config, "profile")
    check.unknown(profile, "profile", {"current_agency", "current_agency_terms"})
    check.string(profile, "current_agency", "profile", "")
    check.strings(profile, "current_agency_terms", "profile", [])

    search = check.section(config, "search")
    check.unknown(search, "search", {"locations", "radius_miles", "include_remote_jobs", "date_posted_days",
                                     "salary_query_floor", "target_role_keywords", "search_only_keywords",
                                     "role_title_terms", "target_job_series", "target_agencies"})
    check.strings(search, "locations", "search", None, required=True)
    check.number(search, "radius_miles", "search", 25, integer=True)
    check.boolean(search, "include_remote_jobs", "search", False)
    check.number(search, "date_posted_days", "search", 14, integer=True)
    check.number(search, "salary_query_floor", "search", 0)
    check.strings(search, "target_role_keywords", "search", None, required=True)
    check.strings(search, "search_only_keywords", "search", [])
    check.strings(search, "role_title_terms", "search", [])
    series_hint = 'expected a 4-digit series like "0510"'
    check.strings(search, "target_job_series", "search", [], pattern=r"\d{4}", hint=series_hint)
    check.strings(search, "target_agencies", "search", [])

    filters = check.section(config, "filters")
    check.unknown(filters, "filters", {"salary_upper_must_exceed", "near_miss_upper_salary_must_exceed",
                                       "near_miss_min_gs_grade", "watch_list", "mismatch_terms",
                                       "mismatch_job_series", "excluded_job_series", "exclude_senior_executive",
                                       "excluded_pay_plans", "senior_executive_terms", "temporary_terms"})
    check.number(filters, "salary_upper_must_exceed", "filters", 0)
    check.number(filters, "near_miss_upper_salary_must_exceed", "filters", 0)
    check.number(filters, "near_miss_min_gs_grade", "filters", None, integer=True)
    check.strings(filters, "mismatch_terms", "filters", [])
    check.strings(filters, "mismatch_job_series", "filters", [], pattern=r"\d{4}", hint=series_hint)
    check.strings(filters, "excluded_job_series", "filters", [], pattern=r"\d{4}", hint=series_hint)
    check.boolean(filters, "exclude_senior_executive", "filters", True)
    check.strings(filters, "excluded_pay_plans", "filters", [], pattern=r"[A-Za-z]{2}",
                  hint="expected a two-letter pay plan code like \"EM\"")
    check.strings(filters, "senior_executive_terms", "filters", DEFAULT_SENIOR_EXECUTIVE_TERMS)
    check.strings(filters, "temporary_terms", "filters", DEFAULT_TEMPORARY_TERMS)
    watch = filters.get("watch_list")
    if watch is None:
        filters["watch_list"] = {}
    elif not isinstance(watch, dict):
        check.error("filters.watch_list", "expected a table/object with pay_plans and grades")
        filters["watch_list"] = {}
    else:
        check.unknown(watch, "filters.watch_list", {"pay_plans", "grades"})
        check.strings(watch, "pay_plans", "filters.watch_list", [])
        grades = watch.get("grades")
        if grades is None:
            watch["grades"] = []
        elif not isinstance(grades, list):
            check.error("filters.watch_list.grades", "expected a list of integers")
        else:
            for index, grade in enumerate(grades):
                if isinstance(grade, bool) or not (isinstance(grade, int) or (isinstance(grade, str) and grade.strip().isdigit())):
                    check.error(f"filters.watch_list.grades[{index}]", "expected an integer")
        if bool(watch.get("pay_plans")) != bool(watch.get("grades")):
            check.warnings.append("filters.watch_list: needs both pay_plans and grades; the watch list stays empty")

    eligibility = check.section(config, "eligibility")
    check.unknown(eligibility, "eligibility", {"service", "has_competitive_status", "interchange_agency",
                                               "additional_hiring_paths"})
    check.string(eligibility, "service", "eligibility", "none", choices=SERVICE_VALUES)
    check.boolean(eligibility, "has_competitive_status", "eligibility", False)
    check.string(eligibility, "interchange_agency", "eligibility", "")
    check.strings(eligibility, "additional_hiring_paths", "eligibility", [])

    report = check.section(config, "report")
    check.unknown(report, "report", {"title"})
    check.string(report, "title", "report", DEFAULT_REPORT_TITLE)

    scoring = check.section(config, "scoring")
    check.unknown(scoring, "scoring", set(DEFAULT_SCORING))
    for key, default in DEFAULT_SCORING.items():
        check.number(scoring, key, "scoring", default)

    _check_delivery(check, config)

    if check.errors:
        raise ConfigError(check.errors)
    return config, check.warnings


def _check_delivery(check: _Checker, config: dict[str, Any]) -> None:
    """Types and defaults only. Addresses and secrets are checked at send time,
    after environment overrides, by ``delivery.check_channels``."""
    delivery = check.section(config, "delivery")
    check.unknown(delivery, "delivery", {"notify_on_failure", "max_attempts", "timeout_seconds",
                                         "max_items", *DELIVERY_CHANNELS})
    check.boolean(delivery, "notify_on_failure", "delivery", True)
    for key, default, low, high in (("max_attempts", 3, 1, 5), ("timeout_seconds", 30, 1, 120),
                                    ("max_items", 5, 1, 25)):
        check.number(delivery, key, "delivery", default, integer=True)
        value = delivery.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and not low <= value <= high:
            check.error(f"delivery.{key}", f"expected {low} to {high}")

    for name in DELIVERY_CHANNELS:
        if delivery.get(name) is None:
            continue
        path = f"delivery.{name}"
        channel = check.section(delivery, name)
        secret_keys = {key.removesuffix("_env") for key in DELIVERY_SECRET_ENV[name]}
        for key in secret_keys & set(channel):
            env_key = f"{key}_env"
            check.error(f"{path}.{key}", "secrets are read only from environment variables; remove it and set "
                        + str(channel.get(env_key) or DELIVERY_SECRET_ENV[name][env_key]))
            del channel[key]
        known = {"enabled", *DELIVERY_SECRET_ENV[name]}
        if name == "email":
            known |= {"smtp_host", "smtp_port", "security", "username", "from_address", "to_addresses",
                      "attach_reports"}
        if name == "ntfy":
            known |= {"server", "priority"}
        check.unknown(channel, path, known - secret_keys)
        check.boolean(channel, "enabled", path, True)
        for env_key, default in DELIVERY_SECRET_ENV[name].items():
            check.string(channel, env_key, path, default)
            if isinstance(channel.get(env_key), str) and not re.fullmatch(ENV_NAME, channel[env_key]):
                check.error(f"{path}.{env_key}", "expected an environment variable name like USAJOBS_SMTP_PASSWORD")
        if name == "email":
            check.string(channel, "security", path, "ssl", choices=tuple(SMTP_SECURITY))
            check.string(channel, "smtp_host", path, "")
            check.number(channel, "smtp_port", path, SMTP_SECURITY.get(channel.get("security"), 465), integer=True)
            port = channel.get("smtp_port")
            if isinstance(port, int) and not isinstance(port, bool) and not 1 <= port <= 65535:
                check.error(f"{path}.smtp_port", "expected 1 to 65535")
            check.string(channel, "username", path, "")
            check.string(channel, "from_address", path, "")
            check.strings(channel, "to_addresses", path, [])
            check.boolean(channel, "attach_reports", path, True)
        if name == "ntfy":
            check.string(channel, "server", path, "https://ntfy.sh")
            check.number(channel, "priority", path, 3, integer=True)
            priority = channel.get("priority")
            if isinstance(priority, int) and not isinstance(priority, bool) and not 1 <= priority <= 5:
                check.error(f"{path}.priority", "expected 1 to 5")


def resolve_credentials(config: dict[str, Any], env: Mapping[str, str] | None = None) -> list[str]:
    """Put credentials into ``config["credentials"]``, preferring the environment.

    Returns warnings. Never includes a credential value in any message.
    """
    env = os.environ if env is None else env
    warnings: list[str] = []
    file_creds = config.get("credentials")
    if file_creds is not None and not isinstance(file_creds, dict):
        raise ConfigError(["credentials: expected a table/object"])
    file_creds = file_creds or {}
    file_has = {key: str(file_creds.get(key) or "").strip() not in PLACEHOLDER_VALUES for key in ("email", "api_key")}
    if any(file_has.values()):
        warnings.append("credentials: found in the config file; prefer the USAJOBS_EMAIL and USAJOBS_API_KEY "
                        "environment variables so the key never sits in a file")
    resolved = {}
    errors = []
    for key, var in (("email", "USAJOBS_EMAIL"), ("api_key", "USAJOBS_API_KEY")):
        value = str(env.get(var) or "").strip()
        if value in PLACEHOLDER_VALUES:
            value = str(file_creds.get(key) or "").strip()
        if value in PLACEHOLDER_VALUES:
            errors.append(f"credentials.{key}: missing; set the {var} environment variable")
        resolved[key] = value
    if errors:
        raise ConfigError(errors)
    config["credentials"] = resolved
    return warnings


def read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError([f"{path}: config file not found. Copy examples/usajobs-monitor.example.toml and edit it."])
    data = path.read_bytes()
    try:
        if path.suffix.lower() == ".toml":
            import tomllib
            return tomllib.loads(data.decode("utf-8"))
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        # tomllib and json errors carry line/column but not file contents.
        raise ConfigError([f"{path.name}: could not parse ({type(exc).__name__}: {exc})"]) from None


def load_settings(path: Path | str, env: Mapping[str, str] | None = None, *,
                  require_credentials: bool = True, raw: Mapping[str, Any] | None = None) -> Settings:
    """Load ``path`` (or an already-parsed ``raw`` mapping that lives at ``path``)."""
    config_path = Path(path).expanduser().resolve()
    config, warnings = validate_config(read_config_file(config_path) if raw is None else raw)
    if require_credentials:
        warnings += resolve_credentials(config, env)
    base = config_path.parent

    def resolve(value: str) -> Path:
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else base / candidate

    paths = config["paths"]
    return Settings(
        config=config,
        config_path=config_path,
        reports_dir=resolve(paths["reports_dir"]),
        decisions_path=resolve(paths["decisions_file"]),
        resume_folder=resolve(paths["resume_folder"]) if paths["resume_folder"] else None,
        warnings=warnings,
    )


def current_agency_terms(config: Mapping[str, Any]) -> list[str]:
    """Profile terms, falling back to the pre-package ``filters`` location."""
    terms = (config.get("profile") or {}).get("current_agency_terms")
    if terms is None:
        terms = (config.get("filters") or {}).get("current_agency_terms", [])
    return list(terms or [])


def scoring(config: Mapping[str, Any]) -> dict[str, float]:
    return {**DEFAULT_SCORING, **{k: v for k, v in (config.get("scoring") or {}).items() if v is not None}}
