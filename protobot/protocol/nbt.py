"""Bounds-checked Network Binary Tag reader."""

from __future__ import annotations

import struct
from enum import IntEnum
from typing import Any

from protobot.errors import ProtocolError

from .codec import PacketReader


class TagType(IntEnum):
    END = 0
    BYTE = 1
    SHORT = 2
    INT = 3
    LONG = 4
    FLOAT = 5
    DOUBLE = 6
    BYTE_ARRAY = 7
    STRING = 8
    LIST = 9
    COMPOUND = 10
    INT_ARRAY = 11
    LONG_ARRAY = 12


class NBTReader:
    def __init__(
        self,
        reader: PacketReader,
        *,
        max_depth: int = 64,
        max_collection_length: int = 1 << 20,
    ) -> None:
        self.reader = reader
        self.max_depth = max_depth
        self.max_collection_length = max_collection_length

    def read_anonymous(self) -> Any | None:
        tag_type = self._read_type()
        if tag_type is TagType.END:
            return None
        return self._read_payload(tag_type, 0)

    def read_named(self) -> tuple[str, Any] | None:
        tag_type = self._read_type()
        if tag_type is TagType.END:
            return None
        return self._read_string(), self._read_payload(tag_type, 0)

    def _read_type(self) -> TagType:
        value = self.reader.read_unsigned_byte()
        try:
            return TagType(value)
        except ValueError as error:
            raise ProtocolError(f"unknown NBT tag type {value}") from error

    def _read_length(self) -> int:
        length = self.reader.read_int()
        if length < 0 or length > self.max_collection_length:
            raise ProtocolError(f"invalid NBT collection length {length}")
        return length

    def _read_string(self) -> str:
        length = self.reader.read_unsigned_short()
        raw = self.reader.read_raw(length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("NBT string is not valid UTF-8") from error

    def _read_payload(self, tag_type: TagType, depth: int) -> Any:
        if depth > self.max_depth:
            raise ProtocolError("NBT nesting exceeds depth limit")
        if tag_type is TagType.BYTE:
            return self.reader.read_byte()
        if tag_type is TagType.SHORT:
            return self.reader.read_short()
        if tag_type is TagType.INT:
            return self.reader.read_int()
        if tag_type is TagType.LONG:
            return self.reader.read_long()
        if tag_type is TagType.FLOAT:
            return self.reader.read_float()
        if tag_type is TagType.DOUBLE:
            return self.reader.read_double()
        if tag_type is TagType.BYTE_ARRAY:
            return self.reader.read_raw(self._read_length())
        if tag_type is TagType.STRING:
            return self._read_string()
        if tag_type is TagType.LIST:
            element_type = self._read_type()
            length = self._read_length()
            if element_type is TagType.END and length:
                raise ProtocolError("non-empty NBT list cannot have END element type")
            return [self._read_payload(element_type, depth + 1) for _ in range(length)]
        if tag_type is TagType.COMPOUND:
            result: dict[str, Any] = {}
            while True:
                child_type = self._read_type()
                if child_type is TagType.END:
                    return result
                name = self._read_string()
                result[name] = self._read_payload(child_type, depth + 1)
        if tag_type is TagType.INT_ARRAY:
            return [self.reader.read_int() for _ in range(self._read_length())]
        if tag_type is TagType.LONG_ARRAY:
            return [self.reader.read_long() for _ in range(self._read_length())]
        raise ProtocolError(f"END tag has no payload at depth {depth}")


def read_anonymous_nbt(reader: PacketReader) -> Any | None:
    return NBTReader(reader).read_anonymous()


def read_named_nbt(reader: PacketReader) -> tuple[str, Any] | None:
    return NBTReader(reader).read_named()


def encode_nbt_string(value: str) -> bytes:
    """Minimal writer helper used by tests and packet fixtures."""
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("NBT string exceeds unsigned-short length")
    return struct.pack(">H", len(raw)) + raw
