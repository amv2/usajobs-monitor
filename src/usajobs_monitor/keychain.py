"""Optional OS keychain storage for credentials, through the ``keyring`` package.

Install with ``pip install "usajobs-monitor[keyring]"``. Each secret is stored
under the service ``usajobs-monitor`` with its environment variable name as the
account, for example ``USAJOBS_API_KEY``. macOS Keychain, Windows Credential
Manager and Linux Secret Service (GNOME Keyring, KWallet) all work.

An environment variable that is set always wins over the keychain. Without
``keyring``, with no usable backend, or with ``USAJOBS_KEYRING=0``, nothing is
read from the keychain and the environment works exactly as before. Errors
never include a secret or the backend's own message.
"""

from __future__ import annotations

import os
import re
from typing import Iterator, Mapping, Protocol

SERVICE = "usajobs-monitor"
DISABLE_VAR = "USAJOBS_KEYRING"
OFF_VALUES = {"0", "false", "no", "off"}
NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
# Backends that store nothing (the chainer is judged by what it chains).
UNUSABLE_BACKENDS = {"keyring.backends.fail", "keyring.backends.null", "keyring.backends.chainer"}


class KeychainError(Exception):
    """A fixed, safe diagnostic."""


class Store(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> bool: ...


class KeyringStore:
    """The OS keychain through ``keyring``. ``get`` returns None on any failure."""

    def __init__(self, keyring_module) -> None:
        self._keyring = keyring_module

    def get(self, name: str) -> str | None:
        try:
            value = self._keyring.get_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - a locked or broken keychain reads as "not stored"
            return None
        return value if isinstance(value, str) else None

    def set(self, name: str, value: str) -> None:
        try:
            self._keyring.set_password(SERVICE, name, value)
        except Exception:  # noqa: BLE001
            raise KeychainError(f"Could not save {name} to the keychain. Check that it is unlocked.") from None

    def delete(self, name: str) -> bool:
        try:
            self._keyring.delete_password(SERVICE, name)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "PasswordDeleteError":
                return False
            raise KeychainError(f"Could not remove {name} from the keychain. Check that it is unlocked.") from None
        return True


def _backend_usable(keyring_module) -> bool:
    try:
        backend = keyring_module.get_keyring()
    except Exception:  # noqa: BLE001
        return False
    module = type(backend).__module__
    if module == "keyring.backends.chainer":
        return any(type(b).__module__ not in UNUSABLE_BACKENDS for b in getattr(backend, "backends", []))
    return module not in UNUSABLE_BACKENDS and getattr(backend, "priority", 1) > 0


def default_store(env: Mapping[str, str] | None = None) -> Store | None:
    """The OS keychain, or None when it is turned off, not installed or has no backend."""
    env = os.environ if env is None else env
    if str(env.get(DISABLE_VAR) or "").strip().lower() in OFF_VALUES:
        return None
    try:
        import keyring
    except Exception:  # noqa: BLE001
        return None
    return KeyringStore(keyring) if _backend_usable(keyring) else None


def backend_description() -> str:
    """A short, safe description of where secrets would be stored."""
    if str(os.environ.get(DISABLE_VAR) or "").strip().lower() in OFF_VALUES:
        return f"off ({DISABLE_VAR} is set)"
    try:
        import keyring
    except Exception:  # noqa: BLE001
        return 'not installed (pip install "usajobs-monitor[keyring]")'
    if not _backend_usable(keyring):
        return "no keychain found on this computer"
    backend = keyring.get_keyring()
    chained = [b for b in getattr(backend, "backends", []) if type(b).__module__ not in UNUSABLE_BACKENDS]
    backend = chained[0] if chained else backend
    return str(getattr(backend, "name", None) or type(backend).__name__)


def check_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name or ""):
        raise KeychainError("Expected a variable name like USAJOBS_API_KEY.")
    return name


class Environment(Mapping[str, str]):
    """The environment, with keychain values for variables that are unset or blank.

    Lookups go to the keychain only for names the program asks for, once each.
    """

    def __init__(self, base: Mapping[str, str] | None = None, store: Store | None = None) -> None:
        self.base = os.environ if base is None else base
        self.store = store
        self._cache: dict[str, str | None] = {}

    def _stored(self, name: str) -> str | None:
        if self.store is None:
            return None
        if name not in self._cache:
            value = self.store.get(name)
            self._cache[name] = value if value and value.strip() else None
        return self._cache[name]

    def source(self, name: str) -> str | None:
        """``"environment"``, ``"keychain"`` or None. Never the value."""
        if str(self.base.get(name) or "").strip():
            return "environment"
        return "keychain" if self._stored(name) else None

    def __getitem__(self, name: str) -> str:
        value = self.base.get(name)
        if str(value or "").strip():
            return value
        stored = self._stored(name)
        if stored is not None:
            return stored
        if value is None:
            raise KeyError(name)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self.base)

    def __len__(self) -> int:
        return len(self.base)


def environment(base: Mapping[str, str] | None = None) -> Environment:
    """``os.environ`` (or ``base``) with the OS keychain behind it when available."""
    return Environment(base, default_store(base))


def missing_hint(env: Mapping[str, str], name: str) -> str:
    """Extra advice for a missing variable when the keychain could supply it."""
    if isinstance(env, Environment) and env.store is not None:
        return f" or run: usajobs-monitor credentials set {name}"
    return ""
