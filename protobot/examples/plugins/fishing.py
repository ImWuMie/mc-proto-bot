"""Auto-fishing: cast, detect the bite, reel, repeat.

Three signals feed the detector, in order of reliability; any one of them
reels the line in:

  1. **The bite sound** (the most reliable, and what vanilla itself cues the
     player with) -- ``entity.fishing_bobber.splash`` arrives in a positioned
     sound packet (0x75 = 117 on protocol 775/776) whose coordinates are **the
     bobber's**, while casting and reeling play **at the player**. So "the
     sound happened within 1.5 blocks of the bobber" both recognises a bite
     and rules out our own cast and other people fishing nearby. The numeric
     sound id changes between releases and the server never sends it
     (``minecraft:sound_event`` is a built-in registry), so nothing is
     **hardcoded**: the id is learned the first time position alone identifies
     a bite, and required to match from then on.
  2. **Downward velocity** -- ``entity_motion`` reports the bobber sinking. At
     the bite vanilla sets its Y velocity to ``-0.4 x [0.6, 1.0]`` (-0.24 to
     -0.4 blocks/tick) and plays the splash in the same instant, so this and
     the sound are two views of one moment.
  3. **Position dropping** -- the bobber is pulled below its resting baseline
     by more than a threshold.

Claiming the bobber: the ``minecraft:entity_type`` registry the server sends is
asked for ``minecraft:fishing_bobber`` first. Failing that, an entity spawning
within 2 seconds of the cast and within 8 blocks is taken to be the bobber and
**its type_id is remembered**, so later casts claim it exactly. When the index
computed from the registry does not match (a release or server difference),
that candidate takes over once ``spawn_window`` passes and corrects the
type_id -- otherwise one bad guess would mean never recognising a bobber again
and spinning for an hour.

The velocity and drop signals only arm after ``settle_delay`` (1.2s by default,
which is about how long a cast takes to hit the water): the initial velocity of
a cast can itself be downward, and without the gate every cast would look like
a bite. What must **not** be used instead is "several position updates barely
moved": a bobber resting on the water stops producing position packets at all,
so the baseline would often never be established, both signals would stay
gated forever, and everything would hang on the sound path -- one problem there
and an hour passes with nothing caught. The sound needs no gate; it only plays
when a fish really bites.

Where it landed: ``water_check`` (on by default) checks the two blocks under
the bobber once it is in place and recasts immediately when they are not water,
rather than waiting out ``max_wait``. A chunk that has not arrived reads as
"unknown", so nothing recasts on missing data.

The backstop: ``max_wait`` seconds without a bite (the bobber landed on ground,
the line was cut) reels in and casts again, as does the bobber being removed.
Only ``recast_delay`` seconds (0.4 by default) separate reeling from casting.

Settings live in ``fishing.json`` next to this file (written on first enable)
and reload within about 5 seconds of an edit. It starts **enabled=false**: flip
that to true to fish, or let the LLM call the exposed ``fishing.start`` /
``fishing.stop`` / ``fishing.status``. Holding a rod is on you -- this protocol
stack cannot read item names, so the plugin cannot check what is in hand.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from protobot import Plugin, PluginSettings, log
from protobot.protocol import PacketReader

DEFAULT_SETTINGS: dict = {
    "enabled": False,  # true starts fishing
    "hand": "main_hand",  # Which hand casts: main_hand / off_hand
    "sound_packet_id": None,  # Sound packet id; null = from the version table
    "sound_id": None,  # Numeric bite-sound id; null = learn and remember it
    "sound_radius": 1.5,  # Greatest distance between sound and bobber (blocks)
    "bite_velocity": -0.15,  # Downward threshold (blocks/tick; vanilla bites at
    #                          -0.24 to -0.4); 0 disables this signal
    "bite_drop": 0.12,  # Drop below the resting baseline (blocks); 0 disables
    "settle_delay": 1.2,  # Seconds before watching velocity/drop (time to land)
    "water_check": True,  # Recast at once when it did not land in water
    "recast_delay": 0.4,  # Seconds between reeling and casting again
    "max_wait": 45.0,  # Recast after this long without a bite (seconds)
    "spawn_window": 2.0,  # Claiming: how soon after the cast an entity counts
    "spawn_radius": 8.0,  # Claiming: how close to us it has to spawn (blocks)
}

#: Main-loop step (seconds). Detection is event-driven; this only handles
#: timeouts and recasting.
TICK = 0.1

#: How many consecutive "not in water" reads (x TICK seconds) trigger a
#: recast. A long cast may still be in the air above the water, and one look
#: would throw away a perfectly good cast.
DRY_CONFIRM = 5


class AutoFishing(Plugin):
    name = "fishing"

    def __init__(self) -> None:
        super().__init__()
        self._config: PluginSettings | None = None
        self._settings: dict = AutoFishing._normalize(dict(DEFAULT_SETTINGS))
        self._loop_task: asyncio.Task | None = None
        self._tick_count = 0
        # Bobber tracking
        self._bobber_id: int | None = None
        self._bobber_type: int | None = None  # type_id learned on a claim
        self._baseline: float | None = None  # Resting water-line Y
        self._last_y: float | None = None
        self._armed = False  # Settle delay passed: velocity/drop now count
        self._claim_at = 0.0  # When the bobber was claimed
        self._candidate: tuple[int, int | None, float] | None = None  # Fallback
        self._dry_reads = 0  # Consecutive "not in water" reads
        # State machine: idle -> casting -> waiting -> (bite/timeout) ->
        # cooldown -> ...
        self._state = "idle"
        self._cast_at = 0.0
        self._next_cast_at = 0.0
        self._reeling = False
        self._catches = 0
        self._learned_sound: int | None = None  # Learned bite-sound id
        self._warned_no_sound = False  # Say it once when the id is unverified
        self.subscribe("entity_add", self._on_entity_add)
        self.subscribe("entity_motion", self._on_entity_motion)
        self.subscribe("entity_move", self._on_entity_move)
        self.subscribe("entity_teleport", self._on_entity_teleport)
        self.subscribe("entities_remove", self._on_entities_remove)
        self.subscribe("packet", self._on_packet)
        self.subscribe_session("session_ready", self._on_session_ready)
        # Exposed to other plugins and the LLM: start / stop / status
        self.expose(
            "start",
            self._service_start,
            description=(
                "Start auto-fishing: cast, watch the bobber, reel on a bite, "
                "repeat. The bot must be holding a fishing rod and facing water."
            ),
            llm=True,
            admin=True,
        )
        self.expose(
            "stop",
            self._service_stop,
            description="Stop auto-fishing and leave the rod alone.",
            llm=True,
            admin=True,
        )
        self.expose(
            "status",
            self._service_status,
            description=(
                "Auto-fishing status: whether it is running, what it is doing "
                "right now, and how many fish have been reeled in."
            ),
            llm=True,
        )

    # ---- Capabilities exposed to other plugins and the LLM ----

    async def _service_start(self) -> str:
        if self._settings["enabled"]:
            return f"Already fishing ({self._catches} caught so far)"
        self._set_enabled(True)
        return "Auto-fishing started (rod in hand and water in front are on you)"

    async def _service_stop(self) -> str:
        if not self._settings["enabled"]:
            return "Auto-fishing was not running"
        self._set_enabled(False)
        return f"Auto-fishing stopped ({self._catches} caught this session)"

    async def _service_status(self) -> str:
        states = {
            "idle": "idle",
            "casting": "cast, waiting for the bobber to appear",
            "waiting": "line in the water, watching the bobber",
            "cooldown": "between casts",
        }
        if not self._settings["enabled"]:
            return f"Auto-fishing is off ({self._catches} caught this session)"
        detail = states.get(self._state, self._state)
        if self._state == "waiting" and not self._armed:
            detail = "bobber in flight, not watching yet"
        elif self._state == "waiting" and self._bobber_in_water() is False:
            detail = "bobber is not in water -- recasting shortly"
        return (
            f"Auto-fishing is on: {detail}; {self._catches} caught this session"
        )

    def _set_enabled(self, enabled: bool) -> None:
        """Flip the switch, writing back only that one key of fishing.json.

        Patching one key rather than rewriting the file avoids overwriting
        anything else edited in the meantime, and avoids expanding a file that
        holds a single line into every default.
        """
        if not enabled:
            self._reset()
        error = self._config.patch({"enabled": enabled})
        self._settings = self._config.data
        if error:
            log.warn(f"[fishing] could not write the switch back ({error})")
            self._settings["enabled"] = enabled  # At least apply it in-process
        log.info(f"[fishing] {'started' if enabled else 'stopped'}.")

    # ---- Lifecycle ----

    async def on_enable(self) -> None:
        if self._config is None:
            self._config = self.settings_file(
                "fishing.json", DEFAULT_SETTINGS,
                label="fishing", normalize=self._normalize,
            )
        self._load_settings()
        self._loop_task = asyncio.create_task(
            self._loop(), name="protobot-fishing"
        )
        if self._settings["enabled"]:
            log.info("[fishing] enabled; casting starts once connected.")
        else:
            log.info(
                f"[fishing] loaded but off: set enabled to true in "
                f"{self._config.path} (applies within about 5 seconds)."
            )

    async def on_disable(self) -> None:
        task = self._loop_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        log.info(f"[fishing] stopped ({self._catches} caught this run).")

    async def _on_session_ready(self, bot) -> None:
        self._reset()  # After a reconnect the old bobber is gone

    def _reset(self) -> None:
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._state = "idle"
        self._reeling = False

    # ---- Settings: defaults and clamping here, I/O and reloading in the
    # framework ----

    @staticmethod
    def _normalize(merged: dict) -> dict:
        merged["enabled"] = bool(merged.get("enabled", False))
        if merged.get("hand") not in ("main_hand", "off_hand"):
            merged["hand"] = "main_hand"
        for key in (
            "bite_velocity", "bite_drop", "settle_delay",
            "recast_delay", "max_wait", "spawn_window", "spawn_radius",
            "sound_radius",
        ):
            try:
                merged[key] = float(merged.get(key, DEFAULT_SETTINGS[key]))
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        for key in ("sound_packet_id", "sound_id"):
            value = merged.get(key)
            if value is None or str(value) == "":
                merged[key] = None  # Leave it to the version table / learning
                continue
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        merged["water_check"] = bool(merged.get("water_check", True))
        merged["settle_delay"] = min(10.0, max(0.2, merged["settle_delay"]))
        merged["recast_delay"] = max(0.05, merged["recast_delay"])
        merged["max_wait"] = max(5.0, merged["max_wait"])
        return merged

    def _load_settings(self) -> None:
        self._config.load()
        self._settings = self._config.data

    def _maybe_reload(self) -> None:
        was_on = self._settings.get("enabled")
        if not self._config.reload_if_changed():
            return
        self._settings = self._config.data
        now_on = self._settings.get("enabled")
        if was_on != now_on:
            log.info(f"[fishing] settings updated: {'on' if now_on else 'off'}.")
            if not now_on:
                self._reset()
        else:
            log.info("[fishing] settings updated.")

    # ---- Main loop: casting, timeouts, recasting (bites arrive as events) ----

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            self._tick_count += 1
            if self._tick_count % int(5 / TICK) == 0:
                self._maybe_reload()
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(f"[fishing] loop error: {error!r}")

    async def _step(self) -> None:
        if not self._settings["enabled"] or self.bot is None:
            return
        now = time.monotonic()
        if self._state in ("idle", "cooldown"):
            if now >= self._next_cast_at:
                await self._cast()
            return
        if self._state == "casting":
            # The registry's type_id can be wrong (release or server
            # differences), so fall back to the "just cast, right here"
            # candidate and correct the type_id from it.
            if now - self._cast_at > self._settings["spawn_window"]:
                self._claim_candidate()
        if self._state == "waiting" and not self._armed:
            if now - self._claim_at >= self._settings["settle_delay"]:
                self._arm()
        if self._state == "waiting" and self._armed:
            self._check_water()
        if now - self._cast_at > self._settings["max_wait"]:
            # The bobber may be on land, or the line is gone: reel and recast
            log.info("[fishing] no bite for a while, casting again.")
            await self._reel(caught=False)

    def _arm(self) -> None:
        """The settle delay has passed: take the current Y as the baseline and
        let the velocity and drop signals count.

        This used to require "several position updates barely moved", but a
        bobber resting on water hardly moves, so the server **stops sending
        position packets** -- the baseline was often never established, both
        signals stayed gated forever, and bites went undetected. Time is the
        better test: a cast takes about a second to land, and after that the
        bobber is either in the water or this cast was wasted anyway.
        """
        self._armed = True
        position = self._bobber_position()
        if position is not None:
            self._baseline = position[1]
            self._last_y = position[1]
        elif self._last_y is not None:
            self._baseline = self._last_y

    def _check_water(self) -> None:
        """Recast when it did not land in water -- after DRY_CONFIRM reads.

        One look is not enough: a long cast may still be in the air above the
        water, and that look reads air. A few reads a TICK apart give it time to
        settle without throwing away a good cast.
        """
        if not self._settings["water_check"]:
            return
        if self._bobber_in_water() is False:
            self._dry_reads += 1
        else:
            self._dry_reads = 0
            return
        if self._dry_reads >= DRY_CONFIRM:
            log.info("[fishing] the bobber is not in water, recasting.")
            self._recast_soon()

    def _bobber_in_water(self) -> bool | None:
        """Whether the bobber is in water; None when we cannot tell.

        Every block of an unloaded chunk reads as state 0 (air), so without
        this distinction "the chunk has not arrived" would look like "not
        water" and the plugin would recast forever.
        """
        position = self._bobber_position()
        world = getattr(self.bot, "world", None)
        if position is None or world is None:
            return None
        x, y, z = position
        block_x, block_z = math.floor(x), math.floor(z)
        chunks = getattr(world, "chunks", None)
        if not chunks or (block_x >> 4, block_z >> 4) not in chunks:
            return None
        for block_y in (math.floor(y), math.floor(y) - 1):
            try:
                properties = world.block_properties(block_x, block_y, block_z)
            except Exception:
                return None
            if getattr(properties, "fluid", None) == "water":
                return True
        return False

    def _claim_candidate(self) -> None:
        """No bobber claimed before spawn_window ran out: take the candidate and
        correct the type_id from it."""
        candidate = self._candidate
        if candidate is None:
            return
        entity_id, type_id, _ = candidate
        if type_id is not None and type_id != self._bobber_type:
            log.info(
                f"[fishing] the registry's bobber type_id={self._bobber_type} "
                f"did not match; using the spawned type_id={type_id}."
            )
            self._bobber_type = type_id
        self._claim(entity_id, self._last_y)

    def _claim(self, entity_id: int, y: float | None) -> None:
        self._bobber_id = entity_id
        self._baseline = None
        self._last_y = y
        self._armed = False
        self._dry_reads = 0
        self._candidate = None
        self._claim_at = time.monotonic()
        self._state = "waiting"

    def _recast_soon(self) -> None:
        self._bobber_id = None
        self._baseline = None
        self._armed = False
        self._dry_reads = 0
        self._state = "cooldown"
        self._next_cast_at = time.monotonic() + self._settings["recast_delay"]

    async def _cast(self) -> None:
        bot = self.bot
        if bot is None:
            return
        try:
            await bot.use_item(hand=self._settings["hand"])
        except Exception as error:
            log.error(f"[fishing] casting failed: {error}")
            self._next_cast_at = time.monotonic() + 2.0
            return
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._dry_reads = 0
        self._reeling = False
        self._state = "casting"
        self._cast_at = time.monotonic()

    async def _reel(self, *, caught: bool) -> None:
        """Reel in. ``caught`` separates a catch from a timeout or a lost line;
        it only affects the log and the counter."""
        bot = self.bot
        if bot is None or self._reeling:
            return
        self._reeling = True
        try:
            await bot.use_item(hand=self._settings["hand"])
        except Exception as error:
            log.error(f"[fishing] reeling failed: {error}")
        if caught:
            self._catches += 1
            waited = time.monotonic() - self._cast_at
            log.info(f"[fishing] bite, reeled in (catch {self._catches}, waited {waited:.1f}s).")
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._state = "cooldown"
        self._next_cast_at = time.monotonic() + self._settings["recast_delay"]

    # ---- Claiming the bobber ----

    def _registry_bobber_type(self) -> int | None:
        """Look fishing_bobber up in the server registry (the protocol id is
        the entry index)."""
        registries = getattr(self.bot, "registries", None)
        if registries is None:
            return None
        try:
            entries = registries.get("minecraft:entity_type") or ()
        except Exception:
            return None
        for index, entry in enumerate(entries):
            if getattr(entry, "key", None) == "minecraft:fishing_bobber":
                return index
        return None

    async def _on_entity_add(self, entity) -> None:
        if self._state != "casting" or self.bot is None:
            return
        if time.monotonic() - self._cast_at > self._settings["spawn_window"]:
            return
        player = self.bot.player
        radius = self._settings["spawn_radius"]
        near = (
            abs(entity.x - player.x) <= radius
            and abs(entity.y - player.y) <= radius
            and abs(entity.z - player.z) <= radius
        )
        if not near:
            return
        type_id = getattr(entity, "type_id", None)
        if self._bobber_type is None:
            self._bobber_type = self._registry_bobber_type()
        if self._bobber_type is not None and type_id != self._bobber_type:
            # Wrong type: keep it as the candidate, which covers a bad registry
            # index (see _claim_candidate)
            if self._candidate is None:
                self._candidate = (entity.entity_id, type_id, entity.y)
                self._last_y = entity.y
            return
        if self._bobber_type is None:
            self._bobber_type = type_id
        self._claim(entity.entity_id, entity.y)

    async def _on_entities_remove(self, entity_ids, removed) -> None:
        if self._bobber_id is not None and self._bobber_id in tuple(entity_ids):
            if self._state == "waiting" and not self._reeling:
                # The bobber vanished (line cut, dimension change): not a catch
                self._recast_soon()

    # ---- Bite detection: the sound (most reliable) ----

    async def _on_packet(self, packet) -> None:
        """A positioned sound at the bobber is a bite.

        The ``packet`` event fires for every inbound packet, so the first step
        is a single integer comparison.
        """
        wanted = self._sound_packet_id()
        if not wanted or packet.packet_id != wanted:
            return
        if not self._watching_sound():
            return
        decoded = self._decode_sound(packet.payload)
        if decoded is None:
            return  # Wrong id or layout: treat the sound path as absent
        sound_id, x, y, z = decoded
        position = self._bobber_position()
        if position is None:
            return
        radius = self._settings["sound_radius"]
        if (
            abs(x - position[0]) > radius
            or abs(y - position[1]) > radius
            or abs(z - position[2]) > radius
        ):
            return  # Our cast/reel plays at the player; other people elsewhere
        pinned = self._settings["sound_id"]
        expected = pinned if pinned is not None else self._learned_sound
        if expected is not None and sound_id != expected:
            return
        if expected is None and sound_id is not None:
            self._learned_sound = sound_id
            log.info(f"[fishing] learned the bite-sound id={sound_id}; requiring it now.")
        await self._reel(caught=True)

    def _sound_packet_id(self) -> int:
        """The sound packet id: the setting wins, otherwise the version table.

        0 in the table means this release's id is unverified (1.21.11, say), and
        then the sound path is simply off -- decoding on a guess would only
        misfire, and the other two signals cover it.
        """
        configured = self._settings.get("sound_packet_id")
        if configured:
            return int(configured)
        version = getattr(self.bot, "version", None)
        packets = getattr(version, "packets", None)
        resolved = int(getattr(packets, "clientbound_sound", 0) or 0)
        if not resolved and not self._warned_no_sound:
            self._warned_no_sound = True
            log.info(
                "[fishing] this release has no verified sound packet id, so "
                "sound detection is off (velocity and drop still work; set "
                "sound_packet_id in fishing.json to force it)."
            )
        return resolved

    def _watching_sound(self) -> bool:
        return (
            self._settings["enabled"]
            and self._state == "waiting"
            and not self._reeling
            and self._bobber_id is not None
        )

    def _bobber_position(self) -> tuple[float, float, float] | None:
        bot = self.bot
        if bot is None or self._bobber_id is None:
            return None
        entity = getattr(bot, "entities", {}).get(self._bobber_id)
        if entity is None:
            return None
        return entity.x, entity.y, entity.z

    @staticmethod
    def _decode_sound(payload: bytes):
        """Decode a positioned sound packet; None when the layout disagrees --
        better no detection than a false one.

        Field order: sound holder (varint, 0 = inline name + bool + optional
        float), category varint, x/y/z as fixed-point ints (/8), volume float,
        pitch float, seed long.
        """
        try:
            reader = PacketReader(payload)
            raw = reader.read_varint()
            if raw == 0:
                reader.read_string()
                if reader.read_bool():
                    reader.read_float()
                sound_id = None
            else:
                sound_id = raw - 1
            reader.read_varint()  # Category
            x = reader.read_int() / 8.0
            y = reader.read_int() / 8.0
            z = reader.read_int() / 8.0
            reader.read_float()  # Volume
            reader.read_float()  # Pitch
            reader.read_long()  # Seed
            reader.expect_end()  # Strict: a wrong packet id almost always
            #                      fails right here
        except Exception:
            return None
        return sound_id, x, y, z

    # ---- Bite detection: velocity and drop, both after settling ----

    async def _on_entity_motion(self, entity_id, velocity, entity) -> None:
        if not self._watching(entity_id):
            return
        if not self._armed:
            return  # Still in flight: a cast's initial velocity can be downward
        threshold = self._settings["bite_velocity"]
        if threshold < 0 and velocity[1] <= threshold:
            await self._reel(caught=True)

    async def _on_entity_move(self, entity_id, entity) -> None:
        if entity is not None and self._watching(entity_id):
            await self._check_dip(entity.y)

    async def _on_entity_teleport(self, entity_id, entity, relative) -> None:
        if entity is not None and self._watching(entity_id):
            await self._check_dip(entity.y)

    def _watching(self, entity_id: int) -> bool:
        return (
            self._state == "waiting"
            and not self._reeling
            and entity_id == self._bobber_id
            and self._settings["enabled"]
        )

    async def _check_dip(self, y: float) -> None:
        """Whether the bobber was pulled below the resting water line.

        The baseline comes from :meth:`_arm` after the settle delay rather than
        from "several updates barely moved": a bobber on the water sends no
        position packets at all, so that test often waits forever.
        """
        drop = self._settings["bite_drop"]
        self._last_y = y
        if not self._armed or self._baseline is None:
            return
        if drop > 0 and self._baseline - y >= drop:
            await self._reel(caught=True)
