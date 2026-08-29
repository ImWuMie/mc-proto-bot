"""Progressive high-level API backed by the raw protocol connection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType

from .errors import (
    ConnectionClosed,
    LoginRejected,
    OnlineModeRequired,
    ProtocolError,
    UnsupportedVersion,
)
from .events import EventBus
from .modlist import ChannelSpec, Loader, ModListAdapter, make_adapter
from .navigation import NavigationPath, NavigationTimeout, Pathfinder
from .physics import (
    AABB,
    BoatPhysicsEngine,
    MovementInput,
    PhysicsAttributes,
    PhysicsEngine,
    PhysicsState,
    StatusEffect,
    Vec3,
)
from .physics.math import f32, minecraft_cos, minecraft_sin
from .protocol.codec import PacketReader, PacketWriter
from .protocol.connection import ConnectionState, ProtocolConnection, RawPacket
from .protocol.framing import make_handshake
from .protocol.nbt import read_anonymous_nbt
from .protocol.versions import VersionSpec, get_version
from .registry import RegistryStore
from .srv import resolve_minecraft_srv
from .state import (
    ContainerState,
    EntityMetadataValue,
    EntityState,
    EquipmentSlot,
    ItemStack,
    PlayerAbilities,
    PlayerListEntry,
    PlayerState,
    WorldSessionState,
)
from .text import plain_text
from .world import BlockStateRegistry, World

_MOVEMENT_EFFECTS = {
    7: "minecraft:jump_boost",
    24: "minecraft:levitation",
    27: "minecraft:slow_falling",
    29: "minecraft:dolphins_grace",
    36: "minecraft:weaving",
}

# Player Info Update 的动作位（1.21.4+ 共 8 个，正好一个字节的定长位集）。
_PLAYER_INFO_ADD = 0x01
_PLAYER_INFO_INIT_CHAT = 0x02
_PLAYER_INFO_GAME_MODE = 0x04
_PLAYER_INFO_LISTED = 0x08
_PLAYER_INFO_LATENCY = 0x10
_PLAYER_INFO_DISPLAY_NAME = 0x20
_PLAYER_INFO_LIST_ORDER = 0x40
_PLAYER_INFO_HAT = 0x80

#: 进入 PLAY 后这么久之内收到的玩家列表算「初始名单」，不当成有人加入。
#: 原版服务端把在线玩家一次发完，但代理有可能拆成几个包。
_ROSTER_GRACE = 1.0

_MOVEMENT_ATTRIBUTES_774 = {
    14: "minecraft:gravity",
    15: "minecraft:jump_strength",
    21: "minecraft:movement_efficiency",
    22: "minecraft:movement_speed",
    26: "minecraft:sneaking_speed",
    28: "minecraft:step_height",
    32: "minecraft:water_movement_efficiency",
}

_MOVEMENT_ATTRIBUTES_775_776 = {
    0: "minecraft:air_drag_modifier",
    9: "minecraft:bounciness",
    17: "minecraft:friction_modifier",
    18: "minecraft:gravity",
    19: "minecraft:jump_strength",
    25: "minecraft:movement_efficiency",
    26: "minecraft:movement_speed",
    31: "minecraft:sneaking_speed",
    33: "minecraft:step_height",
    37: "minecraft:water_movement_efficiency",
}

_MOVEMENT_ATTRIBUTES = {
    774: _MOVEMENT_ATTRIBUTES_774,
    775: _MOVEMENT_ATTRIBUTES_775_776,
    776: _MOVEMENT_ATTRIBUTES_775_776,
}

_MOVEMENT_ATTRIBUTE_RANGES = {
    "minecraft:air_drag_modifier": (0.0, 2048.0),
    "minecraft:bounciness": (0.0, 1.0),
    "minecraft:friction_modifier": (0.0, 2048.0),
    "minecraft:gravity": (-1.0, 1.0),
    "minecraft:jump_strength": (0.0, 32.0),
    "minecraft:movement_efficiency": (0.0, 1.0),
    "minecraft:movement_speed": (0.0, 1024.0),
    "minecraft:sneaking_speed": (0.0, 1.0),
    "minecraft:step_height": (0.0, 10.0),
    "minecraft:water_movement_efficiency": (0.0, 1.0),
}

_START_SPRINTING_COMMAND = 1
_STOP_SPRINTING_COMMAND = 2
_START_FALL_FLYING_COMMAND = 6
_CHANGE_GAME_MODE_EVENT = 3
_SPRINTING_SPEED_MODIFIER = "minecraft:sprinting"

_CONTAINER_CLICK_TYPES = {
    "pickup": 0,
    "quick_move": 1,
    "swap": 2,
    "clone": 3,
    "throw": 4,
    "quick_craft": 5,
    "pickup_all": 6,
}
_INTERACTION_HANDS = {"main_hand": 0, "mainhand": 0, "off_hand": 1, "offhand": 1}
_CLIENTBOUND_CONFIGURATION_TRANSFER = 0x0B
DEFAULT_MINECRAFT_PORT = 25565


def _parse_chat_text(text: str) -> object:
    """Decode a chat message body, falling back to the raw string.

    The protocol carries message bodies as JSON text components, but lenient
    servers occasionally send plain text instead.
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class _UnsupportedItemComponents(Exception):
    pass


