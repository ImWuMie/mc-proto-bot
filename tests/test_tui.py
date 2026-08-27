"""Tests for the Textual TUI (optional ``tui`` extra).

Pure-function tests run everywhere; App tests are skipped when the extra is
not installed, mirroring the cryptography guard in tests/test_auth.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from protobot.events import EventBus
from protobot.tui import StdoutProxy, classify_submission, tui_enabled

try:
    from protobot.tui import DOT_COMMANDS, DotCommandSuggester, ProtoBotApp

    TEXTUAL = ProtoBotApp is not None
except Exception:  # pragma: no cover - base installs
    TEXTUAL = False


class ClassifyTest(unittest.TestCase):
    def test_chat_message(self) -> None:
        self.assertEqual(classify_submission("hello"), ("chat", "hello"))

    def test_command_strips_the_slash(self) -> None:
        self.assertEqual(classify_submission("/say hi"), ("command", "say hi"))

    def test_blank_input_is_ignored(self) -> None:
        self.assertEqual(classify_submission("   "), ("", ""))
        self.assertEqual(classify_submission("/"), ("", ""))

    def test_surrounding_whitespace_is_stripped(self) -> None:
        self.assertEqual(classify_submission("  hi  "), ("chat", "hi"))


class StdoutProxyTest(unittest.TestCase):
    def test_splits_lines_and_keeps_partial_writes(self) -> None:
        proxy = StdoutProxy()
        proxy.write("ab")
        proxy.write("cd\nline2\npartial")
        self.assertEqual(proxy.drain(10), ["abcd", "line2"])
        self.assertEqual(proxy.drain(10), [])
        proxy.write(" rest\n")
        self.assertEqual(proxy.drain(10), ["partial rest"])

    def test_drain_respects_the_limit(self) -> None:
        proxy = StdoutProxy()
        proxy.write("a\nb\nc\n")
        self.assertEqual(proxy.drain(2), ["a", "b"])
        self.assertEqual(proxy.drain(2), ["c"])
        self.assertEqual(proxy.drain(2), [])

    def test_carriage_returns_are_stripped(self) -> None:
        proxy = StdoutProxy()
        proxy.write("x\r\ny\n")
        self.assertEqual(proxy.drain(10), ["x", "y"])

    def test_flush_is_a_noop_and_never_a_tty(self) -> None:
        proxy = StdoutProxy()
        self.assertIsNone(proxy.flush())
        self.assertFalse(proxy.isatty())


class TuiEnabledTest(unittest.TestCase):
    class _Tty:
        def isatty(self) -> bool:
            return True

    class _NonTty:
        def isatty(self) -> bool:
            return False

    def test_config_switch_wins(self) -> None:
        self.assertFalse(
            tui_enabled(False, stdout=self._Tty(), stdin=self._Tty())
        )

    def test_non_tty_falls_back(self) -> None:
        self.assertFalse(
            tui_enabled(True, stdout=self._NonTty(), stdin=self._Tty())
        )
        self.assertFalse(
            tui_enabled(True, stdout=self._Tty(), stdin=self._NonTty())
        )

    def test_missing_textual_prints_a_hint_and_returns_false(self) -> None:
        captured = io.StringIO()
        with patch("protobot.tui._TEXTUAL", False), \
             contextlib.redirect_stdout(captured):
            result = tui_enabled(
                True, stdout=self._Tty(), stdin=self._Tty()
            )
        self.assertFalse(result)
        self.assertIn("protobot[tui]", captured.getvalue())


class FakeBot:
    def __init__(self) -> None:
        self.username = "FakeBot"
        self.player = SimpleNamespace(x=10.5, y=64.0, z=-20.25)
        self.sent_messages: list[str] = []
        self.sent_commands: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def send_command(self, command: str) -> None:
        self.sent_commands.append(command)


class FakeSession:
    """Session stand-in: run() blocks until request_stop(), like the real one."""

    def __init__(self, bot: FakeBot | None = None) -> None:
        self.events = EventBus()
        self.bot = bot
        self.config = SimpleNamespace(
            host="wolfx.jp", port=25565, version="26.2", online_mode=True
        )
        self.run_calls = 0
        self.stop_calls = 0
        self._stop_event: asyncio.Event | None = None

    async def run(self) -> None:
        self.run_calls += 1
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self.stop_calls += 1
        if self._stop_event is not None:
            self._stop_event.set()


async def wait_until(predicate, pauses: int = 100) -> None:
    for _ in range(pauses):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


@unittest.skipUnless(TEXTUAL, "textual extra not installed")
class DotSuggesterTest(unittest.IsolatedAsyncioTestCase):
    async def test_suggestions_filter_on_the_dot_prefix(self) -> None:
        suggester = DotCommandSuggester(DOT_COMMANDS)
        self.assertEqual(await suggester.get_suggestion("."), ".help")
        self.assertEqual(await suggester.get_suggestion(".r"), ".run")
        self.assertIsNone(await suggester.get_suggestion("hi"))
        self.assertIsNone(await suggester.get_suggestion(".run"))  # complete
        self.assertIsNone(await suggester.get_suggestion(""))


@unittest.skipUnless(TEXTUAL, "textual extra not installed")
class ProtoBotAppTest(unittest.IsolatedAsyncioTestCase):
    def _make_app(self, session: FakeSession) -> tuple[ProtoBotApp, asyncio.Task]:
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(session, manager=None, proxy=StdoutProxy())
        return app, task

    async def _cleanup(
        self, app: ProtoBotApp, session: FakeSession, task: asyncio.Task
    ) -> None:
        session.request_stop()
        if app.session_task is not None:
            try:
                await app.session_task
            except asyncio.CancelledError:
                pass
        task.cancel()

    async def test_submit_sends_a_chat_message(self) -> None:
        bot = FakeBot()
        app, task = self._make_app(FakeSession(bot=bot))
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "e", "l", "l", "o", "enter")
                await wait_until(lambda: bool(bot.sent_messages))
            self.assertEqual(bot.sent_messages, ["hello"])
        finally:
            task.cancel()

    async def test_submit_routes_commands(self) -> None:
        bot = FakeBot()
        app, task = self._make_app(FakeSession(bot=bot))
        try:
            async with app.run_test() as pilot:
                await pilot.press("/", "s", "a", "y", " ", "h", "i", "enter")
                await wait_until(lambda: bool(bot.sent_commands))
            self.assertEqual(bot.sent_commands, ["say hi"])
            self.assertEqual(bot.sent_messages, [])
        finally:
            task.cancel()

    async def test_submit_without_a_bot_logs_a_hint(self) -> None:
        app, task = self._make_app(FakeSession(bot=None))
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "i", "enter")
                await wait_until(
                    lambda: any("尚未连接" in line for line in app.log_lines)
                )
            self.assertTrue(
                any("尚未连接" in line for line in app.log_lines)
            )
        finally:
            task.cancel()

    async def test_status_refresh_shows_bot_position_and_server(self) -> None:
        session = FakeSession(bot=FakeBot())
        app, task = self._make_app(session)
        app.session_task = task  # pretend the session is running
        try:
            async with app.run_test():
                app._refresh_status()
            self.assertIn("FakeBot", app.status_texts["bot"])
            self.assertIn("X=10.5", app.status_texts["pos"])
            self.assertIn("Z=-20.2", app.status_texts["pos"])
            server = app.status_texts["server"]
            self.assertIn("wolfx.jp:25565", server)
            self.assertIn("26.2", server)
            self.assertIn("正版", server)
        finally:
            task.cancel()

    async def test_status_states_without_a_bot_and_with_uptime(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        try:
            async with app.run_test():
                app.connected_at = time.monotonic() - 65
                app._refresh_status()
            self.assertIn("未启动", app.status_texts["bot"])
            self.assertIn("ctrl+c 退出", app.status_texts["bot"])
            self.assertEqual(app.status_texts["pos"], "")
            server = app.status_texts["server"]
            self.assertIn("时长 00:01:05", server)
            self.assertIn("wolfx.jp:25565", server)
        finally:
            task.cancel()

    async def test_status_shows_connecting_while_started_without_a_bot(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        app.session_task = task  # running, not connected yet
        try:
            async with app.run_test():
                app._refresh_status()
            self.assertIn("连接中", app.status_texts["bot"])
        finally:
            task.cancel()

    async def test_session_events_track_uptime(self) -> None:
        bot = FakeBot()
        session = FakeSession(bot=bot)
        app, task = self._make_app(session)
        try:
            async with app.run_test():
                self.assertIsNone(app.connected_at)
                await session.events.emit("session_ready", bot)
                self.assertIsNotNone(app.connected_at)
                await session.events.emit("session_disconnected", "bye", 1)
                self.assertIsNone(app.connected_at)
        finally:
            task.cancel()

    async def test_drain_logs_writes_proxy_lines(self) -> None:
        proxy = StdoutProxy()
        proxy.write("[聊天] hi\n[心跳] X=1\n")
        session = FakeSession(bot=None)
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(session, manager=None, proxy=proxy)
        try:
            async with app.run_test():
                app._drain_logs()
            # The mount hint occupies the first line; the drained lines follow.
            self.assertEqual(app.log_lines[-2:], ["[聊天] hi", "[心跳] X=1"])
        finally:
            task.cancel()

    async def test_send_failure_is_logged_not_fatal(self) -> None:
        bot = FakeBot()

        async def failing_send(text: str) -> None:
            raise ValueError("too long")

        bot.send_message = failing_send  # type: ignore[method-assign]
        app, task = self._make_app(FakeSession(bot=bot))
        try:
            async with app.run_test() as pilot:
                await pilot.press("x", "enter")
                await wait_until(
                    lambda: any("发送失败" in line for line in app.log_lines)
                )
            self.assertTrue(
                any("发送失败" in line for line in app.log_lines)
            )
        finally:
            task.cancel()

    # ---- dot commands ----

    async def test_dot_run_starts_the_session(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "r", "u", "n", "enter")
                await wait_until(lambda: session.run_calls == 1)
                self.assertTrue(app.started)
            self.assertEqual(session.run_calls, 1)
        finally:
            await self._cleanup(app, session, task)

    async def test_dot_run_twice_is_rejected_while_running(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "r", "u", "n", "enter")
                await wait_until(lambda: session.run_calls == 1)
                await pilot.press(".", "r", "u", "n", "enter")
                await wait_until(
                    lambda: any("已在运行" in line for line in app.log_lines)
                )
            self.assertEqual(session.run_calls, 1)
        finally:
            await self._cleanup(app, session, task)

    async def test_dot_stop_stops_a_running_session(self) -> None:
        session = FakeSession(bot=FakeBot())
        app, task = self._make_app(session)
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "r", "u", "n", "enter")
                await wait_until(lambda: session.run_calls == 1)
                await pilot.press(".", "s", "t", "o", "p", "enter")
                await wait_until(lambda: session.stop_calls == 1)
            self.assertEqual(session.stop_calls, 1)
            self.assertFalse(app.started)
        finally:
            await self._cleanup(app, session, task)

    async def test_dot_stop_when_idle_logs_a_hint(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "s", "t", "o", "p", "enter")
                await wait_until(
                    lambda: any("未在运行" in line for line in app.log_lines)
                )
            self.assertEqual(session.stop_calls, 0)
        finally:
            task.cancel()

    async def test_dot_help_lists_commands(self) -> None:
        app, task = self._make_app(FakeSession(bot=None))
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "h", "e", "l", "p", "enter")
                await wait_until(
                    lambda: any(".run" in line for line in app.log_lines)
                )
            for name in DOT_COMMANDS:
                self.assertTrue(
                    any(name in line for line in app.log_lines), name
                )
        finally:
            task.cancel()

    async def test_unknown_dot_command_logs_a_hint(self) -> None:
        app, task = self._make_app(FakeSession(bot=None))
        try:
            async with app.run_test() as pilot:
                await pilot.press(".", "n", "o", "p", "e", "enter")
                await wait_until(
                    lambda: any("未知命令" in line for line in app.log_lines)
                )
            self.assertTrue(
                any("未知命令" in line for line in app.log_lines)
            )
        finally:
            task.cancel()

    async def test_ctrl_c_exits_the_app(self) -> None:
        # The app binds Ctrl+C to quit with priority (Textual 8 removed the
        # default Ctrl+C-quit and binds it to copy in inputs).
        app, task = self._make_app(FakeSession(bot=None))
        try:
            async with app.run_test() as pilot:
                await pilot.press("ctrl+c")
                await wait_until(lambda: app.is_running is False)
        finally:
            task.cancel()


if __name__ == "__main__":
    unittest.main()
