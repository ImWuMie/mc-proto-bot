"""Textual TUI for ``protobot run`` (optional ``tui`` extra).

Claude-Code-style layout: the log area fills the top, a bottom input row, and
a Claude-Code-style status bar at the very bottom (state glyph on the left,
position in the middle, ``·``-separated hints and server info on the right).
The input sends chat messages, ``/``-prefixed server commands, and dot-prefixed
UI commands (``.run`` starts the bot, ``.stop`` stops it, ``.plugins``,
``.help``) with a live suggestion dropdown while typing; ↑/↓ walk the input
history, PageUp/PageDown scroll the log (Ctrl+L returns to the newest line --
keyboard only, because a terminal multiplexer often does not forward the mouse
wheel), and Ctrl+C exits.  When the configuration is complete enough to
connect, the session starts on its own (``autostart``) instead of waiting for
``.run``.

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
    ".run": "start the bot (connect and run)",
    ".stop": "stop the bot (keep this interface)",
    ".plugins": "list the loaded plugins",
    ".llm": "hand the rest of the line to the LLM agent; its reply prints here",
    ".help": "show the available commands",
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
            "[note] protobot[tui] is not installed, falling back to plain logs "
            "(install it with pip install -e \".[tui]\" and run in a real "
            "terminal for the full-screen interface)."
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
        # explicitly with priority over the Input's copy binding.  Up/Down are
        # bound with priority too so they reach the history instead of the
        # Input's own cursor handling.
        BINDINGS = [
            Binding("ctrl+c", "quit", "quit", show=False, priority=True),
            Binding("up", "history_prev", "previous", show=False, priority=True),
            Binding("down", "history_next", "next", show=False, priority=True),
            # Paging the log from the keyboard. The wheel needs the terminal to
            # forward mouse events, which GNU screen does not do by default
            # (tmux needs `set -g mouse on` too), so under SSH + screen the
            # wheel does nothing; and the TUI lives in the alternate screen
            # buffer, so the terminal's own scrollback cannot reach it either.
            # PageUp/PageDown are plain key sequences that need no setup.
            Binding("pageup", "log_page_up", "page up", show=False, priority=True),
            Binding(
                "pagedown", "log_page_down", "page down", show=False, priority=True
            ),
            Binding(
                "ctrl+l", "log_follow", "jump to latest", show=False, priority=True
            ),
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
            autostart: bool = False,
            history_limit: int = 200,
        ) -> None:
            super().__init__()
            self.session = session
            self.manager = manager
            self.proxy = proxy
            self.log_drain_limit = log_drain_limit
            self.autostart = autostart
            self.session_task: asyncio.Task | None = None
            self.connected_at: float | None = None
            # Input history, newest last; ``_history_index`` walks it and equals
            # len(history) while the user is typing a fresh line, whose draft is
            # kept so Down can restore it.
            self.input_history: list[str] = []
            self.history_limit = history_limit
            self._history_index = 0
            self._history_draft = ""
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
                yield Static("⏸ idle", id="bot")
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
            if self.autostart:
                self._log_write(
                    "[note] credentials are ready, starting the bot "
                    "(.stop to stop it, up/down for input history, "
                    "PageUp/PageDown to page the log, .help for commands, "
                    "Ctrl+C to quit)."
                )
                self._command_run()
            else:
                self._log_write(
                    "[note] type .run to start the bot, up/down for input "
                    "history, PageUp/PageDown to page the log, .help for "
                    "commands, Ctrl+C to quit."
                )

        # ---- session events (connection duration) ----

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
            return self.session.running or (task is not None and not task.done())

        def _refresh_status(self) -> None:
            if not self.is_running:  # timer can outlive the screen by a tick
                return
            session = self.session
            config = session.config
            bot = session.bot

            # Claude-Code-style single line: a state glyph and the hints on
            # the left, the position centered, server info on the right.
            if not self.started:
                state = "⏸ idle"
            elif bot is not None:
                state = f"⏵ {bot.username}"
            else:
                state = "… connecting"
            bot_text = f"{state} · ? .help · ctrl+c exit"
            self.status_texts["bot"] = bot_text
            self.query_one("#bot", Static).update(bot_text)

            if bot is not None:
                player = bot.player
                position = (
                    f"X={player.x:.1f} Y={player.y:.1f} Z={player.z:.1f}"
                )
            else:
                position = ""
            if self._log_paused():
                # While following is paused new lines no longer scroll into
                # view, so say so -- otherwise it looks like a freeze
                position = "|| log paused - ctrl+l for the latest"
            self.status_texts["pos"] = position
            self.query_one("#pos", Static).update(position)

            mode = "online" if config.online_mode else "offline"
            uptime = ""
            if self.connected_at is not None:
                seconds = int(time.monotonic() - self.connected_at)
                uptime = (
                    f" · {seconds // 3600:02d}:"
                    f"{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
                )
            server_text = (
                f"{config.host}:{config.port} · {config.version} · {mode}{uptime}"
            )
            self.status_texts["server"] = server_text
            self.query_one("#server", Static).update(server_text)

        # ---- Log scrolling: keyboard only, no mouse forwarding needed ----

        def _log_widget(self) -> "RichLog | None":
            try:
                return self.query_one("#log", RichLog)
            except NoMatches:  # pragma: no cover - teardown race
                return None

        def _log_paused(self) -> bool:
            """Whether following the tail of the log is paused."""
            widget = self._log_widget() if self.is_running else None
            return widget is not None and not widget.auto_scroll

        def action_log_page_up(self) -> None:
            widget = self._log_widget()
            if widget is None:
                return
            # Pause following, or the next log line yanks the view back to the
            # bottom and nothing older can be read
            widget.auto_scroll = False
            widget.scroll_page_up(animate=False)
            self._refresh_status()

        def action_log_page_down(self) -> None:
            widget = self._log_widget()
            if widget is None:
                return
            widget.scroll_page_down(animate=False)
            if widget.is_vertical_scroll_end:
                widget.auto_scroll = True  # Back at the bottom: follow again
            self._refresh_status()

        def action_log_follow(self) -> None:
            widget = self._log_widget()
            if widget is None:
                return
            widget.auto_scroll = True
            widget.scroll_end(animate=False)
            self._refresh_status()

        # ---- input handling ----

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            event.input.clear()
            if not text:
                return
            self._remember(text)
            # Anything sent should be visible: resume following the tail
            self.action_log_follow()
            if text.startswith("."):
                self._run_dot_command(text)
                return
            kind, payload = classify_submission(text)
            asyncio.create_task(self._submit(kind, payload))

        # ---- input history (↑/↓, shell style) ----

        def _remember(self, text: str) -> None:
            """Append to the history, skipping repeats of the previous entry."""
            if not self.input_history or self.input_history[-1] != text:
                self.input_history.append(text)
                del self.input_history[: -self.history_limit]
            self._history_index = len(self.input_history)
            self._history_draft = ""

        def _set_input(self, text: str) -> None:
            try:
                box = self.query_one("#cmd", Input)
            except NoMatches:  # pragma: no cover - teardown race
                return
            box.value = text
            box.cursor_position = len(text)

        def action_history_prev(self) -> None:
            """Older entry; the in-progress line is kept so Down can restore it."""
            if not self.input_history or self._history_index == 0:
                return
            if self._history_index == len(self.input_history):
                try:
                    self._history_draft = self.query_one("#cmd", Input).value
                except NoMatches:  # pragma: no cover - teardown race
                    self._history_draft = ""
            self._history_index -= 1
            self._set_input(self.input_history[self._history_index])

        def action_history_next(self) -> None:
            """Newer entry, ending back on the draft the user was typing."""
            if self._history_index >= len(self.input_history):
                return
            self._history_index += 1
            if self._history_index == len(self.input_history):
                self._set_input(self._history_draft)
            else:
                self._set_input(self.input_history[self._history_index])

        async def _submit(self, kind: str, payload: str) -> None:
            bot = self.session.bot  # re-read: reconnects replace the bot
            if bot is None:
                self._log_write("[note] not connected, message not sent.")
                return
            try:
                if kind == "command":
                    await bot.send_command(payload)
                else:
                    await bot.send_message(payload)
            except Exception as error:
                self._log_write(f"[error] send failed: {error}")

        # ---- dot commands ----

        def _run_dot_command(self, text: str) -> None:
            name, _, argument = text.partition(" ")
            if name not in DOT_COMMANDS:
                self._log_write(
                    f"[command] unknown command {name}; type .help for the list."
                )
                return
            if name == ".run":
                self._command_run()
            elif name == ".stop":
                self._command_stop()
            elif name == ".plugins":
                self._command_plugins()
            elif name == ".llm":
                self._command_llm(argument)
            elif name == ".help":
                self._command_help()

        def _command_llm(self, argument: str) -> None:
            """Hand the text to the llm_agent plugin and print its reply here.

            Say so plainly when the plugin is missing or disabled instead of
            doing nothing: ``llm_agent`` is optional, so it may well not be
            installed or may have been switched off.
            """
            prompt = argument.strip()
            if not prompt:
                self._log_write("[LLM] usage: .llm <what to say>")
                return
            manager = self.manager
            if manager is None or manager.get_service("llm_agent.console") is None:
                self._log_write(
                    "[LLM] the llm_agent plugin is not loaded (or is disabled)."
                )
                return
            self._log_write(f"[LLM] > {prompt}")
            asyncio.create_task(self._ask_llm(prompt), name="protobot-tui-llm")

        async def _ask_llm(self, prompt: str) -> None:
            manager = self.manager
            try:
                reply = await manager.call_service("llm_agent.console", text=prompt)
            except Exception as error:
                self._log_write(f"[LLM] call failed: {error}")
                return
            lines = str(reply).strip().splitlines()
            if not lines:
                self._log_write("[LLM] (no reply)")
                return
            for line in lines:
                self._log_write(f"[LLM] {line}")

        def _command_run(self) -> None:
            if self.started:
                self._log_write("[command] the bot is already running.")
                return
            self._log_write("[command] starting the bot ...")
            self.session_task = asyncio.create_task(
                self._run_session(), name="protobot-session-tui"
            )
            self._refresh_status()

        async def _run_session(self) -> None:
            try:
                await self.session.run()
                if self.is_running:  # the app may have closed first
                    self._log_write("[note] the bot session stopped.")
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                # SystemExit guidance (e.g. missing credentials) has to reach
                # cli_app: it re-awaits this task and the process should exit
                # non-zero, so log it here and re-raise rather than swallowing
                # it and reporting success.
                if self.is_running:
                    self._log_write(f"[error] the bot session died: {error}")
                raise

        def _command_stop(self) -> None:
            if not self.started:
                self._log_write("[command] the bot is not running.")
                return
            self._log_write("[command] stopping the bot ...")
            self.session.request_stop()

        def _command_plugins(self) -> None:
            if self.manager is None:
                self._log_write("[plugin] the plugin manager is unavailable.")
                return
            plugins = self.manager.load_order()
            if not plugins:
                self._log_write("[plugin] no plugins found.")
                return
            names = ", ".join(plugin.name for plugin in plugins)
            self._log_write(f"[plugin] {len(plugins)} plugin(s) loaded: {names}")

        def _command_help(self) -> None:
            self._log_write("[command] available commands:")
            for name, description in DOT_COMMANDS.items():
                self._log_write(f"  {name:<10s} {description}")
            self._log_write(
                "[note] plain text = chat message; /command = server command; "
                "up/down for input history; PageUp/PageDown to page the log, "
                "ctrl+l for the latest; Ctrl+C to quit."
            )

else:  # pragma: no cover - exercised on base installs

    ProtoBotApp = None  # type: ignore[assignment]
