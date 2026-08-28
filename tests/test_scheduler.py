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
