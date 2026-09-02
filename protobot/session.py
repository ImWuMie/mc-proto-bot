"""Bot sessions and the container that stores them.

A :class:`BotSession` is one persistent connection policy: it resolves
credentials, spawns a fresh :class:`~protobot.Bot` per attempt (bots are
one-shot -- their ``closed``/``ready`` events are never reset), runs the 20 Hz
tick loop, and reconnects after a fixed delay when the server drops the
connection.  A :class:`BotContainer` stores named sessions and runs them
concurrently; today the CLI creates one, but the interface is ready for more.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .events import EventBus
from .log import error as log_error
from .log import info
from .physics import MovementInput

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .client import Bot
    from .plugin import PluginManager

__all__ = ["BotContainer", "BotSession", "SessionConfig"]


@dataclass
class SessionConfig:
    """Connection and reconnect policy for one bot session."""

    host: str
    port: int = 25565
    version: str = "26.2"
    online_mode: bool = True
    offline_username: str = "ProtoBot"
    reconnect: bool = True
    reconnect_delay: float = 5.0  # seconds between attempts
    reconnect_max_attempts: int | None = None  # None = reconnect forever
    connect_timeout: float = 30.0  # passed to connect(timeout=...)
    vclip: bool = True
    vclip_up_limit: float = 3.0
    vclip_down_limit: float = 2.0
    anti_kick: bool = True
    anti_kick_interval: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if self.reconnect_delay <= 0:
            raise ValueError(
                f"reconnect delay must be greater than 0 seconds, got {self.reconnect_delay}"
            )
        if self.connect_timeout <= 0:
            raise ValueError(
                f"connect timeout must be greater than 0 seconds, got {self.connect_timeout}"
            )
        if (
            self.reconnect_max_attempts is not None
            and self.reconnect_max_attempts < 0
        ):
            raise ValueError(
                "the reconnect attempt limit cannot be negative, got "
                f"{self.reconnect_max_attempts}"
            )
        if not isinstance(self.vclip, bool):
            raise ValueError("vclip must be a bool")
        if (
            not isinstance(self.vclip_up_limit, (int, float))
            or not isinstance(self.vclip_down_limit, (int, float))
            or isinstance(self.vclip_up_limit, bool)
            or isinstance(self.vclip_down_limit, bool)
            or not math.isfinite(self.vclip_up_limit)
            or not math.isfinite(self.vclip_down_limit)
            or self.vclip_up_limit < 0
            or self.vclip_down_limit < 0
        ):
            raise ValueError("VClip limits must be non-negative")
        if not isinstance(self.anti_kick, bool):
            raise ValueError("anti_kick must be a bool")
        if (
            not isinstance(self.anti_kick_interval, (int, float))
            or isinstance(self.anti_kick_interval, bool)
            or not math.isfinite(self.anti_kick_interval)
            or self.anti_kick_interval < 0.2
        ):
            raise ValueError("anti_kick_interval must be at least 0.2 seconds")


# Returns (username, access_token, profile_uuid) -- resolved fresh per attempt.
Credentials = Callable[[], Awaitable[tuple[str, str | None, uuid.UUID | None]]]


class BotConnector(Protocol):
    """Spawns a fully connected bot; injectable for tests."""

    async def __call__(
        self,
        config: SessionConfig,
        username: str,
        access_token: str | None,
        profile_uuid: uuid.UUID | None,
    ) -> Bot: ...


async def default_connector(
    config: SessionConfig,
    username: str,
    access_token: str | None,
    profile_uuid: uuid.UUID | None,
) -> Bot:
    """Wrap ``protobot.connect``; a fresh Bot per attempt."""
    from .client import connect

    return await connect(
        config.host,
        port=config.port,
        username=username,
        version=config.version,
        access_token=access_token,
        profile_uuid=profile_uuid,
        timeout=config.connect_timeout,
        vclip=config.vclip,
        vclip_up_limit=config.vclip_up_limit,
        vclip_down_limit=config.vclip_down_limit,
        anti_kick=config.anti_kick,
        anti_kick_interval=config.anti_kick_interval,
    )


class BotSession:
    """One persistent bot session: connect, tick, and reconnect.

    Lifecycle events (emitted on the session's own bus, not the bot's):

      session_start ()                                   -- run() entered
      session_connecting (attempt: int)                  -- attempt about to start
      session_ready (bot: Bot)                           -- connected, plugins bound
      session_disconnected (reason: str | None, attempt: int)
      session_stop ()                                    -- run() left

    ``request_stop()`` is synchronous and safe from any context; the tick loop
    polls it every tick and a backoff sleep is interrupted by it immediately.
    """

    def __init__(
        self,
        config: SessionConfig,
        *,
        credentials: Credentials,
        connector: BotConnector | None = None,
        plugin_manager: PluginManager | None = None,
        events: EventBus | None = None,
        tick_interval: float = 0.05,
        heartbeat_ticks: int = 200,
        wait_world_timeout: float = 20.0,
    ) -> None:
        self.config = config
        self.events = events if events is not None else EventBus()
        self.bot: Bot | None = None
        self._stop = asyncio.Event()
        self._credentials = credentials
        self._connector = connector if connector is not None else default_connector
        self._plugins = plugin_manager
        self._tick_interval = tick_interval
        self._heartbeat_ticks = heartbeat_ticks
        self._wait_world_timeout = wait_world_timeout

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Clear the stop flag so a session can be started again after a stop
        # (the TUI's ``.run`` command relies on this).
        self._stop.clear()
        if self._plugins is not None:
            self._plugins.bind_session_all(self)
        await self.events.emit("session_start")
        try:
            await self._run_attempts()
        except asyncio.CancelledError:
            pass  # graceful: the per-attempt finally already closed the bot
        finally:
            await self.events.emit("session_stop")
            if self._plugins is not None:
                self._plugins.unbind_session_all(self)

    async def _run_attempts(self) -> None:
        attempt = 1
        while not self._stop.is_set():
            await self.events.emit("session_connecting", attempt)
            info(
                f"[connect] connecting to {self.config.host}:{self.config.port} "
                f"(version {self.config.version}) ..."
            )
            try:
                # Re-resolved EVERY attempt: a cached token can expire while
                # disconnected between retries.
                username, access_token, profile_uuid = await self._credentials()
                mode = "online mode (authenticated)" if access_token else "offline mode"
                info(f"[mode] {mode}")
                bot = await self._connector(
                    self.config, username, access_token, profile_uuid
                )
            except SystemExit:
                raise  # credential guidance from the CLI -- propagate
            except asyncio.CancelledError:
                raise
            except Exception as error:  # Exception only, never BaseException
                log_error(f"connection attempt {attempt} failed: {error}")
                if not await self._maybe_reconnect(attempt):
                    break
                attempt += 1
                continue

            self.bot = bot
            if self._plugins is not None:
                await self._plugins.bind_all(bot)
            await self.events.emit("session_ready", bot)
            info(
                f"[ready] the bot is in the game\n"
                f"        name: {bot.username}\n"
                f"        spawn: X={bot.player.x:.2f}, "
                f"Y={bot.player.y:.2f}, Z={bot.player.z:.2f}"
            )

            info("[wait] loading world chunks ...")
            try:
                await bot.wait_world(timeout=self._wait_world_timeout)
                info(f"[world] {len(bot.world.chunks)} chunk(s) loaded")
            except Exception:
                info("[world] timed out waiting for chunks, staying online ...")

            try:
                await self._tick_loop(bot)
            finally:
                # Covers every exit path: tick errors, server disconnect,
                # stop request, and cancellation -- the bot closes exactly once.
                if self._plugins is not None:
                    self._plugins.unbind_all(bot)
                self.bot = None
                await bot.close()

            reason = bot.disconnect_reason
            await self.events.emit("session_disconnected", reason, attempt)
            if not await self._maybe_reconnect(attempt):
                break
            attempt += 1

    async def _maybe_reconnect(self, attempt: int) -> bool:
        """Decide whether to keep going after a failure or disconnect."""
        if self._stop.is_set() or not self.config.reconnect:
            return False
        max_attempts = self.config.reconnect_max_attempts
        if max_attempts is not None and attempt >= max_attempts:
            info(f"[note] reconnect attempt limit reached ({max_attempts}), giving up.")
            return False
        delay = self.config.reconnect_delay
        info(f"[reconnect] retrying in {delay:.0f}s ...")
        await self._sleep_or_stop(delay)
        return True

    async def _sleep_or_stop(self, delay: float) -> None:
        """Sleep through the backoff unless a stop arrives first."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            pass  # delay elapsed; retry

    async def _tick_loop(self, bot: Bot) -> None:
        info("\n[running] the bot is running (Ctrl+C to quit) ...")
        tick_count = 0
        while not bot.closed.is_set() and not self._stop.is_set():
            if not getattr(bot, "_navigation_claimed", False) and not getattr(
                bot, "_navigation_active", False
            ) and not getattr(bot, "_navigation_planning", False):
                await bot.tick(MovementInput())
            await asyncio.sleep(self._tick_interval)
            tick_count += 1
            # if tick_count % self._heartbeat_ticks == 0:
            #     pos = bot.player
            #     info(
            #         f"[heartbeat] position X={pos.x:.1f}, Y={pos.y:.1f}, "
            #         f"Z={pos.z:.1f} | online"
            #     )
        if self._stop.is_set():
            info("[exit] shutting down and closing the connection ...")