class _UnsupportedEntityMetadata(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AttributeModifierUpdate:
    """One modifier decoded from a clientbound attribute update."""

    identifier: str
    amount: float
    operation: int


@dataclass(frozen=True, slots=True)
class AttributeUpdate:
    """A decoded attribute snapshot and its vanilla-computed value."""

    registry_id: int
    identifier: str | None
    base: float
    modifiers: tuple[AttributeModifierUpdate, ...]
    value: float


def offline_uuid(username: str) -> uuid.UUID:
    """Match Java's UUID.nameUUIDFromBytes for ``OfflinePlayer:<name>``."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))


class Bot:
    """An offline-mode Minecraft bot.

    ``start`` opens the socket and starts the protocol task. ``wait_ready`` waits
    until configuration is complete. The convenience :func:`connect` does both.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 25565,
        handshake_host: str | None = None,
        username: str = "ProtoBot",
        version: str | VersionSpec = "26.2",
        connect_timeout: float = 10.0,
        resolve_srv: bool = True,
        access_token: str | None = None,
        profile_uuid: str | uuid.UUID | None = None,
        session_server: str = "https://sessionserver.mojang.com",
        loader: Loader | str = Loader.VANILLA,
        mods: Mapping[str, str] | None = None,
        loader_protocol: int = 0,
        configuration_channels: Mapping[str, ChannelSpec | str | int] | None = None,
        play_channels: Mapping[str, ChannelSpec | str | int] | None = None,
        display_names: Mapping[str, str] | None = None,
        common_versions: Iterable[int] = (1,),
        fabric_registry_handler: Callable[[bytes], bool] | None = None,
        velocity_secret: str | bytes | None = None,
        velocity_player_ip: str = "127.0.0.1",
        accept_transfers: bool = True,
        modlist: ModListAdapter | None = None,
        block_states: BlockStateRegistry | None = None,
        block_state_report: str | None = None,
        block_state_table: str | None = None,
        physics_engine: PhysicsEngine | None = None,
        physics_attributes: PhysicsAttributes | None = None,
    ) -> None:
        if not 1 <= len(username) <= 16:
            raise ValueError("username must contain 1 to 16 characters")
        if not 0 < port < 65536:
            raise ValueError("port must be between 1 and 65535")
        if not math.isfinite(connect_timeout) or connect_timeout <= 0.0:
            raise ValueError("connect_timeout must be a finite positive number")
        self.host = host
        self.port = port
        if handshake_host is not None and (
            not isinstance(handshake_host, str) or not handshake_host
        ):
            raise ValueError("handshake_host must be a non-empty string or None")
        self.handshake_host = handshake_host
        self.username = username
        self.version = get_version(version)
        if not isinstance(resolve_srv, bool):
            raise TypeError("resolve_srv must be a bool")
        self.resolve_srv = resolve_srv
        # The address actually dialled, which differs from host/port whenever an
        # SRV record redirects the connection.
        self.connected_host = host
        self.connected_port = port
        if profile_uuid is not None:
            self.uuid = uuid.UUID(str(profile_uuid)) if not isinstance(profile_uuid, uuid.UUID) else profile_uuid
        else:
            self.uuid = offline_uuid(username)
        self.access_token = access_token
        self.session_server = session_server
        self.session_id: uuid.UUID | None = None
        self.connect_timeout = connect_timeout
        if isinstance(velocity_secret, str):
            velocity_secret = velocity_secret.encode("utf-8")
        elif velocity_secret is not None and not isinstance(velocity_secret, bytes):
            raise TypeError("velocity_secret must be str, bytes, or None")
        if velocity_secret is not None and not velocity_secret:
            raise ValueError("velocity_secret must not be empty")
        if not isinstance(velocity_player_ip, str) or not velocity_player_ip:
            raise ValueError("velocity_player_ip must be a non-empty string")
        try:
            ipaddress.ip_address(velocity_player_ip)
        except ValueError as error:
            raise ValueError("velocity_player_ip must be a valid IPv4 or IPv6 address") from error
        self._velocity_secret = velocity_secret
        self.velocity_player_ip = velocity_player_ip
        if not isinstance(accept_transfers, bool):
            raise TypeError("accept_transfers must be a bool")
        self.accept_transfers = accept_transfers
        self.modlist = modlist or make_adapter(
            loader,
            mods,
            protocol_version=loader_protocol,
            configuration_channels=configuration_channels,
            play_channels=play_channels,
            display_names=display_names,
            common_versions=common_versions,
            fabric_registry_handler=fabric_registry_handler,
        )
        self.state = ConnectionState.DISCONNECTED
        self.player = PlayerState()
        self.entities: dict[int, EntityState] = {}
        #: Tab list, keyed by profile UUID; drives player_join / player_leave.
        self.players: dict[uuid.UUID, PlayerListEntry] = {}
        self._roster_synced = False
        self._roster_deadline = 0.0
        self.containers: dict[int, ContainerState] = {}
        self._active_container_id: int | None = None
        self.session = WorldSessionState()
        self.registries = RegistryStore()
        self.world = World(
            block_states,
            protocol=self.version.protocol,
            release=self.version.name,
        )
        self.world.set_entity_collision_provider(self._hard_entity_collision_boxes)
        if block_state_report is not None:
            self.world.block_states.load_report(block_state_report)
        if block_state_table is not None:
            self.world.block_states.load_table(block_state_table)
        self.events = EventBus()
        self.ready = asyncio.Event()
        self.loaded = asyncio.Event()
        self.world_ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.disconnect_reason: str | None = None
        self._connection = ProtocolConnection()
        self._reader_task: asyncio.Task[None] | None = None
        self._terminal_error: BaseException | None = None
        if physics_engine is not None and physics_attributes is not None:
            raise ValueError("pass either physics_engine or physics_attributes, not both")
        self.physics = physics_engine or PhysicsEngine(physics_attributes)
        self.boat_physics = BoatPhysicsEngine()
        self.physics_state = PhysicsState()
        self._last_sent_input_flags = 0
        self._last_sent_sprinting = False
        self._next_sequence = 0

    async def __aenter__(self) -> Bot:
        if self.state is ConnectionState.DISCONNECTED and not self.closed.is_set():
            try:
                await self.start()
                await self.wait_ready()
                await self.wait_loaded()
            except BaseException:
                await self.close()
                raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def on(self, event: str, handler=None):  # type: ignore[no-untyped-def]
        """Register an event handler; usable directly or as a decorator."""
        return self.events.on(event, handler)

    async def start(self) -> None:
        if self.state is not ConnectionState.DISCONNECTED or self._reader_task is not None:
            raise RuntimeError("bot has already been started")
        await self._open_login_connection(
            self.host,
            self.port,
            self.handshake_host or self.host,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"protobot:{self.username}")

    async def _resolve_endpoint(self, host: str, port: int) -> tuple[str, int]:
        """Apply Minecraft SRV redirection the way a vanilla client does.

        Vanilla only looks up ``_minecraft._tcp.<host>`` when the player typed no
        port, so the lookup is confined to the default port. Any lookup failure
        falls through to the address as given.
        """

        if not self.resolve_srv or port != DEFAULT_MINECRAFT_PORT:
            return host, port
        target = await asyncio.to_thread(resolve_minecraft_srv, host)
        if target is None:
            return host, port
        await self.events.emit("srv_resolved", host, target[0], target[1])
        return target

    async def _open_login_connection(
        self,
        host: str,
        port: int,
        handshake_host: str,
    ) -> None:
        connect_host, connect_port = await self._resolve_endpoint(host, port)
        self.connected_host = connect_host
        self.connected_port = connect_port
        await asyncio.wait_for(
            self._connection.open(connect_host, connect_port),
            timeout=self.connect_timeout,
        )
        self.state = ConnectionState.HANDSHAKING
        protocol_host = self.modlist.handshake_host(handshake_host)
        await self._connection.send_packet(
            0,
            make_handshake(self.version.protocol, protocol_host, connect_port, 2),
        )
        self.state = ConnectionState.LOGIN
        login_start = PacketWriter().write_string(self.username, max_chars=16).write_uuid(self.uuid)
        await self._connection.send_packet(0, login_start.to_bytes())

    async def wait_ready(self, *, timeout: float = 30.0) -> None:
        ready_task = asyncio.create_task(self.ready.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"login did not complete within {timeout} seconds")
            if closed_task in done and not self.ready.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(self.disconnect_reason or "connection closed during login")
        finally:
            ready_task.cancel()
            closed_task.cancel()

    async def wait_loaded(self, *, timeout: float = 30.0) -> None:
        """Wait until the server has sent the initial player position."""

        loaded_task = asyncio.create_task(self.loaded.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (loaded_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"initial position was not received within {timeout} seconds")
            if closed_task in done and not self.loaded.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(self.disconnect_reason or "connection closed before spawn")
        finally:
            loaded_task.cancel()
            closed_task.cancel()

    async def wait_world(self, *, timeout: float = 30.0) -> None:
        """Wait until at least one decoded chunk is available."""

        world_task = asyncio.create_task(self.world_ready.wait())
        closed_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait(
                (world_task, closed_task),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(f"no chunk was received within {timeout} seconds")
            if closed_task in done and not self.world_ready.is_set():
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ConnectionClosed(
                    self.disconnect_reason or "connection closed before world data"
                )
        finally:
            world_task.cancel()
            closed_task.cancel()

    async def close(self) -> None:
        task, self._reader_task = self._reader_task, None
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._connection.close()
        self.state = ConnectionState.DISCONNECTED
        self.closed.set()

    async def send_raw(self, packet_id: int, payload: bytes = b"") -> None:
        """Send a packet in the current state for advanced protocol use."""
        await self._connection.send_packet(packet_id, payload)

    async def send_configuration_payload(self, channel: str, data: bytes = b"") -> None:
        """Send a configuration custom payload.

        Loader handshakes use this escape hatch for messages that are not part
        of the common Forge/Fabric adapter.  It is valid only while the
        connection is in the configuration state.
        """

        if self.state is not ConnectionState.CONFIGURATION:
            raise RuntimeError(
                f"configuration payload requires configuration state, currently {self.state.value}"
            )
        payload = PacketWriter().write_string(channel, max_chars=32767).write_raw(data).to_bytes()
        await self.send_raw(0x02, payload)

    async def send_play_payload(self, channel: str, data: bytes = b"") -> None:
        """Send a play-state custom payload for loader/application protocols."""

        self._require_play()
        payload = PacketWriter().write_string(channel, max_chars=32767).write_raw(data).to_bytes()
        await self.send_raw(self.version.packets.serverbound_custom_payload, payload)

    @property
    def active_container(self) -> ContainerState | None:
        """Return the currently open menu, if the server has opened one."""

        if self._active_container_id is None:
            return None
        return self.containers.get(self._active_container_id)

    @property
    def online_players(self) -> tuple[str, ...]:
        """Names in the tab list, sorted; empty before the first roster.

        This is who the server says is online, not who is nearby -- unlike
        :attr:`entities`, it is not limited to the loaded chunks.
        """

        return tuple(sorted(entry.name for entry in self.players.values() if entry.name))

    def find_player(self, name: str) -> PlayerListEntry | None:
        """Look a tab-list entry up by name, case-insensitively."""

        wanted = name.strip().lower()
        for entry in self.players.values():
            if entry.name.lower() == wanted:
                return entry
        return None

    async def click_container(
        self,
        slot: int,
        *,
        button: int = 0,
        click_type: str | int = "pickup",
        container_id: int | None = None,
        state_id: int | None = None,
        changed_slots: Mapping[int, bytes] | None = None,
        carried_item_data: bytes | None = None,
    ) -> None:
        """Send a container click using the current menu state.

        The default is the common left-click/PICKUP action with no client-side
        slot changes and an empty carried ``HashedStack``. Advanced callers may
        provide the raw encoded bodies of changed or carried hashed stacks.
        """

        self._require_play()
        active = self.active_container
        if container_id is None:
            if active is None:
                raise RuntimeError("cannot click a container before the server opens one")
            container_id = active.container_id
        if state_id is None:
            state_id = (
                active.state_id
                if active is not None and active.container_id == container_id
                else 0
            )
        if not -32768 <= slot <= 32767:
            raise ValueError("container slot must fit a signed short")
        if not -128 <= button <= 127:
            raise ValueError("container button must fit a signed byte")
        if isinstance(click_type, str):
            try:
                click_type = _CONTAINER_CLICK_TYPES[click_type.lower()]
            except KeyError as error:
                raise ValueError(f"unknown container click type {click_type!r}") from error
        if not 0 <= click_type <= 6:
            raise ValueError("container click type must be between 0 and 6")
        if not 0 <= state_id < (1 << 31):
            raise ValueError("container state ID must be a non-negative VarInt")
        changed_slots = changed_slots or {}
        if len(changed_slots) > 128:
            raise ValueError("container click cannot contain more than 128 changed slots")
        writer = (
            PacketWriter()
            .write_varint(container_id)
            .write_varint(state_id)
            .write_short(slot)
            .write_byte(button)
            .write_varint(click_type)
            .write_varint(len(changed_slots))
        )
        for changed_slot, stack_data in changed_slots.items():
            if not -32768 <= changed_slot <= 32767:
                raise ValueError("changed container slot must fit a signed short")
            if not isinstance(stack_data, (bytes, bytearray, memoryview)):
                raise TypeError("changed slot data must be bytes")
            writer.write_short(changed_slot).write_bool(True).write_raw(stack_data)
        if carried_item_data is None:
            writer.write_bool(False)
        else:
            if not isinstance(carried_item_data, (bytes, bytearray, memoryview)):
                raise TypeError("carried item data must be bytes")
            writer.write_bool(True).write_raw(carried_item_data)
        await self.send_raw(self.version.packets.serverbound_container_click, writer.to_bytes())

    async def close_container(self, container_id: int | None = None) -> None:
        """Ask the server to close the active container."""

        self._require_play()
        if container_id is None:
            active = self.active_container
            if active is None:
                raise RuntimeError("cannot close a container when none is open")
            container_id = active.container_id
        await self.send_raw(
            self.version.packets.serverbound_container_close,
            PacketWriter().write_varint(container_id).to_bytes(),
        )

    async def use_item(
        self,
        *,
        hand: str | int = "main_hand",
        sequence: int | None = None,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> int:
        """Use the held item and return the sequence number sent on the wire."""

        self._require_play()
        if isinstance(hand, str):
            try:
                hand = _INTERACTION_HANDS[hand.lower()]
            except KeyError as error:
                raise ValueError(f"unknown interaction hand {hand!r}") from error
        if hand not in (0, 1):
            raise ValueError("interaction hand must be main_hand (0) or off_hand (1)")
        if sequence is None:
            sequence = self._next_sequence
            self._next_sequence = (sequence + 1) & 0x7FFFFFFF
        elif not 0 <= sequence < (1 << 31):
            raise ValueError("use-item sequence must be a non-negative VarInt")
        yaw = self.player.yaw if yaw is None else yaw
        pitch = self.player.pitch if pitch is None else pitch
        self._validate_finite((yaw, pitch), "use-item rotation")
        payload = (
            PacketWriter()
            .write_unsigned_byte(hand)
            .write_varint(sequence)
            .write_float(yaw)
            .write_float(pitch)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_use_item, payload)
        return sequence

    async def select_hotbar_slot(self, slot: int) -> None:
        """Select a zero-based hotbar slot using vanilla's carried-item packet."""

        self._require_play()
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise TypeError("hotbar slot must be an int")
        if not 0 <= slot <= 8:
            raise ValueError("hotbar slot must be between 0 and 8")
        await self.send_raw(
            self.version.packets.serverbound_set_carried_item,
            PacketWriter().write_short(slot).to_bytes(),
        )
        self.player.selected_hotbar_slot = slot

    async def respawn(self) -> None:
        """Leave the death screen: Client Status with action 0 (perform respawn).

        The server never respawns a dead player on its own — not even with
        ``doImmediateRespawn`` on, where the vanilla client just sends this
        without showing the screen first. Confirming the teleport that follows
        and re-sending Player Loaded is handled by the position handler, since
        :meth:`_handle_respawn` clears ``player.loaded``.
        """

        self._require_play()
        packet_id = self.version.packets.serverbound_client_command
        if not packet_id:
            raise UnsupportedVersion(
                "serverbound_client_command 的包 ID 在 "
                f"{self.version.name} 上未经核实，无法请求重生"
            )
        await self.send_raw(packet_id, PacketWriter().write_varint(0).to_bytes())

    async def send_command(self, command: str) -> None:
        """Send an unsigned command; a leading slash is accepted and removed."""

        self._require_play()
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        if command.startswith("/"):
            command = command[1:]
        if not command:
            raise ValueError("command must not be empty")
        payload = PacketWriter().write_string(command, max_chars=32767).to_bytes()
        await self.send_raw(self.version.packets.serverbound_chat_command, payload)

    async def send_message(self, message: str) -> None:
        """Send a chat message without a signature.

        The packet carries the timestamp, salt, an empty last-seen offset and
        acknowledgement bitset, and a zero checksum, but no signature. Servers
        that do not enforce secure chat (most plugin servers) accept this;
        a server with ``enforce-secure-profile=true`` will drop or reject it,
        since signing requires the account's local chat keypair, which a bot
        holding only an access token does not have.
        """

        self._require_play()
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if not message:
            raise ValueError("message must not be empty")
        if len(message) > 256:
            raise ValueError("message exceeds the 256 character chat limit")
        payload = (
            PacketWriter()
            .write_string(message, max_chars=256)
            .write_long(int(time.time() * 1000))
            .write_unsigned_long(secrets.randbits(64))
            .write_bool(False)  # no message signature
            .write_varint(0)  # last-seen offset: nothing acknowledged
            .write_raw(b"\x00\x00\x00")  # fixed 20-bit acknowledged bitset
            .write_unsigned_byte(0)  # last-seen checksum (unused without a session)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_chat, payload)
        # Let plugins see what this bot said, whoever sent it: the server
        # echoes chat back as ``player_chat``, and without this a plugin
        # cannot tell its own words (or another plugin's) from a stranger's.
        await self.events.emit("chat_sent", message)

    def load_block_state_report(self, report: str) -> int:
        """Install a Mojang ``blocks.json`` report for collision prediction."""

        return self.world.block_states.load_report(report)

    async def send_position(
        self,
        x: float,
        y: float,
        z: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.physics_state.position = Vec3(x, y, z)
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        payload = (
            PacketWriter()
            .write_double(x)
            .write_double(y)
            .write_double(z)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_position, payload)

    async def send_position_and_rotation(
        self,
        x: float,
        y: float,
        z: float,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.position = Vec3(x, y, z)
        self.physics_state.yaw, self.physics_state.pitch = yaw, pitch
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        payload = (
            PacketWriter()
            .write_double(x)
            .write_double(y)
            .write_double(z)
            .write_float(yaw)
            .write_float(pitch)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_position_look, payload)

    async def send_vehicle_position_and_rotation(
        self,
        x: float,
        y: float,
        z: float,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
    ) -> None:
        """Move the locally controlled root vehicle.

        This is the mounted counterpart to :meth:`send_position_and_rotation`.
        Vanilla accepts this packet only from the root vehicle's controlling
        passenger, so the high-level API rejects non-controlled mounts.
        """

        self._require_play()
        self._validate_finite((x, y, z, yaw, pitch), "vehicle movement values")
        vehicle = self._controlled_root_vehicle()
        if vehicle is None:
            raise RuntimeError("the local player is not controlling a supported vehicle")
        vehicle.x, vehicle.y, vehicle.z = x, y, z
        vehicle.yaw, vehicle.pitch = f32(yaw), f32(pitch)
        if on_ground is not None:
            vehicle.on_ground = bool(on_ground)
        self._sync_local_riding_position()
        await self._send_vehicle_movement(vehicle)

    async def send_input(
        self,
        *,
        forward: bool = False,
        backward: bool = False,
        left: bool = False,
        right: bool = False,
        jump: bool = False,
        sneak: bool = False,
        sprint: bool = False,
    ) -> None:
        self._require_play()
        flags = self._input_flags(forward, backward, left, right, jump, sneak, sprint)
        await self.send_raw(
            self.version.packets.serverbound_player_input,
            PacketWriter().write_unsigned_byte(flags).to_bytes(),
        )
        self._last_sent_input_flags = flags

    async def _sync_input(
        self,
        *,
        forward: bool,
        backward: bool,
        left: bool,
        right: bool,
        jump: bool,
        sneak: bool,
        sprint: bool,
    ) -> None:
        flags = self._input_flags(forward, backward, left, right, jump, sneak, sprint)
        if flags == self._last_sent_input_flags:
            return
        await self.send_input(
            forward=forward,
            backward=backward,
            left=left,
            right=right,
            jump=jump,
            sneak=sneak,
            sprint=sprint,
        )

    async def set_flying(self, enabled: bool) -> None:
        """Enable or disable abilities flight and synchronize it to the server."""

        self._require_play()
        abilities = self.player.abilities
        if enabled and not abilities.allow_flying:
            raise RuntimeError("the server has not granted flight permission")
        if not enabled and self.physics_state.spectator:
            raise RuntimeError("spectator flight is locked")
        if enabled == abilities.flying:
            return
        self._set_local_flying(enabled)
        await self._send_flying_state()

    async def start_gliding(self) -> None:
        """Request fall-flying and start the local Elytra predictor.

        The server remains authoritative about whether an equipped item has a
        usable glider component. ProtoBot tracks the item identity but does not
        yet decode arbitrary positive component patches, so an explicit call is
        treated as the caller's assertion that the equipment is usable.
        """

        self._require_play()
        state = self.physics_state
        if state.gliding:
            return
        if state.flying:
            raise RuntimeError("abilities flight must be disabled before gliding")
        if state.on_ground:
            raise RuntimeError("gliding cannot start while on the ground")
        if state.in_water:
            raise RuntimeError("gliding cannot start while touching water")
        if state.status_effect("levitation") is not None:
            raise RuntimeError("gliding cannot start while levitating")
        entity_id = self.session.entity_id
        if entity_id is None:
            raise RuntimeError("gliding cannot start before play login")
        payload = (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(_START_FALL_FLYING_COMMAND)
            .write_varint(0)
            .to_bytes()
        )
        state.gliding = True
        self.player.gliding = True
        await self.send_raw(self.version.packets.serverbound_player_command, payload)

    def stop_gliding(self) -> None:
        """Stop local fall-flying prediction; vanilla has no stop-gliding packet."""

        self._require_play()
        self.physics_state.gliding = False
        self.physics_state.gliding_ticks = 0
        self.player.gliding = False

    def _set_local_flying(self, enabled: bool) -> None:
        self.player.abilities.flying = enabled
        self.physics_state.flying = enabled
        if enabled:
            self.physics_state.gliding = False
            self.physics_state.gliding_ticks = 0
            self.player.gliding = False

    async def _send_flying_state(self) -> None:
        flags = 0x02 if self.player.abilities.flying else 0x00
        await self.send_raw(
            self.version.packets.serverbound_player_abilities,
            PacketWriter().write_unsigned_byte(flags).to_bytes(),
        )

    async def _sync_sprinting(self, sprinting: bool) -> None:
        if sprinting == self._last_sent_sprinting:
            return
        entity_id = self.session.entity_id
        if entity_id is None:
            raise RuntimeError("sprint state cannot be synchronized before play login")
        command = _START_SPRINTING_COMMAND if sprinting else _STOP_SPRINTING_COMMAND
        payload = (
            PacketWriter()
            .write_varint(entity_id)
            .write_varint(command)
            .write_varint(0)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_player_command, payload)
        self._last_sent_sprinting = sprinting

    async def end_tick(self) -> None:
        self._require_play()
        await self.send_raw(self.version.packets.serverbound_tick_end)

    async def send_look(
        self,
        yaw: float,
        pitch: float,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        """Send a vanilla look-only movement packet and update local state."""

        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.yaw, self.physics_state.pitch = yaw, pitch
        payload = (
            PacketWriter()
            .write_float(yaw)
            .write_float(pitch)
            .write_unsigned_byte(self._movement_flags())
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_look, payload)

    async def send_status(
        self,
        *,
        on_ground: bool | None = None,
        horizontal_collision: bool | None = None,
    ) -> None:
        """Send the status-only movement packet used when position is unchanged."""

        self._require_play()
        self._set_movement_flags(on_ground, horizontal_collision)
        await self.send_raw(
            self.version.packets.serverbound_flying,
            PacketWriter().write_unsigned_byte(self._movement_flags()).to_bytes(),
        )

    async def tick(
        self,
        controls: MovementInput | None = None,
        *,
        send_input: bool = True,
        send_position: bool = True,
    ) -> PhysicsState:
        """Advance the local player by one 20 Hz tick.

        The deterministic physics state is updated first, then the ordinary
        serverbound movement/input packets are emitted.  Callers that want to
        drive their own trajectory can pass ``send_position=False`` and use the
        resulting state as a prediction only.
        """

        self._require_play()
        controls = controls or MovementInput()
        self._tick_remote_entity_metadata()
        if self.player.vehicle_id is not None:
            vehicle = self._controlled_root_vehicle()
            if vehicle is not None:
                self.boat_physics.tick(vehicle, controls, self.world)
            self.physics_state.velocity = Vec3()
            self.player.velocity_x = 0.0
            self.player.velocity_y = 0.0
            self.player.velocity_z = 0.0
            self._sync_local_riding_position()
            if send_input:
                await self._sync_input(
                    forward=controls.forward > 0,
                    backward=controls.forward < 0,
                    left=controls.strafe < 0,
                    right=controls.strafe > 0,
                    jump=controls.jump,
                    sneak=controls.sneak or controls.crawl,
                    sprint=controls.sprint,
                )
                self.physics_state.sprinting = controls.sprint
                await self._sync_sprinting(controls.sprint)
                if vehicle is not None:
                    await self._send_paddle_boat(vehicle)
            if send_position:
                await self.send_look(
                    self.physics_state.yaw,
                    self.physics_state.pitch,
                    on_ground=self.physics_state.on_ground,
                    horizontal_collision=self.physics_state.horizontal_collision,
                )
                if vehicle is not None:
                    await self._send_vehicle_movement(vehicle)
            await self.end_tick()
            return self.physics_state
        if (
            self.physics_state.spectator
            and self.player.abilities.allow_flying
            and not self.physics_state.flying
        ):
            # Spectator flight is locked on by ClientPlayerEntity before it
            # applies movement input.
            self._set_local_flying(True)
            await self._send_flying_state()
        was_flying = self.physics_state.flying
        self.physics.tick(self.physics_state, controls, self.world)
        velocity = self.physics_state.velocity
        self.player.velocity_x = velocity.x
        self.player.velocity_y = velocity.y
        self.player.velocity_z = velocity.z
        if self.physics_state.flying != was_flying:
            self.player.abilities.flying = self.physics_state.flying
            await self._send_flying_state()
        if send_input:
            await self._sync_input(
                forward=controls.forward > 0,
                backward=controls.forward < 0,
                left=controls.strafe < 0,
                right=controls.strafe > 0,
                jump=controls.jump,
                sneak=controls.sneak or controls.crawl,
                sprint=controls.sprint,
            )
            await self._sync_sprinting(self.physics_state.sprinting)
        if send_position:
            state = self.physics_state
            await self.send_position(
                state.position.x,
                state.position.y,
                state.position.z,
                on_ground=state.on_ground,
                horizontal_collision=state.horizontal_collision,
            )
            self.player.yaw, self.player.pitch = state.yaw, state.pitch
        self.player.pose = self.physics_state.pose
        self.player.crouching = self.physics_state.crouching
        self.player.swimming = self.physics_state.swimming
        self.player.gliding = self.physics_state.gliding
        if self.physics_state.gliding_collision_damage > 0.0:
            await self.events.emit(
                "gliding_collision",
                self.physics_state.gliding_collision_damage,
            )
        await self.end_tick()
        return self.physics_state

    async def walk_to(
        self,
        x: float,
        z: float,
        *,
        tolerance: float = 0.35,
        sprint: bool = False,
        timeout: float = 30.0,
    ) -> PhysicsState:
        """Walk in a straight line to an X/Z target using the physics engine.

        This intentionally is a small automation primitive, not a pathfinder;
        applications can build higher-level navigation on the same tick API.
        """

        self._require_play()
        if tolerance <= 0.0 or timeout <= 0.0:
            raise ValueError("tolerance and timeout must be positive")
        if not self.world_ready.is_set():
            await self.wait_world(timeout=timeout)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            state = self.physics_state
            dx, dz = x - state.position.x, z - state.position.z
            if dx * dx + dz * dz <= tolerance * tolerance:
                await self.tick(MovementInput(), send_input=True, send_position=True)
                return state
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("walk_to did not reach its target before timeout")
            state.yaw = math.degrees(math.atan2(-dx, dz))
            # ``tick`` sends a position-only packet.  Vanilla movement input is
            # interpreted using the server's current yaw, so advertise the new
            # heading before advancing the local trajectory; otherwise a target
            # that is not straight ahead would be simulated locally but moved in
            # a different direction by the server.
            await self.send_look(state.yaw, state.pitch)
            await self.tick(MovementInput(forward=1.0, sprint=sprint))
            await asyncio.sleep(0.05)

    def plan_path(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        max_nodes: int = 4096,
        max_drop: float = 3.0,
        allow_diagonal: bool = True,
    ) -> NavigationPath:
        """Plan a collision-aware walking route through the decoded world."""

        self._require_play()
        if not self.world_ready.is_set():
            raise RuntimeError("path planning requires at least one decoded chunk")
        state = self.physics_state
        target_y = state.position.y if y is None else y
        planner = Pathfinder(
            self.world,
            player_width=state.width,
            player_height=state.height,
            step_height=self.physics._attribute(state, "step_height"),
            max_drop=max_drop,
            allow_diagonal=allow_diagonal,
        )
        return planner.find_path(
            state.position,
            Vec3(x, target_y, z),
            match_target_y=y is not None,
            max_nodes=max_nodes,
        )

    async def navigate_to(
        self,
        x: float,
        z: float,
        *,
        y: float | None = None,
        tolerance: float = 0.3,
        sprint: bool = False,
        timeout: float = 60.0,
        max_nodes: int = 4096,
        max_drop: float = 3.0,
        replans: int = 3,
        tick_interval: float = 0.05,
    ) -> PhysicsState:
        """Plan and execute a route using ordinary player input and physics.

        Server corrections or dynamic block changes can invalidate a route.
        Progress is monitored per waypoint and the route is replanned up to
        ``replans`` times before a :class:`NavigationTimeout` is raised.
        """

        self._require_play()
        numeric = (x, z, tolerance, timeout, max_drop, tick_interval)
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("navigation coordinates and limits must be finite")
        if tolerance <= 0.0 or timeout <= 0.0 or max_drop <= 0.0:
            raise ValueError("navigation tolerances and timeouts must be positive")
        if tick_interval < 0.0:
            raise ValueError("tick_interval cannot be negative")
        if replans < 0:
            raise ValueError("replans cannot be negative")
        if y is not None and not math.isfinite(y):
            raise ValueError("navigation Y coordinate must be finite")
        if not self.world_ready.is_set():
            await self.wait_world(timeout=timeout)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        for attempt in range(replans + 1):
            remaining = deadline - loop.time()
            if remaining <= 0.0:
                break
            path = self.plan_path(x, z, y=y, max_nodes=max_nodes, max_drop=max_drop)
            await self.events.emit("path", path, attempt)
            if await self._execute_path(path, deadline, tolerance, sprint, tick_interval):
                await self.tick(MovementInput())
                return self.physics_state

        state = self.physics_state
        distance = math.hypot(x - state.position.x, z - state.position.z)
        raise NavigationTimeout(
            f"navigate_to did not reach ({x:.3f}, {z:.3f}); "
            f"remaining horizontal distance is {distance:.3f} blocks"
        )

    async def _execute_path(
        self,
        path: NavigationPath,
        deadline: float,
        tolerance: float,
        sprint: bool,
        tick_interval: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        for waypoint in path:
            best_distance = math.inf
            stagnant_ticks = 0
            while True:
                state = self.physics_state
                dx = waypoint.position.x - state.position.x
                dz = waypoint.position.z - state.position.z
                horizontal = math.hypot(dx, dz)
                vertical = abs(waypoint.position.y - state.position.y)
                if horizontal <= tolerance and vertical <= 0.55:
                    break
                if loop.time() >= deadline:
                    return False

                if horizontal > tolerance:
                    state.yaw = math.degrees(math.atan2(-dx, dz))
                    await self.send_look(state.yaw, state.pitch)
                jump = waypoint.jump and state.position.y < waypoint.position.y - 0.05
                await self.tick(
                    MovementInput(
                        forward=1.0 if horizontal > tolerance else 0.0,
                        jump=jump,
                        sprint=sprint,
                    )
                )

                distance = horizontal + vertical
                if distance < best_distance - 0.02:
                    best_distance = distance
                    stagnant_ticks = 0
                else:
                    stagnant_ticks += 1
                if stagnant_ticks >= 20:
                    return False
                if tick_interval:
                    await asyncio.sleep(tick_interval)
        return True

    def _require_play(self) -> None:
        if self.state is not ConnectionState.PLAY:
            if self._terminal_error is not None:
                raise self._terminal_error
            raise RuntimeError(f"operation requires play state, currently {self.state.value}")

    def _reset_server_state(self) -> None:
        """Discard state that cannot survive a backend or TCP server switch."""

        self.ready.clear()
        self.loaded.clear()
        self.world_ready.clear()
        self.world.chunks.clear()
        self.player = PlayerState()
        self.session = WorldSessionState()
        self.session_id = None
        self.entities.clear()
        self.players.clear()
        self._roster_synced = False
        self._roster_deadline = 0.0
        self.containers.clear()
        self._active_container_id = None
        self.registries = RegistryStore()
        self.physics_state = PhysicsState()
        self._last_sent_input_flags = 0
        self._last_sent_sprinting = False
        self._next_sequence = 0

    async def _handle_transfer(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        host = reader.read_string(max_chars=32767)
        port = reader.read_varint()
        reader.expect_end()
        if not host:
            raise ProtocolError("transfer host must not be empty")
        if not 0 < port < 65536:
            raise ProtocolError(f"invalid transfer port {port}")
        if not self.accept_transfers:
            await self.events.emit("transfer", host, port)
            reason = f"server requested transfer to {host}:{port}"
            self.disconnect_reason = reason
            raise ConnectionClosed(reason)

        max_packet_size = self._connection.max_packet_size
        await self._connection.close()
        self._connection = ProtocolConnection(max_packet_size=max_packet_size)
        self._reset_server_state()
        self.state = ConnectionState.DISCONNECTED
        self.host = host
        self.port = port
        self.handshake_host = host
        self.disconnect_reason = None
        await self.events.emit("transfer", host, port)
        await self._open_login_connection(host, port, host)

    def _movement_flags(self) -> int:
        return int(self.player.on_ground) | (int(self.player.horizontal_collision) << 1)

    @staticmethod
    def _input_flags(*values: bool) -> int:
        return sum((1 << bit) for bit, enabled in enumerate(values) if enabled)

    def _set_movement_flags(
        self,
        on_ground: bool | None,
        horizontal_collision: bool | None,
    ) -> None:
        """Keep the public player snapshot and predictor flags in lockstep."""

        if on_ground is not None:
            value = bool(on_ground)
            self.player.on_ground = value
            self.physics_state.on_ground = value
        if horizontal_collision is not None:
            value = bool(horizontal_collision)
            self.player.horizontal_collision = value
            self.physics_state.horizontal_collision = value

    async def _read_loop(self) -> None:
        try:
            while True:
                packet = await self._connection.receive_packet(self.state)
                await self.events.emit("packet", packet)
                await self.events.emit(f"packet:{self.state.value}:{packet.packet_id}", packet)
                if self.state is ConnectionState.LOGIN:
                    await self._handle_login(packet)
                elif self.state is ConnectionState.CONFIGURATION:
                    await self._handle_configuration(packet)
                elif self.state is ConnectionState.PLAY:
                    await self._handle_play(packet)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._terminal_error = error
            if isinstance(error, LoginRejected):
                self.disconnect_reason = str(error)
            await self.events.emit("error", error)
        finally:
            await self._connection.close()
            self.state = ConnectionState.DISCONNECTED
            self.closed.set()
            await self.events.emit("close", self.disconnect_reason)

    async def _handle_login(self, packet: RawPacket) -> None:
        if packet.packet_id == 0x00:
            raise LoginRejected(self._best_effort_reason(packet.payload))
        if packet.packet_id == 0x01:
            await self._handle_encryption_request(packet.payload)
            return
        if packet.packet_id == 0x02:
            self._read_login_success(packet.payload)
            await self.send_raw(0x03)
            self.state = ConnectionState.CONFIGURATION
            await self._send_client_information()
            await self.events.emit("login", self)
            return
        if packet.packet_id == 0x03:
            reader = PacketReader(packet.payload)
            threshold = reader.read_varint()
            reader.expect_end()
            if threshold < 0:
                raise ProtocolError("negative compression threshold")
            self._connection.compression_threshold = threshold
            return
        if packet.packet_id == 0x04:
            reader = PacketReader(packet.payload)
            message_id = reader.read_varint()
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("login_plugin_request", channel, data)
            if channel == "velocity:player_info" and self._velocity_secret is not None:
                response_data = self._velocity_forwarding_payload()
            else:
                response_data = self.modlist.handle(channel, data)
            response = PacketWriter().write_varint(message_id).write_bool(
                response_data is not None
            )
            if response_data is not None:
                response.write_raw(response_data)
            await self.send_raw(0x02, response.to_bytes())
            return
        if packet.packet_id == 0x05:
            reader = PacketReader(packet.payload)
            key = reader.read_string(max_chars=32767)
            reader.expect_end()
            await self.events.emit("cookie_request", key)
            response = PacketWriter().write_string(key).write_bool(False).to_bytes()
            await self.send_raw(0x04, response)

    async def _handle_encryption_request(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        server_id = reader.read_string(max_chars=20)
        public_key = reader.read_bytes(max_length=1024)
        verify_token = reader.read_bytes(max_length=1024)
        should_authenticate = reader.read_bool() if self.version.protocol >= 766 and reader.remaining else True
        reader.expect_end()

        shared_secret = secrets.token_bytes(16)

        if should_authenticate:
            if not self.access_token:
                raise OnlineModeRequired(
                    "Server requested online-mode authentication, but no access_token was provided. "
                    "Provide access_token to connect() / Bot() or use device_code_login()."
                )
            from .auth import join_session_server, minecraft_sha1_digest

            server_hash = minecraft_sha1_digest(server_id, shared_secret, public_key)
            await join_session_server(
                self.access_token,
                self.uuid,
                server_hash,
                self.session_server,
            )

        from .auth import rsa_encrypt

        encrypted_secret = rsa_encrypt(public_key, shared_secret)
        encrypted_token = rsa_encrypt(public_key, verify_token)

        response = (
            PacketWriter()
            .write_bytes(encrypted_secret)
            .write_bytes(encrypted_token)
            .to_bytes()
        )
        await self.send_raw(0x01, response)
        self._connection.enable_encryption(shared_secret)

    def _velocity_forwarding_payload(self) -> bytes:
        """Build Velocity modern-forwarding v1 data and its HMAC signature."""

        if self._velocity_secret is None:
            raise RuntimeError("Velocity forwarding secret is not configured")
        body = (
            PacketWriter()
            .write_varint(1)
            .write_string(self.velocity_player_ip, max_chars=32767)
            .write_uuid(self.uuid)
            .write_string(self.username, max_chars=16)
            .write_varint(0)
            .to_bytes()
        )
        signature = hmac.new(self._velocity_secret, body, hashlib.sha256).digest()
        return signature + body

    async def _handle_configuration(self, packet: RawPacket) -> None:
        if packet.packet_id == 0x00:
            reader = PacketReader(packet.payload)
            key = reader.read_string(max_chars=32767)
            response = PacketWriter().write_string(key).write_bool(False).to_bytes()
            await self.send_raw(0x01, response)
        elif packet.packet_id == 0x01:
            reader = PacketReader(packet.payload)
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("configuration_payload", channel, data)
            await self.events.emit("mod_payload", channel, data)
            response = self.modlist.handle_response(channel, data)
            if response is not None:
                await self.send_configuration_payload(response.channel, response.data)
        elif packet.packet_id == 0x02:
            raise LoginRejected(self._best_effort_reason(packet.payload))
        elif packet.packet_id == 0x03:
            await self.send_raw(0x03)
            self.state = ConnectionState.PLAY
            self.ready.set()
            await self.events.emit("ready", self)
        elif packet.packet_id == 0x04:
            reader = PacketReader(packet.payload)
            value = reader.read_long()
            reader.expect_end()
            await self.send_raw(0x04, PacketWriter().write_long(value).to_bytes())
        elif packet.packet_id == 0x05:
            reader = PacketReader(packet.payload)
            value = reader.read_int()
            reader.expect_end()
            await self.send_raw(0x05, PacketWriter().write_int(value).to_bytes())
        elif packet.packet_id == 0x07:
            registry_id = self.registries.apply_packet(packet.payload)
            await self.events.emit("registry", registry_id)
        elif packet.packet_id == 0x0E:
            await self.send_raw(0x07, PacketWriter().write_varint(0).to_bytes())
        elif packet.packet_id == 0x13:
            await self.send_raw(0x09)
        elif packet.packet_id == _CLIENTBOUND_CONFIGURATION_TRANSFER:
            await self._handle_transfer(packet.payload)

    async def _handle_play(self, packet: RawPacket) -> None:
        ids = self.version.packets
        if packet.packet_id == ids.clientbound_disconnect:
            raise ConnectionClosed(self._best_effort_reason(packet.payload))
        if packet.packet_id == ids.clientbound_container_close:
            await self._handle_container_close(packet.payload)
        elif packet.packet_id == ids.clientbound_container_set_content:
            await self._handle_container_content(packet.payload)
        elif packet.packet_id == ids.clientbound_container_set_slot:
            await self._handle_container_slot(packet.payload)
        elif packet.packet_id == ids.clientbound_open_screen:
            await self._handle_open_screen(packet.payload)
        elif packet.packet_id == ids.clientbound_add_entity:
            await self._handle_add_entity(packet.payload)
        elif packet.packet_id == ids.clientbound_player_abilities:
            await self._handle_player_abilities(packet.payload)
        elif packet.packet_id == ids.clientbound_game_event:
            await self._handle_game_event(packet.payload)
        elif packet.packet_id == ids.clientbound_block_update:
            await self._handle_block_update(packet.payload)
        elif packet.packet_id == ids.clientbound_custom_payload:
            reader = PacketReader(packet.payload)
            channel = reader.read_string(max_chars=32767)
            data = reader.read_remaining()
            await self.events.emit("play_payload", channel, data)
            response = self.modlist.handle_response(channel, data)
            if response is not None:
                await self.send_play_payload(response.channel, response.data)
        elif packet.packet_id == ids.clientbound_chunk_batch_finished:
            await self._handle_chunk_batch_finished(packet.payload)
        elif packet.packet_id == ids.clientbound_chunk_data:
            await self._handle_chunk_data(packet.payload)
        elif packet.packet_id == ids.clientbound_keep_alive:
            reader = PacketReader(packet.payload)
            value = reader.read_long()
            reader.expect_end()
            response = PacketWriter().write_long(value).to_bytes()
            await self.send_raw(ids.serverbound_keep_alive, response)
        elif packet.packet_id == ids.clientbound_login:
            self._handle_play_login(packet.payload)
            await self.events.emit("world", self.session)
        elif packet.packet_id == ids.clientbound_move_vehicle:
            await self._handle_move_vehicle(packet.payload)
        elif packet.packet_id == ids.clientbound_move_entity_pos:
            await self._handle_move_entity(packet.payload, position=True, rotation=False)
        elif packet.packet_id == ids.clientbound_move_entity_pos_rot:
            await self._handle_move_entity(packet.payload, position=True, rotation=True)
        elif packet.packet_id == ids.clientbound_move_entity_rot:
            await self._handle_move_entity(packet.payload, position=False, rotation=True)
        elif packet.packet_id == ids.clientbound_ping:
            reader = PacketReader(packet.payload)
            value = reader.read_int()
            reader.expect_end()
            await self.send_raw(ids.serverbound_pong, PacketWriter().write_int(value).to_bytes())
        elif packet.packet_id == ids.clientbound_player_chat:
            await self._handle_player_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_profileless_chat:
            await self._handle_profileless_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_position:
            await self._handle_position(packet.payload)
        elif packet.packet_id == ids.clientbound_remove_mob_effect:
            await self._handle_remove_mob_effect(packet.payload)
        elif packet.packet_id == ids.clientbound_remove_entities:
            await self._handle_remove_entities(packet.payload)
        elif packet.packet_id == ids.clientbound_respawn:
            self._handle_respawn(packet.payload)
            await self.events.emit("respawn", self.session)
        elif (
            ids.clientbound_player_combat_kill
            and packet.packet_id == ids.clientbound_player_combat_kill
        ):
            await self._handle_player_combat_kill(packet.payload)
        elif ids.clientbound_set_health and packet.packet_id == ids.clientbound_set_health:
            await self._handle_set_health(packet.payload)
        elif (
            ids.clientbound_player_info_update
            and packet.packet_id == ids.clientbound_player_info_update
        ):
            await self._handle_player_info_update(packet.payload)
        elif (
            ids.clientbound_player_info_remove
            and packet.packet_id == ids.clientbound_player_info_remove
        ):
            await self._handle_player_info_remove(packet.payload)
        elif packet.packet_id == ids.clientbound_section_blocks_update:
            await self._handle_section_blocks_update(packet.payload)
        elif packet.packet_id == ids.clientbound_set_entity_data:
            await self._handle_set_entity_data(packet.payload)
        elif packet.packet_id == ids.clientbound_set_entity_motion:
            await self._handle_set_entity_motion(packet.payload)
        elif packet.packet_id == ids.clientbound_set_equipment:
            await self._handle_set_equipment(packet.payload)
        elif packet.packet_id == ids.clientbound_set_passengers:
            await self._handle_set_passengers(packet.payload)
        elif packet.packet_id == ids.clientbound_set_player_inventory:
            await self._handle_set_player_inventory(packet.payload)
        elif packet.packet_id == ids.clientbound_start_configuration:
            await self.send_raw(ids.serverbound_configuration_acknowledged)
            self._reset_server_state()
            self.state = ConnectionState.CONFIGURATION
            await self.events.emit("reconfiguration", self)
            await self._send_client_information()
        elif packet.packet_id == ids.clientbound_system_chat:
            await self._handle_system_chat(packet.payload)
        elif packet.packet_id == ids.clientbound_unload_chunk:
            reader = PacketReader(packet.payload)
            chunk_x, chunk_z = reader.read_chunk_pos()
            reader.expect_end()
            self.world.unload_chunk(chunk_x, chunk_z)
            await self.events.emit("chunk_unload", chunk_x, chunk_z)
        elif packet.packet_id == ids.clientbound_teleport_entity:
            await self._handle_teleport_entity(packet.payload)
        elif packet.packet_id == ids.clientbound_transfer:
            await self._handle_transfer(packet.payload)
        elif packet.packet_id == ids.clientbound_update_attributes:
            await self._handle_update_attributes(packet.payload)
        elif packet.packet_id == ids.clientbound_update_mob_effect:
            await self._handle_update_mob_effect(packet.payload)

    async def _handle_player_abilities(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        flags = reader.read_unsigned_byte()
        fly_speed = reader.read_float()
        walk_speed = reader.read_float()
        reader.expect_end()
        if not math.isfinite(fly_speed) or not math.isfinite(walk_speed):
            raise ProtocolError("player ability speeds must be finite")
        abilities = PlayerAbilities(
            invulnerable=bool(flags & 0x01),
            flying=bool(flags & 0x02),
            allow_flying=bool(flags & 0x04),
            instant_build=bool(flags & 0x08),
            fly_speed=fly_speed,
            walk_speed=walk_speed,
        )
        self.player.abilities = abilities
        self.physics_state.allow_flying = abilities.allow_flying
        self.physics_state.fly_speed = abilities.fly_speed
        self._set_local_flying(abilities.flying)
        await self.events.emit("abilities", abilities)

    async def _handle_system_chat(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        component = read_anonymous_nbt(reader)
        overlay = reader.read_bool()
        reader.expect_end()
        await self.events.emit("system_chat", component, overlay)

    def _read_chat_type_holder(self, reader: PacketReader) -> int | None:
        """Read a ChatType holder: a registry id, or an inline custom type.

        Returns the registry id, or ``None`` when the server inlined a custom
        chat type (holder value 0); the inline definition is validated and
        skipped, since decoration is left to the caller.
        """

        value = reader.read_varint()
        if value != 0:
            return value - 1
        reader.read_string(max_chars=32767)  # translation key
        parameter_count = reader.read_varint()
        if parameter_count < 0 or parameter_count > 16:
            raise ProtocolError(f"invalid chat type parameter count {parameter_count}")
        for _ in range(parameter_count):
            reader.read_varint()
        read_anonymous_nbt(reader)  # style
        return None

    async def _handle_player_chat(self, payload: bytes) -> None:
        """Decode a signed player chat message and emit ``player_chat``.

        Layout per the 1.21.5+ protocol (verified against MCProtocolLib for
        protocol 776): global index, sender, index, optional signature, the
        message body, timestamp, salt, last-seen signatures, optional unsigned
        content, a filter mask, a ChatType holder, then the name and optional
        target name components.
        """

        reader = PacketReader(payload)
        reader.read_varint()  # global index
        sender = reader.read_uuid()
        reader.read_varint()  # per-sender index
        if reader.read_bool():
            reader.read_raw(256)  # message signature
        content = reader.read_string(max_chars=256)
        reader.read_long()  # timestamp
        reader.read_long()  # salt
        seen_count = reader.read_varint()
        if seen_count < 0 or seen_count > 20:
            raise ProtocolError(f"invalid last-seen message count {seen_count}")
        for _ in range(seen_count):
            if reader.read_varint() == 0:
                reader.read_raw(256)  # previous message signature
        unsigned_content = None
        if reader.read_bool():
            unsigned_content = read_anonymous_nbt(reader)
        filter_mask = reader.read_varint()
        if filter_mask == 2:
            mask_count = reader.read_varint()
            if mask_count < 0 or mask_count > 1024:
                raise ProtocolError(f"invalid chat filter mask length {mask_count}")
            for _ in range(mask_count):
                reader.read_long()
        chat_type_id = self._read_chat_type_holder(reader)
        name = read_anonymous_nbt(reader)
        target_name = read_anonymous_nbt(reader) if reader.read_bool() else None
        reader.expect_end()

        message = unsigned_content if unsigned_content is not None else _parse_chat_text(content)
        await self.events.emit("player_chat", sender, name, message, chat_type_id, target_name)

    async def _handle_profileless_chat(self, payload: bytes) -> None:
        """Decode a profileless (unsigned) chat message and emit ``player_chat``.

        Servers that do not enforce chat signing broadcast this packet instead
        of the signed variant; the sender uuid is therefore ``None``.
        """

        reader = PacketReader(payload)
        message = read_anonymous_nbt(reader)
        chat_type_id = self._read_chat_type_holder(reader)
        name = read_anonymous_nbt(reader)
        target_name = read_anonymous_nbt(reader) if reader.read_bool() else None
        reader.expect_end()
        await self.events.emit("player_chat", None, name, message, chat_type_id, target_name)

    async def _handle_game_event(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        event_id = reader.read_unsigned_byte()
        value = reader.read_float()
        reader.expect_end()
        if not math.isfinite(value):
            raise ProtocolError("game event value must be finite")
        if event_id == _CHANGE_GAME_MODE_EVENT:
            game_mode = int(value)
            if value != game_mode or not 0 <= game_mode <= 3:
                raise ProtocolError(f"invalid game mode event value {value!r}")
            self.session.previous_game_mode = self.session.game_mode
            self._set_game_mode(game_mode)
            await self.events.emit("game_mode", game_mode)
        await self.events.emit("game_event", event_id, value)

    async def _handle_block_update(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        x, y, z = reader.read_position()
        state_id = reader.read_varint()
        reader.expect_end()
        self.world.set_state_id(x, y, z, state_id)
        await self.events.emit("block_update", x, y, z, state_id)

    async def _handle_chunk_batch_finished(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        batch_size = reader.read_varint()
        reader.expect_end()
        if batch_size < 0:
            raise ProtocolError("negative chunk batch size")
        response = PacketWriter().write_float(7.0).to_bytes()
        await self.send_raw(self.version.packets.serverbound_chunk_batch_received, response)
        await self.events.emit("chunk_batch", batch_size)

    async def _handle_chunk_data(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        chunk_x = reader.read_int()
        chunk_z = reader.read_int()
        heightmap_count = reader.read_varint()
        if not 0 <= heightmap_count <= 64:
            raise ProtocolError(f"invalid heightmap count {heightmap_count}")
        for _ in range(heightmap_count):
            reader.read_varint()
            self._skip_long_array(reader, limit=256)
        chunk_data = reader.read_bytes(max_length=2 * 1024 * 1024)

        block_entity_count = reader.read_varint()
        if not 0 <= block_entity_count <= 65536:
            raise ProtocolError(f"invalid block entity count {block_entity_count}")
        for _ in range(block_entity_count):
            reader.read_byte()
            reader.read_short()
            reader.read_varint()
            read_anonymous_nbt(reader)

        for _ in range(4):
            self._skip_long_array(reader, limit=1024)
        for _ in range(2):
            light_count = reader.read_varint()
            if not 0 <= light_count <= 1024:
                raise ProtocolError(f"invalid light array count {light_count}")
            for _ in range(light_count):
                reader.read_bytes(max_length=2048)
        reader.expect_end()

        chunk = self.world.load_chunk(chunk_x, chunk_z, chunk_data)
        if not self.world_ready.is_set():
            self.world_ready.set()
            await self.events.emit("world_ready", self.world)
        await self.events.emit("chunk", chunk)

    async def _handle_section_blocks_update(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        packed_section = reader.read_unsigned_long()
        section_x = self._signed_bits(packed_section >> 42, 22)
        section_z = self._signed_bits((packed_section >> 20) & 0x3FFFFF, 22)
        section_y = self._signed_bits(packed_section & 0xFFFFF, 20)
        count = reader.read_varint()
        if not 0 <= count <= 4096:
            raise ProtocolError(f"invalid section block update count {count}")
        updates: list[tuple[int, int, int, int]] = []
        for _ in range(count):
            record = reader.read_varlong()
            state_id = record >> 12
            local = record & 0xFFF
            x = (section_x << 4) + ((local >> 8) & 0xF)
            y = (section_y << 4) + (local & 0xF)
            z = (section_z << 4) + ((local >> 4) & 0xF)
            self.world.set_state_id(x, y, z, state_id)
            updates.append((x, y, z, state_id))
        reader.expect_end()
        await self.events.emit("section_blocks_update", updates)

    @staticmethod
    def _unpack_degrees(value: int) -> float:
        return f32(value * (360.0 / 256.0))

    @staticmethod
    def _validate_finite(values: Iterable[float], description: str) -> None:
        if not all(math.isfinite(value) for value in values):
            raise ProtocolError(f"{description} must be finite")

    def _entity_for_state(self, entity_id: int) -> EntityState:
        entity = self.entities.get(entity_id)
        if entity is None:
            entity = EntityState(entity_id)
            self.entities[entity_id] = entity
        return entity

    def _vehicle_passenger_ids(self, vehicle_id: int) -> tuple[int, ...]:
        if vehicle_id == self.session.entity_id:
            return self.player.passenger_ids
        vehicle = self.entities.get(vehicle_id)
        return () if vehicle is None else vehicle.passenger_ids

    def _set_vehicle_passenger_ids(
        self, vehicle_id: int, passenger_ids: tuple[int, ...]
    ) -> None:
        if vehicle_id == self.session.entity_id:
            self.player.passenger_ids = passenger_ids
        else:
            self._entity_for_state(vehicle_id).passenger_ids = passenger_ids

    def _passenger_vehicle_id(self, passenger_id: int) -> int | None:
        if passenger_id == self.session.entity_id:
            return self.player.vehicle_id
        passenger = self.entities.get(passenger_id)
        return None if passenger is None else passenger.vehicle_id

    def _set_passenger_vehicle_id(
        self, passenger_id: int, vehicle_id: int | None
    ) -> None:
        if passenger_id == self.session.entity_id:
            self.player.vehicle_id = vehicle_id
        else:
            self._entity_for_state(passenger_id).vehicle_id = vehicle_id

    def _root_vehicle_id(self, entity_id: int) -> int:
        current = entity_id
        visited: set[int] = set()
        while current not in visited:
            visited.add(current)
            if current == self.session.entity_id:
                vehicle_id = self.player.vehicle_id
            else:
                entity = self.entities.get(current)
                vehicle_id = None if entity is None else entity.vehicle_id
            if vehicle_id is None:
                return current
            current = vehicle_id
        return current

    def _controlled_root_vehicle(self) -> EntityState | None:
        local_id = self.session.entity_id
        if local_id is None or self.player.vehicle_id is None:
            return None
        root_id = self._root_vehicle_id(local_id)
        if root_id == local_id:
            return None
        vehicle = self.entities.get(root_id)
        if (
            vehicle is None
            or vehicle.type_id is None
            or self.version.boat_kind(vehicle.type_id) is None
            or not vehicle.passenger_ids
            or vehicle.passenger_ids[0] != local_id
        ):
            return None
        return vehicle

    def _packet_controlled_root_vehicle(self) -> EntityState | None:
        """Resolve the vehicle targeted by an authoritative correction packet."""

        local_id = self.session.entity_id
        if local_id is None or self.player.vehicle_id is None:
            return None
        root_id = self._root_vehicle_id(local_id)
        if root_id == local_id:
            return None
        vehicle = self.entities.get(root_id)
        if (
            vehicle is None
            or not vehicle.passenger_ids
            or vehicle.passenger_ids[0] != local_id
        ):
            return None
        return vehicle

    def _sync_local_riding_position(self) -> bool:
        """Apply AbstractBoat.positionRider to the local player when possible."""

        local_id = self.session.entity_id
        vehicle_id = self.player.vehicle_id
        if local_id is None or vehicle_id is None:
            return False
        vehicle = self.entities.get(vehicle_id)
        if vehicle is None or vehicle.type_id is None:
            return False
        kind = self.version.boat_kind(vehicle.type_id)
        if kind is None:
            return False
        try:
            passenger_index = vehicle.passenger_ids.index(local_id)
        except ValueError:
            return False

        passenger_count = len(vehicle.passenger_ids)
        if kind.startswith("chest_"):
            longitudinal_offset = float(f32(0.15))
        elif passenger_count > 1:
            longitudinal_offset = float(f32(0.2 if passenger_index == 0 else -0.6))
        else:
            longitudinal_offset = 0.0

        if kind.endswith("raft"):
            ride_height = float(f32(f32(0.5625) * f32(0.8888889)))
        else:
            ride_height = float(f32(f32(0.5625) / f32(3.0)))
        radians = f32(f32(-vehicle.yaw) * f32(f32(math.pi) / f32(180.0)))
        sin_yaw = minecraft_sin(radians)
        cos_yaw = minecraft_cos(radians)
        x = vehicle.x + longitudinal_offset * sin_yaw
        y = vehicle.y + ride_height - 0.6
        z = vehicle.z + longitudinal_offset * cos_yaw

        delta_yaw = f32((f32(self.player.yaw - vehicle.yaw) + 180.0) % 360.0 - 180.0)
        clamped_yaw = f32(max(-105.0, min(105.0, delta_yaw)))
        yaw = f32(self.player.yaw + clamped_yaw - delta_yaw)
        self.player.x, self.player.y, self.player.z = x, y, z
        self.player.yaw = yaw
        self.physics_state.position = Vec3(x, y, z)
        self.physics_state.velocity = Vec3()
        self.physics_state.yaw = yaw
        return True

    async def _send_vehicle_movement(self, vehicle: EntityState) -> None:
        payload = (
            PacketWriter()
            .write_double(vehicle.x)
            .write_double(vehicle.y)
            .write_double(vehicle.z)
            .write_float(vehicle.yaw)
            .write_float(vehicle.pitch)
            .write_bool(vehicle.on_ground)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_move_vehicle, payload)

    async def _send_paddle_boat(self, vehicle: EntityState) -> None:
        payload = (
            PacketWriter()
            .write_bool(vehicle.boat_left_paddle)
            .write_bool(vehicle.boat_right_paddle)
            .to_bytes()
        )
        await self.send_raw(self.version.packets.serverbound_paddle_boat, payload)

    def _tick_remote_entity_metadata(self) -> None:
        for entity in self.entities.values():
            if entity.type_id != 112:
                continue
            previous = entity.shulker_current_peek
            entity.shulker_previous_peek = previous
            target = f32(float(entity.shulker_peek) * f32(0.01))
            if previous == target:
                continue
            if previous > target:
                entity.shulker_current_peek = f32(
                    max(target, min(1.0, f32(previous - f32(0.05))))
                )
            else:
                entity.shulker_current_peek = f32(
                    max(0.0, min(target, f32(previous + f32(0.05))))
                )

    @staticmethod
    def _metadata_value(entity: EntityState, index: int, default: object) -> object:
        value = entity.metadata.get(index)
        return default if value is None else value.value

    def _happy_ghast_collides_with_player(self, entity: EntityState) -> bool:
        health = self._metadata_value(entity, 9, 20.0)
        baby = self._metadata_value(entity, 16, False)
        stays_still = self._metadata_value(entity, 19, False)
        return (
            isinstance(health, (float, int))
            and health > 0.0
            and not bool(baby)
            and (bool(stays_still) or self.player.y >= entity.y + 4.0)
        )

    @staticmethod
    def _shulker_collision_box(entity: EntityState) -> AABB:
        amount = f32(entity.shulker_current_peek)
        angle = f32(f32(f32(0.5) + amount) * f32(math.pi))
        physical_peek = f32(
            f32(0.5) - f32(minecraft_sin(angle) * f32(0.5))
        )
        opposite_steps = (
            (0.0, 1.0, 0.0),
            (0.0, -1.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 0.0, -1.0),
            (1.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0),
        )
        step_x, step_y, step_z = opposite_steps[entity.shulker_attach_face % 6]
        return AABB(-0.5, 0.0, -0.5, 0.5, 1.0, 0.5).expand_towards(
            Vec3(
                step_x * physical_peek,
                step_y * physical_peek,
                step_z * physical_peek,
            )
        ).move(entity.x, entity.y, entity.z)

    def _hard_entity_collision_boxes(self, query: AABB) -> list[AABB]:
        epsilon = 1.0e-7
        expanded = AABB(
            query.min_x - epsilon,
            query.min_y - epsilon,
            query.min_z - epsilon,
            query.max_x + epsilon,
            query.max_y + epsilon,
            query.max_z + epsilon,
        )
        local_id = self.session.entity_id
        local_root = self._root_vehicle_id(local_id) if local_id is not None else None
        result: list[AABB] = []
        for entity in self.entities.values():
            width = entity.collision_width
            height = entity.collision_height
            if width is None or height is None:
                continue
            if (
                local_id is not None
                and local_root == self._root_vehicle_id(entity.entity_id)
            ):
                continue
            if entity.type_id == 58:
                if not self._happy_ghast_collides_with_player(entity):
                    continue
                half_width = width / 2.0
                box = AABB(
                    entity.x - half_width,
                    entity.y,
                    entity.z - half_width,
                    entity.x + half_width,
                    entity.y + height,
                    entity.z + half_width,
                )
            elif entity.type_id == 112:
                health = self._metadata_value(entity, 9, 30.0)
                if not isinstance(health, (float, int)) or health <= 0.0:
                    continue
                box = self._shulker_collision_box(entity)
            else:
                half_width = width / 2.0
                box = AABB(
                    entity.x - half_width,
                    entity.y,
                    entity.z - half_width,
                    entity.x + half_width,
                    entity.y + height,
                    entity.z + half_width,
                )
            if box.intersects(expanded):
                result.append(box)
        return result

    async def _handle_add_entity(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        entity_uuid = reader.read_uuid()
        type_id = reader.read_varint()
        if type_id < 0:
            raise ProtocolError(f"invalid entity-type registry ID {type_id}")
        position = tuple(reader.read_double() for _ in range(3))
        velocity = reader.read_lp_vec3()
        pitch = self._unpack_degrees(reader.read_byte())
        yaw = self._unpack_degrees(reader.read_byte())
        head_yaw = self._unpack_degrees(reader.read_byte())
        data = reader.read_varint()
        reader.expect_end()
        self._validate_finite((*position, *velocity), "entity position and velocity")

        entity = self._entity_for_state(entity_id)
        entity.entity_uuid = entity_uuid
        entity.type_id = type_id
        dimensions = self.version.hard_collision_dimensions(type_id)
        if dimensions is None:
            entity.collision_width = None
            entity.collision_height = None
        else:
            entity.collision_width, entity.collision_height = dimensions
        entity.x, entity.y, entity.z = position
        entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
        entity.pitch = pitch
        entity.yaw = yaw
        entity.head_yaw = head_yaw
        entity.data = data
        await self.events.emit("entity_add", entity)

    async def _handle_move_entity(
        self,
        payload: bytes,
        *,
        position: bool,
        rotation: bool,
    ) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        delta = (
            tuple(reader.read_short() / 4096.0 for _ in range(3))
            if position
            else None
        )
        angles = (
            (
                self._unpack_degrees(reader.read_byte()),
                self._unpack_degrees(reader.read_byte()),
            )
            if rotation
            else None
        )
        on_ground = reader.read_bool()
        reader.expect_end()

        entity = self.entities.get(entity_id)
        if entity is not None:
            if delta is not None:
                entity.x += delta[0]
                entity.y += delta[1]
                entity.z += delta[2]
            if angles is not None:
                entity.yaw, entity.pitch = angles
            entity.on_ground = on_ground
        await self.events.emit("entity_move", entity_id, entity)

    async def _handle_move_vehicle(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        position = tuple(reader.read_double() for _ in range(3))
        yaw = reader.read_float()
        pitch = reader.read_float()
        reader.expect_end()
        self._validate_finite((*position, yaw, pitch), "vehicle movement values")

        vehicle = self._packet_controlled_root_vehicle()
        if vehicle is None:
            await self.events.emit("vehicle_move", None)
            return
        dx = vehicle.x - position[0]
        dy = vehicle.y - position[1]
        dz = vehicle.z - position[2]
        if dx * dx + dy * dy + dz * dz > 1.0e-5:
            vehicle.x, vehicle.y, vehicle.z = position
            vehicle.yaw, vehicle.pitch = yaw, pitch
            self._sync_local_riding_position()
        await self._send_vehicle_movement(vehicle)
        await self.events.emit("vehicle_move", vehicle)

    def _read_entity_metadata_value(
        self,
        reader: PacketReader,
        serializer_id: int,
    ) -> object:
        if serializer_id == 0:
            return reader.read_byte()
        if serializer_id == 1:
            return reader.read_varint()
        if serializer_id == 2:
            return reader.read_varlong()
        if serializer_id == 3:
            return reader.read_float()
        if serializer_id == 4:
            return reader.read_string()
        if serializer_id == 5:
            return read_anonymous_nbt(reader)
        if serializer_id == 6:
            return read_anonymous_nbt(reader) if reader.read_bool() else None
        if serializer_id == 7:
            try:
                return self._read_item_stack(reader)
            except _UnsupportedItemComponents as error:
                raise _UnsupportedEntityMetadata(str(error)) from error
        if serializer_id == 8:
            return reader.read_bool()
        if serializer_id == 9:
            return tuple(reader.read_float() for _ in range(3))
        if serializer_id == 10:
            return reader.read_position()
        if serializer_id == 11:
            return reader.read_position() if reader.read_bool() else None
        if serializer_id == 12:
            return reader.read_varint() % 6
        if serializer_id == 13:
            return reader.read_uuid() if reader.read_bool() else None
        if serializer_id in (14, 15):
            encoded = reader.read_varint()
            return None if serializer_id == 15 and encoded == 0 else encoded
        if serializer_id == 16:
            raise _UnsupportedEntityMetadata("particle options require a type-specific codec")
        if serializer_id == 17:
            count = reader.read_varint()
            if not 0 <= count <= 16384:
                raise ProtocolError(f"invalid entity particle count {count}")
            if count:
                raise _UnsupportedEntityMetadata(
                    "non-empty particle lists require type-specific codecs"
                )
            return ()
        if serializer_id == 18:
            return tuple(reader.read_varint() for _ in range(3))
        if serializer_id == 19:
            encoded = reader.read_varint()
            return None if encoded == 0 else encoded - 1
        if serializer_id == 20:
            return reader.read_varint()

        if self.version.protocol == 774:
            optional_global, vector, quaternion, profile, arm = 29, 35, 36, 37, 38
        else:
            optional_global, vector, quaternion, profile, arm = 33, 39, 40, 41, 42
        if 21 <= serializer_id < vector and serializer_id != optional_global:
            return reader.read_varint()
        if serializer_id == optional_global:
            if not reader.read_bool():
                return None
            return reader.read_string(), reader.read_position()
        if serializer_id == vector:
            return tuple(reader.read_float() for _ in range(3))
        if serializer_id == quaternion:
            return tuple(reader.read_float() for _ in range(4))
        if serializer_id == profile:
            raise _UnsupportedEntityMetadata(
                "resolvable profiles require profile-property and skin codecs"
            )
        if serializer_id == arm:
            return reader.read_varint()
        raise ProtocolError(f"unknown entity metadata serializer {serializer_id}")

    async def _handle_set_entity_data(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        updates: list[tuple[int, EntityMetadataValue]] = []
        while True:
            index = reader.read_unsigned_byte()
            if index == 0xFF:
                break
            serializer_id = reader.read_varint()
            if serializer_id < 0:
                raise ProtocolError(
                    f"invalid entity metadata serializer {serializer_id}"
                )
            try:
                value = self._read_entity_metadata_value(reader, serializer_id)
            except _UnsupportedEntityMetadata as error:
                await self.events.emit(
                    "entity_data_unparsed",
                    entity_id,
                    index,
                    serializer_id,
                    str(error),
                    payload,
                )
                return
            updates.append((index, EntityMetadataValue(serializer_id, value)))
        reader.expect_end()

        entity = None if entity_id == self.session.entity_id else self.entities.get(entity_id)
        metadata = self.player.metadata if entity_id == self.session.entity_id else None
        if entity is not None:
            metadata = entity.metadata
        if metadata is not None:
            for index, value in updates:
                metadata[index] = value
        if entity is not None and entity.type_id == 112:
            for index, value in updates:
                if index == 16 and value.serializer_id == 12:
                    entity.shulker_attach_face = int(value.value)
                elif index == 17 and value.serializer_id == 0:
                    entity.shulker_peek = int(value.value)
        await self.events.emit("entity_data", entity_id, tuple(updates), entity)

    async def _handle_remove_entities(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        count = reader.read_varint()
        if not 0 <= count <= 65536:
            raise ProtocolError(f"invalid removed-entity count {count}")
        entity_ids = tuple(reader.read_varint() for _ in range(count))
        reader.expect_end()

        removed = tuple(
            entity
            for entity_id in entity_ids
            if (entity := self.entities.pop(entity_id, None)) is not None
        )
        removed_ids = set(entity_ids)
        if self.player.vehicle_id in removed_ids:
            self.player.vehicle_id = None
        if any(passenger_id in removed_ids for passenger_id in self.player.passenger_ids):
            self.player.passenger_ids = tuple(
                passenger_id
                for passenger_id in self.player.passenger_ids
                if passenger_id not in removed_ids
            )
        for entity in self.entities.values():
            if entity.vehicle_id in removed_ids:
                entity.vehicle_id = None
            if any(passenger_id in removed_ids for passenger_id in entity.passenger_ids):
                entity.passenger_ids = tuple(
                    passenger_id
                    for passenger_id in entity.passenger_ids
                    if passenger_id not in removed_ids
                )
        await self.events.emit("entities_remove", entity_ids, removed)

    async def _handle_set_entity_motion(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        velocity = reader.read_lp_vec3()
        reader.expect_end()
        self._validate_finite(velocity, "entity velocity")
        entity = self.entities.get(entity_id)
        if entity is not None:
            entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
        if entity_id == self.session.entity_id:
            self.player.velocity_x, self.player.velocity_y, self.player.velocity_z = velocity
            self.physics_state.velocity = Vec3(*velocity)
        await self.events.emit("entity_motion", entity_id, velocity, entity)

    async def _handle_set_passengers(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        vehicle_id = reader.read_varint()
        count = reader.read_varint()
        if not 0 <= count <= 65536:
            raise ProtocolError(f"invalid passenger count {count}")
        passenger_ids = tuple(reader.read_varint() for _ in range(count))
        reader.expect_end()

        previous_ids = set(self._vehicle_passenger_ids(vehicle_id))
        next_ids = set(passenger_ids)
        self._set_vehicle_passenger_ids(vehicle_id, passenger_ids)
        for passenger_id in previous_ids - next_ids:
            if self._passenger_vehicle_id(passenger_id) == vehicle_id:
                self._set_passenger_vehicle_id(passenger_id, None)
        for passenger_id in passenger_ids:
            previous_vehicle_id = self._passenger_vehicle_id(passenger_id)
            if previous_vehicle_id is not None and previous_vehicle_id != vehicle_id:
                previous_passengers = self._vehicle_passenger_ids(previous_vehicle_id)
                self._set_vehicle_passenger_ids(
                    previous_vehicle_id,
                    tuple(
                        existing_id
                        for existing_id in previous_passengers
                        if existing_id != passenger_id
                    ),
                )
            self._set_passenger_vehicle_id(passenger_id, vehicle_id)
        self._sync_local_riding_position()
        await self.events.emit("passengers", vehicle_id, passenger_ids)

    def _read_item_stack(self, reader: PacketReader) -> ItemStack:
        count = reader.read_varint()
        if count <= 0:
            return ItemStack()
        item_id = reader.read_varint()
        if item_id < 0:
            raise ProtocolError(f"invalid item registry ID {item_id}")
        positive_count = reader.read_varint()
        negative_count = reader.read_varint()
        if not 0 <= positive_count <= 65536 or not 0 <= negative_count <= 65536:
            raise ProtocolError(
                "item data-component counts must be between 0 and 65536"
            )
        if positive_count:
            raise _UnsupportedItemComponents(
                f"{positive_count} added data component(s) are not self-delimiting"
            )
        removed_component_ids = tuple(
            reader.read_varint() for _ in range(negative_count)
        )
        if any(component_id < 0 for component_id in removed_component_ids):
            raise ProtocolError("invalid removed data-component registry ID")
        identifier = self.version.item_identifier(item_id)
        return ItemStack(count, item_id, identifier, removed_component_ids)

    async def _handle_set_equipment(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        updates: list[tuple[EquipmentSlot, ItemStack]] = []
        try:
            while True:
                encoded_slot = reader.read_unsigned_byte()
                slot_id = encoded_slot & 0x7F
                try:
                    slot = EquipmentSlot(slot_id)
                except ValueError as error:
                    raise ProtocolError(f"invalid equipment slot {slot_id}") from error
                updates.append((slot, self._read_item_stack(reader)))
                if not encoded_slot & 0x80:
                    break
        except _UnsupportedItemComponents as error:
            await self.events.emit("equipment_unparsed", entity_id, str(error), payload)
            return
        reader.expect_end()

        entity = self._entity_for_state(entity_id)
        for slot, item in updates:
            entity.equipment[slot] = item
        if entity_id == self.session.entity_id:
            for slot, item in updates:
                self.player.equipment[slot] = item
            feet = self.player.equipment.get(EquipmentSlot.FEET)
            self.physics_state.wearing_leather_boots = (
                feet is not None and feet.identifier == "minecraft:leather_boots"
            )
        await self.events.emit("equipment", entity_id, tuple(updates))

    async def _handle_set_player_inventory(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        slot = reader.read_varint()
        try:
            item = self._read_item_stack(reader)
        except _UnsupportedItemComponents as error:
            await self.events.emit("inventory_unparsed", slot, str(error), payload)
            return
        reader.expect_end()
        self.player.inventory[slot] = item
        await self.events.emit("inventory", slot, item)

    def _container_state(self, container_id: int) -> ContainerState:
        state = self.containers.get(container_id)
        if state is None:
            state = ContainerState(container_id)
            self.containers[container_id] = state
        return state

    async def _handle_open_screen(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        menu_type_id = reader.read_varint()
        title_data = reader.read_remaining()
        state = ContainerState(
            container_id=container_id,
            menu_type_id=menu_type_id,
            title_data=title_data,
        )
        self.containers[container_id] = state
        self._active_container_id = container_id
        await self.events.emit("container_open", state)

    async def _handle_container_content(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        state_id = reader.read_varint()
        state = self._container_state(container_id)
        state.state_id = state_id
        state.content_data = reader.read_remaining()
        state.slots.clear()
        state.carried_item_data = b""
        state.is_open = True
        self._active_container_id = container_id
        await self.events.emit("container_content", state)

    async def _handle_container_slot(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        state_id = reader.read_varint()
        slot = reader.read_short()
        state = self._container_state(container_id)
        state.state_id = state_id
        state.slots[slot] = reader.read_remaining()
        state.is_open = True
        self._active_container_id = container_id
        await self.events.emit("container_slot", state, slot)

    async def _handle_container_close(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        container_id = reader.read_varint()
        reader.expect_end()
        state = self._container_state(container_id)
        state.is_open = False
        if self._active_container_id == container_id:
            self._active_container_id = None
        await self.events.emit("container_close", state)

    async def _handle_teleport_entity(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        position = [reader.read_double() for _ in range(3)]
        velocity = [reader.read_double() for _ in range(3)]
        packet_yaw = reader.read_float()
        packet_pitch = reader.read_float()
        relative = reader.read_unsigned_int()
        on_ground = reader.read_bool()
        reader.expect_end()
        self._validate_finite(
            (*position, *velocity, packet_yaw, packet_pitch),
            "entity teleport values",
        )

        entity = self.entities.get(entity_id)
        if entity is not None:
            current_position = (entity.x, entity.y, entity.z)
            current_velocity = (
                entity.velocity_x,
                entity.velocity_y,
                entity.velocity_z,
            )
            for index in range(3):
                if relative & (1 << index):
                    position[index] += current_position[index]

            current_yaw = f32(entity.yaw)
            current_pitch = f32(entity.pitch)
            yaw = (
                f32(packet_yaw + current_yaw)
                if relative & (1 << 3)
                else f32(packet_yaw)
            )
            pitch_input = (
                packet_pitch + current_pitch
                if relative & (1 << 4)
                else packet_pitch
            )
            pitch = f32(max(-90.0, min(90.0, pitch_input)))

            rotated_current = list(current_velocity)
            if relative & (1 << 8):
                rotate_x = f32(current_pitch - pitch)
                rotate_y = f32(current_yaw - yaw)
                radians_x = f32(math.radians(rotate_x))
                cx = minecraft_cos(radians_x)
                sx = minecraft_sin(radians_x)
                x, y, z = rotated_current
                y, z = y * cx + z * sx, z * cx - y * sx
                radians_y = f32(math.radians(rotate_y))
                cy = minecraft_cos(radians_y)
                sy = minecraft_sin(radians_y)
                x, z = x * cy + z * sy, z * cy - x * sy
                rotated_current = [x, y, z]
            for index in range(3):
                if relative & (1 << (index + 5)):
                    velocity[index] += rotated_current[index]

            entity.x, entity.y, entity.z = position
            entity.velocity_x, entity.velocity_y, entity.velocity_z = velocity
            entity.yaw = yaw
            entity.pitch = pitch
            entity.on_ground = on_ground
        await self.events.emit("entity_teleport", entity_id, entity, relative)

    async def _handle_update_mob_effect(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        effect_id = reader.read_varint()
        amplifier = reader.read_varint()
        duration = reader.read_varint()
        flags = reader.read_unsigned_byte()
        reader.expect_end()

        if effect_id < 0:
            raise ProtocolError(f"invalid status-effect registry ID {effect_id}")
        if not 0 <= amplifier <= 255:
            raise ProtocolError(f"invalid status-effect amplifier {amplifier}")
        if duration < -1:
            raise ProtocolError(f"invalid status-effect duration {duration}")
        if flags & ~0x0F:
            raise ProtocolError(f"invalid status-effect flags {flags:#04x}")

        identifier = _MOVEMENT_EFFECTS.get(effect_id)
        effect = StatusEffect(
            amplifier=amplifier,
            duration=duration,
            ambient=bool(flags & 0x01),
            show_particles=bool(flags & 0x02),
            show_icon=bool(flags & 0x04),
            keep_fading=bool(flags & 0x08),
        )
        if entity_id == self.session.entity_id and identifier is not None:
            effect = self.physics_state.set_status_effect(
                identifier,
                amplifier=effect.amplifier,
                duration=effect.duration,
                ambient=effect.ambient,
                show_particles=effect.show_particles,
                show_icon=effect.show_icon,
                keep_fading=effect.keep_fading,
            )
        await self.events.emit("effect_update", entity_id, effect_id, identifier, effect)

    async def _handle_remove_mob_effect(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        effect_id = reader.read_varint()
        reader.expect_end()
        if effect_id < 0:
            raise ProtocolError(f"invalid status-effect registry ID {effect_id}")

        identifier = _MOVEMENT_EFFECTS.get(effect_id)
        removed: StatusEffect | None = None
        if entity_id == self.session.entity_id and identifier is not None:
            removed = self.physics_state.remove_status_effect(identifier)
        await self.events.emit("effect_remove", entity_id, effect_id, identifier, removed)

    async def _handle_update_attributes(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        count = reader.read_varint()
        if not 0 <= count <= 128:
            raise ProtocolError(f"invalid attribute update count {count}")

        identifiers = _MOVEMENT_ATTRIBUTES.get(self.version.protocol, {})
        updates: list[AttributeUpdate] = []
        for _ in range(count):
            registry_id = reader.read_varint()
            if registry_id < 0:
                raise ProtocolError(f"invalid attribute registry ID {registry_id}")
            base = reader.read_double()
            if not math.isfinite(base):
                raise ProtocolError("attribute base value must be finite")
            modifier_count = reader.read_varint()
            if not 0 <= modifier_count <= 1024:
                raise ProtocolError(f"invalid attribute modifier count {modifier_count}")

            modifiers: list[AttributeModifierUpdate] = []
            for _ in range(modifier_count):
                modifier_id = reader.read_string(max_chars=32767)
                amount = reader.read_double()
                operation = reader.read_varint()
                if not math.isfinite(amount):
                    raise ProtocolError("attribute modifier amount must be finite")
                if operation not in (0, 1, 2):
                    raise ProtocolError(f"invalid attribute modifier operation {operation}")
                modifiers.append(AttributeModifierUpdate(modifier_id, amount, operation))

            identifier = identifiers.get(registry_id)
            value = self._calculate_attribute_value(base, modifiers)
            if identifier is not None:
                minimum, maximum = _MOVEMENT_ATTRIBUTE_RANGES[identifier]
                value = min(max(value, minimum), maximum)
            updates.append(
                AttributeUpdate(
                    registry_id=registry_id,
                    identifier=identifier,
                    base=base,
                    modifiers=tuple(modifiers),
                    value=value,
                )
            )
        reader.expect_end()

        if entity_id == self.session.entity_id:
            for update in updates:
                if update.identifier is not None:
                    value = update.value
                    if update.identifier == "minecraft:movement_speed":
                        value = self._calculate_attribute_value(
                            update.base,
                            (
                                modifier
                                for modifier in update.modifiers
                                if modifier.identifier != _SPRINTING_SPEED_MODIFIER
                            ),
                        )
                        minimum, maximum = _MOVEMENT_ATTRIBUTE_RANGES[update.identifier]
                        value = min(max(value, minimum), maximum)
                    self.physics_state.set_attribute(update.identifier, value)
        await self.events.emit("attributes", entity_id, tuple(updates))

    @staticmethod
    def _calculate_attribute_value(
        base: float, modifiers: Iterable[AttributeModifierUpdate]
    ) -> float:
        modifiers = tuple(modifiers)
        base_after_add = base
        for modifier in modifiers:
            if modifier.operation == 0:
                base_after_add += modifier.amount
        value = base_after_add
        for modifier in modifiers:
            if modifier.operation == 1:
                value += base_after_add * modifier.amount
        for modifier in modifiers:
            if modifier.operation == 2:
                value *= 1.0 + modifier.amount
        if not math.isfinite(value):
            raise ProtocolError("computed attribute value must be finite")
        return value

    def _handle_play_login(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        # 新一轮 PLAY：玩家列表从零开始，随后到来的名单算「初始名单」。
        self.players.clear()
        self._roster_synced = False
        self._roster_deadline = time.monotonic() + _ROSTER_GRACE
        self.session.entity_id = reader.read_int()
        self.session.hardcore = reader.read_bool()
        level_count = reader.read_varint()
        if not 0 <= level_count <= 1024:
            raise ProtocolError(f"invalid level count {level_count}")
        for _ in range(level_count):
            reader.read_string(max_chars=32767)
        reader.read_varint()  # Max players is retained by the server for compatibility.
        self.session.view_distance = reader.read_varint()
        self.session.simulation_distance = reader.read_varint()
        reader.read_bool()  # Reduced debug info.
        reader.read_bool()  # Show death screen.
        reader.read_bool()  # Limited crafting.
        self._read_spawn_info(reader)
        if self.version.protocol >= 776:
            self.session.online_mode = reader.read_bool()
        self.session.enforces_secure_chat = reader.read_bool()
        reader.expect_end()

    def _handle_respawn(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        self._read_spawn_info(reader)
        reader.read_unsigned_byte()  # Data components to retain.
        reader.expect_end()
        self.world.chunks.clear()
        self.entities.clear()
        self.containers.clear()
        self._active_container_id = None
        self.player.vehicle_id = None
        self.player.passenger_ids = ()
        self.player.loaded = False
        self.player.gliding = False
        self.player.dead = False
        self.physics_state.gliding = False
        self.physics_state.gliding_ticks = 0
        self.world_ready.clear()
        self.loaded.clear()

    async def _handle_set_health(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        health = reader.read_float()
        food = reader.read_varint()
        saturation = reader.read_float()
        reader.expect_end()
        self.player.health = health
        self.player.food = food
        self.player.saturation = saturation
        await self.events.emit("health", health, food, saturation)
        # 血量归零也是死亡信号，但不如 combat_kill 权威：服务端按 tick 边界
        # 补发，且插件可以缩放血量。死亡窗口内会反复收到 0，用 dead 去重。
        if health <= 0.0 and not self.player.dead:
            self.player.dead = True
            await self.events.emit("death", None)

    async def _handle_player_combat_kill(self, payload: bytes) -> None:
        """Combat Death：服务端要求客户端显示死亡界面，即权威的死亡信号。"""
        reader = PacketReader(payload)
        entity_id = reader.read_varint()
        message = read_anonymous_nbt(reader)
        reader.expect_end()
        if self.session.entity_id is not None and entity_id != self.session.entity_id:
            return  # 别人的死亡（旁观视角等）不算自己死了。
        self.player.health = 0.0
        if self.player.dead:
            return
        self.player.dead = True
        await self.events.emit("death", message)

    async def _handle_player_info_update(self, payload: bytes) -> None:
        """玩家列表更新：维护 :attr:`players`，新玩家发出 ``player_join``。

        整个包先解析到一边再落库：动作位集是定长的（1.21.4+ 八个动作正好一
        字节），下一个版本多加一个动作就会变成两字节，届时这里读出来的都是错
        位数据。要求 ``expect_end()`` 恰好读完，一旦对不上就整包丢弃并发出
        ``player_list_unparsed``——宁可没有加入/退出事件，也不要拿错位的字节
        编出人名来。
        """

        try:
            decoded = self._decode_player_info(payload)
        except (ProtocolError, ValueError) as error:
            await self.events.emit("player_list_unparsed", str(error), payload)
            return
        joined: list[PlayerListEntry] = []
        for entry_uuid, name, game_mode, latency, listed, display_name in decoded:
            entry = self.players.get(entry_uuid)
            if entry is None:
                entry = PlayerListEntry(uuid=entry_uuid)
                self.players[entry_uuid] = entry
                if name is not None:
                    joined.append(entry)
            if name is not None:
                entry.name = name
            if game_mode is not None:
                entry.game_mode = game_mode
            if latency is not None:
                entry.latency = latency
            if listed is not None:
                entry.listed = listed
            if display_name is not None:
                entry.display_name = display_name
        # 刚进服时服务端把全部在线玩家发过来，那不是「有人加入」。
        if not self._roster_synced or time.monotonic() < self._roster_deadline:
            self._roster_synced = True
            await self.events.emit("player_list", tuple(self.players.values()))
            return
        for entry in joined:
            if entry.uuid == self.uuid:
                continue  # 自己出现在列表里不算加入
            await self.events.emit("player_join", entry)

    def _decode_player_info(
        self, payload: bytes
    ) -> list[tuple[uuid.UUID, str | None, int | None, int | None, bool | None, str | None]]:
        reader = PacketReader(payload)
        actions = reader.read_unsigned_byte()
        count = reader.read_varint()
        if not 0 <= count <= 4096:
            raise ProtocolError(f"invalid player info entry count {count}")
        decoded: list[
            tuple[uuid.UUID, str | None, int | None, int | None, bool | None, str | None]
        ] = []
        for _ in range(count):
            entry_uuid = reader.read_uuid()
            name: str | None = None
            game_mode: int | None = None
            latency: int | None = None
            listed: bool | None = None
            display_name: str | None = None
            if actions & _PLAYER_INFO_ADD:
                name = reader.read_string(max_chars=16)
                if not name or any(character < " " for character in name):
                    raise ProtocolError(f"invalid player name {name!r}")
                properties = reader.read_varint()
                if not 0 <= properties <= 64:
                    raise ProtocolError(f"invalid property count {properties}")
                for _ in range(properties):
                    reader.read_string(max_chars=64)  # Property name.
                    reader.read_string(max_chars=32767)  # Value.
                    if reader.read_bool():
                        reader.read_string(max_chars=32767)  # Signature.
            if actions & _PLAYER_INFO_INIT_CHAT and reader.read_bool():
                reader.read_uuid()  # Chat session ID.
                reader.read_long()  # Public key expiry.
                reader.read_bytes(max_length=1 << 16)  # Public key.
                reader.read_bytes(max_length=1 << 16)  # Key signature.
            if actions & _PLAYER_INFO_GAME_MODE:
                game_mode = reader.read_varint()
            if actions & _PLAYER_INFO_LISTED:
                listed = reader.read_bool()
            if actions & _PLAYER_INFO_LATENCY:
                latency = reader.read_varint()
            if actions & _PLAYER_INFO_DISPLAY_NAME:
                display_name = (
                    plain_text(read_anonymous_nbt(reader)) if reader.read_bool() else ""
                )
            if actions & _PLAYER_INFO_LIST_ORDER:
                reader.read_varint()  # Tab-list sort order.
            if actions & _PLAYER_INFO_HAT:
                reader.read_bool()  # Show hat.
            decoded.append(
                (entry_uuid, name, game_mode, latency, listed, display_name)
            )
        reader.expect_end()
        return decoded

    async def _handle_player_info_remove(self, payload: bytes) -> None:
        """玩家列表移除：发出 ``player_leave``。"""

        try:
            reader = PacketReader(payload)
            count = reader.read_varint()
            if not 0 <= count <= 4096:
                raise ProtocolError(f"invalid player removal count {count}")
            removed_uuids = [reader.read_uuid() for _ in range(count)]
            reader.expect_end()
        except (ProtocolError, ValueError) as error:
            await self.events.emit("player_list_unparsed", str(error), payload)
            return
        for entry_uuid in removed_uuids:
            entry = self.players.pop(entry_uuid, None)
            if entry is None or entry.uuid == self.uuid or not entry.name:
                continue
            await self.events.emit("player_leave", entry)

    def _read_spawn_info(self, reader: PacketReader) -> None:
        dimension_type_id = reader.read_varint()
        self.session.dimension_type_id = dimension_type_id
        self.session.dimension_name = reader.read_string(max_chars=32767)
        reader.read_long()  # Hashed seed.
        self._set_game_mode(reader.read_byte())
        self.session.previous_game_mode = reader.read_byte()
        self.session.is_debug = reader.read_bool()
        self.session.is_flat = reader.read_bool()
        if reader.read_bool():
            reader.read_string(max_chars=32767)
            reader.read_position()
        reader.read_varint()  # Portal cooldown.
        self.session.sea_level = reader.read_varint()
        bounds = self.registries.dimension_bounds(dimension_type_id)
        if bounds is not None:
            self.world.set_dimension_bounds(*bounds)

    def _set_game_mode(self, game_mode: int) -> None:
        if not 0 <= game_mode <= 3:
            raise ProtocolError(f"invalid game mode {game_mode}")
        entering_spectator = game_mode == 3 and not self.physics_state.spectator
        self.session.game_mode = game_mode
        self.physics_state.spectator = game_mode == 3
        if entering_spectator:
            velocity = self.physics_state.velocity
            self.physics_state.velocity = Vec3(velocity.x, 0.0, velocity.z)

    @staticmethod
    def _skip_long_array(reader: PacketReader, *, limit: int) -> None:
        count = reader.read_varint()
        if not 0 <= count <= limit:
            raise ProtocolError(f"invalid long array count {count}")
        for _ in range(count):
            reader.read_long()

    @staticmethod
    def _signed_bits(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return value - (1 << bits) if value & sign else value

    async def _handle_position(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        teleport_id = reader.read_varint()
        values = [reader.read_double() for _ in range(6)]
        packet_yaw = reader.read_float()
        packet_pitch = reader.read_float()
        relative = reader.read_unsigned_int()
        reader.expect_end()

        position = values[:3]
        packet_velocity = values[3:]
        current_position = (self.player.x, self.player.y, self.player.z)
        current_velocity = (
            self.player.velocity_x,
            self.player.velocity_y,
            self.player.velocity_z,
        )
        for index in range(3):
            if relative & (1 << index):
                position[index] += current_position[index]

        # Relative rotation is resolved before applying ROTATE_DELTA.  This
        # mirrors PositionMoveRotation.calculateAbsolute in the 26.2 server:
        # X_ROT is clamped to [-90, 90], while Y_ROT is left unwrapped.
        current_yaw = f32(self.player.yaw)
        current_pitch = f32(self.player.pitch)
        yaw = f32(packet_yaw + current_yaw) if relative & (1 << 3) else f32(packet_yaw)
        pitch_input = packet_pitch + current_pitch if relative & (1 << 4) else packet_pitch
        pitch = f32(max(-90.0, min(90.0, pitch_input)))

        # DELTA_* resolves as ``packet + current`` only for the flagged axes.
        # ROTATE_DELTA rotates the *current* movement vector before that
        # per-axis resolution; it does not rotate the packet's delta itself.
        rotated_current = list(current_velocity)
        if relative & (1 << 8):  # Relative.ROTATE_DELTA
            # The current movement vector is rotated from its old orientation
            # into the resulting player orientation, first around X then around
            # Y (the order used by Vec3.xRot(...).yRot(...)).
            # Keep the subtraction in Java-float precision before converting
            # to radians and looking up Minecraft's sine/cosine table.
            rotate_x = f32(current_pitch - pitch)
            rotate_y = f32(current_yaw - yaw)
            radians_x = f32(math.radians(rotate_x))
            cx = minecraft_cos(radians_x)
            sx = minecraft_sin(radians_x)
            x, y, z = rotated_current
            y, z = y * cx + z * sx, z * cx - y * sx
            radians_y = f32(math.radians(rotate_y))
            cy = minecraft_cos(radians_y)
            sy = minecraft_sin(radians_y)
            x, z = x * cy + z * sy, z * cy - x * sy
            rotated_current = [x, y, z]
        velocity = list(packet_velocity)
        for index in range(3):
            if relative & (1 << (index + 5)):
                velocity[index] += rotated_current[index]

        self.player.x, self.player.y, self.player.z = position
        self.player.velocity_x, self.player.velocity_y, self.player.velocity_z = velocity
        self.player.yaw, self.player.pitch = yaw, pitch
        self.physics_state.position = Vec3(*position)
        self.physics_state.velocity = Vec3(*velocity)
        self.physics_state.yaw = yaw
        self.physics_state.pitch = pitch
        self.physics_state.on_ground = self.player.on_ground
        self.physics_state.horizontal_collision = self.player.horizontal_collision
        await self.send_raw(
            self.version.packets.serverbound_teleport_confirm,
            PacketWriter().write_varint(teleport_id).to_bytes(),
        )
        if not self.player.loaded:
            self.player.loaded = True
            self.loaded.set()
            await self.send_raw(self.version.packets.serverbound_player_loaded)
        await self.events.emit("position", self.player)

    async def _send_client_information(self) -> None:
        payload = (
            PacketWriter()
            .write_string("en_us", max_chars=16)
            .write_byte(10)
            .write_varint(0)
            .write_bool(True)
            .write_unsigned_byte(0x7F)
            .write_varint(1)
            .write_bool(False)
            .write_bool(True)
            .write_varint(0)
            .to_bytes()
        )
        await self.send_raw(0x00, payload)

    def _read_login_success(self, payload: bytes) -> None:
        reader = PacketReader(payload)
        self.uuid = reader.read_uuid()
        self.username = reader.read_string(max_chars=16)
        property_count = reader.read_varint()
        if property_count < 0 or property_count > 1024:
            raise ProtocolError("invalid login property count")
        for _ in range(property_count):
            reader.read_string()
            reader.read_string()
            if reader.read_bool():
                reader.read_string()
        if self.version.protocol >= 776:
            self.session_id = reader.read_uuid()
        reader.expect_end()

    @staticmethod
    def _best_effort_reason(payload: bytes) -> str:
        reader = PacketReader(payload)
        try:
            text = reader.read_string(max_chars=262144)
            if not reader.remaining:
                try:
                    component = json.loads(text)
                    if isinstance(component, dict) and isinstance(component.get("text"), str):
                        return component["text"]
                except json.JSONDecodeError:
                    pass
                return text
        except ProtocolError:
            pass
        return f"server disconnected (encoded reason: {payload.hex()})"


async def connect(
    host: str,
    *,
    port: int = 25565,
    handshake_host: str | None = None,
    username: str = "ProtoBot",
    version: str | VersionSpec = "26.2",
    timeout: float = 30.0,
    resolve_srv: bool = True,
    access_token: str | None = None,
    profile_uuid: str | uuid.UUID | None = None,
    session_server: str = "https://sessionserver.mojang.com",
    loader: Loader | str = Loader.VANILLA,
    mods: Mapping[str, str] | None = None,
    loader_protocol: int = 0,
    configuration_channels: Mapping[str, ChannelSpec | str | int] | None = None,
    play_channels: Mapping[str, ChannelSpec | str | int] | None = None,
    display_names: Mapping[str, str] | None = None,
    common_versions: Iterable[int] = (1,),
    fabric_registry_handler: Callable[[bytes], bool] | None = None,
    velocity_secret: str | bytes | None = None,
    velocity_player_ip: str = "127.0.0.1",
    accept_transfers: bool = True,
    modlist: ModListAdapter | None = None,
    block_states: BlockStateRegistry | None = None,
    block_state_report: str | None = None,
    block_state_table: str | None = None,
    physics_engine: PhysicsEngine | None = None,
    physics_attributes: PhysicsAttributes | None = None,
) -> Bot:
    """Connect a bot (offline or online) and return it after the initial spawn position."""
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout must be a finite positive number")
    bot = Bot(
        host,
        port=port,
        handshake_host=handshake_host,
        username=username,
        version=version,
        connect_timeout=timeout,
        resolve_srv=resolve_srv,
        access_token=access_token,
        profile_uuid=profile_uuid,
        session_server=session_server,
        loader=loader,
        mods=mods,
        loader_protocol=loader_protocol,
        configuration_channels=configuration_channels,
        play_channels=play_channels,
        display_names=display_names,
        common_versions=common_versions,
        fabric_registry_handler=fabric_registry_handler,
        velocity_secret=velocity_secret,
        velocity_player_ip=velocity_player_ip,
        accept_transfers=accept_transfers,
        modlist=modlist,
        block_states=block_states,
        block_state_report=block_state_report,
        block_state_table=block_state_table,
        physics_engine=physics_engine,
        physics_attributes=physics_attributes,
    )
    try:
        await bot.start()
        await bot.wait_ready(timeout=timeout)
        await bot.wait_loaded(timeout=timeout)
        return bot
    except BaseException:
        await bot.close()
        raise
