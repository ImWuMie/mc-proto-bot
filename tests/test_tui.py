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


class FakeManager:
    """PluginManager stand-in: just the service lookup the TUI needs."""

    def __init__(self, services: dict) -> None:
        self._services = services

    def get_service(self, qualified: str):
        return self._services.get(qualified)

    async def call_service(self, qualified: str, **kwargs):
        handler = self._services[qualified]
        result = handler(**kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    def load_order(self) -> list:
        return []


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
            self.assertIn("online", server)
        finally:
            task.cancel()

    async def test_status_without_a_bot_shows_idle_state(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        try:
            async with app.run_test():
                app._refresh_status()
            self.assertIn("idle", app.status_texts["bot"])
            self.assertIn("ctrl+c exit", app.status_texts["bot"])
            self.assertEqual(app.status_texts["pos"], "")
            # Not connected yet: server facts only, no duration.
            self.assertEqual(
                app.status_texts["server"], "wolfx.jp:25565 · 26.2 · online"
            )
        finally:
            task.cancel()

    async def test_status_shows_the_running_duration(self) -> None:
        session = FakeSession(bot=FakeBot())
        app, task = self._make_app(session)
        app.session_task = task  # pretend the session is running
        try:
            async with app.run_test():
                app.connected_at = time.monotonic() - 65
                app._refresh_status()
            server = app.status_texts["server"]
            self.assertIn("00:01:05", server)  # bare HH:MM:SS, no 时长 label
            self.assertNotIn("时长", server)
            self.assertIn("wolfx.jp:25565 · 26.2 · online · ", server)
        finally:
            task.cancel()

    async def test_session_events_track_the_duration(self) -> None:
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

    async def test_status_shows_connecting_while_started_without_a_bot(self) -> None:
        session = FakeSession(bot=None)
        app, task = self._make_app(session)
        app.session_task = task  # running, not connected yet
        try:
            async with app.run_test():
                app._refresh_status()
            self.assertIn("connecting", app.status_texts["bot"])
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

    async def test_log_sink_feeds_the_tui_log_area(self) -> None:
        # The end-to-end chain: protobot.log -> sink -> proxy -> drain -> log.
        # (Textual swallows plain print() output while running, so this is the
        # only path for session/plugin logging to reach the UI.)
        from protobot import log

        proxy = StdoutProxy()
        session = FakeSession(bot=None)
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(session, manager=None, proxy=proxy)
        try:
            async with app.run_test():
                log.set_sink(lambda line: proxy.write(line + "\n"))
                log.info("[心跳] tick 1")
                log.warn("careful")
                app._drain_logs()
            self.assertTrue(any("tick 1" in line for line in app.log_lines))
            self.assertTrue(any("careful" in line for line in app.log_lines))
        finally:
            log.set_sink(None)
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

    async def test_dot_llm_without_the_plugin_says_so(self) -> None:
        app, task = self._make_app(FakeSession(bot=None))  # manager=None
        try:
            async with app.run_test() as pilot:
                await pilot.press(*".llm 在吗", "enter")
                await wait_until(
                    lambda: any("未加载 llm_agent" in line for line in app.log_lines)
                )
        finally:
            task.cancel()

    async def test_dot_llm_calls_the_agent_and_prints_the_reply(self) -> None:
        calls: list[dict] = []

        async def console(text: str = "") -> str:
            calls.append({"text": text})
            return "在的，怎么了\n（第二行）"

        manager = FakeManager({"llm_agent.console": console})
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(FakeSession(bot=None), manager=manager, proxy=StdoutProxy())
        try:
            async with app.run_test() as pilot:
                await pilot.press(*".llm 在吗", "enter")
                await wait_until(
                    lambda: any("第二行" in line for line in app.log_lines)
                )
            self.assertEqual(calls, [{"text": "在吗"}])
            # 提问与回复都留在日志区，多行回复逐行打印
            self.assertTrue(any("> 在吗" in line for line in app.log_lines))
            self.assertTrue(any("在的，怎么了" in line for line in app.log_lines))
        finally:
            task.cancel()

    async def test_dot_llm_without_text_shows_usage(self) -> None:
        manager = FakeManager({"llm_agent.console": lambda text="": "unused"})
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(FakeSession(bot=None), manager=manager, proxy=StdoutProxy())
        try:
            async with app.run_test() as pilot:
                await pilot.press(*".llm", "enter")
                await wait_until(
                    lambda: any("用法" in line for line in app.log_lines)
                )
        finally:
            task.cancel()

    async def test_dot_llm_reports_a_failing_agent(self) -> None:
        async def console(text: str = "") -> str:
            raise RuntimeError("api key missing")

        manager = FakeManager({"llm_agent.console": console})
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(FakeSession(bot=None), manager=manager, proxy=StdoutProxy())
        try:
            async with app.run_test() as pilot:
                await pilot.press(*".llm 在吗", "enter")
                await wait_until(
                    lambda: any("调用失败" in line for line in app.log_lines)
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


@unittest.skipUnless(TEXTUAL, "textual extra not installed")
class AutostartTest(unittest.IsolatedAsyncioTestCase):
    """配置齐全时进界面即自动 .run，不必手动敲。"""

    async def test_autostart_runs_the_session(self) -> None:
        session = FakeSession(bot=None)
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(session, manager=None, proxy=StdoutProxy(), autostart=True)
        try:
            async with app.run_test():
                await wait_until(lambda: session.run_calls == 1)
                self.assertTrue(app.started)
            self.assertTrue(
                any("自动启动" in line for line in app.log_lines)
            )
        finally:
            session.request_stop()
            if app.session_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await app.session_task
            task.cancel()

    async def test_without_autostart_it_waits(self) -> None:
        session = FakeSession(bot=None)
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(session, manager=None, proxy=StdoutProxy())
        try:
            async with app.run_test():
                self.assertEqual(session.run_calls, 0)
                self.assertFalse(app.started)
            self.assertTrue(
                any(".run 启动" in line for line in app.log_lines)
            )
        finally:
            task.cancel()


@unittest.skipUnless(TEXTUAL, "textual extra not installed")
class InputHistoryTest(unittest.IsolatedAsyncioTestCase):
    """↑/↓ 翻输入历史，像 shell 一样。"""

    def _make_app(self) -> tuple[ProtoBotApp, asyncio.Task]:
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(FakeSession(bot=FakeBot()), manager=None, proxy=StdoutProxy())
        return app, task

    async def test_submissions_are_remembered(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "i", "enter")
                await pilot.press("/", "s", "a", "y", " ", "x", "enter")
                await pilot.press(".", "h", "e", "l", "p", "enter")
            self.assertEqual(app.input_history, ["hi", "/say x", ".help"])
        finally:
            task.cancel()

    async def test_up_walks_back_and_down_returns(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("o", "n", "e", "enter")
                await pilot.press("t", "w", "o", "enter")
                box = app.query_one("#cmd")

                await pilot.press("up")
                self.assertEqual(box.value, "two")
                await pilot.press("up")
                self.assertEqual(box.value, "one")
                await pilot.press("up")  # 已到最旧，保持不动
                self.assertEqual(box.value, "one")

                await pilot.press("down")
                self.assertEqual(box.value, "two")
                await pilot.press("down")
                self.assertEqual(box.value, "")  # 回到空白新行
        finally:
            task.cancel()

    async def test_draft_is_restored_by_down(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("o", "l", "d", "enter")
                await pilot.press("d", "r", "a", "f")  # 正在打字，未提交
                box = app.query_one("#cmd")
                await pilot.press("up")
                self.assertEqual(box.value, "old")
                await pilot.press("down")
                self.assertEqual(box.value, "draf")  # 草稿回来了
        finally:
            task.cancel()

    async def test_down_without_history_does_nothing(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                box = app.query_one("#cmd")
                await pilot.press("a")
                await pilot.press("down")
                self.assertEqual(box.value, "a")
                await pilot.press("up")  # 没有历史，也不该清空
                self.assertEqual(box.value, "a")
        finally:
            task.cancel()

    async def test_consecutive_duplicates_collapse(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "i", "enter")
                await pilot.press("h", "i", "enter")
                await pilot.press("y", "o", "enter")
                await pilot.press("h", "i", "enter")
            self.assertEqual(app.input_history, ["hi", "yo", "hi"])
        finally:
            task.cancel()

    async def test_history_is_capped(self) -> None:
        app, task = self._make_app()
        app.history_limit = 3
        try:
            async with app.run_test():
                for text in ("a", "b", "c", "d", "e"):
                    app._remember(text)
            self.assertEqual(app.input_history, ["c", "d", "e"])
        finally:
            task.cancel()

    async def test_cursor_sits_at_the_end_of_a_recalled_line(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "e", "l", "l", "o", "enter")
                await pilot.press("up")
                box = app.query_one("#cmd")
                self.assertEqual(box.cursor_position, len("hello"))
        finally:
            task.cancel()

    async def test_submitting_a_recalled_line_resets_the_cursor(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "i", "enter")
                await pilot.press("up", "enter")  # 重发历史里的那条
                await wait_until(lambda: len(app.session.bot.sent_messages) == 2)
                self.assertEqual(app.input_history, ["hi"])  # 连续重复不叠加
                await pilot.press("up")
                self.assertEqual(app.query_one("#cmd").value, "hi")
        finally:
            task.cancel()


@unittest.skipUnless(TEXTUAL, "textual extra not installed")
class LogScrollTest(unittest.IsolatedAsyncioTestCase):
    """键盘翻日志：SSH + screen 下滚轮事件根本到不了应用，只能靠按键。"""

    def _make_app(self) -> tuple[ProtoBotApp, asyncio.Task]:
        task = asyncio.ensure_future(asyncio.sleep(3600))
        app = ProtoBotApp(FakeSession(bot=FakeBot()), manager=None, proxy=StdoutProxy())
        return app, task

    def _fill(self, app: ProtoBotApp, count: int = 200) -> None:
        for index in range(count):
            app._log_write(f"[日志] 第 {index} 行")

    async def test_page_up_scrolls_and_pauses_following(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                widget = app._log_widget()
                bottom = widget.scroll_offset.y
                await pilot.press("pageup")
                self.assertLess(widget.scroll_offset.y, bottom)
                # 暂停跟随，否则下一行日志一到就把视图拽回底部
                self.assertFalse(widget.auto_scroll)
        finally:
            task.cancel()

    async def test_new_lines_do_not_yank_the_view_back(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                await pilot.press("pageup")
                widget = app._log_widget()
                where = widget.scroll_offset.y
                app._log_write("[日志] 新来的一行")
                await pilot.pause()
                self.assertEqual(widget.scroll_offset.y, where)
        finally:
            task.cancel()

    async def test_paging_back_to_the_bottom_resumes_following(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                await pilot.press("pageup")
                self.assertFalse(app._log_widget().auto_scroll)
                for _ in range(4):
                    await pilot.press("pagedown")
                self.assertTrue(app._log_widget().auto_scroll)
        finally:
            task.cancel()

    async def test_ctrl_l_returns_to_the_newest_line(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                await pilot.press("pageup")
                await pilot.press("ctrl+l")
                widget = app._log_widget()
                self.assertTrue(widget.auto_scroll)
                self.assertTrue(widget.is_vertical_scroll_end)
        finally:
            task.cancel()

    async def test_status_bar_says_when_following_is_paused(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                await pilot.press("pageup")
                self.assertIn("日志已暂停", app.status_texts["pos"])
                await pilot.press("ctrl+l")
                self.assertNotIn("日志已暂停", app.status_texts["pos"])
        finally:
            task.cancel()

    async def test_submitting_input_returns_to_the_newest_line(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                self._fill(app)
                await pilot.pause()
                await pilot.press("pageup")
                await pilot.press("h", "i", "enter")
                self.assertTrue(app._log_widget().auto_scroll)
        finally:
            task.cancel()

    async def test_page_keys_leave_the_input_text_alone(self) -> None:
        app, task = self._make_app()
        try:
            async with app.run_test() as pilot:
                await pilot.press("h", "i")
                await pilot.press("pageup", "pagedown", "ctrl+l")
                self.assertEqual(app.query_one("#cmd").value, "hi")
        finally:
            task.cancel()


if __name__ == "__main__":
    unittest.main()
