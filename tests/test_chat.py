"""Regression tests for player chat message decoding (protocol 774-776)."""

from __future__ import annotations

import struct
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from protobot.client import Bot
from protobot.errors import ProtocolError
from protobot.protocol.codec import PacketReader, PacketWriter
from protobot.protocol.connection import ConnectionState, RawPacket
from protobot.protocol.nbt import encode_nbt_string
from protobot.protocol.versions import SUPPORTED_VERSIONS


def component(value: str) -> bytes:
    """Encode a plain-text component as its anonymous NBT StringTag."""
    return b"\x08" + encode_nbt_string(value)


EMPTY_COMPOUND_STYLE = b"\x0a\x00"  # anonymous TAG_Compound (no root name), TAG_End


def make_player_chat_payload(
    *,
    sender: uuid.UUID | None = None,
    content: str = '{"text":"hello"}',
    seen: list[int] | None = None,
    unsigned: str | None = None,
    filter_mask: int = 0,
    mask_longs: tuple[int, ...] = (),
    chat_type_holder: int = 3,
    custom_chat_type: bool = False,
    name: str = "Steve",
    with_target: bool = False,
) -> bytes:
    writer = PacketWriter()
    writer.write_varint(7)  # global index
    writer.write_uuid(sender or uuid.UUID(int=0))
    writer.write_varint(3)  # per-sender index
    writer.write_bool(True)
    writer.write_raw(b"\xAA" * 256)  # message signature
    writer.write_string(content)
    writer.write_long(12345)  # timestamp
    writer.write_long(67890)  # salt
    writer.write_varint(len(seen or []))
    for entry in seen or []:
        writer.write_varint(entry)
        if entry == 0:
            writer.write_raw(b"\xBB" * 256)
    if unsigned is None:
        writer.write_bool(False)
    else:
        writer.write_bool(True)
        writer.write_raw(component(unsigned))
    writer.write_varint(filter_mask)
    if filter_mask == 2:
        writer.write_varint(len(mask_longs))
        for value in mask_longs:
            writer.write_long(value)
    if custom_chat_type:
        writer.write_varint(0)
        writer.write_string("chat.type.custom")
        writer.write_varint(2)
        writer.write_varint(0)
        writer.write_varint(1)
        writer.write_raw(EMPTY_COMPOUND_STYLE)
    else:
        writer.write_varint(chat_type_holder)
    writer.write_raw(component(name))
    writer.write_bool(with_target)
    if with_target:
        writer.write_raw(component("Target"))
    return writer.to_bytes()


def make_profileless_payload(
    *,
    message: str = "hi",
    chat_type_holder: int = 1,
    name: str = "Alex",
    with_target: bool = False,
) -> bytes:
    writer = PacketWriter()
    writer.write_raw(component(message))
    writer.write_varint(chat_type_holder)
    writer.write_raw(component(name))
    writer.write_bool(with_target)
    if with_target:
        writer.write_raw(component("Target"))
    return writer.to_bytes()


class ChatCapture:
    """Sync event handler that records player_chat emissions."""

    def __init__(self, bot: Bot) -> None:
        self.events: list[tuple] = []
        bot.on("player_chat", self._record)

    def _record(self, *args) -> None:
        self.events.append(args)


class PlayerChatDecodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1")
        self.capture = ChatCapture(self.bot)
        self.sender = uuid.UUID(int=42)

    async def test_decodes_signed_chat_fields(self) -> None:
        payload = make_player_chat_payload(
            sender=self.sender,
            seen=[0, 5],
            with_target=True,
        )
        await self.bot._handle_player_chat(payload)

        (sender, name, message, chat_type_id, target_name), = self.capture.events
        self.assertEqual(sender, self.sender)
        self.assertEqual(name, "Steve")
        self.assertEqual(message, {"text": "hello"})
        self.assertEqual(chat_type_id, 2)  # holder 3 -> registry id 2
        self.assertEqual(target_name, "Target")

    async def test_unsigned_content_wins_over_signed_body(self) -> None:
        payload = make_player_chat_payload(unsigned="modified by server")
        await self.bot._handle_player_chat(payload)
        self.assertEqual(self.capture.events[0][2], "modified by server")

    async def test_non_json_body_falls_back_to_plain_text(self) -> None:
        payload = make_player_chat_payload(content="plain text body")
        await self.bot._handle_player_chat(payload)
        self.assertEqual(self.capture.events[0][2], "plain text body")

    async def test_filter_mask_bitset_is_consumed(self) -> None:
        payload = make_player_chat_payload(filter_mask=2, mask_longs=(1, 2))
        await self.bot._handle_player_chat(payload)
        self.assertEqual(self.capture.events[0][2], {"text": "hello"})

    async def test_custom_chat_type_holder_is_skipped(self) -> None:
        payload = make_player_chat_payload(custom_chat_type=True)
        await self.bot._handle_player_chat(payload)
        self.assertEqual(self.capture.events[0][3], None)

    async def test_absent_target_name_is_none(self) -> None:
        payload = make_player_chat_payload(with_target=False)
        await self.bot._handle_player_chat(payload)
        self.assertIsNone(self.capture.events[0][4])

    async def test_seen_count_above_vanilla_cap_is_rejected(self) -> None:
        writer = PacketWriter()
        writer.write_varint(7)
        writer.write_uuid(self.sender)
        writer.write_varint(3)
        writer.write_bool(False)
        writer.write_string("hi")
        writer.write_long(1)
        writer.write_long(2)
        writer.write_varint(21)  # vanilla caps last-seen at 20
        with self.assertRaises(ProtocolError):
            await self.bot._handle_player_chat(writer.to_bytes())

    async def test_truncated_packet_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            await self.bot._handle_player_chat(b"\x07")


class ProfilelessChatDecodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1")
        self.capture = ChatCapture(self.bot)

    async def test_decodes_profileless_chat(self) -> None:
        await self.bot._handle_profileless_chat(
            make_profileless_payload(with_target=True)
        )
        (sender, name, message, chat_type_id, target_name), = self.capture.events
        self.assertIsNone(sender)
        self.assertEqual(name, "Alex")
        self.assertEqual(message, "hi")
        self.assertEqual(chat_type_id, 0)  # holder 1 -> registry id 0
        self.assertEqual(target_name, "Target")

    async def test_absent_target_is_none(self) -> None:
        await self.bot._handle_profileless_chat(make_profileless_payload())
        self.assertIsNone(self.capture.events[0][4])


class ChatDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_play_dispatch_routes_both_chat_packets(self) -> None:
        for version in ("1.21.11", "26.2"):
            bot = Bot("127.0.0.1", version=version)
            capture = ChatCapture(bot)
            ids = bot.version.packets

            await bot._handle_play(
                RawPacket(ConnectionState.PLAY, ids.clientbound_player_chat,
                          make_player_chat_payload())
            )
            await bot._handle_play(
                RawPacket(ConnectionState.PLAY, ids.clientbound_profileless_chat,
                          make_profileless_payload())
            )
            self.assertEqual(len(capture.events), 2)
            self.assertEqual(capture.events[0][1], "Steve")
            self.assertIsNone(capture.events[1][0])


class PacketIdTableTest(unittest.TestCase):
    def test_ids_match_verified_wire_values(self) -> None:
        """IDs verified against PrismarineJS/minecraft-data (774, 775) and
        MCProtocolLib feature/26.2 (776), cross-checked on ten shared packets."""
        self.assertEqual(SUPPORTED_VERSIONS["1.21.11"].packets.clientbound_player_chat, 0x3F)
        self.assertEqual(SUPPORTED_VERSIONS["1.21.11"].packets.clientbound_profileless_chat, 0x21)
        self.assertEqual(SUPPORTED_VERSIONS["1.21.11"].packets.serverbound_chat, 0x08)
        for version in ("26.1", "26.1.1", "26.1.2", "26.2"):
            packets = SUPPORTED_VERSIONS[version].packets
            self.assertEqual(packets.clientbound_player_chat, 0x41)
            self.assertEqual(packets.clientbound_profileless_chat, 0x21)
            self.assertEqual(packets.serverbound_chat, 0x09)


