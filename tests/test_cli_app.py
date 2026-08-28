"""Tests for the merged CLI: config codec, credentials cache, subcommands."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from protobot.cli_app import (
    _parse_address,
    credentials_ready,
    get_credentials,
    load_plugin_config,
    load_profile,
    load_session_config,
    load_tui_autostart,
    main,
    run_setup,
    save_profile,
)
from protobot.config import load_config, save_config
from protobot.session import SessionConfig


class ConfigCodecTest(unittest.TestCase):
    def test_round_trip_preserves_values(self) -> None:
        data = {
            "server": {"host": "wolfx.jp", "port": 25565, "version": "26.2"},
            "login": {"mode": "online", "offline_username": "ProtoBot"},
            "session": {
                "reconnect": True,
                "reconnect_delay": 5.0,
                "reconnect_max_attempts": None,
            },
            "plugins": {"directory": "plugins", "disabled": [], "watch": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            save_config(path, data)
            self.assertEqual(load_config(path), data)

    def test_comments_and_quoted_strings(self) -> None:
        text = """\
# 服务器配置
server:
  host: "wolfx.jp"   # 地址
  note: "a # not a comment"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            data = load_config(path)
            self.assertEqual(data["server"]["host"], "wolfx.jp")
            self.assertEqual(data["server"]["note"], "a # not a comment")

    def test_inline_lists_and_scalars(self) -> None:
        text = """\
plugins:
  disabled: [a, b]
  watch: true
  ratio: 1.5
  nothing: null
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            data = load_config(path)["plugins"]
            self.assertEqual(data["disabled"], ["a", "b"])
            self.assertIs(data["watch"], True)
            self.assertEqual(data["ratio"], 1.5)
            self.assertIsNone(data["nothing"])

    def test_deeply_nested_maps(self) -> None:
        text = "a:\n  b:\n    c: 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(load_config(path), {"a": {"b": {"c": 1}}})

    def test_errors_name_the_line(self) -> None:
        cases = {
            "a: 1\n  b: 2\n": "缩进错误",
            "a:\n\tb: 1\n": "制表符",
            "a: 1\na: 2\n": "重复的键",
            'a: "unclosed\n': "引号未闭合",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "config.yaml"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ValueError) as ctx:
                        load_config(path)
                    self.assertIn(fragment, str(ctx.exception))

    def test_strings_that_look_like_scalars_stay_strings_when_quoted(self) -> None:
        text = 'a: "true"\nb: "5"\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(text, encoding="utf-8")
            data = load_config(path)
            self.assertEqual(data, {"a": "true", "b": "5"})


class ConfigMappingTest(unittest.TestCase):
    def test_session_config_defaults(self) -> None:
        config = load_session_config({"server": {"host": "example.com"}})
        self.assertEqual(config.host, "example.com")
        self.assertEqual(config.port, 25565)
        self.assertEqual(config.version, "26.2")
        self.assertTrue(config.online_mode)
        self.assertEqual(config.reconnect_delay, 5.0)  # 自动重连默认 5 秒
        self.assertIsNone(config.reconnect_max_attempts)

    def test_session_config_overrides(self) -> None:
        config = load_session_config({
            "server": {"host": "h", "port": 1234, "version": "1.21.11"},
            "login": {"mode": "offline", "offline_username": "Steve"},
            "session": {"reconnect": False, "reconnect_delay": 9.0,
                        "reconnect_max_attempts": 3, "connect_timeout": 8.0},
        })
        self.assertEqual(config.port, 1234)
        self.assertEqual(config.version, "1.21.11")
        self.assertFalse(config.online_mode)
        self.assertEqual(config.offline_username, "Steve")
        self.assertFalse(config.reconnect)
        self.assertEqual(config.reconnect_delay, 9.0)
        self.assertEqual(config.reconnect_max_attempts, 3)
        self.assertEqual(config.connect_timeout, 8.0)

    def test_invalid_values_raise(self) -> None:
        with self.assertRaises(ValueError):
            load_session_config({"server": {}})  # missing host
        with self.assertRaises(ValueError):
            load_session_config({"server": {"host": "h"}, "login": {"mode": "x"}})
        with self.assertRaises(ValueError):
            load_session_config({"server": {"host": "h", "port": 0}})

    def test_plugin_config_resolves_directory_against_config_dir(self) -> None:
        base = Path("D:/somewhere/project")
        config = load_plugin_config({"plugins": {"directory": "my_plugins"}}, base)
        self.assertEqual(config.directory, base / "my_plugins")
        self.assertEqual(config.watch, True)
        self.assertEqual(config.disabled, ())

        config = load_plugin_config({
            "plugins": {"disabled": ["a", "b"], "watch": False}
        }, base)
        self.assertEqual(config.disabled, ("a", "b"))
        self.assertFalse(config.watch)
        self.assertEqual(config.directory, base / "plugins")  # default


class CredentialsCacheTest(unittest.TestCase):
    def test_save_then_load_round_trip(self) -> None:
        from protobot import MinecraftProfile

        profile = MinecraftProfile(
            id=uuid.UUID(int=42),
            name="mie_233",
            access_token="access",
            refresh_token="refresh",
            expires_at=1234.5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            save_profile(cache, profile, {"azure_ad": True, "client_id": "cid"})
            loaded, options = load_profile(cache)
            self.assertEqual(loaded, profile)
            self.assertEqual(options, {"client_id": "cid", "azure_ad": True})

    def test_corrupted_cache_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            cache.write_text("{not json", encoding="utf-8")
            self.assertIsNone(load_profile(cache))

    def test_missing_cache_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_profile(Path(tmp) / "nope.json"))


class GetCredentialsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "auth_cache.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_offline_mode_needs_no_cache(self) -> None:
        result = await get_credentials(
            self.cache, online_mode=False, offline_username="Offline"
        )
        self.assertEqual(result, ("Offline", None, None))

    async def test_missing_cache_in_online_mode_exits_with_guidance(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            await get_credentials(self.cache, online_mode=True, offline_username="x")
        self.assertIn("protobot login", str(ctx.exception))

    async def test_valid_profile_is_returned_as_is(self) -> None:
        from protobot import MinecraftProfile

        profile = MinecraftProfile(
            id=uuid.UUID(int=7), name="me", access_token="tok",
            refresh_token="rtok", expires_at=10**12,  # far future
        )
        save_profile(self.cache, profile, {"azure_ad": False, "client_id": None})
        name, token, profile_uuid = await get_credentials(
            self.cache, online_mode=True, offline_username="x"
        )
        self.assertEqual((name, token, profile_uuid), ("me", "tok", profile.id))

    async def test_expired_profile_refreshes_and_resaves(self) -> None:
        from protobot import MinecraftProfile

        profile = MinecraftProfile(
            id=uuid.UUID(int=7), name="me", access_token="old",
            refresh_token="rtok", expires_at=0.0,  # expired
        )
        save_profile(self.cache, profile, {"azure_ad": False, "client_id": None})

        renewed = MinecraftProfile(
            id=profile.id, name="me", access_token="new",
            refresh_token="rtok2", expires_at=10**12,
        )
        with patch("protobot.cli_app.refresh_login",
                   AsyncMock(return_value=renewed)) as refresh:
            name, token, profile_uuid = await get_credentials(
                self.cache, online_mode=True, offline_username="x"
            )
        refresh.assert_awaited_once_with("rtok")
        self.assertEqual((name, token, profile_uuid), ("me", "new", profile.id))
        # the refreshed token was written back to the cache
        reloaded, _ = load_profile(self.cache)
        self.assertEqual(reloaded.access_token, "new")


class AddressParseTest(unittest.TestCase):
    def test_host_and_port(self) -> None:
        self.assertEqual(_parse_address("wolfx.jp"), ("wolfx.jp", 25565))
        self.assertEqual(_parse_address("wolfx.jp:25566"), ("wolfx.jp", 25566))

    def test_invalid_addresses(self) -> None:
        with self.assertRaises(ValueError):
            _parse_address(":25565")  # missing host
        with self.assertRaises(ValueError):
            _parse_address("host:notaport")
        with self.assertRaises(ValueError):
            _parse_address("host:70000")


class CliSubcommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self, **overrides) -> Path:
        config_path = self.directory / "config.yaml"
        save_config(config_path, {
            "server": {"host": "127.0.0.1", "port": 1, "version": "26.2"},
            "login": {"mode": "offline", "offline_username": "ProtoBot"},
            "session": {"reconnect": False, "reconnect_delay": 5.0,
                        "reconnect_max_attempts": None},
            "plugins": {"directory": "plugins", "disabled": [], "watch": True},
        })
        return config_path

    def test_plugins_listing_shows_enabled_plugins(self) -> None:
        plugins_dir = self.directory / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "demo.py").write_text("""\
