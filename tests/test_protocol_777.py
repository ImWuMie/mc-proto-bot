"""Regression tests for protocol 777 (Minecraft 26.3) wire changes."""

from __future__ import annotations

import struct
import unittest
import uuid

from protobot.client import Bot
from protobot.errors import ProtocolError
from protobot.protocol import PacketReader, PacketWriter
from protobot.protocol.connection import ConnectionState
from protobot.protocol.versions import get_version


def _make_bot(version: str = "26.3") -> tuple[Bot, list[tuple[int, bytes]]]:
    bot = Bot("localhost", username="Proto777", version=version)
    bot.state = ConnectionState.PLAY
    sent: list[tuple[int, bytes]] = []

    async def capture(packet_id: int, payload: bytes = b"") -> None:
        sent.append((packet_id, payload))

    bot.send_raw = capture  # type: ignore[method-assign]
    return bot, sent


def _spawn_info_777(*, game_mode: int, previous: int | None) -> bytes:
    return (
        PacketWriter()
        .write_varint(0)  # dimension type
        .write_string("minecraft:overworld")
        .write_long(0)  # hashed seed
        .write_varint(game_mode)
        .write_varint(0 if previous is None else previous + 1)
        .write_bool(False)  # debug
        .write_bool(False)  # flat
        .write_bool(False)  # no death location
        .write_varint(0)  # portal cooldown
        .write_varint(63)  # sea level
        .to_bytes()
    )


def _player_position(teleport_id: int, x: float, y: float, z: float) -> bytes:
    writer = PacketWriter().write_varint(teleport_id)
    for value in (x, y, z, 0.0, 0.0, 0.0):
        writer.write_double(value)
    writer.write_float(90.0).write_float(-10.0).write_raw(struct.pack(">I", 0))
    return writer.to_bytes()


class VersionTableTests(unittest.TestCase):
    def test_26_3_maps_to_protocol_777(self) -> None:
        spec = get_version("26.3")
        self.assertEqual(spec.protocol, 777)
        self.assertEqual(spec.data_version, 5023)

    def test_configuration_ids_shift_after_post_effects(self) -> None:
        old = get_version("26.2").configuration
        new = get_version("26.3").configuration
        self.assertEqual(
            (
                old.clientbound_transfer,
                old.clientbound_select_known_packs,
                old.clientbound_code_of_conduct,
            ),
            (0x0B, 0x0E, 0x13),
        )
        self.assertEqual(
            (
                new.clientbound_transfer,
                new.clientbound_select_known_packs,
                new.clientbound_code_of_conduct,
            ),
            (0x0C, 0x0F, 0x14),
        )

    def test_entity_ids_follow_26_3_registry(self) -> None:
        old, new = get_version("26.2"), get_version("26.3")
        self.assertEqual((old.happy_ghast_entity, old.shulker_entity), (58, 112))
        self.assertEqual((new.happy_ghast_entity, new.shulker_entity), (59, 115))

    def test_serverbound_ids_shift_after_punch(self) -> None:
        packets = get_version("26.3").packets
        self.assertEqual(packets.serverbound_set_carried_item, 0x36)
        self.assertEqual(packets.clientbound_chunk_data, 0x2E)

    def test_bundled_block_table_is_loaded(self) -> None:
        bot, _sent = _make_bot()
        # 26.3 inserts poplar_planks at 27, shifting every later state id.
        self.assertEqual(bot.world.block_states.get(27).name, "minecraft:poplar_planks")


