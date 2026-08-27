"""Textual TUI for ``protobot run`` (optional ``tui`` extra).

Claude-Code-style layout: the log area fills the top, a three-column status
bar (bot name / position / server info plus uptime) sits above a bottom input
row.  The input sends chat messages, ``/``-prefixed server commands, and
dot-prefixed UI commands (``.run`` starts the bot, ``.stop`` stops it,
``.plugins``, ``.help``, ``.quit``) with a live suggestion dropdown while
typing.

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
    from textual.containers import Horizontal, Vertical
    from textual.suggester import Suggester
    from textual.widgets import Input, Log, Static

    _TEXTUAL = True
except ImportError:  # pragma: no cover - exercised on base installs
    _TEXTUAL = False

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .plugin import PluginManager
    from .session import BotSession

__all__ = ["ProtoBotApp", "StdoutProxy", "classify_submission", "tui_enabled"]

#: Dot commands offered by the TUI input, with their help text.
DOT_COMMANDS: dict[str, str] = {
    ".run": "启动 bot（连接服务器并开始运行）",
    ".stop": "停止 bot（保持界面）",
    ".plugins": "列出已加载插件",
    ".help": "显示可用命令",
    ".quit": "退出界面",
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

        CSS = """
        Screen {
            background: #121212;
        }
        #log {
            border: none;
            padding: 0 1;
        }
        #statusbar {
            height: 1;
            background: #1e1e2e;
            color: #a6adc8;
        }
        #bot, #pos, #server {
            width: 1fr;
            height: 1;
            padding: 0 1;
        }
        #cmd {
            border: none;
            border-top: solid #2b2b2b;
            padding: 0 1;
        }
        #cmd:focus {
            border: none;
            border-top: solid $accent;
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
            self.connected_at: float | None = None
            self.session_task: asyncio.Task | None = None
            # Headless audit trails: every log line and the last footer texts,
            # so tests (and debugging) never need Textual widget internals.
            self.log_lines: list[str] = []
            self.status_texts: dict[str, str] = {"bot": "", "pos": "", "server": ""}
            # Handlers carry a ``session`` infix: Textual dispatches its own
            # lifecycle messages (Ready, ...) to methods named ``_on_<name>``,
            # so names like ``_on_ready`` would be hijacked by the framework.
            session.events.on("session_connecting", self._on_session_connecting)
            session.events.on("session_ready", self._on_session_ready)
            session.events.on("session_disconnected", self._on_session_disconnected)
            session.events.on("session_stop", self._on_session_stop)

        # ---- composition ----

        def compose(self) -> ComposeResult:
            yield Log(id="log")
            with Horizontal(id="statusbar"):
                yield Static("未启动", id="bot")
                yield Static("", id="pos")
                yield Static("", id="server")
            yield Input(
                placeholder="输入消息回车发送；/命令 执行服务器命令；.help 查看命令",
                id="cmd",
                suggester=DotCommandSuggester(DOT_COMMANDS),
            )

        # ---- lifecycle ----

        def on_mount(self) -> None:
            # Textual 8's Log is focusable and composed first, so it would
            # steal the initial focus (and Tab) from the input box.
            self.query_one("#log", Log).can_focus = False
            self.query_one("#cmd", Input).focus()
            self.set_interval(1.0, self._drain_logs)
            self.set_interval(1.0, self._refresh_status)
            self._refresh_status()
            self._log_write("[提示] 输入 .run 启动 bot，.help 查看可用命令。")

        # ---- session events (footer state) ----

        def _on_session_connecting(self, attempt: int) -> None:
            self.connected_at = None

        def _on_session_ready(self, bot: Any) -> None:
            self.connected_at = time.monotonic()

        def _on_session_disconnected(self, reason: str | None, attempt: int) -> None:
            self.connected_at = None

        def _on_session_stop(self) -> None:
            self.connected_at = None

        # ---- periodic refresh ----

        def _log_write(self, line: str) -> None:
            """Write one line to the log widget and the headless audit trail."""
            self.query_one("#log", Log).write(line)
            self.log_lines.append(line)

        def _drain_logs(self) -> None:
            for line in self.proxy.drain(self.log_drain_limit):
                self._log_write(line)

        @property
        def started(self) -> bool:
            task = self.session_task
            return task is not None and not task.done()

        def _refresh_status(self) -> None:
            session = self.session
            config = session.config
            bot = session.bot

            if not self.started:
                bot_name = "未启动（输入 .run）"
            elif bot is not None:
                bot_name = bot.username
            else:
                bot_name = "连接中..."
            self.status_texts["bot"] = bot_name
            self.query_one("#bot", Static).update(bot_name)

            if bot is not None:
                player = bot.player
                position = (
                    f"X={player.x:.1f} Y={player.y:.1f} Z={player.z:.1f}"
                )
            else:
                position = ""
            self.status_texts["pos"] = position
            self.query_one("#pos", Static).update(position)

            mode = "正版" if config.online_mode else "离线"
            uptime = ""
            if self.connected_at is not None:
                seconds = int(time.monotonic() - self.connected_at)
                uptime = (
                    f" 时长 {seconds // 3600:02d}:"
                    f"{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
                )
            server_text = (
                f"{config.host}:{config.port} · {config.version} · {mode}{uptime}"
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
            elif name == ".quit":
                self.exit()

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
            self._log_write("[提示] 普通文本 = 聊天消息；/命令 = 服务器命令。")

else:  # pragma: no cover - exercised on base installs

    ProtoBotApp = None  # type: ignore[assignment]