from protobot.plugin import Plugin

class Demo(Plugin):
    name = "demo"
    dependencies = ()
""", encoding="utf-8")
        config_path = self._write_config()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["plugins", "--config", str(config_path)])
        self.assertEqual(code, 0)
        self.assertIn("demo", stdout.getvalue())
        self.assertIn("启用", stdout.getvalue())

    def test_plugins_listing_reports_syntax_errors(self) -> None:
        plugins_dir = self.directory / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        config_path = self._write_config()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["plugins", "--config", str(config_path)])
        self.assertEqual(code, 1)
        self.assertIn("加载失败", stdout.getvalue())

    def test_first_launch_runs_the_setup_wizard(self) -> None:
        config_path = self.directory / "config.yaml"
        inputs = iter(["2", "127.0.0.1:25566", ""])
        stdout = io.StringIO()
        with patch("builtins.input", side_effect=lambda prompt="": next(inputs)), \
             contextlib.redirect_stdout(stdout):
            code = main(["plugins", "--config", str(config_path)])
        self.assertEqual(code, 0)
        self.assertTrue(config_path.exists())
        data = load_config(config_path)
        self.assertEqual(data["server"]["host"], "127.0.0.1")
        self.assertEqual(data["server"]["port"], 25566)
        self.assertEqual(data["server"]["version"], "26.2")
        self.assertEqual(data["login"]["mode"], "online")
        self.assertIn("首次配置向导", stdout.getvalue())

        # A second launch finds the config and skips the wizard.
        with patch("builtins.input", side_effect=AssertionError("wizard reran")), \
             contextlib.redirect_stdout(io.StringIO()):
            code = main(["plugins", "--config", str(config_path)])
        self.assertEqual(code, 0)

    def test_run_without_network_returns_cleanly(self) -> None:
        config_path = self._write_config()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["run", "--config", str(config_path)])
        self.assertEqual(code, 0)  # offline, reconnect=false, port closed

    def test_run_bot_shim_is_importable(self) -> None:
        import run_bot  # the PyCharm entry point must survive the refactor

        self.assertTrue(callable(run_bot.main))


class CredentialsReadyTest(unittest.TestCase):
    """TUI 自动启动的前提：凭据是否已就绪。"""

    def _config(self, *, online: bool) -> SessionConfig:
        return SessionConfig(host="h", online_mode=online)

    def test_offline_is_always_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            self.assertTrue(credentials_ready(cache, self._config(online=False)))

    def test_online_without_a_cache_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            self.assertFalse(credentials_ready(cache, self._config(online=True)))

    def test_online_with_a_fresh_token_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "name": "P",
                        "uuid": str(uuid.uuid4()),
                        "access_token": "t",
                        "refresh_token": None,
                        "expires_at": time.time() + 3600,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(credentials_ready(cache, self._config(online=True)))

    def test_expired_token_with_a_refresh_token_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "name": "P",
                        "uuid": str(uuid.uuid4()),
                        "access_token": "t",
                        "refresh_token": "r",
                        "expires_at": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(credentials_ready(cache, self._config(online=True)))

    def test_expired_token_without_a_refresh_token_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "name": "P",
                        "uuid": str(uuid.uuid4()),
                        "access_token": "t",
                        "refresh_token": None,
                        "expires_at": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse(credentials_ready(cache, self._config(online=True)))

    def test_corrupt_cache_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "auth_cache.json"
            cache.write_text("not json", encoding="utf-8")
            self.assertFalse(credentials_ready(cache, self._config(online=True)))


class TuiAutostartConfigTest(unittest.TestCase):
    def test_defaults_to_true(self) -> None:
        self.assertTrue(load_tui_autostart({}))
        self.assertTrue(load_tui_autostart({"tui": {}}))

    def test_explicit_switch_wins(self) -> None:
        self.assertFalse(load_tui_autostart({"tui": {"autostart": False}}))
        self.assertTrue(load_tui_autostart({"tui": {"autostart": True}}))

    def test_non_mapping_section_falls_back(self) -> None:
        self.assertTrue(load_tui_autostart({"tui": "nope"}))

    def test_wizard_writes_the_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            answers = iter(["1", "Bot", "example.com", "26.2"])
            with patch("builtins.input", lambda *a: next(answers)):
                run_setup(config)
            data = load_config(config)
            self.assertTrue(data["tui"]["autostart"])


if __name__ == "__main__":
    unittest.main()
