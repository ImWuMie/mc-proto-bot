"""Async TCP transport for Minecraft packet frames."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from protobot.errors import ConnectionClosed, ProtocolError

from .codec import MAX_VARINT_BYTES
from .framing import DEFAULT_MAX_PACKET_SIZE, decode_frame, encode_frame

if TYPE_CHECKING:
    from protobot.auth import StreamCipher


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    HANDSHAKING = "handshaking"
    LOGIN = "login"
    CONFIGURATION = "configuration"
    PLAY = "play"


@dataclass(frozen=True, slots=True)
class RawPacket:
    state: ConnectionState
    packet_id: int
    payload: bytes


class ProtocolConnection:
    def __init__(self, *, max_packet_size: int = DEFAULT_MAX_PACKET_SIZE) -> None:
        self.max_packet_size = max_packet_size
        self.compression_threshold: int | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._cipher: StreamCipher | None = None

    @property
    def is_open(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    def enable_encryption(self, shared_secret: bytes) -> None:
        """Enable continuous AES-128 CFB8 stream encryption for both directions."""
        from protobot.auth import StreamCipher

        self._cipher = StreamCipher(shared_secret)

    async def open(self, host: str, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)

    async def close(self) -> None:
        writer, self.writer = self.writer, None
        self.reader = None
        self._cipher = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _read_length(self) -> int:
        if self.reader is None:
            raise ConnectionClosed("connection is not open")
        result = 0
        for index in range(MAX_VARINT_BYTES):
            try:
                raw_byte = await self.reader.readexactly(1)
            except asyncio.IncompleteReadError as error:
                raise ConnectionClosed("server closed the connection") from error
            if self._cipher is not None:
                raw_byte = self._cipher.decrypt(raw_byte)
            byte = raw_byte[0]
            result |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                if result <= 0 or result > self.max_packet_size:
                    raise ProtocolError(f"invalid frame length {result}")
                return result
        raise ProtocolError("frame length VarInt is longer than 5 bytes")

    async def receive_packet(self, state: ConnectionState) -> RawPacket:
        if self.reader is None:
            raise ConnectionClosed("connection is not open")
        length = await self._read_length()
        try:
            body = await self.reader.readexactly(length)
        except asyncio.IncompleteReadError as error:
            raise ConnectionClosed("server closed in the middle of a packet") from error
        if self._cipher is not None:
            body = self._cipher.decrypt(body)
        packet_id, payload = decode_frame(
            body,
            self.compression_threshold,
            max_packet_size=self.max_packet_size,
        )
        return RawPacket(state, packet_id, payload)

    async def send_packet(self, packet_id: int, payload: bytes = b"") -> None:
        if self.writer is None or self.writer.is_closing():
            raise ConnectionClosed("connection is not open")
        frame = encode_frame(packet_id, payload, self.compression_threshold)
        if self._cipher is not None:
            frame = self._cipher.encrypt(frame)
        self.writer.write(frame)
        await self.writer.drain()
