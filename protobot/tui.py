"""Textual TUI for ``protobot run`` (optional ``tui`` extra).

Claude-Code-style layout: the log area fills the top, a bottom input row, and
a Claude-Code-style status bar at the very bottom (state glyph on the left,
position in the middle, ``·``-separated hints and server info on the right).
The input sends chat messages, ``/``-prefixed server commands, and dot-prefixed
UI commands (``.run`` starts the bot, ``.stop`` stops it, ``.plugins``,
``.help``) with a live suggestion dropdown while typing; Ctrl+C exits.

The module imports Textual lazily so the plain-log fallback keeps working
without the extra installed: :func:`tui_enabled` decides, and
:class:`ProtoBotApp` is ``None`` when Textual is missing.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import time
from typing import TYPE_CHECKING, Any, TextIO

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.css.query import NoMatches
    from textual.suggester import Suggester
    from textual.widgets import Input, RichLog, Static

    _TEXTUAL = True
except ImportError:  # pragma: no cover - exercised on base installs
    _TEXTUAL = False

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .plugin import PluginManager
    from .session import BotSession

__all__ = ["ProtoBotApp", "StdoutProxy", "classify_submission", "tui_enabled"]

#: Dot commands offered by the TUI input, with their help text.
#: Exiting is Ctrl+C (Textual's built-in system binding), not a dot command.
DOT_COMMANDS: dict[str, str] = {
    ".run": "启动 bot（连接服务器并开始运行）",
    ".stop": "停止 bot（保持界面）",
    ".plugins": "列出已加载插件",
    ".help": "显示可用命令",
}


def classify_submission(text: str) -> tuple[str, str]:
    """Classify input-box content.

    Returns ``("command", cmd)`` for ``/``-prefixed input, ``("chat", message)``
    otherwise, and ``("", "")`` for blank input.
    """
    stripped = text.strip()
    if not stripped:
        return "", ""
    if stripped.startswith("/"):
        command = stripped[1:].strip()
        if not command:
            return "", ""
        return "command", command
    return "chat", stripped


def tui_enabled(
    config_enabled: bool,
    *,
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
) -> bool:
    """Whether the TUI can run: config switch, a real terminal, and Textual.

    Prints a one-time hint when the config wants the TUI but Textual is not
    installed, so base installs degrade gracefully to plain line logging.
    """
    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    if not config_enabled:
        return False
    isatty = getattr(stdout, "isatty", lambda: False)
    if not isatty() or not getattr(stdin, "isatty", lambda: False)():
        return False
    if not _TEXTUAL:
        print(
            "[提示] 未安装 protobot[tui]，使用普通日志模式"
            "（pip install -e \".[tui]\" 后在真终端运行可启用 TUI 界面）。"
        )
        return False
    return True


class StdoutProxy:
    """Capture print() output line by line into a thread-safe queue.

    ``contextlib.redirect_stdout`` points ``sys.stdout`` here while the bot
    runs, so every existing print (session lifecycle, plugins, heartbeat) lands
    in the TUI log without touching the printing code.  The Textual app drains
    the queue on a timer; the proxy itself knows nothing about Textual.
    """

    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self._partial: list[str] = []

    def write(self, text: str) -> int:
        self._partial.append(text)
        buffer = "".join(self._partial)
        if "\n" not in buffer:
            return len(text)
        pieces = buffer.split("\n")
        self._partial = [pieces[-1]]
        for line in pieces[:-1]:
            self.lines.put(line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def drain(self, limit: int) -> list[str]:
        """Take up to ``limit`` queued lines (flood protection)."""
        lines: list[str] = []
        for _ in range(limit):
            try:
                lines.append(self.lines.get_nowait())
            except queue.Empty:
                break
        return lines


if _TEXTUAL:  # pragma: no cover - class bodies skipped without Textual

    class DotCommandSuggester(Suggester):
        """Suggest the next dot command while the input starts with ``.``."""

        def __init__(self, commands: dict[str, str]) -> None:
            super().__init__(case_sensitive=True)
            self._commands = sorted(commands)

        async def get_suggestion(self, value: str) -> str | None:
            if not value.startswith("."):
                return None
            for name in self._commands:
                if name.startswith(value) and name != value:
                    return name
            return None

    class ProtoBotApp(App):
        """Full-screen TUI bound to one bot session.

        The session does NOT auto-start: the user types ``.run``.  The app owns
        the session task (``self.session_task``) so ``.stop`` and the Ctrl+C
        teardown in cli_app can stop and await it.
        """

        # Textual 8 no longer quits on Ctrl+C (it shows a hint and binds
        # Ctrl+C to copy in inputs); the user wants Ctrl+C to exit, so bind it
        # explicitly with priority over the Input's copy binding.
        BINDINGS = [
            Binding("ctrl+c", "quit", "退出", show=False, priority=True),
        ]

        CSS = """
        Screen {
            background: #121212;
        }
        #log {
            border: none;
            padding: 0 1;
            scrollbar-size: 0 0;   /* no scroll bars: long lines wrap */
        }
        #statusbar {
            height: 1;
            color: #6c7086;   /* muted, Claude-Code style */
        }
        #bot, #pos, #server {
            width: 1fr;
            height: 1;
            padding: 0;
        }
        #bot {
            content-align: left middle;
        }
        #pos {
            content-align: center middle;
        }
        #server {
            content-align: right middle;
        }
        #cmd {
            border: none;
            border-top: solid #2b2b2b;
            border-bottom: solid #2b2b2b;  /* the rule above the status bar */
            padding: 0 1;
        }
        #cmd:focus {
            border: none;
            border-top: solid $accent;
            border-bottom: solid $accent;
        }
        #cmd > .input--placeholder {
            color: #6c7086;
        }
        """

        def __init__(
            self,
            session: BotSession,
            manager: PluginManager | None,
            proxy: StdoutProxy,
            *,
            log_drain_limit: int = 200,
        ) -> None:
            super().__init__()
            self.session = session
            self.manager = manager
            self.proxy = proxy
            self.log_drain_limit = log_drain_limit
            self.session_task: asyncio.Task | None = None
            # Headless audit trails: every log line and the last footer texts,
            # so tests (and debugging) never need Textual widget internals.
            self.log_lines: list[str] = []
            self.status_texts: dict[str, str] = {"bot": "", "pos": "", "server": ""}

        # ---- composition ----

        def compose(self) -> ComposeResult:
            # Claude-Code layout: log fills the top, the input row sits above
            # the very bottom status bar.
            yield RichLog(
                id="log", markup=False, wrap=True, max_lines=2000
            )
            yield Input(
                id="cmd",
                suggester=DotCommandSuggester(DOT_COMMANDS),
            )
            with Horizontal(id="statusbar"):
                yield Static("未启动", id="bot")
                yield Static("", id="pos")
                yield Static("", id="server")

        # ---- lifecycle ----

        def on_mount(self) -> None:
            # Textual 8's RichLog is focusable and composed first, so it would
            # steal the initial focus (and Tab) from the input box.
            self.query_one("#log", RichLog).can_focus = False
            self.query_one("#cmd", Input).focus()
            self.set_interval(1.0, self._drain_logs)
            self.set_interval(1.0, self._refresh_status)
            self._refresh_status()
            self._log_write("[提示] 输入 .run 启动 bot，.help 查看可用命令，Ctrl+C 退出。")

        # ---- periodic refresh ----

        def _log_write(self, line: str) -> None:
            """Write one line to the log widget and the headless audit trail.

            Timer callbacks can fire once more while the screen is being torn
            down; the audit trail always records, the widget write is guarded.
            """
            self.log_lines.append(line)
            if not self.is_running:
                return
            try:
                self.query_one("#log", RichLog).write(line)
            except NoMatches:  # pragma: no cover - teardown race
                pass

        def _drain_logs(self) -> None:
            if not self.is_running:
                return
            for line in self.proxy.drain(self.log_drain_limit):
                self._log_write(line)

        @property
        def started(self) -> bool:
            task = self.session_task
            return task is not None and not task.done()

        def _refresh_status(self) -> None:
            if not self.is_running:  # timer can outlive the screen by a tick
                return
            session = self.session
            config = session.config
            bot = session.bot

            # Claude-Code-style single line: a state glyph and the hints on
            # the left, the position centered, server info on the right.
            if not self.started:
                state = "⏸ 未启动"
            elif bot is not None:
                state = f"⏵ {bot.username}"
            else:
                state = "… 连接中"
            bot_text = f"{state} · ? .help · ctrl+c 退出"
            self.status_texts["bot"] = bot_text
            self.query_one("#bot", Static).update(bot_text)

            if bot is not None:
                player = bot.player
                position = (
                    f"X={player.x:.1f} Y={player.y:.1f} Z={player.z:.1f}"
                )
            else:
                position = ""
            self.status_texts["pos"] = position
            self.query_one("#pos", Static).update(position)

            mode = "online" if config.online_mode else "offline"
            server_text = (
                f"{config.host}:{config.port} · {config.version} · {mode} · "
                f"{time.strftime('%H:%M:%S')}"
            )
            self.status_texts["server"] = server_text
            self.query_one("#server", Static).update(server_text)

        # ---- input handling ----

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            event.input.clear()
            if not text:
                return
            if text.startswith("."):
                self._run_dot_command(text)
                return
            kind, payload = classify_submission(text)
            asyncio.create_task(self._submit(kind, payload))

        async def _submit(self, kind: str, payload: str) -> None:
            bot = self.session.bot  # re-read: reconnects replace the bot
            if bot is None:
                self._log_write("[提示] 尚未连接，消息未发送。")
                return
            try:
                if kind == "command":
                    await bot.send_command(payload)
                else:
                    await bot.send_message(payload)
            except Exception as error:
                self._log_write(f"[错误] 发送失败: {error}")

        # ---- dot commands ----

        def _run_dot_command(self, text: str) -> None:
            name = text.split(" ", 1)[0]
            if name not in DOT_COMMANDS:
                self._log_write(
                    f"[命令] 未知命令 {name}，输入 .help 查看可用命令。"
                )
                return
            if name == ".run":
                self._command_run()
            elif name == ".stop":
                self._command_stop()
            elif name == ".plugins":
                self._command_plugins()
            elif name == ".help":
                self._command_help()

        def _command_run(self) -> None:
            if self.started:
                self._log_write("[命令] bot 已在运行。")
                return
            self._log_write("[命令] 正在启动 bot ...")
            self.session_task = asyncio.create_task(
                self._run_session(), name="protobot-session-tui"
            )
            self._refresh_status()

        async def _run_session(self) -> None:
            try:
                await self.session.run()
                if self.is_running:  # the app may have closed first
                    self._log_write("[提示] bot 会话已停止。")
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                # SystemExit guidance (e.g. missing credentials) lands here;
                # the task keeps its exception for cli_app to re-await.
                if self.is_running:
                    self._log_write(f"[错误] bot 会话异常退出: {error}")

        def _command_stop(self) -> None:
            if not self.started:
                self._log_write("[命令] bot 未在运行。")
                return
            self._log_write("[命令] 正在停止 bot ...")
            self.session.request_stop()

        def _command_plugins(self) -> None:
            if self.manager is None:
                self._log_write("[插件] 插件管理器不可用。")
                return
            plugins = self.manager.load_order()
            if not plugins:
                self._log_write("[插件] 未发现插件。")
                return
            names = ", ".join(plugin.name for plugin in plugins)
            self._log_write(f"[插件] 已加载 {len(plugins)} 个插件: {names}")

        def _command_help(self) -> None:
            self._log_write("[命令] 可用命令:")
            for name, description in DOT_COMMANDS.items():
                self._log_write(f"  {name:<10s} {description}")
            self._log_write("[提示] 普通文本 = 聊天消息；/命令 = 服务器命令；Ctrl+C 退出。")

else:  # pragma: no cover - exercised on base installs

    ProtoBotApp = None  # type: ignore[assignment]
