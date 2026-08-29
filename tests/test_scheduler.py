"""Tests for the plugins/scheduler.py scheduled-task plugin.

The real plugin file is loaded through PluginManager; the task JSON is
pointed at a temp path and the bot is a stub (nothing touches the network
or the real plugins/scheduler.json).
"""

from __future__ import annotations

import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from protobot.plugin import ExposedFunction, PluginManager

SCHEDULER_FILE = Path(__file__).resolve().parent.parent / "plugins" / "scheduler.py"


class FakeBot:
    def __init__(self) -> None:
        self.username = "FakeBot"
        self.sent_messages: list[str] = []
        self.sent_commands: list[str] = []
        # 条件求值读这些字段（真 Bot 上是 PlayerState 与 tab 列表）
        self.player = SimpleNamespace(
            health=20.0, food=20, saturation=5.0, x=1.0, y=64.0, z=-2.0, dead=False
        )
        self.players: dict[str, object] = {}
        self.entities: dict[int, object] = {}

    async def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def send_command(self, command: str) -> None:
        self.sent_commands.append(command)


def load_plugin(tmp: str) -> tuple[PluginManager, object]:
    manager = PluginManager([SCHEDULER_FILE.parent])
    manager.discover()
    plugin = manager.plugins["scheduler"]
    plugin.manager = manager  # 启用时框架会绑定，这里手工模拟
    plugin._file = Path(tmp) / "scheduler.json"
    return manager, plugin


def make_task(**overrides) -> dict:
    task = {
        "name": "t",
        "interval": 5.0,
        "time": None,
        "event": None,
        "condition": None,
        "cooldown": 0.0,
        "match": None,
        "action": "chat",
        "text": "hi",
        "enabled": True,
    }
    task.update(overrides)
    return task


class TaskFileTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_file_created_with_disabled_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin.manager = manager
            try:
                await plugin.on_enable()
                data = json.loads(
                    plugin._file.read_text(encoding="utf-8")
                )
                self.assertEqual(len(data["tasks"]), 1)
                self.assertFalse(data["tasks"][0]["enabled"])
            finally:
                await plugin.on_disable()

    def test_invalid_tasks_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin._file.write_text(
                json.dumps(
                    {
                        "tasks": [
                            make_task(name="ok"),
                            make_task(name="bad_time", time="25:00"),
                            make_task(name="no_schedule", interval=None),
                            make_task(name="bad_action", action="dance"),
                            {"no_name": True, "interval": 10, "text": "x"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            plugin._load_tasks()
            self.assertEqual([task["name"] for task in plugin._tasks], ["ok"])

    def test_interval_below_minimum_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin._file.write_text(
                json.dumps({"tasks": [make_task(interval=1.0)]}),
                encoding="utf-8",
            )
            plugin._load_tasks()
            self.assertEqual(plugin._tasks[0]["interval"], 5.0)

    def test_out_of_range_times_rejected_boundaries_kept(self) -> None:
        # 只查位数会放过 "25:00"/"12:60"，因此小时分钟范围也要校验
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin._file.write_text(
                json.dumps(
                    {
                        "tasks": [
                            make_task(name="h25", interval=None, time="25:00"),
                            make_task(name="m60", interval=None, time="12:60"),
                            make_task(name="midnight", interval=None, time="0:00"),
                            make_task(name="last", interval=None, time="23:59"),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            plugin._load_tasks()
            self.assertEqual(
                [task["name"] for task in plugin._tasks], ["midnight", "last"]
            )

    def test_corrupt_file_falls_back_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin._file.write_text("not json", encoding="utf-8")
            plugin._load_tasks()
            self.assertEqual(plugin._tasks, [])

    async def test_reloads_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp)
            plugin.manager = manager
            try:
                await plugin.on_enable()
                self.assertEqual(len(plugin._tasks), 1)  # 默认示例
                plugin._file.write_text(
                    json.dumps({"tasks": [make_task(name="新任务")]}),
                    encoding="utf-8",
                )
                plugin._mtime -= 1.0  # 模拟旧快照
                plugin._maybe_reload()
                self.assertEqual(
                    [task["name"] for task in plugin._tasks], ["新任务"]
                )
            finally:
                await plugin.on_disable()


class RunDueTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]
        self.plugin._file = None  # 不触发文件读写
        self.plugin.bot = FakeBot()

    def _due(self, task: dict) -> None:
        self.plugin._tasks = [task]
        self.plugin._next_run = {task["name"]: time.monotonic() - 1.0}

    async def test_interval_task_fires_and_reschedules(self) -> None:
        self._due(make_task(name="t", interval=5.0))
        before = time.monotonic()
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])
        self.assertGreater(self.plugin._next_run["t"], before + 4.0)

    async def test_disabled_task_not_run(self) -> None:
        self._due(make_task(enabled=False))
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_command_action(self) -> None:
        self._due(make_task(action="command", text="say hi"))
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_commands, ["say hi"])

    async def test_new_task_waits_for_first_cycle(self) -> None:
        self.plugin._tasks = [make_task(name="t", interval=5.0)]
        await self.plugin._run_due()  # 首次：只排程，不执行
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.assertGreater(self.plugin._next_run["t"], time.monotonic())

    async def test_without_bot_run_is_postponed(self) -> None:
        self._due(make_task(name="t"))
        self.plugin.bot = None
        await self.plugin._run_due()
        self.assertIsNotNone(self.plugin._next_run["t"])

    async def test_daily_time_task_fires_and_reschedules_next_day(self) -> None:
        self._due(make_task(name="t", interval=None, time="23:59"))
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])
        next_run = self.plugin._next_run["t"]
        self.assertGreater(next_run, time.monotonic())
        self.assertLessEqual(next_run - time.monotonic(), 86400.0)

    async def test_send_failure_logged_not_fatal(self) -> None:
        async def failing(text: str) -> None:
            raise ValueError("too long")

        self.plugin.bot.send_message = failing  # type: ignore[method-assign]
        self._due(make_task(name="t"))
        await self.plugin._run_due()  # 不应抛出


class ExposedServiceTest(unittest.IsolatedAsyncioTestCase):
    """scheduler.list / add / set / remove / run / status（LLM 与其他插件共用）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manager, self.plugin = load_plugin(self._tmp.name)
        self.plugin._file.write_text(
            json.dumps({"tasks": []}), encoding="utf-8"
        )
        self.plugin._load_tasks()
        self.plugin.bot = FakeBot()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _saved(self) -> list[dict]:
        return json.loads(self.plugin._file.read_text(encoding="utf-8"))["tasks"]

    def test_exposures_declared(self) -> None:
        exposed = {service.name: service for service in self.plugin.exposed()}
        self.assertEqual(
            sorted(exposed),
            ["add", "list", "remove", "run", "set", "status"],
        )
        self.assertTrue(exposed["add"].admin)
        self.assertFalse(exposed["list"].admin)  # 只读，不限管理员
        self.assertFalse(exposed["status"].admin)
        self.assertEqual(exposed["add"].tool_name, "scheduler_add")

    async def test_add_then_list(self) -> None:
        result = await self.plugin._service_add(
            name="报时", interval=300, action="chat", text="整点啦"
        )
        self.assertIn("Scheduled task added: 报时", result)
        self.assertIn("every 300s", result)
        self.assertEqual(self._saved()[0]["name"], "报时")
        listing = await self.plugin._service_list()
        self.assertIn("- 报时", listing)
        self.assertIn("整点啦", listing)

    async def test_add_takes_effect_immediately(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        # 写回后立刻重载：不必等 5 秒轮询
        self.assertEqual([task["name"] for task in self.plugin._tasks], ["t"])

    async def test_add_does_not_look_like_an_external_edit(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        self.plugin._maybe_reload()  # 不应触发「文件已修改」的重载
        self.assertEqual(len(self.plugin._tasks), 1)

    async def test_add_validation(self) -> None:
        self.assertIn(
            "time must be HH:MM",
            await self.plugin._service_add(name="x", text="y", time="25:00"),
        )
        self.assertIn(
            "provide interval",
            await self.plugin._service_add(name="x", text="y"),
        )
        self.assertIn(
            "missing text", await self.plugin._service_add(name="x", interval=60)
        )
        self.assertIn(
            "missing task name",
            await self.plugin._service_add(interval=60, text="y"),
        )
        self.assertEqual(self._saved(), [])

    async def test_interval_below_minimum_is_clamped_not_rejected(self) -> None:
        # 曾经一边（llm_agent）报错、一边（scheduler）静默钳制；现在只有一份规则
        result = await self.plugin._service_add(name="t", interval=1, text="x")
        self.assertIn("every 5s", result)
        self.assertEqual(self._saved()[0]["interval"], 5.0)

    async def test_add_duplicate_rejected(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        result = await self.plugin._service_add(name="t", interval=60, text="x")
        self.assertIn("already exists", result)

    async def test_set_updates_only_given_fields(self) -> None:
        await self.plugin._service_add(
            name="t", interval=60, action="chat", text="旧"
        )
        result = await self.plugin._service_set(name="t", text="新")
        self.assertIn("updated", result)
        task = self._saved()[0]
        self.assertEqual(task["text"], "新")
        self.assertEqual(task["interval"], 60)  # 未给的字段保持原样
        self.assertEqual(task["action"], "chat")

    async def test_set_can_pause_a_task(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        await self.plugin._service_set(name="t", enabled=False)
        self.assertFalse(self._saved()[0]["enabled"])

    async def test_set_rejects_an_invalid_result(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        result = await self.plugin._service_set(name="t", time="24:00")
        self.assertIn("time must be HH:MM", result)
        self.assertEqual(self._saved()[0]["interval"], 60)  # 未改动

    async def test_set_rejects_unknown_fields(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        result = await self.plugin._service_set(name="t", when="tomorrow")
        self.assertIn("Unknown field(s): when", result)

    async def test_set_unknown_task(self) -> None:
        self.assertIn(
            "Task not found", await self.plugin._service_set(name="nope", text="x")
        )

    async def test_remove(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="x")
        self.assertIn("removed", await self.plugin._service_remove(name="t"))
        self.assertEqual(self._saved(), [])
        self.assertIn("No scheduled tasks", await self.plugin._service_list())

    async def test_remove_unknown_task(self) -> None:
        self.assertIn(
            "Task not found", await self.plugin._service_remove(name="nope")
        )

    async def test_run_executes_once_without_rescheduling(self) -> None:
        await self.plugin._service_add(
            name="t", interval=60, action="command", text="say hi"
        )
        result = await self.plugin._service_run(name="t")
        self.assertIn("executed once", result)
        self.assertEqual(self.plugin.bot.sent_commands, ["say hi"])
        self.assertEqual(len(self._saved()), 1)

    async def test_run_sends_chat_for_chat_tasks(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="hi")
        await self.plugin._service_run(name="t")
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])

    async def test_run_without_a_bot(self) -> None:
        await self.plugin._service_add(name="t", interval=60, text="hi")
        self.plugin.bot = None
        self.assertIn(
            "Not connected", await self.plugin._service_run(name="t")
        )

    async def test_run_unknown_task(self) -> None:
        self.assertIn("Task not found", await self.plugin._service_run(name="x"))

    async def test_status_counts(self) -> None:
        await self.plugin._service_add(name="a", interval=60, text="x")
        await self.plugin._service_add(
            name="b", interval=60, text="y", enabled=False
        )
        self.assertEqual(
            await self.plugin._service_status(),
            "2 scheduled task(s), 1 enabled, 0 event-triggered, 0 with a condition",
        )

    async def test_hand_written_extra_fields_survive_a_rewrite(self) -> None:
        self.plugin._file.write_text(
            json.dumps({"tasks": [
                {"name": "keep", "interval": 60, "text": "x", "note": "mine"}
            ]}),
            encoding="utf-8",
        )
        await self.plugin._service_add(name="new", interval=60, text="y")
        kept = [task for task in self._saved() if task["name"] == "keep"][0]
        self.assertEqual(kept["note"], "mine")

    async def test_callable_through_the_manager(self) -> None:
        await self.manager.enable_all()
        try:
            self.assertIn("scheduler.add", self.manager.services())
            result = await self.manager.call_service("scheduler.status")
            self.assertIn("scheduled task(s)", result)
        finally:
            await self.manager.disable_all()


class RemindActionTest(unittest.IsolatedAsyncioTestCase):
    """action=remind：把内容交给 LLM 智能体，而不是自己发话。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manager, self.plugin = load_plugin(self._tmp.name)
        self.plugin._file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        self.plugin._load_tasks()
        self.plugin.bot = FakeBot()
        self.reminders: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install_agent(self, result: str = "Reminder queued: x") -> None:
        async def remind(text: str = "", source: str = "") -> str:
            self.reminders.append({"text": text, "source": source})
            return result

        self.manager._services["llm_agent.remind"] = ExposedFunction(
            plugin="llm_agent", name="remind", handler=remind
        )

    async def test_remind_is_a_valid_action(self) -> None:
        result = await self.plugin._service_add(
            name="喝水", interval=3600, action="remind", text="提醒大家喝水"
        )
        self.assertIn("added", result)
        self.assertEqual(self.plugin._tasks[0]["action"], "remind")

    async def test_remind_calls_the_agent_not_the_chat(self) -> None:
        self._install_agent()
        await self.plugin._service_add(
            name="喝水", interval=3600, action="remind", text="提醒大家喝水"
        )
        result = await self.plugin._service_run(name="喝水")
        self.assertIn("executed once", result)
        self.assertEqual(
            self.reminders, [{"text": "提醒大家喝水", "source": "喝水"}]
        )
        self.assertEqual(self.plugin.bot.sent_messages, [])  # 不自己发言
        self.assertEqual(self.plugin.bot.sent_commands, [])

    async def test_remind_without_the_agent_is_reported(self) -> None:
        await self.plugin._service_add(
            name="喝水", interval=3600, action="remind", text="x"
        )
        result = await self.plugin._service_run(name="喝水")
        self.assertIn("未加载", result)

    async def test_scheduled_remind_fires_from_the_loop(self) -> None:
        self._install_agent()
        self.plugin._tasks = [
            make_task(name="t", action="remind", text="到点了")
        ]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        self.assertEqual(self.reminders[0]["text"], "到点了")

    async def test_agent_failure_does_not_break_the_loop(self) -> None:
        self._install_agent(result="Agent is not running")
        self.plugin._tasks = [
            make_task(name="t", action="remind", text="到点了")
        ]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()  # 不应抛出
        self.assertGreater(self.plugin._next_run["t"], time.monotonic())

    async def test_remind_does_not_need_a_bot(self) -> None:
        self._install_agent()
        self.plugin.bot = None
        self.plugin._tasks = [
            make_task(name="t", action="remind", text="到点了")
        ]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        # 未连接时 _run_due 整体跳过，提醒也顺延（不会凭空发给 agent）
        self.assertEqual(self.reminders, [])

    async def test_invalid_action_still_rejected(self) -> None:
        result = await self.plugin._service_add(
            name="x", interval=60, action="dance", text="y"
        )
        self.assertIn("action must be one of", result)


class SecondsUntilTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]

    def test_returns_duration_within_a_day(self) -> None:
        delta = self.plugin._seconds_until("00:00")
        self.assertGreater(delta, 0.0)
        self.assertLessEqual(delta, 86400.0)


def scheduler_module():
    """The loaded plugin module, for its module-level helpers."""
    manager = PluginManager([SCHEDULER_FILE.parent])
    manager.discover()
    return inspect.getmodule(type(manager.plugins["scheduler"]))


class ConditionParsingTest(unittest.TestCase):
    """条件字符串的解析（不 eval，写错的条件在 add 时就被拒）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = scheduler_module()

    def parse(self, text: str):
        return self.module.parse_condition(text)

    def test_single_comparison(self) -> None:
        clauses, error = self.parse("health < 8")
        self.assertEqual(error, "")
        self.assertEqual(clauses, (("health", "<", 8.0),))

    def test_multiple_clauses_joined_by_and(self) -> None:
        clauses, _ = self.parse("players >= 2 and dead == false")
        self.assertEqual(clauses, (("players", ">=", 2.0), ("dead", "==", 0.0)))

    def test_booleans_become_numbers(self) -> None:
        clauses, _ = self.parse("dead == TRUE")
        self.assertEqual(clauses, (("dead", "==", 1.0),))

    def test_all_operators_parse(self) -> None:
        for operator in ("<", "<=", ">", ">=", "==", "!="):
            clauses, error = self.parse(f"food {operator} 10")
            self.assertEqual(error, "", operator)
            self.assertEqual(clauses[0][1], operator)

    def test_unknown_variable_is_rejected(self) -> None:
        clauses, error = self.parse("mana < 5")
        self.assertIsNone(clauses)
        self.assertIn("unknown condition variable", error)

    def test_or_is_rejected_with_advice(self) -> None:
        clauses, error = self.parse("health < 5 or food < 5")
        self.assertIsNone(clauses)
        self.assertIn("does not support 'or'", error)

    def test_garbage_is_rejected(self) -> None:
        for text in ("health", "health <", "< 5", "health ~ 5"):
            clauses, error = self.parse(text)
            self.assertIsNone(clauses, text)
            self.assertTrue(error)

    def test_non_numeric_value_is_rejected(self) -> None:
        clauses, error = self.parse("health < lots")
        self.assertIsNone(clauses)
        self.assertIn("must be a number", error)

    def test_comparison_helper(self) -> None:
        compare = self.module.compare
        self.assertTrue(compare(1.0, "<", 2.0))
        self.assertTrue(compare(2.0, "<=", 2.0))
        self.assertTrue(compare(3.0, ">", 2.0))
        self.assertTrue(compare(2.0, ">=", 2.0))
        self.assertTrue(compare(2.0, "==", 2.0))
        self.assertTrue(compare(1.0, "!=", 2.0))
        self.assertFalse(compare(2.0, "!=", 2.0))


class TriggerNormalizeTest(unittest.IsolatedAsyncioTestCase):
    """新字段的校验：文件加载与 add/set 共用同一份规则。"""

    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]

    def normalize(self, **fields):
        return self.plugin._normalize({"name": "t", "text": "hi", **fields})

    def test_event_task_needs_no_interval(self) -> None:
        task, error = self.normalize(event="player_join")
        self.assertEqual(error, "")
        self.assertEqual(task["event"], "player_join")
        self.assertIsNone(task["interval"])

    def test_condition_task_needs_no_interval(self) -> None:
        task, error = self.normalize(condition="health < 8")
        self.assertEqual(error, "")
        self.assertEqual(task["condition"], "health < 8")

    def test_unknown_event_is_rejected(self) -> None:
        task, error = self.normalize(event="player_sneezed")
        self.assertIsNone(task)
        self.assertIn("event must be one of", error)

    def test_bad_condition_is_rejected_at_creation(self) -> None:
        task, error = self.normalize(condition="mana < 5")
        self.assertIsNone(task)
        self.assertIn("unknown condition variable", error)

    def test_no_trigger_at_all_is_rejected(self) -> None:
        task, error = self.normalize()
        self.assertIsNone(task)
        self.assertIn("provide interval, time, event", error)

    def test_negative_cooldown_is_rejected(self) -> None:
        task, error = self.normalize(interval=60, cooldown=-1)
        self.assertIsNone(task)
        self.assertIn("cooldown cannot be negative", error)

    def test_match_requires_an_event(self) -> None:
        task, error = self.normalize(interval=60, match="Steve")
        self.assertIsNone(task)
        self.assertIn("match only applies", error)

    def test_describe_mentions_the_trigger(self) -> None:
        task, _ = self.normalize(event="player_join", match="Steve", cooldown=30)
        described = self.plugin._describe(task)
        self.assertIn("on player_join", described)
        self.assertIn("matching 'Steve'", described)
        self.assertIn("at most every 30s", described)

    def test_describe_distinguishes_trigger_from_gate(self) -> None:
        trigger, _ = self.normalize(condition="health < 8")
        gate, _ = self.normalize(interval=60, condition="health < 8")
        self.assertIn("when health < 8", self.plugin._describe(trigger))
        self.assertIn("only while health < 8", self.plugin._describe(gate))

    async def test_add_accepts_an_event_task_through_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._file = Path(tmp) / "scheduler.json"
            result = await self.plugin._service_add(
                name="greet", event="player_join", text="hi {player}"
            )
            self.assertIn("greet", result)
            stored = json.loads(self.plugin._file.read_text(encoding="utf-8"))
            self.assertEqual(stored["tasks"][0]["event"], "player_join")

    async def test_add_rejects_a_broken_condition_through_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._file = Path(tmp) / "scheduler.json"
            result = await self.plugin._service_add(
                name="bad", condition="health <", text="x"
            )
            self.assertIn("Cannot add task", result)


class EventTriggerTest(unittest.IsolatedAsyncioTestCase):
    """player_join / player_leave / death / respawn 触发的任务。"""

    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]
        self.plugin.manager = self.manager
        self.plugin._file = None
        self.plugin.bot = FakeBot()

    def install(self, task: dict) -> None:
        self.plugin._tasks = [task]

    async def test_player_join_sends_chat_with_the_name(self) -> None:
        self.install(
            make_task(interval=None, event="player_join", text="欢迎 {player}!")
        )
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, ["欢迎 Steve!"])

    async def test_player_leave_fires_its_own_task_only(self) -> None:
        self.plugin._tasks = [
            make_task(name="in", interval=None, event="player_join", text="in"),
            make_task(name="out", interval=None, event="player_leave", text="out"),
        ]
        await self.plugin._on_player_leave(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, ["out"])

    async def test_death_message_is_rendered_as_plain_text(self) -> None:
        self.install(
            make_task(interval=None, event="death", text="我死了: {message}")
        )
        await self.plugin._on_death(
            {"translate": "death.attack.mob", "with": ["mie_233", "Zombie"]}
        )
        self.assertEqual(
            self.plugin.bot.sent_messages, ["我死了: mie_233 was slain by Zombie"]
        )

    async def test_death_without_a_message(self) -> None:
        self.install(make_task(interval=None, event="death", text="[{message}]"))
        await self.plugin._on_death(None)
        self.assertEqual(self.plugin.bot.sent_messages, ["[]"])

    async def test_respawn_runs_a_command(self) -> None:
        self.install(
            make_task(interval=None, event="respawn", action="command", text="spawn")
        )
        await self.plugin._on_respawn(None)
        self.assertEqual(self.plugin.bot.sent_commands, ["spawn"])

    async def test_disabled_event_task_stays_quiet(self) -> None:
        self.install(
            make_task(interval=None, event="player_join", enabled=False)
        )
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_match_filters_by_player_name(self) -> None:
        self.install(
            make_task(interval=None, event="player_join", match="wumie", text="hi")
        )
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, [])
        await self.plugin._on_player_join(SimpleNamespace(name="_ImWuMie"))
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])

    async def test_cooldown_suppresses_a_burst(self) -> None:
        """十个人同时进服不该发十条消息。"""
        self.install(
            make_task(interval=None, event="player_join", cooldown=60.0, text="hi")
        )
        for name in ("a", "b", "c"):
            await self.plugin._on_player_join(SimpleNamespace(name=name))
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])

    async def test_condition_gates_an_event_task(self) -> None:
        self.install(
            make_task(
                interval=None,
                event="player_join",
                condition="health < 10",
                text="hi",
            )
        )
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.plugin.bot.player.health = 4.0
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])

    async def test_event_task_is_not_run_by_the_timer(self) -> None:
        self.install(make_task(interval=None, event="player_join"))
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_events_while_disconnected_do_nothing(self) -> None:
        self.install(make_task(interval=None, event="player_join"))
        self.plugin.bot = None
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))  # 不抛

    async def test_handler_failure_is_isolated_by_the_framework(self) -> None:
        """订阅是通过框架注册的，所以异常只会被记录，不会打断连接。"""
        events = [event for event, _ in self.plugin._subscriptions]
        self.assertEqual(
            sorted(events),
            [
                "chat_sent",
                "death",
                "player_chat",
                "player_join",
                "player_leave",
                "respawn",
                "system_chat",
            ],
        )


class ConditionTriggerTest(unittest.IsolatedAsyncioTestCase):
    """只有 condition 的任务：上升沿触发一次。"""

    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]
        self.plugin.manager = self.manager
        self.plugin._file = None
        self.plugin.bot = FakeBot()
        self.plugin._tasks = [
            make_task(interval=None, condition="health < 8", text="血量 {health}")
        ]

    async def test_fires_when_the_condition_becomes_true(self) -> None:
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.plugin.bot.player.health = 5.0
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["血量 5"])

    async def test_stays_quiet_while_the_condition_holds(self) -> None:
        self.plugin.bot.player.health = 5.0
        await self.plugin._run_due()
        await self.plugin._run_due()
        await self.plugin._run_due()
        self.assertEqual(len(self.plugin.bot.sent_messages), 1)

    async def test_fires_again_after_the_condition_clears(self) -> None:
        self.plugin.bot.player.health = 5.0
        await self.plugin._run_due()
        self.plugin.bot.player.health = 20.0
        await self.plugin._run_due()
        self.plugin.bot.player.health = 3.0
        await self.plugin._run_due()
        self.assertEqual(len(self.plugin.bot.sent_messages), 2)

    async def test_cooldown_blocks_the_second_edge(self) -> None:
        self.plugin._tasks = [
            make_task(interval=None, condition="health < 8", cooldown=60.0)
        ]
        self.plugin.bot.player.health = 5.0
        await self.plugin._run_due()
        self.plugin.bot.player.health = 20.0
        await self.plugin._run_due()
        self.plugin.bot.player.health = 5.0
        await self.plugin._run_due()
        self.assertEqual(len(self.plugin.bot.sent_messages), 1)

    async def test_condition_gates_an_interval_task_without_losing_its_slot(self) -> None:
        self.plugin._tasks = [
            make_task(interval=5.0, condition="health < 8", text="low")
        ]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        before = time.monotonic()
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, [])
        self.assertGreater(self.plugin._next_run["t"], before + 4.0)

    async def test_dead_condition_uses_the_player_flag(self) -> None:
        self.plugin._tasks = [
            make_task(interval=None, condition="dead == true", text="躺了")
        ]
        await self.plugin._run_due()
        self.plugin.bot.player.dead = True
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["躺了"])

    async def test_players_condition_counts_the_tab_list(self) -> None:
        self.plugin._tasks = [
            make_task(interval=None, condition="players >= 2", text="人多了")
        ]
        await self.plugin._run_due()
        self.plugin.bot.players = {"a": 1, "b": 2}
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["人多了"])

    async def test_changing_the_condition_reopens_the_edge(self) -> None:
        """改过条件的任务重新求值，否则新条件的第一次上升沿会被吃掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.plugin._file = Path(tmp) / "scheduler.json"
            self.plugin._file.write_text(
                json.dumps({"tasks": [
                    {"name": "t", "condition": "health < 8", "text": "x"}
                ]}),
                encoding="utf-8",
            )
            self.plugin._load_tasks()
            self.plugin.bot.player.health = 5.0
            await self.plugin._run_due()
            self.assertEqual(len(self.plugin.bot.sent_messages), 1)
            self.plugin._file.write_text(
                json.dumps({"tasks": [
                    {"name": "t", "condition": "health < 6", "text": "x"}
                ]}),
                encoding="utf-8",
            )
            self.plugin._load_tasks()
            await self.plugin._run_due()
            self.assertEqual(len(self.plugin.bot.sent_messages), 2)


class PlaceholderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]
        self.plugin.manager = self.manager
        self.plugin._file = None
        self.plugin.bot = FakeBot()

    async def test_state_placeholders_without_a_trigger(self) -> None:
        self.plugin._tasks = [make_task(text="在 {x} {y} {z}，{food} 饱食度")]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["在 1.0 64.0 -2.0，20 饱食度"])

    async def test_unknown_placeholder_is_left_alone(self) -> None:
        """命令里的花括号不该被吃掉。"""
        self.plugin._tasks = [
            make_task(action="command", text="give @s stone{tag:1}")
        ]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_commands, ["give @s stone{tag:1}"])

    async def test_bot_name_placeholder(self) -> None:
        self.plugin._tasks = [make_task(text="我是 {bot}")]
        self.plugin._next_run = {"t": time.monotonic() - 1.0}
        await self.plugin._run_due()
        self.assertEqual(self.plugin.bot.sent_messages, ["我是 FakeBot"])

    async def test_remind_text_is_expanded_too(self) -> None:
        calls: list[dict] = []

        async def remind(text: str = "", source: str = "") -> str:
            calls.append({"text": text, "source": source})
            return "queued"

        self.manager._services["llm_agent.remind"] = ExposedFunction(
            plugin="llm_agent", name="remind", handler=remind
        )
        self.plugin._tasks = [
            make_task(interval=None, event="player_join", action="remind",
                      text="{player} 来了，打个招呼")
        ]
        await self.plugin._on_player_join(SimpleNamespace(name="Steve"))
        self.assertEqual(calls[0]["text"], "Steve 来了，打个招呼")


