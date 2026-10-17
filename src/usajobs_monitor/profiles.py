"""Several search profiles in one install.

A profile is an ordinary config file kept in the profiles folder:

- ``$USAJOBS_MONITOR_HOME/profiles`` when that variable is set,
- ``%APPDATA%\\usajobs-monitor\\profiles`` on Windows,
- ``$XDG_CONFIG_HOME/usajobs-monitor/profiles`` (default ``~/.config/...``) elsewhere.

``--profile NAME`` picks ``<profiles folder>/NAME.toml``. Each profile keeps its
own reports, history and decisions, so two people's searches never mix.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping

HOME_VAR = "USAJOBS_MONITOR_HOME"
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}"


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(HOME_VAR):
        return Path(env[HOME_VAR]).expanduser()
    if sys.platform == "win32" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "usajobs-monitor"
    base = Path(env["XDG_CONFIG_HOME"]).expanduser() if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "usajobs-monitor"


def profiles_dir(env: Mapping[str, str] | None = None) -> Path:
    return home_dir(env) / "profiles"


def check_name(name: str) -> str:
    name = name.strip()
    if not re.fullmatch(NAME_PATTERN, name):
        raise ValueError("a profile name uses letters, digits, - and _ (up to 40), e.g. \"alex\" or \"spouse-dc\"")
    return name


def profile_path(name: str, env: Mapping[str, str] | None = None) -> Path:
    """The file for ``name``; an existing JSON profile is used when there is no TOML one."""
    folder = profiles_dir(env)
    name = check_name(name)
    toml, json = folder / f"{name}.toml", folder / f"{name}.json"
    return json if json.is_file() and not toml.is_file() else toml


def list_profiles(env: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    folder = profiles_dir(env)
    if not folder.is_dir():
        return []
    found: dict[str, Path] = {}
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in (".toml", ".json") and path.is_file() and re.fullmatch(NAME_PATTERN, path.stem):
            found.setdefault(path.stem, path)
    return sorted(found.items())


def resolve(config: Path | None, profile: str | None, default: Path,
            env: Mapping[str, str] | None = None) -> Path:
    """The config file a command uses: ``--profile``, else ``--config``, else
    ``./usajobs-monitor.toml`` when it exists, else the only saved profile."""
    if profile:
        path = profile_path(profile, env)
        if not path.is_file():
            names = ", ".join(name for name, _ in list_profiles(env)) or "none yet; create one with usajobs-monitor init"
            raise FileNotFoundError(f"no profile named \"{profile}\" in {profiles_dir(env)} (saved profiles: {names})")
        return path
    if config is not None:
        return config
    if default.is_file():
        return default
    saved = list_profiles(env)
    if len(saved) == 1:
        return saved[0][1]
    return default