class BotContainer:
    """Stores named bot sessions and runs them concurrently.

    Designed for multiple sessions; the CLI adds one today.  On cancellation
    every session is asked to stop and the container waits for the graceful
    closes to finish before disabling plugins.
    """

    def __init__(self, plugin_manager: PluginManager | None = None) -> None:
        self.plugin_manager = plugin_manager
        self.sessions: dict[str, BotSession] = {}
        self.events = EventBus()  # container-level bus (future cross-session)

    def add_session(self, name: str, session: BotSession) -> BotSession:
        if name in self.sessions:
            raise ValueError(f"duplicate session name: {name}")
        self.sessions[name] = session
        return session

    def remove_session(self, name: str) -> BotSession | None:
        return self.sessions.pop(name, None)

    async def broadcast(self, event: str, *args) -> None:
        """Emit on the container bus and every session bus (future use)."""
        await self.events.emit(event, *args)
        for session in self.sessions.values():
            await session.events.emit(event, *args)

    async def run(self) -> None:
        if self.plugin_manager is not None:
            await self.plugin_manager.enable_all()
        tasks = [
            asyncio.create_task(session.run(), name=f"protobot-session:{name}")
            for name, session in self.sessions.items()
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Ask every session to stop, then re-await the same task objects:
            # each session.run() swallows CancelledError only after its
            # per-attempt finally closed the current bot, so this blocks until
            # every bot is actually closed.
            for session in self.sessions.values():
                session.request_stop()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if self.plugin_manager is not None:
                await self.plugin_manager.disable_all()