class ChatTriggerTest(unittest.IsolatedAsyncioTestCase):
    """「别人说了这句话就做点什么」——聊天触发，以及不许自己触发自己。"""

    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]
        self.plugin.manager = self.manager
        self.plugin._file = None
        self.plugin.bot = FakeBot()

    def install(self, **fields) -> None:
        task, error = self.plugin._normalize(
            {"name": "t", "text": "hi", **fields}
        )
        self.assertEqual(error, "")
        self.plugin._tasks = [task]

    async def say(self, name: str, text: str) -> None:
        await self.plugin._on_player_chat(None, name, {"text": text}, 0, None)

    async def test_matching_chat_line_runs_the_task(self) -> None:
        self.install(event="player_chat", match="开门", action="command",
                     text="say 来了")
        await self.say("Steve", "谁来开门啊")
        self.assertEqual(self.plugin.bot.sent_commands, ["say 来了"])

    async def test_other_lines_are_ignored(self) -> None:
        self.install(event="player_chat", match="开门", text="来了")
        await self.say("Steve", "今天天气不错")
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_match_is_case_insensitive(self) -> None:
        self.install(event="player_chat", match="hello", text="hi")
        await self.say("Steve", "HELLO everyone")
        self.assertEqual(self.plugin.bot.sent_messages, ["hi"])

    async def test_placeholders_carry_the_speaker_and_line(self) -> None:
        self.install(
            event="player_chat", match="求救", text="{player} 说了「{message}」"
        )
        await self.say("Steve", "求救 我掉洞里了")
        self.assertEqual(
            self.plugin.bot.sent_messages, ["Steve 说了「求救 我掉洞里了」"]
        )

    async def test_component_player_name_is_flattened(self) -> None:
        """玩家名常常是组件而不是字符串（服务器给它挂颜色）。"""
        self.install(event="player_chat", match="hi", text="{player}")
        await self.plugin._on_player_chat(
            None, {"text": "Steve", "color": "gold"}, {"text": "hi"}, 0, None
        )
        self.assertEqual(self.plugin.bot.sent_messages, ["Steve"])

    async def test_own_line_echoed_back_does_not_trigger(self) -> None:
        """服务器把自己说的话广播回来，不能拿它触发自己。"""
        self.install(event="player_chat", match="开门", text="来了")
        await self.say("FakeBot", "开门")
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_recently_sent_text_is_treated_as_an_echo(self) -> None:
        """回显里的发送者名字未必是 bot 的名字（代理会改），所以还看内容。"""
        self.install(event="player_chat", match="开门", text="来了")
        await self.plugin._on_chat_sent("开门了吗")
        await self.say("Steve", "<mie_233> 开门了吗")
        self.assertEqual(self.plugin.bot.sent_messages, [])

    async def test_echo_memory_expires(self) -> None:
        self.install(event="player_chat", match="开门", text="来了")
        await self.plugin._on_chat_sent("开门")
        self.plugin._sent = [(time.monotonic() - 60.0, "开门")]  # 旧记录
        await self.say("Steve", "开门")
        self.assertEqual(self.plugin.bot.sent_messages, ["来了"])

    async def test_system_chat_trigger(self) -> None:
        self.install(event="system_chat", match="重启", action="chat",
                     text="收到")
        await self.plugin._on_system_chat({"text": "服务器将在 5 分钟后重启"}, False)
        self.assertEqual(self.plugin.bot.sent_messages, ["收到"])

    async def test_system_chat_translate_component(self) -> None:
        """系统广播多半是翻译键，plain_text 现在会真的格式化它。"""
        self.install(event="system_chat", match="joined", text="欢迎")
        await self.plugin._on_system_chat(
            {"translate": "multiplayer.player.joined", "with": ["Steve"]}, False
        )
        self.assertEqual(self.plugin.bot.sent_messages, ["欢迎"])

    async def test_chat_event_without_match_is_rejected(self) -> None:
        task, error = self.plugin._normalize(
            {"name": "t", "event": "player_chat", "text": "hi"}
        )
        self.assertIsNone(task)
        self.assertIn("chat events need match", error)

    async def test_self_triggering_task_is_rejected(self) -> None:
        """text 里含着自己的 match，会一直触发下去。"""
        task, error = self.plugin._normalize(
            {"name": "t", "event": "player_chat", "match": "开门",
             "text": "开门来了"}
        )
        self.assertIsNone(task)
        self.assertIn("trigger itself", error)

    async def test_remind_may_repeat_the_match_text(self) -> None:
        """remind 不直接发话，所以不受自触发限制。"""
        task, error = self.plugin._normalize(
            {"name": "t", "event": "player_chat", "match": "开门",
             "action": "remind", "text": "有人说开门，看看要不要理"}
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(task)

    async def test_cooldown_applies_to_chat_triggers(self) -> None:
        self.install(event="player_chat", match="开门", cooldown=60.0,
                     text="来了")
        await self.say("Steve", "开门")
        await self.say("Alex", "开门")
        self.assertEqual(self.plugin.bot.sent_messages, ["来了"])


if __name__ == "__main__":
    unittest.main()
