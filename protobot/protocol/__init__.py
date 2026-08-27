"""Minecraft protocol primitives and version metadata."""

from .codec import PacketReader, PacketWriter, decode_varint, encode_varint
from .connection import ConnectionState, RawPacket
from .versions import SUPPORTED_VERSIONS, VersionSpec, get_version

__all__ = [
    "SUPPORTED_VERSIONS",
    "ConnectionState",
    "PacketReader",
    "PacketWriter",
    "RawPacket",
    "VersionSpec",
    "decode_varint",
    "encode_varint",
    "get_version",
]