class Decoding777Tests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_light_masks_are_byte_arrays(self) -> None:
        bot, _sent = _make_bot()
        loaded: list[tuple[int, int, bytes]] = []

        def load_chunk(x: int, z: int, data: bytes) -> object:
            loaded.append((x, z, data))
            return object()

        bot.world.load_chunk = load_chunk  # type: ignore[method-assign]
        payload = (
            PacketWriter()
            .write_int(3)
            .write_int(-2)
            .write_varint(0)  # heightmaps
            .write_bytes(b"\x01\x02")  # section data (stubbed loader)
            .write_varint(0)  # block entities
            .write_bytes(b"\xff\xff\x03")  # sky mask, 26.3 byte form
            .write_bytes(b"")  # block mask
            .write_bytes(b"\x01")  # empty sky mask
            .write_bytes(b"\x00\x01")  # empty block mask
            .write_varint(1)
            .write_bytes(bytes(2048))
            .write_varint(0)
            .to_bytes()
        )
        await bot._handle_chunk_data(payload)
        self.assertEqual(loaded, [(3, -2, b"\x01\x02")])

    async def test_partial_chat_filter_mask_is_byte_array(self) -> None:
        bot, _sent = _make_bot()
        received: list[tuple[object, ...]] = []

        async def on_chat(*args: object) -> None:
            received.append(args)

        bot.on("player_chat", on_chat)
        payload = (
            PacketWriter()
            .write_varint(0)  # global index
            .write_uuid(uuid.UUID(int=1))
            .write_varint(0)  # per-sender index
            .write_bool(False)  # no signature
            .write_string("hi", max_chars=256)
            .write_long(0)
            .write_long(0)
            .write_varint(0)  # last seen
            .write_bool(False)  # no unsigned content
            .write_varint(2)  # PARTIALLY_FILTERED
            .write_bytes(b"\x05")  # BitSet as byte array
            .write_varint(1)  # chat type registry id 0
            .write_raw(b"\x08\x00\x05Alice")
            .write_bool(False)  # no target name
            .to_bytes()
        )
        await bot._handle_player_chat(payload)
        self.assertEqual(len(received), 1)

    def test_spawn_info_uses_varint_game_types(self) -> None:
        bot, _sent = _make_bot()
        reader = PacketReader(_spawn_info_777(game_mode=3, previous=None))
        bot._read_spawn_info(reader)
        reader.expect_end()
        self.assertEqual(bot.session.game_mode, 3)
        self.assertEqual(bot.session.previous_game_mode, -1)
        self.assertTrue(bot.physics_state.spectator)

        reader = PacketReader(_spawn_info_777(game_mode=1, previous=0))
        bot._read_spawn_info(reader)
        reader.expect_end()
        self.assertEqual(
            (bot.session.game_mode, bot.session.previous_game_mode), (1, 0)
        )


    async def test_move_entity_linear_and_stepped_deltas(self) -> None:
        bot, _sent = _make_bot()
        entity = bot._entity_for_state(7)
        entity.x, entity.y, entity.z = 10.0, 64.0, -5.0

        linear = (
            PacketWriter()
            .write_varint(7)
            .write_varint(1)  # on ground, no steps
            .write_short(4096)
            .write_short(0)
            .write_short(-2048)
            .to_bytes()
        )
        await bot._handle_move_entity(linear, position=True, rotation=False)
        self.assertEqual((entity.x, entity.y, entity.z), (11.0, 64.0, -5.5))
        self.assertTrue(entity.on_ground)

        stepped = (
            PacketWriter()
            .write_varint(7)
            .write_varint(2 << 1)  # two chained steps, airborne
            .write_varint(1)
            .write_short(2048)
            .write_short(0)
            .write_short(0)
            .write_varint(2)
            .write_short(2048)
            .write_short(4096)
            .write_short(0)
            .write_byte(64)  # yaw 90
            .write_byte(0)
            .to_bytes()
        )
        await bot._handle_move_entity(stepped, position=True, rotation=True)
        self.assertEqual((entity.x, entity.y, entity.z), (12.0, 65.0, -5.5))
        self.assertFalse(entity.on_ground)
        self.assertAlmostEqual(entity.yaw, 90.0)

    async def test_move_entity_rot_reads_on_ground_first(self) -> None:
        bot, _sent = _make_bot()
        entity = bot._entity_for_state(9)
        payload = (
            PacketWriter()
            .write_varint(9)
            .write_bool(True)
            .write_byte(64)
            .write_byte(32)
            .to_bytes()
        )
        await bot._handle_move_entity(payload, position=False, rotation=True)
        self.assertTrue(entity.on_ground)
        self.assertAlmostEqual(entity.yaw, 90.0)
        self.assertAlmostEqual(entity.pitch, 45.0)

    async def test_move_entity_rejects_oversized_step_count(self) -> None:
        bot, _sent = _make_bot()
        payload = (
            PacketWriter().write_varint(1).write_varint(50 << 1).write_short(0).to_bytes()
        )
        with self.assertRaises(ProtocolError):
            await bot._handle_move_entity(payload, position=True, rotation=False)

    def test_dye_color_metadata_serializer(self) -> None:
        bot, _sent = _make_bot()
        self.assertEqual(bot._read_entity_metadata_value(PacketReader(b"\x0e"), 43), 14)
        old_bot, _ = _make_bot("26.2")
        with self.assertRaises(ProtocolError):
            old_bot._read_entity_metadata_value(PacketReader(b"\x0e"), 43)



class Movement777Tests(unittest.IsolatedAsyncioTestCase):
    async def test_teleport_confirm_echoes_position(self) -> None:
        bot, sent = _make_bot()
        bot.player.loaded = True
        await bot._handle_position(_player_position(42, 1.5, 70.0, -3.25))

        confirm_id = bot.version.packets.serverbound_teleport_confirm
        confirms = [payload for packet_id, payload in sent if packet_id == confirm_id]
        self.assertEqual(len(confirms), 1)
        reader = PacketReader(confirms[0])
        self.assertEqual(reader.read_varint(), 42)
        self.assertEqual([reader.read_double() for _ in range(3)], [1.5, 70.0, -3.25])
        self.assertEqual((reader.read_float(), reader.read_float()), (90.0, -10.0))
        reader.expect_end()

    async def test_teleport_confirm_unchanged_before_777(self) -> None:
        bot, sent = _make_bot("26.2")
        bot.player.loaded = True
        await bot._handle_position(_player_position(5, 0.0, 0.0, 0.0))
        confirm_id = bot.version.packets.serverbound_teleport_confirm
        self.assertEqual(
            [payload for packet_id, payload in sent if packet_id == confirm_id],
            [b"\x05"],
        )

    async def test_second_position_packet_closes_tick_first(self) -> None:
        bot, sent = _make_bot()
        packets = bot.version.packets
        await bot.send_position(0.0, 64.0, 0.0, on_ground=True)
        await bot.send_position_and_rotation(0.1, 64.0, 0.0, 0.0, 0.0, on_ground=True)
        await bot.end_tick()
        await bot.send_position(0.2, 64.0, 0.0, on_ground=True)
        self.assertEqual(
            [packet_id for packet_id, _ in sent],
            [
                packets.serverbound_position,
                packets.serverbound_tick_end,
                packets.serverbound_position_look,
                packets.serverbound_tick_end,
                packets.serverbound_position,
            ],
        )

    async def test_multiple_positions_per_tick_allowed_before_777(self) -> None:
        bot, sent = _make_bot("26.2")
        await bot.send_position(0.0, 64.0, 0.0, on_ground=True)
        await bot.send_position(0.1, 64.0, 0.0, on_ground=True)
        self.assertEqual(
            [packet_id for packet_id, _ in sent],
            [bot.version.packets.serverbound_position] * 2,
        )


if __name__ == "__main__":
    unittest.main()

