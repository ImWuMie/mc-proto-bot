"""Auto-respawn plugin: get back on our feet without anyone watching.

The death signal is the core's ``death`` event, which merges two sources
(``protobot.client`` already dedupes them to one event per death):

  1. **Combat Death** (0x44 = 68 on protocol 775/776) -- this packet exists to
     make the client show the death screen, so the server always sends it when
     the player dies. It is the signal this plugin relies on, and it carries
     the death-message component, which is recorded along the way.
  2. **Health reaching zero** (``set_health`` 0x68 = 104 with health <= 0) --
     the weaker signal. The server resends it on tick boundaries, ordering is
     not guaranteed by the protocol, and server plugins can scale health, so
     it is only a fallback rather than something to act on alone.

Respawning is **Client Status** (serverbound 0x0C = 12, payload is a single
VarInt, 0 = perform respawn). The server never respawns a player by itself:
even with ``doImmediateRespawn`` all that changes is that the vanilla client
skips the death screen and sends this packet immediately. Without it the bot
lies on the death screen forever -- no movement, no physics.

After the request the server sends the respawn packet plus a position sync
carrying a teleport id. Confirming that teleport and re-sending Player Loaded
is the core's job (``_handle_respawn`` clears ``player.loaded``, so the next
position packet confirms and re-sends), and this plugin only waits for the
``respawn`` event as its acknowledgement.

On releases where the packet ids are unverified (1.21.11 / protocol 774)
``bot.respawn()`` raises ``UnsupportedVersion`` -- the plugin says so once and
stops, rather than sending packets built on a guess.

Settings live in ``respawn.json`` next to this file (written on first enable);
edits apply within about 5 seconds. By default it only respawns; with
``return_to_death_point`` on it also walks back to where it died.
"""

from __future__ import annotations

import asyncio
import math
import time

from protobot import Plugin, PluginSettings, log, plain_text
from protobot.errors import UnsupportedVersion

DEFAULT_SETTINGS: dict = {
    "enabled": True,  # false keeps only respawn.status / respawn.now
    "delay": 1.0,  # Seconds between the death and the respawn request
    "retry_delay": 2.0,  # Retry after this long without an acknowledgement
    "max_retries": 2,  # How many retries (0 = ask once)
    "announce": "",  # Non-empty: say this in chat after respawning
    "return_to_death_point": False,  # Walk back to where we died
    "return_max_distance": 200.0,  # Do not walk back further than this (blocks)
}

#: Settings poll interval (seconds). Respawning itself is event-driven.
RELOAD_INTERVAL = 5.0

#: How long to wait for the world before walking back (seconds); give up
#: afterwards rather than hanging here.
WORLD_TIMEOUT = 30.0


