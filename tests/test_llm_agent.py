"""Tests for the plugins/llm_agent.py LLM agent plugin.

The real plugin file is loaded through PluginManager so the coverage matches
what ships; settings, memory, and generated-plugin paths are pointed at temp
directories and the LLM HTTP transport is faked (nothing touches the network).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from protobot.events import EventBus
from protobot.plugin import PluginManager

PLUGIN_FILE = Path(__file__).resolve().parent.parent / "plugins" / "llm_agent.py"

TEMP_PLUGIN_SRC = (
    "from protobot import Plugin\n\n"
    "class TempA(Plugin):\n"
    '    name = "temp_a"\n\n'
    "    async def on_bot_ready(self) -> None:\n"
    "        pass\n"
)


class FakeBot:
    def __init__(self, username: str = "FakeBot") -> None:
        self.username = username
        self.player = SimpleNamespace(
            x=10.5, y=64.0, z=-20.25, yaw=90.0, pitch=0.0, on_ground=True
        )
        self.session = SimpleNamespace(
            game_mode=0, dimension_name="minecraft:overworld"
        )
        self.world = SimpleNamespace(chunks={1: None, 2: None, 3: None})
        self.entities = {101: None, 102: None}
        self.events = EventBus()
        self.sent_messages: list[str] = []
        self.sent_commands: list[str] = []
        self.walk_calls: list[tuple] = []
        self.navigate_calls: list[tuple] = []

    async def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def send_command(self, command: str) -> None:
        self.sent_commands.append(command)

    async def walk_to(self, x: float, z: float, **kwargs) -> None:
        self.walk_calls.append((x, z, kwargs))
        self.player.x, self.player.z = x, z

    async def navigate_to(self, x: float, z: float, **kwargs) -> None:
        self.navigate_calls.append((x, z, kwargs))
        self.player.x, self.player.z = x, z


class FakeSession:
    def __init__(self, host: str = "wolfx.jp", port: int = 25565) -> None:
        self.config = SimpleNamespace(
            host=host, port=port, version="26.2", online_mode=True
        )
        self.events = EventBus()


class FakeLLM:
    """Records each request and returns canned chat-completion responses."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, dict, float]] = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers, timeout))
        if not self._responses:
            raise AssertionError("no fake response left")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def assistant(content=None, tool_calls=None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def make_manager() -> PluginManager:
    manager = PluginManager([PLUGIN_FILE.parent])
    manager.discover()
    return manager


def configure(plugin, tmp: str, settings: dict | None = None) -> None:
    """Point the plugin at temp settings and derived dirs (no repo writes)."""
    path = Path(tmp) / "llm_agent.json"
    plugin._settings_file = path
    if settings is not None:
        path.write_text(
            json.dumps(settings, ensure_ascii=False), encoding="utf-8"
        )
    plugin._resolve_dirs()
    plugin._generated_dir = Path(tmp) / "gen"  # 默认 ../plugins_llm 会逃出 tmp


async def wait_until(predicate, pauses: int = 100) -> None:
    for _ in range(pauses):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


class TriggerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]
        self.plugin.bot = FakeBot(username="FakeBot")
        self.plugin._queue = asyncio.Queue()

    async def test_mention_triggers_and_records(self) -> None:
        self.plugin._settings["reply"] = {
            "all": False, "name_mention": True, "prefix": "hey,claude",
            "keywords": [],
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "你好 FakeBot！"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 1)
        entry = self.plugin._chat_log[-1]
        self.assertEqual(entry["name"], "Steve")
        self.assertEqual(entry["text"], "你好 FakeBot！")
        self.assertFalse(entry["system"])

    async def test_mention_is_case_insensitive(self) -> None:
        self.plugin._settings["reply"] = {
            "all": False, "name_mention": True, "prefix": "", "keywords": []
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "HELLO fakebot"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 1)

    async def test_prefix_triggers_even_without_mention(self) -> None:
        self.plugin._settings["reply"] = {
            "all": False, "name_mention": False, "prefix": "hey,claude",
            "keywords": [],
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "hey,claude 在吗"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 1)

    async def test_keyword_triggers(self) -> None:
        self.plugin._settings["reply"] = {
            "all": False, "name_mention": False, "prefix": "",
            "keywords": ["claude", "小助手"],
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "claude 在吗"}, None, None
        )
        await self.plugin._on_player_chat(
            None, "Alex", {"text": "CLAUDE 你好"}, None, None  # 忽略大小写
        )
        await self.plugin._on_player_chat(
            None, "Bob", {"text": "随便聊聊"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 2)

    async def test_all_mode_triggers_any_message(self) -> None:
        self.plugin._settings["reply"] = {
            "all": True, "name_mention": False, "prefix": "", "keywords": []
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "随便聊聊"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 1)

    async def test_quiet_message_recorded_but_not_triggered(self) -> None:
        self.plugin._settings["reply"] = {
            "all": False, "name_mention": True, "prefix": "hey,claude",
            "keywords": [],
        }
        await self.plugin._on_player_chat(
            None, "Steve", {"text": "今天天气不错"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 0)
        self.assertEqual(len(self.plugin._chat_log), 1)

    async def test_own_echo_matched_by_recent_send_is_ignored(self) -> None:
        # 回显按「近期发送过的内容」判定，不按名字：玩家与 bot 同名（同一
        # 正版账号）时，玩家本人的消息不能被误判成回显。
        self.plugin._sent_recent.append((time.monotonic(), "hey,claude 我自己"))
        await self.plugin._on_player_chat(
            None, "FakeBot", {"text": "hey,claude 我自己"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 0)
        self.assertEqual(self.plugin._chat_log, [])

    async def test_same_account_player_message_still_triggers(self) -> None:
        # 玩家本人与 bot 同名（同一账号）时也必须能正常触发。
        await self.plugin._on_player_chat(
            None, "FakeBot", {"text": "hey,claude 在吗"}, None, None
        )
        self.assertEqual(self.plugin._queue.qsize(), 1)
        self.assertEqual(self.plugin._chat_log[-1]["name"], "FakeBot")

    async def test_system_chat_recorded(self) -> None:
        await self.plugin._on_system_chat({"text": "[服务器] 欢迎 Steve 加入"}, False)
        entry = self.plugin._chat_log[-1]
        self.assertTrue(entry["system"])
        self.assertEqual(entry["text"], "[服务器] 欢迎 Steve 加入")

    async def test_history_capped_at_limit(self) -> None:
        self.plugin._settings["history_limit"] = 3
        for index in range(5):
            self.plugin._record_chat(
                system=False, name="Steve", text=f"消息{index}"
            )
        self.assertEqual(len(self.plugin._chat_log), 3)
        self.assertEqual(self.plugin._chat_log[0]["text"], "消息2")


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]

    def _load(self, tmp: str, settings: dict | None = None) -> None:
        self.plugin._settings_file = Path(tmp) / "llm_agent.json"
        if settings is not None:
            self.plugin._settings_file.write_text(
                json.dumps(settings, ensure_ascii=False), encoding="utf-8"
            )
        self.plugin._load_settings()

    def test_missing_settings_file_creates_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._load(tmp)
            saved = json.loads(
                self.plugin._settings_file.read_text(encoding="utf-8")
            )
            self.assertEqual(saved["llm"]["model"], "gpt-4o-mini")
            self.assertEqual(saved["history_limit"], 200)
            self.assertEqual(saved["llm"]["max_tokens"], 1_000_000)
            self.assertEqual(saved["llm"]["compact_reserve_ratio"], 0.05)
            self.assertIn("hey,claude", saved["reply"]["prefix"])
            self.assertEqual(saved["reply"]["keywords"], [])

    def test_custom_settings_merged_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._load(
                tmp,
                {"llm": {"api_key": "k", "model": "my-model"},
                 "reply": {"all": True, "keywords": ["bot"]},
                 "admins": ["mie_233"]},
            )
            self.assertEqual(self.plugin._settings["llm"]["api_key"], "k")
            self.assertEqual(self.plugin._settings["llm"]["model"], "my-model")
            self.assertEqual(self.plugin._settings["llm"]["timeout"], 120.0)  # 默认保留
            self.assertTrue(self.plugin._settings["reply"]["all"])
            self.assertEqual(self.plugin._settings["reply"]["prefix"], "hey,claude")
            self.assertEqual(self.plugin._settings["admins"], ["mie_233"])

    def test_corrupt_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._load(tmp)
            self.plugin._settings_file.write_text("not json", encoding="utf-8")
            self._load(tmp)
            self.assertEqual(self.plugin._settings["llm"]["model"], "gpt-4o-mini")

    def test_limits_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._load(tmp, {"history_limit": 5})
            self.assertEqual(self.plugin._settings["history_limit"], 10)
            self._load(tmp, {"history_limit": 99999})
            self.assertEqual(self.plugin._settings["history_limit"], 2000)

    def test_llm_window_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._load(
                tmp,
                {"llm": {"max_tokens": 100, "compact_reserve_ratio": 0.99}},
            )
            self.assertEqual(self.plugin._settings["llm"]["max_tokens"], 1000)
            self.assertEqual(
                self.plugin._settings["llm"]["compact_reserve_ratio"], 0.5
            )


class SettingsReloadTest(unittest.IsolatedAsyncioTestCase):
    """llm_agent.json 修改后约 3 秒内自动重新加载（无需热重载插件）。"""

    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]

    def _init(self, tmp: str, settings: dict | None = None) -> None:
        configure(self.plugin, tmp, settings=settings)
        self.plugin._load_settings()  # 记录 mtime 快照

    async def test_changed_settings_file_auto_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp, settings={"admins": []})
            self.assertEqual(self.plugin._settings["admins"], [])
            path = self.plugin._settings_file
            self.plugin._settings_mtime -= 1.0  # 模拟旧快照，避免同秒 mtime 抖动
            path.write_text(
                json.dumps({"admins": ["_ImWuMie"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            await self.plugin._check_settings_changed()
            self.assertEqual(self.plugin._settings["admins"], ["_ImWuMie"])

    async def test_unchanged_settings_file_not_reloaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp, settings={"admins": ["a"]})
            self.plugin._settings["admins"] = ["marker"]
            await self.plugin._check_settings_changed()
            self.assertEqual(self.plugin._settings["admins"], ["marker"])  # 未重载

    async def test_admin_effective_right_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp, settings={"admins": ["mie_233"]})
            self.plugin._generated_dir = Path(tmp) / "gen"
            self.plugin.manager = self.manager
            self.plugin._requester = "_ImWuMie"
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "x.py", "code": TEMP_PLUGIN_SRC}
            )
            self.assertIn("Permission denied for _ImWuMie", result)
            # 管理员名单改成当前玩家后，设置自动重载立即生效
            path = self.plugin._settings_file
            self.plugin._settings_mtime -= 1.0
            path.write_text(
                json.dumps({"admins": ["_ImWuMie"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            await self.plugin._check_settings_changed()
            result = await self.plugin._run_tool(
                "write_plugin",
                {
                    "filename": "x.py",
                    "code": TEMP_PLUGIN_SRC.replace('"temp_a"', '"temp_ok"'),
                },
            )
            self.assertIn("Saved and loaded", result)
            self.assertIn("temp_ok", self.manager.plugins)


class MemoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]

    def _init(self, tmp: str, host: str = "wolfx.jp") -> None:
        configure(self.plugin, tmp)
        self.plugin.session = FakeSession(host=host)

    async def test_save_memory_appends_to_memory_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            result = await self.plugin._run_tool("save_memory", {"note": "记住 A"})
            self.assertIn("Appended to MEMORY.md", result)
            await self.plugin._run_tool("save_memory", {"note": "记住 B"})
            file = Path(tmp) / "llm_agent_memory" / "wolfx_jp_25565" / "MEMORY.md"
            content = file.read_text(encoding="utf-8")
            self.assertIn("- 记住 A", content)
            self.assertIn("- 记住 B", content)

    async def test_memory_isolated_between_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            await self.plugin._run_tool("save_memory", {"note": "记住 A"})
            self.plugin.session = FakeSession(host="other.example.com")
            result = await self.plugin._run_tool("read_memory", {})
            self.assertIn("No memory files", result)

    async def test_write_memory_rewrites_whole_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            await self.plugin._run_tool("save_memory", {"note": "旧内容"})
            result = await self.plugin._run_tool(
                "write_memory", {"content": "# 规则\n\n- 不挖矿\n"}
            )
            self.assertIn("Rewrote MEMORY.md", result)
            file = Path(tmp) / "llm_agent_memory" / "wolfx_jp_25565" / "MEMORY.md"
            self.assertEqual(
                file.read_text(encoding="utf-8"), "# 规则\n\n- 不挖矿\n"
            )

    async def test_clear_memory_deletes_all_md_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            await self.plugin._run_tool("save_memory", {"note": "记住 A"})
            directory = Path(tmp) / "llm_agent_memory" / "wolfx_jp_25565"
            (directory / "notes.md").write_text("# 额外记忆\n", encoding="utf-8")
            result = await self.plugin._run_tool("clear_memory", {})
            self.assertIn("deleted 2 file(s)", result)
            self.assertEqual(list(directory.glob("*.md")), [])

    def test_multiple_md_files_included_memory_md_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            directory = Path(tmp) / "llm_agent_memory" / "wolfx_jp_25565"
            directory.mkdir(parents=True)
            (directory / "notes.md").write_text("# 备注\n", encoding="utf-8")
            (directory / "MEMORY.md").write_text("# 记忆\n", encoding="utf-8")
            text = self.plugin._read_memory_text()
            self.assertIn("## Memory file: MEMORY.md", text)
            self.assertIn("## Memory file: notes.md", text)
            self.assertLess(text.index("MEMORY.md"), text.index("notes.md"))

    def test_no_memory_files_yields_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._init(tmp)
            self.assertEqual(self.plugin._read_memory_text(), "(none yet)")


class ToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]
        self.plugin.manager = self.manager  # 模拟启用状态下的绑定
        self.plugin.bot = FakeBot()

    async def test_get_status_reports_state(self) -> None:
        result = await self.plugin._run_tool("get_status", {})
        self.assertIn("X=10.5", result)
        self.assertIn("survival", result)
        self.assertIn("3 chunks", result)
        self.assertIn("2 entities", result)
        self.assertIn("llm_agent", result)  # 插件列表

    async def test_send_message_tool_sends_and_records(self) -> None:
        result = await self.plugin._run_tool("send_message", {"text": "你好"})
        self.assertIn("Sent", result)
        self.assertEqual(self.plugin.bot.sent_messages, ["你好"])
        entry = self.plugin._chat_log[-1]
        self.assertEqual(entry["name"], "FakeBot")
        self.assertEqual(entry["text"], "你好")

    async def test_send_command_tool(self) -> None:
        result = await self.plugin._run_tool(
            "send_command", {"command": "say 你好"}
        )
        self.assertIn("Command executed", result)
        self.assertEqual(self.plugin.bot.sent_commands, ["say 你好"])

    async def test_long_reply_chunked_at_250(self) -> None:
        text = "长" * 600
        await self.plugin._run_tool("send_message", {"text": text})
        messages = self.plugin.bot.sent_messages
        self.assertEqual([len(m) for m in messages], [250, 250, 100])
        self.assertEqual(
            sum(len(entry["text"]) for entry in self.plugin._chat_log), 600
        )

    async def test_duplicate_send_skipped(self) -> None:
        first = await self.plugin._run_tool("send_message", {"text": "哈哈"})
        self.assertIn("Sent", first)
        second = await self.plugin._run_tool("send_message", {"text": "哈哈"})
        self.assertIn("Skipped duplicate message", second)
        self.assertEqual(self.plugin.bot.sent_messages, ["哈哈"])
        self.assertEqual(len(self.plugin._chat_log), 1)

    async def test_repeat_allowed_after_dedupe_window(self) -> None:
        self.plugin._sent_recent = [(time.monotonic() - 200, "哈哈")]
        result = await self.plugin._run_tool("send_message", {"text": "哈哈"})
        self.assertIn("Sent", result)
        self.assertEqual(self.plugin.bot.sent_messages, ["哈哈"])

    async def test_partial_duplicate_only_skips_matching_chunk(self) -> None:
        text = "x" * 600  # 分 3 段发送
        await self.plugin._run_tool("send_message", {"text": text})
        result = await self.plugin._run_tool(
            "send_message", {"text": "x" * 250}
        )
        self.assertIn("Skipped duplicate message", result)
        self.assertEqual(len(self.plugin.bot.sent_messages), 3)

    async def test_move_to_tool_calls_walk(self) -> None:
        result = await self.plugin._run_tool(
            "move_to", {"x": 100, "z": 200, "sprint": True}
        )
        self.assertIn("Arrived at", result)
        self.assertEqual(
            self.plugin.bot.walk_calls,
            [(100, 200, {"sprint": True, "timeout": 30.0})],
        )

    async def test_navigate_to_tool_calls_navigate(self) -> None:
        await self.plugin._run_tool("navigate_to", {"x": 50, "z": -30})
        self.assertEqual(
            self.plugin.bot.navigate_calls, [(50, -30, {"timeout": 60.0})]
        )

    async def test_unknown_tool_reported(self) -> None:
        result = await self.plugin._run_tool("no_such_tool", {})
        self.assertIn("Unknown tool", result)

    async def test_set_plugin_refuses_self(self) -> None:
        result = await self.plugin._run_tool(
            "set_plugin", {"name": "llm_agent", "enabled": False}
        )
        self.assertIn("Refused", result)
        self.assertIn("llm_agent", self.manager.plugins)

    async def test_set_plugin_disable_and_reenable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "a.py"
            file.write_text(TEMP_PLUGIN_SRC, encoding="utf-8")
            await self.manager.hot_load_file(file)
            self.assertIn("temp_a", self.manager.plugins)

            result = await self.plugin._run_tool(
                "set_plugin", {"name": "temp_a", "enabled": False}
            )
            self.assertIn("disabled", result)
            self.assertNotIn("temp_a", self.manager.plugins)

            result = await self.plugin._run_tool(
                "set_plugin", {"name": "temp_a", "enabled": True}
            )
            self.assertIn("enabled", result)
            self.assertIn("temp_a", self.manager.plugins)

    async def test_set_plugin_unknown_name(self) -> None:
        result = await self.plugin._run_tool(
            "set_plugin", {"name": "nope", "enabled": False}
        )
        self.assertIn("Plugin not found", result)

    async def test_write_plugin_loads_into_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            code = TEMP_PLUGIN_SRC.replace('"temp_a"', '"temp_hello"')
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "hello.py", "code": code}
            )
            self.assertIn("Saved and loaded plugin(s): temp_hello", result)
            self.assertIn("temp_hello", self.manager.plugins)
            target = Path(tmp) / "gen" / "hello.py"
            self.assertEqual(target.read_text(encoding="utf-8"), code)
            self.assertIn("hello.py", self.plugin._generated)
            state = json.loads(
                (Path(tmp) / "gen" / ".llm_agent_state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["generated_plugins"], ["hello.py"])

    async def test_write_plugin_reloads_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            code = TEMP_PLUGIN_SRC.replace('"temp_a"', '"temp_hello"')
            await self.plugin._run_tool(
                "write_plugin", {"filename": "hello.py", "code": code}
            )
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "hello.py", "code": code}
            )
            self.assertIn("reloaded", result)
            self.assertEqual(self.plugin._generated, ["hello.py"])

    async def test_write_plugin_rejects_bad_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "../evil.py", "code": "x = 1"}
            )
            self.assertIn("Invalid filename", result)
            self.assertFalse((Path(tmp) / "gen").exists())

    async def test_write_plugin_reports_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "broken.py", "code": "def broken(:"}
            )
            self.assertIn("load failed", result)
            self.assertNotIn("broken", self.manager.plugins)


class ReadChatToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]
        self.plugin._record_chat(system=False, name="Steve", text="你好")
        self.plugin._record_chat(system=False, name="Alex", text="hello")
        self.plugin._record_chat(system=False, name="Steve", text="你好呀")
        self.plugin._record_chat(system=True, name="", text="欢迎 Steve 加入")

    async def test_default_returns_player_lines_newest_last(self) -> None:
        result = await self.plugin._run_tool("read_chat", {})
        self.assertNotIn("[system]", result)
        self.assertIn("<Steve> 你好", result)
        self.assertIn("<Alex> hello", result)
        self.assertIn("<Steve> 你好呀", result)
        self.assertLess(result.index("你好"), result.index("你好呀"))

    async def test_players_filter_case_insensitive(self) -> None:
        result = await self.plugin._run_tool(
            "read_chat", {"players": ["STEVE"]}
        )
        self.assertIn("<Steve>", result)
        self.assertNotIn("<Alex>", result)

    async def test_keyword_filter(self) -> None:
        result = await self.plugin._run_tool("read_chat", {"keyword": "hello"})
        self.assertIn("<Alex> hello", result)
        self.assertNotIn("<Steve>", result)

    async def test_include_system(self) -> None:
        result = await self.plugin._run_tool(
            "read_chat", {"include_system": True}
        )
        self.assertIn("[system] 欢迎 Steve 加入", result)

    async def test_players_filter_excludes_system_lines(self) -> None:
        result = await self.plugin._run_tool(
            "read_chat", {"players": ["Steve"], "include_system": True}
        )
        self.assertNotIn("[system]", result)

    async def test_limit_clamped_and_respected(self) -> None:
        result = await self.plugin._run_tool("read_chat", {"limit": 1})
        self.assertIn("Latest 1 matching", result)
        self.assertNotIn("hello", result)  # 只有最新一条：Steve 的你好呀

    async def test_no_match_reported(self) -> None:
        result = await self.plugin._run_tool("read_chat", {"keyword": "zzz"})
        self.assertIn("No matching chat entries", result)

    async def test_own_messages_filterable_by_name(self) -> None:
        self.plugin._record_chat(system=False, name="FakeBot", text="我在呢")
        result = await self.plugin._run_tool(
            "read_chat", {"players": ["fakebot"]}
        )
        self.assertIn("<FakeBot> 我在呢", result)
        self.assertNotIn("<Steve>", result)


class TokenEstimateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]

    def test_cjk_counts_one_token_per_char(self) -> None:
        # 2 个中文字符 + 每条消息 4 token 开销
        self.assertEqual(
            self.plugin._estimate_messages_tokens([{"content": "中文"}]), 6
        )

    def test_ascii_counts_quarter_token_per_char(self) -> None:
        self.assertEqual(
            self.plugin._estimate_messages_tokens([{"content": "abcd"}]), 5
        )

    def test_budget_leaves_the_configured_reserve(self) -> None:
        self.plugin._settings["llm"]["max_tokens"] = 1_000_000
        self.plugin._settings["llm"]["compact_reserve_ratio"] = 0.05
        self.assertEqual(self.plugin._context_budget(), 950_000)


class ManagerSetEnabledTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_and_reopen_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text(TEMP_PLUGIN_SRC, encoding="utf-8")
            manager = PluginManager([Path(tmp)])
            manager.discover()
            self.assertIn("temp_a", manager.plugins)
            closed = await manager.set_enabled("temp_a", False)
            self.assertIsNotNone(closed)
            self.assertNotIn("temp_a", manager.plugins)
            restored = await manager.set_enabled("temp_a", True)
            self.assertIsNotNone(restored)
            self.assertIn("temp_a", manager.plugins)
            self.assertIsNotNone(manager.source_of("temp_a"))

    async def test_unknown_name_returns_none(self) -> None:
        manager = PluginManager([])
        self.assertIsNone(await manager.set_enabled("nope", True))
        self.assertIsNone(await manager.set_enabled("nope", False))


class LlmLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]
        self.plugin.manager = self.manager
        self.plugin.bot = FakeBot()
        self.plugin._settings["llm"]["api_key"] = "test-key"

    async def test_end_to_end_tool_loop_and_final_reply(self) -> None:
        fake = FakeLLM(
            assistant(tool_calls=[tool_call("send_message", {"text": "你好"})]),
            assistant(content="任务完成。"),
        )
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")

        self.assertEqual(self.plugin.bot.sent_messages, ["你好", "任务完成。"])
        url, payload, headers, _ = fake.calls[0]
        self.assertTrue(url.endswith("/chat/completions"))
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(payload["model"], "gpt-4o-mini")
        tool_names = [tool["function"]["name"] for tool in payload["tools"]]
        for expected in ("send_message", "read_chat", "write_plugin", "save_memory"):
            self.assertIn(expected, tool_names)  # 工具表随请求发送
        self.assertIn("Long-term memory", payload["messages"][0]["content"])
        # agent 对话上下文：触发消息是 user 轮次，不塞聊天日志
        self.assertEqual(payload["messages"][1]["content"], "<Steve>: 在吗")
        # 第二轮请求携带工具结果
        self.assertIn("tool", [m["role"] for m in fake.calls[1][1]["messages"]])
        # 本轮并入对话上下文：触发 + 工具调用 + 工具结果 + 最终回复
        self.assertEqual(
            self.plugin._conversation[0]["content"], "<Steve>: 在吗"
        )
        self.assertEqual(
            self.plugin._conversation[-1]["content"], "任务完成。"
        )

    async def test_duplicate_tool_then_final_sent_once(self) -> None:
        # 复现线上 bug：模型先调用 send_message 工具、最终回复又重复同一段
        # 文字——去重后只应发送一次。
        text = "哈哈，谁是美国豆包啊！找我有什么事嘛？"
        fake = FakeLLM(
            assistant(tool_calls=[tool_call("send_message", {"text": text})]),
            assistant(content=text),
        )
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(self.plugin.bot.sent_messages, [text])

    async def test_no_reply_marker_suppressed(self) -> None:
        self.plugin._post_json = FakeLLM(assistant(content="NO_REPLY"))
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.assertEqual(self.plugin._conversation[-1]["content"], "NO_REPLY")

    async def test_api_error_logged_not_fatal(self) -> None:
        self.plugin._post_json = FakeLLM(RuntimeError("HTTP 500: boom"))
        await self.plugin._handle_trigger("Steve", "在吗")  # 不应抛出
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.assertEqual(self.plugin._conversation, [])

    async def test_tool_round_cap_stops_loop(self) -> None:
        self.plugin._settings["llm"]["max_tool_rounds"] = 2
        fake = FakeLLM(
            assistant(tool_calls=[tool_call("get_status", {})]),
            assistant(tool_calls=[tool_call("get_status", {})]),
            assistant(content="晚了"),
        )
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(len(fake.calls), 2)  # 第三轮未发起
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.assertEqual(self.plugin._conversation, [])  # 放弃的轮次不并入

    async def test_trigger_skipped_without_api_key(self) -> None:
        self.plugin._settings["llm"]["api_key"] = ""
        fake = FakeLLM(assistant(content="hi"))
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(fake.calls, [])

    def _fill_conversation(self, count: int) -> None:
        for index in range(count):
            self.plugin._conversation.append(
                {
                    "role": "user",
                    "content": f"<Steve>: 消息消息消息消息消息消息消息消息消息消息{index}",
                }
            )  # 每条约 24 token（20 个 CJK + 4 开销）

    async def test_auto_compact_when_over_token_budget(self) -> None:
        self.plugin._settings["llm"]["max_tokens"] = 2000  # 预算 1900
        self._fill_conversation(60)  # 远超预算
        fake = FakeLLM(
            assistant(content="摘要内容"),
            assistant(content="回复"),
        )
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")

        self.assertEqual(self.plugin.bot.sent_messages, ["回复"])
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("tools", fake.calls[0][1])  # 摘要请求不携带工具表
        self.assertIn("tools", fake.calls[1][1])
        summary = self.plugin._conversation[0]["content"]
        self.assertTrue(summary.startswith("[Auto-compacted history]"))
        self.assertIn("摘要内容", summary)
        # 摘要 1 条 + 保留最近 10 条 + 本轮触发与回复
        self.assertEqual(len(self.plugin._conversation), 1 + 10 + 2)

    async def test_compact_failure_drops_oldest(self) -> None:
        self.plugin._settings["llm"]["max_tokens"] = 2000
        self._fill_conversation(60)
        fake = FakeLLM(
            RuntimeError("boom"),  # 摘要请求失败 → 丢弃最旧一半兜底
            assistant(content="回复"),
        )
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(self.plugin.bot.sent_messages, ["回复"])
        self.assertGreater(len(self.plugin._conversation), 20)
        self.assertFalse(
            any(
                "[Auto-compacted history]" in message["content"]
                for message in self.plugin._conversation
            )
        )

    async def test_no_compact_within_budget(self) -> None:
        self._fill_conversation(5)
        fake = FakeLLM(assistant(content="回复"))
        self.plugin._post_json = fake
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertEqual(len(fake.calls), 1)  # 未触发压缩
        self.assertEqual(self.plugin.bot.sent_messages, ["回复"])


class AdminGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = make_manager()
        self.plugin = self.manager.plugins["llm_agent"]
        self.plugin.manager = self.manager
        self.plugin.bot = FakeBot()
        self.plugin._settings["admins"] = ["mie_233"]

    async def test_non_admin_cannot_write_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            self.plugin._requester = "Steve"
            result = await self.plugin._run_tool(
                "write_plugin",
                {"filename": "x.py", "code": TEMP_PLUGIN_SRC},
            )
            self.assertIn("Permission denied", result)
            self.assertFalse((Path(tmp) / "gen").exists())

    async def test_non_admin_cannot_set_plugin(self) -> None:
        self.plugin._requester = "Steve"
        result = await self.plugin._run_tool(
            "set_plugin", {"name": "chat_logger", "enabled": False}
        )
        self.assertIn("Permission denied", result)
        self.assertIn("chat_logger", self.manager.plugins)

    async def test_admin_can_write_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            self.plugin._requester = "mie_233"
            code = TEMP_PLUGIN_SRC.replace('"temp_a"', '"temp_hello"')
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "hello.py", "code": code}
            )
            self.assertIn("Saved and loaded", result)
            self.assertIn("temp_hello", self.manager.plugins)

    async def test_admin_match_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._generated_dir = Path(tmp) / "gen"
            self.plugin._requester = "MIE_233"
            code = TEMP_PLUGIN_SRC.replace('"temp_a"', '"temp_hello"')
            result = await self.plugin._run_tool(
                "write_plugin", {"filename": "hello.py", "code": code}
            )
            self.assertIn("Saved and loaded", result)

    async def test_empty_admin_list_allows_everyone(self) -> None:
        self.plugin._settings["admins"] = []
        self.plugin._requester = None
        result = await self.plugin._run_tool(
            "set_plugin", {"name": "nope", "enabled": False}
        )
        self.assertNotIn("Permission denied", result)

    async def test_requester_reset_after_trigger(self) -> None:
        self.plugin._settings["llm"]["api_key"] = ""  # 提前返回路径
        await self.plugin._handle_trigger("Steve", "在吗")
        self.assertIsNone(self.plugin._requester)


class WorkerPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_pipeline_chat_to_llm_to_game(self) -> None:
        manager = make_manager()
        plugin = manager.plugins["llm_agent"]
        with tempfile.TemporaryDirectory() as tmp:
            configure(
                plugin,
                tmp,
                settings={"llm": {"api_key": "k", "model": "fake-model"}},
            )
            await manager.enable_all()
            try:
                self.assertIs(plugin.manager, manager)
                self.assertIsNotNone(plugin._worker_task)
                fake = FakeLLM(assistant(content="你好呀"))
                plugin._post_json = fake

                session = FakeSession()
                manager.bind_session_all(session)
                bot = FakeBot(username="FakeBot")
                await manager.bind_all(bot)
                await session.events.emit("session_ready", bot)  # 记忆/登记加载
                await bot.events.emit(
                    "player_chat",
                    None,
                    "Steve",
                    {"text": "hey,claude 你好"},
                    None,
                    None,
                )
                await wait_until(lambda: "你好呀" in bot.sent_messages)
                self.assertEqual(bot.sent_messages[-1], "你好呀")
                self.assertTrue(plugin._memory_loaded)
                self.assertTrue(
                    any(
                        message.get("content") == "你好呀"
                        for message in plugin._conversation
                    )
                )
            finally:
                manager.unbind_all(bot)
                manager.unbind_session_all(session)
                await manager.disable_all()
            self.assertIsNone(plugin.manager)
            self.assertIsNone(plugin._worker_task)
            self.assertIsNone(plugin._settings_task)


if __name__ == "__main__":
    unittest.main()
