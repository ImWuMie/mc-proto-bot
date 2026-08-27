"""Packet framing and zlib compression."""

from __future__ import annotations

import zlib

from protobot.errors import ProtocolError

from .codec import PacketReader, PacketWriter, decode_varint, encode_varint

DEFAULT_MAX_PACKET_SIZE = 8 * 1024 * 1024


def encode_frame(packet_id: int, payload: bytes, compression_threshold: int | None) -> bytes:
    packet = encode_varint(packet_id) + payload
    if compression_threshold is None:
        body = packet
    elif len(packet) >= compression_threshold:
        body = encode_varint(len(packet)) + zlib.compress(packet)
    else:
        body = b"\x00" + packet
    return encode_varint(len(body)) + body


def decode_frame(
    body: bytes,
    compression_threshold: int | None,
    *,
    max_packet_size: int = DEFAULT_MAX_PACKET_SIZE,
) -> tuple[int, bytes]:
    if compression_threshold is not None:
        reader = PacketReader(body)
        uncompressed_length = reader.read_varint()
        compressed_or_raw = reader.read_remaining()
        if uncompressed_length < 0 or uncompressed_length > max_packet_size:
            raise ProtocolError("invalid uncompressed packet length")
        if uncompressed_length == 0:
            packet = compressed_or_raw
            if len(packet) >= compression_threshold:
                raise ProtocolError("uncompressed packet is not below compression threshold")
        else:
            if uncompressed_length < compression_threshold:
                raise ProtocolError("compressed packet is below compression threshold")
            try:
                decompressor = zlib.decompressobj()
                # Give zlib one byte beyond the declared size so an oversized
                # stream is detected without ever materializing its complete
                # output.  Calling ``flush()`` here would remove that bound and
                # let a small hostile frame expand far beyond max_packet_size.
                packet = decompressor.decompress(
                    compressed_or_raw,
                    uncompressed_length + 1,
                )
            except zlib.error as error:
                raise ProtocolError("invalid zlib packet") from error
            if (
                not decompressor.eof
                or decompressor.unused_data
                or decompressor.unconsumed_tail
            ):
                raise ProtocolError("compressed packet contains trailing data")
            if len(packet) != uncompressed_length:
                raise ProtocolError("uncompressed packet length does not match header")
    else:
        packet = body

    if not packet or len(packet) > max_packet_size:
        raise ProtocolError("invalid packet size")
    packet_id, payload_offset = decode_varint(packet)
    if packet_id < 0:
        raise ProtocolError("packet id cannot be negative")
    return packet_id, packet[payload_offset:]


def frame_body(frame: bytes) -> bytes:
    """Strip and validate the outer length from a complete frame (primarily for tests)."""
    reader = PacketReader(frame)
    length = reader.read_varint()
    if length < 0 or length != reader.remaining:
        raise ProtocolError("outer packet length does not match frame")
    return reader.read_remaining()


def make_handshake(protocol: int, host: str, port: int, next_state: int) -> bytes:
    return (
        PacketWriter()
        .write_varint(protocol)
        .write_string(host, max_chars=255)
        .write_unsigned_short(port)
        .write_varint(next_state)
        .to_bytes()
    )
