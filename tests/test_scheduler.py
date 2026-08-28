"""Tests for the plugins/scheduler.py scheduled-task plugin.

The real plugin file is loaded through PluginManager; the task JSON is
pointed at a temp path and the bot is a stub (nothing touches the network
or the real plugins/scheduler.json).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from protobot.plugin import PluginManager

SCHEDULER_FILE = Path(__file__).resolve().parent.parent / "plugins" / "scheduler.py"


class FakeBot:
    def __init__(self) -> None:
        self.username = "FakeBot"
        self.sent_messages: list[str] = []
        self.sent_commands: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def send_command(self, command: str) -> None:
        self.sent_commands.append(command)


def load_plugin(tmp: str) -> tuple[PluginManager, object]:
    manager = PluginManager([SCHEDULER_FILE.parent])
    manager.discover()
    plugin = manager.plugins["scheduler"]
    plugin._file = Path(tmp) / "scheduler.json"
    return manager, plugin


def make_task(**overrides) -> dict:
    task = {
        "name": "t",
        "interval": 5.0,
        "time": None,
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
            await self.plugin._service_status(), "2 scheduled task(s), 1 enabled"
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


class SecondsUntilTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = PluginManager([SCHEDULER_FILE.parent])
        self.manager.discover()
        self.plugin = self.manager.plugins["scheduler"]

    def test_returns_duration_within_a_day(self) -> None:
        delta = self.plugin._seconds_until("00:00")
        self.assertGreater(delta, 0.0)
        self.assertLessEqual(delta, 86400.0)


if __name__ == "__main__":
    unittest.main()
