"""Binary codecs used by the Minecraft Java protocol."""

from __future__ import annotations

import math
import struct
import uuid
from dataclasses import dataclass

from protobot.errors import ProtocolError

MAX_VARINT_BYTES = 5
MAX_VARLONG_BYTES = 10


def encode_varint(value: int) -> bytes:
    """Encode a signed 32-bit Minecraft VarInt."""
    if not -(1 << 31) <= value < (1 << 31):
        raise ValueError("VarInt value is outside signed 32-bit range")
    unsigned = value & 0xFFFFFFFF
    result = bytearray()
    while True:
        byte = unsigned & 0x7F
        unsigned >>= 7
        if unsigned:
            byte |= 0x80
        result.append(byte)
        if not unsigned:
            return bytes(result)


def decode_varint(data: bytes | bytearray | memoryview, offset: int = 0) -> tuple[int, int]:
    """Decode a VarInt and return ``(value, new_offset)``."""
    result = 0
    for index in range(MAX_VARINT_BYTES):
        position = offset + index
        if position >= len(data):
            raise ProtocolError("truncated VarInt")
        byte = data[position]
        if index == MAX_VARINT_BYTES - 1 and byte & 0xF0:
            raise ProtocolError("VarInt exceeds 32 bits")
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if result & (1 << 31):
                result -= 1 << 32
            return result, position + 1
    raise ProtocolError("VarInt is longer than 5 bytes")


def encode_varlong(value: int) -> bytes:
    if not -(1 << 63) <= value < (1 << 63):
        raise ValueError("VarLong value is outside signed 64-bit range")
    unsigned = value & 0xFFFFFFFFFFFFFFFF
    result = bytearray()
    while True:
        byte = unsigned & 0x7F
        unsigned >>= 7
        if unsigned:
            byte |= 0x80
        result.append(byte)
        if not unsigned:
            return bytes(result)


@dataclass(slots=True)
class PacketReader:
    """Bounds-checked reader for a single packet payload."""

    data: memoryview
    offset: int = 0

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        self.data = memoryview(data)
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def _read(self, size: int) -> memoryview:
        if size < 0 or size > self.remaining:
            raise ProtocolError(f"packet is truncated: need {size}, have {self.remaining}")
        start = self.offset
        self.offset += size
        return self.data[start : start + size]

    def read_remaining(self) -> bytes:
        return bytes(self._read(self.remaining))

    def read_raw(self, size: int) -> bytes:
        return bytes(self._read(size))

    def read_bool(self) -> bool:
        value = self.read_unsigned_byte()
        if value not in (0, 1):
            raise ProtocolError(f"invalid boolean value {value}")
        return bool(value)

    def read_byte(self) -> int:
        return struct.unpack(">b", self._read(1))[0]

    def read_unsigned_byte(self) -> int:
        return self._read(1)[0]

    def read_short(self) -> int:
        return struct.unpack(">h", self._read(2))[0]

    def read_unsigned_short(self) -> int:
        return struct.unpack(">H", self._read(2))[0]

    def read_int(self) -> int:
        return struct.unpack(">i", self._read(4))[0]

    def read_unsigned_int(self) -> int:
        return struct.unpack(">I", self._read(4))[0]

    def read_long(self) -> int:
        return struct.unpack(">q", self._read(8))[0]

    def read_unsigned_long(self) -> int:
        return struct.unpack(">Q", self._read(8))[0]

    def read_float(self) -> float:
        return struct.unpack(">f", self._read(4))[0]

    def read_double(self) -> float:
        return struct.unpack(">d", self._read(8))[0]

    def read_varint(self) -> int:
        value, self.offset = decode_varint(self.data, self.offset)
        return value

    def read_varlong(self) -> int:
        result = 0
        for index in range(MAX_VARLONG_BYTES):
            byte = self.read_unsigned_byte()
            if index == MAX_VARLONG_BYTES - 1 and byte & 0xFE:
                raise ProtocolError("VarLong exceeds 64 bits")
            result |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                if result & (1 << 63):
                    result -= 1 << 64
                return result
        raise ProtocolError("VarLong is longer than 10 bytes")

    def read_bytes(self, *, max_length: int = 1 << 20) -> bytes:
        length = self.read_varint()
        if length < 0 or length > max_length:
            raise ProtocolError(f"byte array length {length} exceeds limit {max_length}")
        return bytes(self._read(length))

    def read_string(self, *, max_chars: int = 32767) -> str:
        raw = self.read_bytes(max_length=max_chars * 3)
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("string is not valid UTF-8") from error
        if len(value) > max_chars:
            raise ProtocolError(f"string length {len(value)} exceeds limit {max_chars}")
        return value

    def read_uuid(self) -> uuid.UUID:
        return uuid.UUID(bytes=bytes(self._read(16)))

    def read_position(self) -> tuple[int, int, int]:
        packed = self.read_unsigned_long()
        x = packed >> 38
        z = (packed >> 12) & 0x3FFFFFF
        y = packed & 0xFFF
        if x >= 1 << 25:
            x -= 1 << 26
        if z >= 1 << 25:
            z -= 1 << 26
        if y >= 1 << 11:
            y -= 1 << 12
        return x, y, z

    def read_chunk_pos(self) -> tuple[int, int]:
        """Read Mojang's packed ``ChunkPos`` (two signed 32-bit integers)."""

        packed = self.read_unsigned_long()
        chunk_x = (packed >> 32) & 0xFFFFFFFF
        chunk_z = packed & 0xFFFFFFFF
        if chunk_x >= 1 << 31:
            chunk_x -= 1 << 32
        if chunk_z >= 1 << 31:
            chunk_z -= 1 << 32
        return chunk_x, chunk_z

    def read_lp_vec3(self) -> tuple[float, float, float]:
        """Read Mojang's variable-length low-precision velocity vector."""

        first = self.read_unsigned_byte()
        if first == 0:
            return 0.0, 0.0, 0.0
        second = self.read_unsigned_byte()
        packed = (self.read_unsigned_int() << 16) | (second << 8) | first
        scale = first & 0x03
        if first & 0x04:
            scale |= (self.read_varint() & 0xFFFFFFFF) << 2

        def unpack(shift: int) -> float:
            encoded = min((packed >> shift) & 0x7FFF, 32766)
            return ((encoded * 2.0) / 32766.0 - 1.0) * scale

        return unpack(3), unpack(18), unpack(33)

    def expect_end(self) -> None:
        if self.remaining:
            raise ProtocolError(f"packet has {self.remaining} trailing bytes")


