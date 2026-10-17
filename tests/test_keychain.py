"""Optional keychain storage: precedence over the environment, safe fallbacks, and the CLI."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usajobs_monitor import cli, delivery, keychain
from usajobs_monitor.config import ConfigError, Settings, resolve_credentials

KEY = "k3y-value-that-must-not-leak"


class FakeStore:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.reads = []

    def get(self, name):
        self.reads.append(name)
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


class PasswordDeleteError(Exception):
    pass


class FakeKeyring:
    """Stands in for the ``keyring`` module."""

    def __init__(self, backend_module="keyring.backends.macOS", fail=False):
        self.saved = {}
        self.fail = fail
        self.backend = type("Keyring", (), {"__module__": backend_module, "priority": 5, "name": "Fake"})()

    def get_keyring(self):
        return self.backend

    def get_password(self, service, name):
        if self.fail:
            raise RuntimeError(f"locked; {KEY}")
        return self.saved.get((service, name))

    def set_password(self, service, name, value):
        if self.fail:
            raise RuntimeError(f"locked; {KEY}")
        self.saved[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.saved:
            raise PasswordDeleteError(name)
        del self.saved[(service, name)]


class EnvironmentTests(unittest.TestCase):
    def test_environment_wins_over_keychain(self):
        env = keychain.Environment({"USAJOBS_API_KEY": "from-env"}, FakeStore({"USAJOBS_API_KEY": KEY}))
        self.assertEqual(env.get("USAJOBS_API_KEY"), "from-env")
        self.assertEqual(env.source("USAJOBS_API_KEY"), "environment")

    def test_keychain_fills_unset_or_blank_variables(self):
        store = FakeStore({"USAJOBS_API_KEY": KEY, "USAJOBS_EMAIL": "me@example.com"})
        env = keychain.Environment({"USAJOBS_EMAIL": "  "}, store)
        self.assertEqual(env.get("USAJOBS_API_KEY"), KEY)
        self.assertEqual(env.get("USAJOBS_EMAIL"), "me@example.com")
        self.assertEqual(env.source("USAJOBS_API_KEY"), "keychain")
        self.assertIsNone(env.get("OTHER"))
        self.assertIsNone(env.source("OTHER"))

    def test_each_name_is_read_once(self):
        store = FakeStore({"USAJOBS_API_KEY": KEY})
        env = keychain.Environment({}, store)
        for _ in range(3):
            env.get("USAJOBS_API_KEY")
            env.get("MISSING")
        self.assertEqual(sorted(store.reads), ["MISSING", "USAJOBS_API_KEY"])

    def test_without_store_behaves_like_the_environment(self):
        env = keychain.Environment({"A": "1", "B": ""}, None)
        self.assertEqual((env.get("A"), env.get("B"), env.get("C")), ("1", "", None))
        self.assertEqual(sorted(env), ["A", "B"])
        self.assertEqual(keychain.missing_hint(env, "A"), "")

    def test_resolve_credentials_reads_the_keychain(self):
        config = {}
        store = FakeStore({"USAJOBS_EMAIL": "me@example.com", "USAJOBS_API_KEY": KEY})
        self.assertEqual(resolve_credentials(config, keychain.Environment({}, store)), [])
        self.assertEqual(config["credentials"], {"email": "me@example.com", "api_key": KEY})

    def test_missing_credentials_suggest_the_keychain_only_when_it_exists(self):
        with self.assertRaises(ConfigError) as caught:
            resolve_credentials({}, keychain.Environment({}, FakeStore()))
        self.assertIn("or run: usajobs-monitor credentials set USAJOBS_API_KEY", str(caught.exception))
        with self.assertRaises(ConfigError) as caught:
            resolve_credentials({}, keychain.Environment({}, None))
        self.assertNotIn("credentials set", str(caught.exception))

    def test_delivery_secrets_come_from_the_keychain(self):
        hook = "https://hooks.slack.com/services/T000/B000/abc"
        settings = Settings(config={"delivery": {"slack": {"webhook_url_env": "USAJOBS_SLACK_WEBHOOK_URL"}}},
                            config_path=Path("x.toml"), reports_dir=Path("."), decisions_path=Path("d.csv"),
                            resume_folder=None)
        ready = delivery.check_channels(settings.config, keychain.Environment(
            {}, FakeStore({"USAJOBS_SLACK_WEBHOOK_URL": hook})))
        self.assertEqual(ready, [("slack", "ready")])
        missing = dict(delivery.check_channels(settings.config, keychain.Environment({}, FakeStore())))
        self.assertIn("credentials set USAJOBS_SLACK_WEBHOOK_URL", missing["slack"])


class StoreTests(unittest.TestCase):
    def test_keyring_store_round_trip(self):
        fake = FakeKeyring()
        store = keychain.KeyringStore(fake)
        store.set("USAJOBS_API_KEY", KEY)
        self.assertEqual(fake.saved, {("usajobs-monitor", "USAJOBS_API_KEY"): KEY})
        self.assertEqual(store.get("USAJOBS_API_KEY"), KEY)
        self.assertTrue(store.delete("USAJOBS_API_KEY"))
        self.assertFalse(store.delete("USAJOBS_API_KEY"))
        self.assertIsNone(store.get("USAJOBS_API_KEY"))

    def test_keyring_errors_never_include_secrets(self):
        store = keychain.KeyringStore(FakeKeyring(fail=True))
        self.assertIsNone(store.get("USAJOBS_API_KEY"))
        with self.assertRaises(keychain.KeychainError) as caught:
            store.set("USAJOBS_API_KEY", KEY)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_default_store_falls_back_safely(self):
        with patch.dict("sys.modules", {"keyring": FakeKeyring()}):
            self.assertIsInstance(keychain.default_store({}), keychain.KeyringStore)
            for off in ("0", "off", "False"):
                self.assertIsNone(keychain.default_store({"USAJOBS_KEYRING": off}))
        with patch.dict("sys.modules", {"keyring": FakeKeyring("keyring.backends.fail")}):
            self.assertIsNone(keychain.default_store({}))
        with patch.dict("sys.modules", {"keyring": None}):
            self.assertIsNone(keychain.default_store({}))

    def test_names_are_checked(self):
        self.assertEqual(keychain.check_name("USAJOBS_API_KEY"), "USAJOBS_API_KEY")
        for bad in ("", "1ABC", "has space", "a-b"):
            with self.assertRaises(keychain.KeychainError):
                keychain.check_name(bad)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        patcher = patch.object(keychain, "default_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.missing_config = Path(self.temp.name) / "none.toml"

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("sys.stdin", io.StringIO(stdin)), patch.dict("os.environ", {}, clear=True):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_set_from_stdin_status_and_delete(self):
        code, out, err = self.run_cli("credentials", "set", "USAJOBS_API_KEY", "--stdin", stdin=KEY + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.values, {"USAJOBS_API_KEY": KEY})
        code, out, err = self.run_cli("credentials", "status", "--config", str(self.missing_config))
        self.assertEqual(code, 0)
        self.assertIn("USAJOBS_API_KEY: stored in the keychain", out)
        self.assertIn("USAJOBS_EMAIL: missing", out)
        self.assertNotIn(KEY, out + err)
        self.assertEqual(self.run_cli("credentials", "delete", "USAJOBS_API_KEY")[0], 0)
        self.assertEqual(self.store.values, {})

    def test_set_prompts_without_echo(self):
        with patch("getpass.getpass", return_value=KEY) as prompt:
            code, out, err = self.run_cli("credentials", "set", "USAJOBS_SMTP_PASSWORD")
        self.assertEqual(code, 0)
        prompt.assert_called_once()
        self.assertEqual(self.store.values["USAJOBS_SMTP_PASSWORD"], KEY)
        self.assertNotIn(KEY, out + err)

    def test_empty_value_and_bad_name_are_refused(self):
        self.assertEqual(self.run_cli("credentials", "set", "USAJOBS_API_KEY", "--stdin", stdin="\n")[0], 2)
        self.assertEqual(self.run_cli("credentials", "set", "not valid", "--stdin", stdin="x\n")[0], 2)
        self.assertEqual(self.store.values, {})

    def test_status_lists_delivery_secrets_from_the_config(self):
        config = Path(self.temp.name) / "profile.toml"
        config.write_text('[search]\nlocations = ["Arlington, Virginia"]\ntarget_role_keywords = ["budget"]\n'
                          '[delivery.ntfy]\n')
        self.store.values["USAJOBS_NTFY_TOPIC"] = "topic-secret"
        code, out, err = self.run_cli("credentials", "status", "--config", str(config))
        self.assertEqual(code, 0)
        self.assertIn("USAJOBS_NTFY_TOPIC: stored in the keychain", out)
        self.assertIn("USAJOBS_NTFY_TOKEN: not set (optional)", out)
        self.assertNotIn("topic-secret", out)

    def test_no_keychain_is_reported(self):
        with patch.object(keychain, "default_store", return_value=None):
            code, out, err = self.run_cli("credentials", "set", "USAJOBS_API_KEY", "--stdin", stdin="x\n")
        self.assertEqual(code, 2)
        self.assertIn("No keychain available", err)


if __name__ == "__main__":
    unittest.main()
