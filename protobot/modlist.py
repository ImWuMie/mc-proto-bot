"""Loader negotiation helpers for configuration and login custom payloads.

The vanilla protocol has no portable ``mod list`` packet.  The three loader
families supported by ProtoBot use different, loader-owned payloads instead:

* Forge 1.21.11--26.2 marks the handshake host with ``FORGE``, exchanges
  dynamic channels through ``minecraft:register``/``minecraft:unregister``,
  and uses discriminator-framed (0 through 6) ``forge:handshake`` payloads.
* NeoForge and Fabric use the common ``minecraft:register``, ``c:version`` and
  ``c:register`` negotiation.  NeoForge additionally queries
  ``neoforge:register`` with a map of typed network components.
* Fabric's registry synchronisation is intentionally not guessed.  Applications
  must provide a handler that decodes, validates and applies the registry map;
  the adapter acknowledges the sync only when that handler succeeds.

The module is deliberately transport agnostic.  :meth:`ModListAdapter.handle`
keeps the historical ``bytes | None`` API for responses sent on the same
channel.  :meth:`ModListAdapter.handle_response` returns a
:class:`PayloadResponse` when a response must be sent on another channel (for
example Fabric's ``fabric:registry/sync/complete``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import ProtocolError
from .protocol.codec import PacketReader, PacketWriter


class Loader(StrEnum):
    VANILLA = "vanilla"
    FORGE = "forge"
    NEOFORGE = "neoforge"
    FABRIC = "fabric"


class ChannelFlow(StrEnum):
    """NeoForge payload direction.

    ``None`` on :class:`ChannelSpec.flow` means bidirectional, matching the
    absent optional ``PacketFlow`` in NeoForge's wire codec.
    """

    SERVERBOUND = "serverbound"
    CLIENTBOUND = "clientbound"


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """A NeoForge/Fabric channel declaration.

    NeoForge compares the version, direction and optional flag during channel
    negotiation.  Fabric only uses the identifier, but accepting the same
    declaration makes a profile reusable between the two loaders.
    """

    version: str = "1"
    flow: ChannelFlow | str | None = None
    optional: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", str(self.version))
        if not self.version:
            raise ValueError("channel version cannot be empty")
        if self.flow is not None:
            object.__setattr__(self, "flow", ChannelFlow(self.flow))


@dataclass(frozen=True, slots=True)
class PayloadResponse:
    """A loader response and the channel on which it must be sent."""

    channel: str
    data: bytes


_NAMESPACE_RE = re.compile(r"^[a-z0-9_.-]+$")
_PATH_RE = re.compile(r"^[a-z0-9_./-]+$")
_MAX_CHANNELS = 8192
_MAX_MODS = 4096
_MAX_STRING = 256
_NEOFORGE_CORE_CONFIGURATION_CHANNELS = {
    "neoforge:extensible_enum_data": ChannelSpec("1", ChannelFlow.CLIENTBOUND, True),
    "neoforge:extensible_enum_ack": ChannelSpec("1", ChannelFlow.SERVERBOUND, True),
}
_NEOFORGE_CORE_PLAY_CHANNELS = {
    "neoforge:recipe_content": ChannelSpec("1", ChannelFlow.CLIENTBOUND, True),
}


def _channel(value: str) -> str:
    """Validate and return a Minecraft resource identifier.

    Java's ``Identifier`` parser rejects uppercase characters and malformed
    namespace/path components.  Silently lower-casing here would make a bot
    advertise a channel different from the one requested by its caller.
    """

    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"custom payload channel must be namespaced: {value!r}")
    namespace, path = value.split(":", 1)
    if (
        not namespace
        or not path
        or len(namespace) > _MAX_STRING
        or len(path) > _MAX_STRING
        or _NAMESPACE_RE.fullmatch(namespace) is None
        or _PATH_RE.fullmatch(path) is None
    ):
        raise ValueError(f"invalid custom payload channel: {value!r}")
    return f"{namespace}:{path}"


def _normalize_specs(
    values: Mapping[str, ChannelSpec | str | int] | Iterable[str] | None,
) -> dict[str, ChannelSpec]:
    if values is None:
        return {}
    if isinstance(values, Mapping):
        items = values.items()
    else:
        items = ((value, ChannelSpec()) for value in values)
    result: dict[str, ChannelSpec] = {}
    for name, spec in items:
        name = _channel(str(name))
        if isinstance(spec, ChannelSpec):
            normalized = spec
        elif isinstance(spec, (str, int)):
            normalized = ChannelSpec(str(spec))
        else:
            raise TypeError(f"invalid channel declaration for {name!r}")
        result[name] = normalized
    if len(result) > _MAX_CHANNELS:
        raise ValueError(f"too many custom payload channels (maximum {_MAX_CHANNELS})")
    return result


@dataclass(slots=True)
class ModListAdapter:
    """Describe a loader profile and answer its standard negotiation frames.

    ``mods`` maps a mod id to a version string.  Forge's 26.x wire format also
    carries a display name; when no display-name mapping is supplied the mod id
    is used as a deterministic, valid display name.  NeoForge and Fabric do
    not exchange this mapping on the wire; they negotiate payload channels.

    ``configuration_channels`` and ``play_channels`` are optional declarations
    for NeoForge/Fabric.  Values may be :class:`ChannelSpec`, a version string,
    or a bare channel name when passed as an iterable.  For Forge they are also
    included in the channel-version map with version ``protocol_version``.

    ``fabric_registry_handler`` must apply a complete Fabric registry snapshot
    and return exactly ``True`` before the adapter sends the completion payload.
    """

    loader: Loader = Loader.VANILLA
    mods: dict[str, str] = field(default_factory=dict)
    # Forge/loader network marker in the target releases is 0 (``FORGE``).
    protocol_version: int = 0
    configuration_channels: dict[str, ChannelSpec] = field(default_factory=dict)
    play_channels: dict[str, ChannelSpec] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)
    common_versions: tuple[int, ...] = (1,)
    fabric_registry_handler: Callable[[bytes], bool] | None = None

    def __post_init__(self) -> None:
        self.loader = Loader(self.loader)
        self.mods = {str(name): str(version) for name, version in self.mods.items()}
        if any(not name or len(name) > _MAX_STRING for name in self.mods):
            raise ValueError("mod ids must be non-empty and at most 256 characters")
        if len(self.mods) > _MAX_MODS:
            raise ValueError(f"too many mods (maximum {_MAX_MODS})")
        if self.protocol_version < 0:
            raise ValueError("loader protocol version cannot be negative")
        self.configuration_channels = _normalize_specs(self.configuration_channels)
        self.play_channels = _normalize_specs(self.play_channels)
        self.display_names = {
            str(name): str(display) for name, display in self.display_names.items()
        }
        versions = tuple(sorted({int(value) for value in self.common_versions if int(value) > 0}))
        if not versions:
            raise ValueError("at least one positive common networking version is required")
        self.common_versions = versions
        if self.fabric_registry_handler is not None and not callable(
            self.fabric_registry_handler
        ):
            raise TypeError("fabric registry handler must be callable")

    @classmethod
    def create(
        cls,
        loader: Loader | str = Loader.VANILLA,
        mods: Mapping[str, str] | None = None,
        *,
        protocol_version: int = 0,
        configuration_channels: Mapping[str, ChannelSpec | str | int] | Iterable[str] | None = None,
        play_channels: Mapping[str, ChannelSpec | str | int] | Iterable[str] | None = None,
        display_names: Mapping[str, str] | None = None,
        common_versions: Iterable[int] = (1,),
        fabric_registry_handler: Callable[[bytes], bool] | None = None,
    ) -> ModListAdapter:
        return cls(
            Loader(loader),
            dict(mods or {}),
            protocol_version,
            _normalize_specs(configuration_channels),
            _normalize_specs(play_channels),
            dict(display_names or {}),
            tuple(common_versions),
            fabric_registry_handler,
        )

    @property
    def handshake_channel(self) -> str | None:
        """Primary loader channel, retained for compatibility with old callers."""

        if self.loader is Loader.FORGE:
            return "forge:handshake"
        if self.loader is Loader.NEOFORGE:
            return "neoforge:register"
        if self.loader is Loader.FABRIC:
            return "minecraft:register"
        return None

    @property
    def handshake_channels(self) -> frozenset[str]:
        if self.loader is Loader.FORGE:
            return frozenset(
                {
                    "minecraft:register",
                    "minecraft:unregister",
                    "forge:login",
                    "forge:handshake",
                }
            )
        if self.loader is Loader.NEOFORGE:
            return frozenset(
                {
                    "minecraft:register",
                    "minecraft:unregister",
                    "neoforge:register",
                    "c:version",
                    "c:register",
                }
            )
        if self.loader is Loader.FABRIC:
            names = {
                "minecraft:register",
                "minecraft:unregister",
                "c:version",
                "c:register",
            }
            if self.fabric_registry_handler is not None:
                names.add("fabric:registry/sync")
            return frozenset(names)
        return frozenset()

    @property
    def channels(self) -> frozenset[str]:
        """Channels advertised by this profile in ``minecraft:register``."""

        if self.loader is Loader.FORGE:
            names = {
                "forge:login",
                "forge:handshake",
            }
        elif self.loader is Loader.NEOFORGE:
            names = {
                "minecraft:register",
                "minecraft:unregister",
                "neoforge:register",
                "neoforge:network",
                "neoforge:modded_network_setup_failed",
                "c:version",
                "c:register",
            }
            names.update(_NEOFORGE_CORE_CONFIGURATION_CHANNELS)
            names.update(_NEOFORGE_CORE_PLAY_CHANNELS)
        elif self.loader is Loader.FABRIC:
            names = {
                "c:version",
                "c:register",
            }
            if self.fabric_registry_handler is not None:
                names.add("fabric:registry/sync")
        else:
            names = set()
        names.update(self.configuration_channels)
        names.update(self.play_channels)
        return frozenset(names)

    @property
    def forge_channel_versions(self) -> dict[str, int]:
        """Forge ``ChannelVersions`` map (all target Forge channels use 0)."""

        if self.loader is not Loader.FORGE:
            return {}
        result = {
            "forge:login": self.protocol_version,
            "forge:handshake": self.protocol_version,
            "forge:channel_registration": self.protocol_version,
        }
        for name in (*self.configuration_channels, *self.play_channels):
            result.setdefault(name, self.protocol_version)
        return result

    def handshake_host(self, host: str) -> str:
        """Return the host field that should be placed in the intention packet.

        Forge detects a modded client before login by looking for a NUL-separated
        ``FORGE`` marker.  The target branches all use network marker version 0;
        a non-zero ``protocol_version`` is retained for forward-compatible
        branches and encoded as ``FORGE<n>``.
        """

        if self.loader is not Loader.FORGE:
            return host
        marker = "FORGE" if self.protocol_version == 0 else f"FORGE{self.protocol_version}"
        return f"{host}\0{marker}"

    def handle(self, channel: str, data: bytes) -> bytes | None:
        """Return a same-channel response, or ``None`` for application data.

        This compatibility method intentionally does not return a response for
        Fabric registry completion because that response uses a different
        channel.  Use :meth:`handle_response` when integrating a transport that
        supports channel-changing responses.
        """

        response = self.handle_response(channel, data)
        if response is None or response.channel != _channel(channel):
            return None
        return response.data

    def handle_response(self, channel: str, data: bytes) -> PayloadResponse | None:
        """Decode one loader payload and return its exact response frame."""

        channel = _channel(channel)
        try:
            if self.loader is Loader.FORGE:
                return self._handle_forge(channel, data)
            if self.loader is Loader.NEOFORGE:
                return self._handle_neoforge(channel, data)
            if self.loader is Loader.FABRIC:
                return self._handle_fabric(channel, data)
        except (ProtocolError, UnicodeError, ValueError, OverflowError):
            # A malformed loader payload is not safe to answer speculatively.
            # The transport can expose it to the application for diagnostics.
            return None
        return None

    # ------------------------------------------------------------------
    # Common Fabric/NeoForge payloads
    # ------------------------------------------------------------------
    def _registration_response(
        self,
        channel: str,
        data: bytes,
        *,
        trailing_nul: bool = False,
    ) -> PayloadResponse | None:
        # ``minecraft:register`` and ``minecraft:unregister`` are NUL-separated
        # ASCII identifiers without a length prefix.
        names = _read_nul_channels(data)
        if len(names) > _MAX_CHANNELS:
            return None
        advertised = sorted(self.channels)
        encoded = b"\0".join(name.encode("ascii") for name in advertised)
        if trailing_nul and encoded:
            encoded += b"\0"
        return PayloadResponse(channel, encoded)

    def _common_version_response(self, channel: str, data: bytes) -> PayloadResponse | None:
        reader = PacketReader(data)
        count = reader.read_varint()
        if count < 0 or count > 64:
            return None
        offered = [reader.read_varint() for _ in range(count)]
        reader.expect_end()
        common = sorted(set(offered).intersection(self.common_versions), reverse=True)
        if not common:
            return None
        # Fabric records the negotiated highest common version, whereas
        # NeoForge advertises its complete supported list (currently [1]).
        advertised_versions = (
            (common[0],) if self.loader is Loader.FABRIC else self.common_versions
        )
        writer = PacketWriter().write_varint(len(advertised_versions))
        for version in advertised_versions:
            writer.write_varint(version)
        return PayloadResponse(channel, writer.to_bytes())

    def _common_register_response(self, channel: str, data: bytes) -> PayloadResponse | None:
        reader = PacketReader(data)
        version = reader.read_varint()
        protocol = reader.read_string(max_chars=32)
        if protocol not in {"configuration", "play"}:
            return None
        count = reader.read_varint()
        if count < 0 or count > _MAX_CHANNELS:
            return None
        for _ in range(count):
            _channel(reader.read_string(max_chars=_MAX_STRING))
        reader.expect_end()
        # NeoForge currently negotiates PLAY channels during configuration and
        # responds with its fixed common version. Fabric supports both phase
        # values and sends the phase-specific receiver set.
        if self.loader is Loader.NEOFORGE:
            version, protocol = 1, "play"
            specs = self.play_channels
        else:
            specs = (
                self.configuration_channels
                if protocol == "configuration"
                else self.play_channels
            )
        writer = PacketWriter().write_varint(version).write_string(protocol, max_chars=32)
        names = sorted(
            name
            for name, spec in specs.items()
            if spec.flow in {None, ChannelFlow.CLIENTBOUND}
            and (self.loader is Loader.FABRIC or spec.optional)
        )
        writer.write_varint(len(names))
        for name in names:
            writer.write_string(name, max_chars=_MAX_STRING)
        return PayloadResponse(channel, writer.to_bytes())

    def _handle_fabric(self, channel: str, data: bytes) -> PayloadResponse | None:
        if channel == "minecraft:register":
            return self._registration_response(channel, data)
        if channel == "minecraft:unregister":
            _read_nul_channels(data)
            return None
        if channel == "c:version":
            return self._common_version_response(channel, data)
        if channel == "c:register":
            return self._common_register_response(channel, data)
        if channel == "fabric:registry/sync":
            handler = self.fabric_registry_handler
            if handler is None:
                return None
            try:
                applied = handler(data)
            except Exception:
                return None
            if applied is not True:
                return None
            return PayloadResponse("fabric:registry/sync/complete", b"")
        return None

    def _handle_neoforge(self, channel: str, data: bytes) -> PayloadResponse | None:
        # Pre-26 NeoForge builds exposed the legacy FML-style handshake channel.
        # Keep a deliberately narrow compatibility decoder for applications
        # written against the original ProtoBot API; current NeoForge never
        # sends this channel and uses ``neoforge:register`` below.
        if channel == "neoforge:handshake":
            return self._handle_legacy_neoforge(channel, data)
        if channel == "minecraft:register":
            return self._registration_response(channel, data, trailing_nul=True)
        if channel == "minecraft:unregister":
            _read_nul_channels(data)
            return None
        if channel == "c:version":
            return self._common_version_response(channel, data)
        if channel == "c:register":
            return self._common_register_response(channel, data)
        if channel == "neoforge:register":
            # ModdedNetworkQueryPayload: Map<ConnectionProtocol, Set<Component>>.
            # Decode and validate the server query, then answer with the
            # components declared by this profile.
            _read_neoforge_query(data)
            return PayloadResponse(channel, _write_neoforge_query(self))
        if channel == "neoforge:extensible_enum_data":
            if not _neoforge_unextended_enums(data):
                return None
            return PayloadResponse("neoforge:extensible_enum_ack", b"")
        return None

    # ------------------------------------------------------------------
    # Forge 26.x payloads
    # ------------------------------------------------------------------
    def _handle_forge(self, channel: str, data: bytes) -> PayloadResponse | None:
        # Compatibility with the pre-26 FML handshake used by older callers.
        # This branch is intentionally restricted to the old channel and frame
        # shape; all target Forge releases use ``forge:handshake`` below.
        if channel == "fml:handshake":
            return self._handle_legacy_forge(channel, data)
        if channel == "minecraft:register":
            # ChannelListManager's logical parent is
            # ``forge:channel_registration``, but RegisterPayload is created
            # with the default Minecraft namespace and has no discriminator.
            _read_registration_payload(data)
            names = sorted(self.channels)
            encoded = b"".join(name.encode("ascii") + b"\0" for name in names)
            return PayloadResponse(channel, encoded)
        if channel == "minecraft:unregister":
            _read_registration_payload(data)
            return None
        if channel == "forge:login":
            # LoginWrapper: Identifier + VarInt length + inner payload.  This
            # is useful for application login channels; core configuration
            # handshake messages use forge:handshake directly.
            reader = PacketReader(data)
            inner_channel = _channel(reader.read_string(max_chars=_MAX_STRING))
            size = reader.read_varint()
            if size < 0 or size > 1 << 20:
                return None
            inner = reader.read_raw(size)
            reader.expect_end()
            response = self._handle_forge(inner_channel, inner)
            if response is None:
                return None
            wrapped = (
                PacketWriter()
                .write_string(response.channel, max_chars=_MAX_STRING)
                .write_bytes(response.data)
                .to_bytes()
            )
            return PayloadResponse(channel, wrapped)

        if channel != "forge:handshake":
            return None
        reader = PacketReader(data)
        discriminator = reader.read_varint()
        if discriminator == 0:  # Acknowledge
            # A server normally does not send this direction; do not echo it
            # and create an acknowledgement loop.
            reader.read_varint()
            reader.expect_end()
            return None
        if discriminator == 1:  # ModVersions
            self._read_forge_mod_versions(reader)
            writer = PacketWriter().write_varint(1)
            items = sorted(self.mods.items())
            writer.write_varint(len(items))
            for mod_id, version in items:
                writer.write_string(mod_id, max_chars=_MAX_STRING)
                writer.write_string(self.display_names.get(mod_id, mod_id), max_chars=_MAX_STRING)
                writer.write_string(version, max_chars=_MAX_STRING)
            return PayloadResponse(channel, writer.to_bytes())
        if discriminator == 2:  # ChannelVersions
            self._read_forge_channel_versions(reader)
            writer = PacketWriter().write_varint(2)
            channels = self.forge_channel_versions
            writer.write_varint(len(channels))
            for name, version in sorted(channels.items()):
                writer.write_string(name, max_chars=_MAX_STRING).write_varint(version)
            return PayloadResponse(channel, writer.to_bytes())
        if discriminator == 3:  # RegistryList -> acknowledge token
            token = reader.read_varint()
            _read_identifier_list(reader)
            _read_identifier_list(reader)
            reader.expect_end()
            payload = PacketWriter().write_varint(0).write_varint(token).to_bytes()
            return PayloadResponse(channel, payload)
        if discriminator == 4:  # RegistryData -> acknowledge token
            token = reader.read_varint()
            _channel(reader.read_string(max_chars=_MAX_STRING))
            # The registry snapshot is loader-specific and intentionally kept
            # opaque; Forge's client still acknowledges each packet.
            reader.read_remaining()
            payload = PacketWriter().write_varint(0).write_varint(token).to_bytes()
            return PayloadResponse(channel, payload)
        # ConfigData (5) and MismatchData (6) are server notifications; no
        # automatic response is valid.
        return None

    def _handle_legacy_forge(self, channel: str, data: bytes) -> PayloadResponse | None:
        reader = PacketReader(data)
        discriminator = reader.read_unsigned_byte()
        if discriminator != 0:
            return None
        protocol = reader.read_varint()
        reader.expect_end()
        payload = PacketWriter().write_unsigned_byte(1).write_varint(protocol).to_bytes()
        return PayloadResponse(channel, payload)

    def _handle_legacy_neoforge(self, channel: str, data: bytes) -> PayloadResponse | None:
        reader = PacketReader(data)
        discriminator = reader.read_unsigned_byte()
        if discriminator != 2:
            return None
        # The old frame carried a client mod-list request.  Consume its count
        # so malformed data is rejected, then answer with the deterministic
        # sorted id/version map expected by the historical API.
        count = reader.read_varint()
        if count < 0 or count > _MAX_MODS:
            return None
        reader.expect_end()
        writer = PacketWriter().write_unsigned_byte(2)
        items = sorted(self.mods.items())
        writer.write_varint(len(items))
        for mod_id, version in items:
            writer.write_string(mod_id, max_chars=_MAX_STRING)
            writer.write_string(version, max_chars=_MAX_STRING)
        return PayloadResponse(channel, writer.to_bytes())

    @staticmethod
    def _read_forge_mod_versions(reader: PacketReader) -> None:
        count = reader.read_varint()
        if count < 0 or count > _MAX_MODS:
            raise ProtocolError("invalid Forge mod count")
        for _ in range(count):
            reader.read_string(max_chars=_MAX_STRING)
            reader.read_string(max_chars=_MAX_STRING)
            reader.read_string(max_chars=_MAX_STRING)
        reader.expect_end()

    @staticmethod
    def _read_forge_channel_versions(reader: PacketReader) -> None:
        count = reader.read_varint()
        if count < 0 or count > _MAX_CHANNELS:
            raise ProtocolError("invalid Forge channel count")
        for _ in range(count):
            _channel(reader.read_string(max_chars=_MAX_STRING))
            version = reader.read_varint()
            if version < 0:
                raise ProtocolError("negative Forge channel version")
        reader.expect_end()


def _read_nul_channels(data: bytes) -> list[str]:
    if not data:
        return []
    try:
        values = data.decode("ascii").split("\0")
    except UnicodeDecodeError as error:
        raise ProtocolError("registration payload is not ASCII") from error
    if values and values[-1] == "":
        values.pop()
    if any(not value for value in values):
        raise ProtocolError("registration payload contains an empty channel")
    return [_channel(value) for value in values]


def _read_registration_payload(data: bytes) -> list[str]:
    """Decode Forge's dynamic ``minecraft:register``/``unregister`` payload.

    ``ChannelListManager`` writes a NUL after every identifier, while its
    decoder also accepts an unterminated final identifier.  Accept both forms
    but reject interior/duplicate empty entries so malformed frames are never
    reflected back to the server.
    """

    if not data:
        return []
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise ProtocolError("Forge channel registration is not ASCII") from error
    values = text.split("\0")
    if values and values[-1] == "":
        values.pop()
    if any(not value for value in values):
        raise ProtocolError("Forge channel registration contains an empty channel")
    if len(values) > _MAX_CHANNELS:
        raise ProtocolError("too many Forge registered channels")
    return [_channel(value) for value in values]


def _read_identifier_list(reader: PacketReader) -> list[str]:
    count = reader.read_varint()
    if count < 0 or count > _MAX_CHANNELS:
        raise ProtocolError("invalid identifier list length")
    return [_channel(reader.read_string(max_chars=_MAX_STRING)) for _ in range(count)]


def _read_neoforge_query(data: bytes) -> dict[int, list[tuple[str, str, int | None, bool]]]:
    reader = PacketReader(data)
    map_count = reader.read_varint()
    if map_count < 0 or map_count > 8:
        raise ProtocolError("invalid NeoForge query map length")
    result: dict[int, list[tuple[str, str, int | None, bool]]] = {}
    for _ in range(map_count):
        protocol = reader.read_varint()
        if protocol not in {1, 4}:  # PLAY / CONFIGURATION enum ordinals
            raise ProtocolError("invalid NeoForge protocol ordinal")
        count = reader.read_varint()
        if count < 0 or count > _MAX_CHANNELS:
            raise ProtocolError("invalid NeoForge component count")
        components: list[tuple[str, str, int | None, bool]] = []
        for _ in range(count):
            name = _channel(reader.read_string(max_chars=_MAX_STRING))
            version = reader.read_string(max_chars=_MAX_STRING)
            has_flow = reader.read_bool()
            flow = reader.read_varint() if has_flow else None
            if flow is not None and flow not in {0, 1}:
                raise ProtocolError("invalid NeoForge packet flow")
            components.append((name, version, flow, reader.read_bool()))
        result[protocol] = components
    reader.expect_end()
    return result


def _write_neoforge_query(adapter: ModListAdapter) -> bytes:
    configuration_channels = dict(_NEOFORGE_CORE_CONFIGURATION_CHANNELS)
    configuration_channels.update(adapter.configuration_channels)
    play_channels = dict(_NEOFORGE_CORE_PLAY_CHANNELS)
    play_channels.update(adapter.play_channels)
    phases = {
        4: configuration_channels,
        1: play_channels,
    }
    phases = {ordinal: specs for ordinal, specs in phases.items() if specs}
    writer = PacketWriter().write_varint(len(phases))
    for ordinal, specs in sorted(phases.items()):
        writer.write_varint(ordinal).write_varint(len(specs))
        for name, spec in sorted(specs.items()):
            writer.write_string(name, max_chars=_MAX_STRING)
            writer.write_string(spec.version, max_chars=_MAX_STRING)
            if spec.flow is None:
                writer.write_bool(False)
            else:
                writer.write_bool(True).write_varint(
                    0 if spec.flow is ChannelFlow.SERVERBOUND else 1
                )
            writer.write_bool(spec.optional)
    return writer.to_bytes()


def _neoforge_unextended_enums(data: bytes) -> bool:
    """Validate the core enum snapshot before acknowledging compatibility."""

    reader = PacketReader(data)
    count = reader.read_varint()
    if count < 0 or count > _MAX_CHANNELS:
        raise ProtocolError("invalid NeoForge extensible enum count")
    for _ in range(count):
        reader.read_string(max_chars=_MAX_STRING)
        network_check = reader.read_string(max_chars=32)
        if network_check not in {"CLIENTBOUND", "SERVERBOUND", "BIDIRECTIONAL"}:
            raise ProtocolError("invalid NeoForge enum network check")
        if reader.read_bool():
            return False
    reader.expect_end()
    return True


def make_adapter(
    loader: Loader | str = Loader.VANILLA,
    mods: Mapping[str, str] | None = None,
    *,
    protocol_version: int = 0,
    configuration_channels: Mapping[str, ChannelSpec | str | int] | Iterable[str] | None = None,
    play_channels: Mapping[str, ChannelSpec | str | int] | Iterable[str] | None = None,
    display_names: Mapping[str, str] | None = None,
    common_versions: Iterable[int] = (1,),
    fabric_registry_handler: Callable[[bytes], bool] | None = None,
) -> ModListAdapter:
    """Convenience factory used by :class:`protobot.client.Bot`."""

    return ModListAdapter.create(
        loader,
        mods,
        protocol_version=protocol_version,
        configuration_channels=configuration_channels,
        play_channels=play_channels,
        display_names=display_names,
        common_versions=common_versions,
        fabric_registry_handler=fabric_registry_handler,
    )


__all__ = [
    "ChannelFlow",
    "ChannelSpec",
    "Loader",
    "ModListAdapter",
    "PayloadResponse",
    "make_adapter",
]
