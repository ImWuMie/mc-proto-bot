"""Textual TUI for ``protobot run`` (optional ``tui`` extra).

Layout, Claude-Code style: an input row on top (chat message, or a server
command with a ``/`` prefix), the log area in the middle (everything the
session and plugins print, captured via a stdout proxy), and a three-column
footer (bot name / position / server info plus connection uptime).

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
    from textual.widgets import Input, Log, Static

    _TEXTUAL = True
except ImportError:  # pragma: no cover - exercised on base installs
    _TEXTUAL = False

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .session import BotSession

__all__ = ["ProtoBotApp", "StdoutProxy", "classify_submission", "tui_enabled"]


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


if _TEXTUAL:  # pragma: no cover - class body skipped without Textual

    class ProtoBotApp(App):
        """Full-screen TUI bound to one bot session."""

        CSS = """
        Vertical {
            height: 100%;
        }
        #cmd {
            dock: top;
        }
        #footer {
            height: 1;
        }
        #bot, #pos, #server {
            width: 1fr;
            height: 1;
        }
        """

        def __init__(
            self,
            session: BotSession,
            container_task: asyncio.Task,
            proxy: StdoutProxy,
            *,
            log_drain_limit: int = 200,
        ) -> None:
            super().__init__()
            self.session = session
            self.container_task = container_task
            self.proxy = proxy
            self.log_drain_limit = log_drain_limit
            self.connected_at: float | None = None
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
            yield Input(
                placeholder="输入消息回车发送；/命令 执行服务器命令", id="cmd"
            )
            yield Log(id="log")
            with Horizontal(id="footer"):
                yield Static("未连接", id="bot")
                yield Static("", id="pos")
                yield Static("", id="server")

        # ---- lifecycle ----

        def on_mount(self) -> None:
            self.set_interval(1.0, self._drain_logs)
            self.set_interval(1.0, self._refresh_status)
            self._watch_task = asyncio.create_task(self._watch_container())
            self._refresh_status()

        async def _watch_container(self) -> None:
            """Close the UI when the container finishes on its own.

            Exceptions from the container task are left for the caller, which
            re-awaits the same task after ``run_async`` returns.
            """
            try:
                await self.container_task
            except asyncio.CancelledError:
                raise
            except BaseException:
                pass
            self.exit()

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

        def _refresh_status(self) -> None:
            session = self.session
            config = session.config
            bot = session.bot

            bot_name = bot.username if bot is not None else "未连接"
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
            kind, payload = classify_submission(event.value)
            event.input.clear()
            if not kind:
                return
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

else:  # pragma: no cover - exercised on base installs

    ProtoBotApp = None  # type: ignore[assignment]