class SendMessageTest(unittest.IsolatedAsyncioTestCase):
    """The chat packet carries timestamp/salt/ack fields but no signature."""

    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1")
        self.bot.state = ConnectionState.PLAY
        self.bot._connection.send_packet = AsyncMock()

    async def test_builds_an_unsigned_chat_packet(self) -> None:
        with patch("protobot.client.time.time", return_value=1000.0), \
             patch("protobot.client.secrets.randbits", return_value=0x1122334455667788):
            await self.bot.send_message("hello")

        self.bot._connection.send_packet.assert_called_once()
        packet_id, payload = self.bot._connection.send_packet.call_args[0]
        self.assertEqual(packet_id, self.bot.version.packets.serverbound_chat)

        reader = PacketReader(payload)
        self.assertEqual(reader.read_string(max_chars=256), "hello")
        self.assertEqual(reader.read_long(), 1_000_000)
        self.assertEqual(reader.read_unsigned_long(), 0x1122334455667788)
        self.assertFalse(reader.read_bool())
        self.assertEqual(reader.read_varint(), 0)
        self.assertEqual(reader.read_raw(3), b"\x00\x00\x00")
        self.assertEqual(reader.read_unsigned_byte(), 0)
        self.assertEqual(reader.remaining, 0)

    async def test_rejects_non_string(self) -> None:
        with self.assertRaises(TypeError):
            await self.bot.send_message(123)  # type: ignore[arg-type]

    async def test_rejects_empty_message(self) -> None:
        with self.assertRaises(ValueError):
            await self.bot.send_message("")

    async def test_rejects_oversized_message(self) -> None:
        with self.assertRaises(ValueError):
            await self.bot.send_message("x" * 257)
        # exactly 256 characters is allowed
        await self.bot.send_message("x" * 256)

    async def test_requires_play_state(self) -> None:
        self.bot.state = ConnectionState.DISCONNECTED
        with self.assertRaises(RuntimeError):
            await self.bot.send_message("hi")

    async def test_works_for_every_supported_version(self) -> None:
        for version in SUPPORTED_VERSIONS:
            bot = Bot("127.0.0.1", version=version)
            bot.state = ConnectionState.PLAY
            bot._connection.send_packet = AsyncMock()
            with patch("protobot.client.time.time", return_value=1000.0), \
                 patch("protobot.client.secrets.randbits", return_value=0):
                await bot.send_message("hi")
            self.assertEqual(
                bot._connection.send_packet.call_args[0][0],
                bot.version.packets.serverbound_chat,
            )


class ChatTextParseTest(unittest.TestCase):
    def test_json_components_decode(self) -> None:
        from protobot.client import _parse_chat_text

        self.assertEqual(_parse_chat_text('{"text":"a","color":"red"}'), {"text": "a", "color": "red"})

    def test_plain_text_survives(self) -> None:
        from protobot.client import _parse_chat_text

        self.assertEqual(_parse_chat_text("just words"), "just words")


class ComponentEncodingTest(unittest.TestCase):
    def test_string_tag_helper_roundtrip(self) -> None:
        from protobot.protocol.codec import PacketReader
        from protobot.protocol.nbt import read_anonymous_nbt

        reader = PacketReader(component("测试"))
        self.assertEqual(read_anonymous_nbt(reader), "测试")
        self.assertEqual(reader.remaining, 0)

    def test_style_compound_parses_empty(self) -> None:
        from protobot.protocol.codec import PacketReader
        from protobot.protocol.nbt import read_anonymous_nbt

        reader = PacketReader(EMPTY_COMPOUND_STYLE)
        self.assertEqual(read_anonymous_nbt(reader), {})

    def test_encode_rejects_oversized_string(self) -> None:
        with self.assertRaises(ValueError):
            encode_nbt_string("x" * 70000)

    def test_packed_structure_stays_intact(self) -> None:
        # The helper packs an unsigned short length; assert the layout.
        packed = encode_nbt_string("ab")
        self.assertEqual(packed, struct.pack(">H", 2) + b"ab")


if __name__ == "__main__":
    unittest.main()