class AutoRespawn(Plugin):
    name = "respawn"

    def __init__(self) -> None:
        super().__init__()
        self._config: PluginSettings | None = None
        self._settings: dict = AutoRespawn._normalize(dict(DEFAULT_SETTINGS))
        self._reload_task: asyncio.Task | None = None
        self._respawn_task: asyncio.Task | None = None
        self._respawned = asyncio.Event()
        self._deaths = 0
        self._last_death: tuple[float, float, float] | None = None
        self._last_death_at: float | None = None
        self._last_message = ""
        self._warned_unsupported = False
        self.subscribe("death", self._on_death)
        self.subscribe("respawn", self._on_respawn)
        # Exposed to other plugins and the LLM: status / now / set
        self.expose(
            "status",
            self._service_status,
            description=(
                "Auto-respawn status: whether it is on, whether the bot is "
                "currently alive, its health and food, how many times it has "
                "died on this connection and where it died last."
            ),
            llm=True,
        )
        self.expose(
            "now",
            self._service_now,
            description=(
                "Respawn right now instead of waiting out the configured "
                "delay. Only useful while the bot is dead."
            ),
            llm=True,
            admin=True,
        )
        self.expose(
            "set",
            self._service_set,
            description=(
                "Turn auto-respawn on or off, or change how it behaves: "
                "delay before respawning, and whether to walk back to the "
                "place of death afterwards."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Respawn automatically on death",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds to wait before respawning",
                    },
                    "return_to_death_point": {
                        "type": "boolean",
                        "description": "Walk back to where the bot died",
                    },
                    "announce": {
                        "type": "string",
                        "description": (
                            "Chat message to send after respawning; empty to "
                            "say nothing"
                        ),
                    },
                },
            },
            llm=True,
            admin=True,
        )

    # ---- Lifecycle ----

    async def on_enable(self) -> None:
        if self._config is None:
            self._config = self.settings_file(
                "respawn.json", DEFAULT_SETTINGS,
                label="respawn", normalize=self._normalize,
            )
        self._config.load()
        self._settings = self._config.data
        self._reload_task = asyncio.create_task(
            self._reload_loop(), name="protobot-respawn-settings"
        )
        state = "enabled" if self._settings["enabled"] else "loaded but off"
        log.info(f"[respawn] {state} (settings: {self._config.path}).")

    async def on_disable(self) -> None:
        for task in (self._reload_task, self._respawn_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reload_task = None
        self._respawn_task = None
        log.info(f"[respawn] stopped ({self._deaths} death(s) handled this run).")

    async def on_bot_ready(self) -> None:
        """A new connection: drop any respawn flow left over from the old one."""

        task = self._respawn_task
        if task is not None and not task.done():
            task.cancel()
        self._respawn_task = None
        self._respawned.clear()
        # A hot reload or reconnect can land on the death screen; respawn now.
        bot = self.bot
        if bot is not None and bot.player.dead and self._settings["enabled"]:
            log.info("[respawn] still dead when the connection came up, asking again.")
            self._start_respawn()

    async def _reload_loop(self) -> None:
        while True:
            await asyncio.sleep(RELOAD_INTERVAL)
            try:
                if self._config.reload_if_changed():
                    was_on = self._settings.get("enabled")
                    self._settings = self._config.data
                    now_on = self._settings.get("enabled")
                    if was_on != now_on:
                        log.info(
                            f"[respawn] settings updated: auto-respawn {'on' if now_on else 'off'}."
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(f"[respawn] failed to read the settings: {error!r}")

    # ---- Events ----

    async def _on_death(self, message) -> None:
        self._deaths += 1
        self._last_death_at = time.time()
        self._last_message = plain_text(message).strip() if message else ""
        bot = self.bot
        if bot is not None:
            self._last_death = bot.player.position
        where = ""
        if self._last_death is not None:
            x, y, z = self._last_death
            where = f" at {x:.1f} {y:.1f} {z:.1f}"
        reason = f": {self._last_message}" if self._last_message else ""
        log.info(f"[respawn] death detected{where}{reason}")
        if not self._settings["enabled"]:
            log.info("[respawn] auto-respawn is off, staying on the death screen.")
            return
        self._start_respawn()

    async def _on_respawn(self, session) -> None:
        self._respawned.set()

    # ---- The respawn flow ----

    def _start_respawn(self, *, delay: float | None = None) -> None:
        task = self._respawn_task
        if task is not None and not task.done():
            return  # One death, one flow
        self._respawned.clear()
        if delay is None:
            delay = float(self._settings["delay"])
        self._respawn_task = asyncio.create_task(
            self._respawn_flow(delay), name="protobot-respawn"
        )

    async def _respawn_flow(self, delay: float) -> None:
        settings = self._settings
        await asyncio.sleep(max(0.0, delay))
        attempts = 1 + max(0, int(settings["max_retries"]))
        timeout = max(0.5, float(settings["retry_delay"]))
        for attempt in range(1, attempts + 1):
            bot = self.bot
            if bot is None:
                log.warn("[respawn] not connected, dropping this respawn request.")
                return
            if not bot.player.dead:
                # Someone respawned manually, or the server did it for us.
                await self._after_respawn()
                return
            try:
                await bot.respawn()
            except UnsupportedVersion as error:
                if not self._warned_unsupported:
                    self._warned_unsupported = True
                    log.warn(f"[respawn] {error}; auto-respawn is unavailable on this release.")
                return
            except Exception as error:
                log.error(f"[respawn] failed to send the respawn request: {error!r}")
                return  # The connection itself is broken; retrying is pointless
            try:
                await asyncio.wait_for(self._respawned.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                if attempt < attempts:
                    log.warn(f"[respawn] request {attempt} was not acknowledged, retrying.")
                continue
            await self._after_respawn()
            return
        log.warn(f"[respawn] {attempts} request(s) went unacknowledged, giving up.")

    async def _after_respawn(self) -> None:
        log.info(f"[respawn] respawned (death {self._deaths} this run).")
        bot = self.bot
        announce = str(self._settings["announce"]).strip()
        if announce and bot is not None:
            try:
                await bot.send_message(announce)
            except Exception as error:
                log.warn(f"[respawn] failed to announce the respawn: {error!r}")
        if not self._settings["return_to_death_point"]:
            return
        if bot is None or self._last_death is None:
            return
        x, y, z = self._last_death
        distance = math.dist(bot.player.position, (x, y, z))
        limit = float(self._settings["return_max_distance"])
        if distance > limit:
            log.info(
                f"[respawn] the death point is {distance:.0f} blocks away (limit {limit:.0f}), staying put."
            )
            return
        log.info(f"[respawn] walking back to {x:.1f} {z:.1f} ({distance:.0f} blocks).")
        try:
            await bot.wait_world(timeout=WORLD_TIMEOUT)
            await bot.navigate_to(x, z, sprint=True)
        except TimeoutError:
            log.warn("[respawn] timed out waiting for the world, not walking back.")
        except Exception as error:
            log.warn(f"[respawn] failed to walk back: {error!r}")
        else:
            log.info("[respawn] back near the death point.")

    # ---- Capabilities exposed to other plugins and the LLM ----

    async def _service_status(self) -> str:
        parts = [
            "Auto-respawn is " + ("on" if self._settings["enabled"] else "off")
        ]
        bot = self.bot
        if bot is None:
            parts.append("not connected")
        else:
            parts.append(
                f"health {bot.player.health:.1f}/20, food {bot.player.food}"
            )
            parts.append("currently DEAD" if bot.player.dead else "alive")
        parts.append(f"{self._deaths} death(s) so far")
        if self._last_death is not None:
            x, y, z = self._last_death
            ago = ""
            if self._last_death_at is not None:
                ago = f" {time.time() - self._last_death_at:.0f}s ago"
            parts.append(f"last died at {x:.1f} {y:.1f} {z:.1f}{ago}")
        if self._last_message:
            parts.append(f'death message: "{self._last_message}"')
        if self._settings["return_to_death_point"]:
            parts.append("walks back to the death point after respawning")
        return "; ".join(parts)

    async def _service_now(self) -> str:
        bot = self.bot
        if bot is None:
            return "Not connected"
        if not bot.player.dead:
            return "Not dead, so there is nothing to respawn from"
        task = self._respawn_task
        if task is not None and not task.done():
            task.cancel()  # Cancel the one still waiting out the delay and ask now
            self._respawn_task = None
        self._start_respawn(delay=0.0)
        return "Respawn requested"

    async def _service_set(self, **changes) -> str:
        allowed = ("enabled", "delay", "return_to_death_point", "announce")
        unknown = sorted(key for key in changes if key not in allowed)
        if unknown:
            return f"Unknown field(s): {', '.join(unknown)}"
        if not changes:
            return "Nothing to change"
        merged = dict(self._settings)
        merged.update(changes)
        merged = self._normalize(merged)
        patch = {key: merged[key] for key in changes}
        error = self._config.patch(patch)
        self._settings = self._config.data
        if error:
            self._settings.update(patch)  # At least apply it in this process
            return f"Applied in memory but could not be saved: {error}"
        described = ", ".join(f"{key}={patch[key]!r}" for key in sorted(patch))
        return f"Auto-respawn updated: {described}"

    # ---- Settings validation ----

    @staticmethod
    def _normalize(merged: dict) -> dict:
        for key in ("enabled", "return_to_death_point"):
            merged[key] = bool(merged.get(key, DEFAULT_SETTINGS[key]))
        for key in ("delay", "retry_delay", "return_max_distance"):
            try:
                merged[key] = max(0.0, float(merged.get(key, DEFAULT_SETTINGS[key])))
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        try:
            merged["max_retries"] = max(0, int(merged.get("max_retries", 2)))
        except (TypeError, ValueError):
            merged["max_retries"] = DEFAULT_SETTINGS["max_retries"]
        announce = merged.get("announce") or ""
        merged["announce"] = str(announce)[:256]
        return merged