class PacketWriter:
    """Fluent writer for a packet payload."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data = bytearray()

    def to_bytes(self) -> bytes:
        return bytes(self._data)

    def write_raw(self, value: bytes | bytearray | memoryview) -> PacketWriter:
        self._data.extend(value)
        return self

    def write_bool(self, value: bool) -> PacketWriter:
        self._data.append(1 if value else 0)
        return self

    def write_byte(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">b", value))
        return self

    def write_unsigned_byte(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">B", value))
        return self

    def write_short(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">h", value))
        return self

    def write_unsigned_short(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">H", value))
        return self

    def write_int(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">i", value))
        return self

    def write_unsigned_int(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">I", value))
        return self

    def write_long(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">q", value))
        return self

    def write_unsigned_long(self, value: int) -> PacketWriter:
        self._data.extend(struct.pack(">Q", value))
        return self

    def write_float(self, value: float) -> PacketWriter:
        self._data.extend(struct.pack(">f", value))
        return self

    def write_double(self, value: float) -> PacketWriter:
        self._data.extend(struct.pack(">d", value))
        return self

    def write_varint(self, value: int) -> PacketWriter:
        self._data.extend(encode_varint(value))
        return self

    def write_varlong(self, value: int) -> PacketWriter:
        self._data.extend(encode_varlong(value))
        return self

    def write_bytes(self, value: bytes | bytearray | memoryview) -> PacketWriter:
        self.write_varint(len(value))
        return self.write_raw(value)

    def write_string(self, value: str, *, max_chars: int = 32767) -> PacketWriter:
        if len(value) > max_chars:
            raise ValueError(f"string length {len(value)} exceeds limit {max_chars}")
        raw = value.encode("utf-8")
        if len(raw) > max_chars * 3:
            raise ValueError("encoded string exceeds protocol byte limit")
        return self.write_bytes(raw)

    def write_uuid(self, value: uuid.UUID) -> PacketWriter:
        return self.write_raw(value.bytes)

    def write_position(self, x: int, y: int, z: int) -> PacketWriter:
        if not -(1 << 25) <= x < (1 << 25):
            raise ValueError("position x is outside signed 26-bit range")
        if not -(1 << 11) <= y < (1 << 11):
            raise ValueError("position y is outside signed 12-bit range")
        if not -(1 << 25) <= z < (1 << 25):
            raise ValueError("position z is outside signed 26-bit range")
        packed = ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF)
        return self.write_unsigned_long(packed)

    def write_chunk_pos(self, chunk_x: int, chunk_z: int) -> PacketWriter:
        """Write Mojang's packed ``ChunkPos`` (two signed 32-bit integers)."""

        if not -(1 << 31) <= chunk_x < (1 << 31):
            raise ValueError("chunk x is outside signed 32-bit range")
        if not -(1 << 31) <= chunk_z < (1 << 31):
            raise ValueError("chunk z is outside signed 32-bit range")
        packed = ((chunk_x & 0xFFFFFFFF) << 32) | (chunk_z & 0xFFFFFFFF)
        return self.write_unsigned_long(packed)

    def write_lp_vec3(self, x: float, y: float, z: float) -> PacketWriter:
        """Write Mojang's variable-length low-precision velocity vector."""

        def sanitize(value: float) -> float:
            if math.isnan(value):
                return 0.0
            return min(max(value, -17179869183.0), 17179869183.0)

        x, y, z = sanitize(x), sanitize(y), sanitize(z)
        magnitude = max(abs(x), abs(y), abs(z))
        if magnitude < 3.051944088384301e-5:
            return self.write_unsigned_byte(0)

        scale = math.ceil(magnitude)
        continuation = (scale & 0x03) != scale
        header_scale = (scale & 0x03) | 0x04 if continuation else scale

        def pack(value: float) -> int:
            return math.floor(((value / scale) * 0.5 + 0.5) * 32766.0 + 0.5)

        packed = (
            header_scale
            | (pack(x) << 3)
            | (pack(y) << 18)
            | (pack(z) << 33)
        )
        self.write_unsigned_byte(packed & 0xFF)
        self.write_unsigned_byte((packed >> 8) & 0xFF)
        self.write_unsigned_int((packed >> 16) & 0xFFFFFFFF)
        if continuation:
            high_scale = (scale >> 2) & 0xFFFFFFFF
            if high_scale >= 1 << 31:
                high_scale -= 1 << 32
            self.write_varint(high_scale)
        return self
