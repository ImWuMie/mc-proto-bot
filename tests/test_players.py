"""玩家列表解码与 player_join / player_leave 事件。

协议部分核对的是 775/776 的两个包：``clientbound_player_info_remove``
0x45 与 ``clientbound_player_info_update`` 0x46——两侧被表里已核实的
``player_combat_kill`` 0x44 与 ``player_position`` 0x48 夹住。1.21.11（774）
这两个 ID 未经核实，表里留 0，核心必须跳过而不是错认 0x00。
"""

from __future__ import annotations

import time
import unittest
import uuid

from protobot.client import Bot
from protobot.protocol.codec import PacketWriter
from protobot.protocol.connection import ConnectionState, RawPacket
from protobot.protocol.nbt import encode_nbt_string
from protobot.protocol.versions import SUPPORTED_VERSIONS
from protobot.state import PlayerListEntry

ADD = 0x01
INIT_CHAT = 0x02
GAME_MODE = 0x04
LISTED = 0x08
LATENCY = 0x10
DISPLAY_NAME = 0x20
LIST_ORDER = 0x40
HAT = 0x80


def component(value: str) -> bytes:
    return b"\x08" + encode_nbt_string(value)


def add_entry(
    writer: PacketWriter,
    entry_uuid: uuid.UUID,
    name: str,
    *,
    properties: int = 0,
) -> PacketWriter:
    writer.write_uuid(entry_uuid).write_string(name).write_varint(properties)
    for index in range(properties):
        writer.write_string(f"prop{index}").write_string("value").write_bool(False)
    return writer


def full_entry(
    writer: PacketWriter,
    entry_uuid: uuid.UUID,
    name: str,
    *,
    game_mode: int = 0,
    latency: int = 0,
    listed: bool = True,
    display_name: str | None = None,
) -> PacketWriter:
    """一个带上全部八个动作的条目（原版进服时就是这么发的）。"""

    add_entry(writer, entry_uuid, name)
    writer.write_bool(False)  # 没有聊天会话
    writer.write_varint(game_mode)
    writer.write_bool(listed)
    writer.write_varint(latency)
    if display_name is None:
        writer.write_bool(False)
    else:
        writer.write_bool(True).write_raw(component(display_name))
    writer.write_varint(0)  # tab 列表排序
    writer.write_bool(True)  # 显示帽子
    return writer


FULL_ACTIONS = (
    ADD | INIT_CHAT | GAME_MODE | LISTED | LATENCY | DISPLAY_NAME | LIST_ORDER | HAT
)


def roster_payload(*players: tuple[uuid.UUID, str]) -> bytes:
    writer = PacketWriter().write_unsigned_byte(FULL_ACTIONS).write_varint(len(players))
    for entry_uuid, name in players:
        full_entry(writer, entry_uuid, name)
    return writer.to_bytes()


def add_payload(*players: tuple[uuid.UUID, str]) -> bytes:
    """只带 add_player 动作（代理与插件常这么发）。"""
    writer = PacketWriter().write_unsigned_byte(ADD).write_varint(len(players))
    for entry_uuid, name in players:
        add_entry(writer, entry_uuid, name)
    return writer.to_bytes()


def remove_payload(*uuids: uuid.UUID) -> bytes:
    writer = PacketWriter().write_varint(len(uuids))
    for entry_uuid in uuids:
        writer.write_uuid(entry_uuid)
    return writer.to_bytes()


class Capture:
    def __init__(self, bot: Bot) -> None:
        self.joined: list[PlayerListEntry] = []
        self.left: list[PlayerListEntry] = []
        self.rosters: list[tuple] = []
        self.unparsed: list[str] = []
        bot.events.on("player_join", self._join)
        bot.events.on("player_leave", self._leave)
        bot.events.on("player_list", self._roster)
        bot.events.on("player_list_unparsed", self._unparsed)

    async def _join(self, entry) -> None:
        self.joined.append(entry)

    async def _leave(self, entry) -> None:
        self.left.append(entry)

    async def _roster(self, players) -> None:
        self.rosters.append(players)

    async def _unparsed(self, reason, payload) -> None:
        self.unparsed.append(reason)


STEVE = uuid.UUID("11111111-1111-1111-1111-111111111111")
ALEX = uuid.UUID("22222222-2222-2222-2222-222222222222")


class PacketIdTableTest(unittest.TestCase):
    def test_verified_ids_for_775_and_776(self) -> None:
        for version in ("26.1", "26.1.1", "26.1.2", "26.2"):
            packets = SUPPORTED_VERSIONS[version].packets
            self.assertEqual(packets.clientbound_player_info_remove, 0x45)
            self.assertEqual(packets.clientbound_player_info_update, 0x46)

    def test_ids_sit_between_their_verified_neighbours(self) -> None:
        """派生依据：combat_kill(0x44) 与 player_position(0x48) 之间只有
        info_remove、info_update、look_at 三个包，没有多余空位。"""
        packets = SUPPORTED_VERSIONS["26.2"].packets
        self.assertEqual(packets.clientbound_player_combat_kill, 0x44)
        self.assertEqual(packets.clientbound_player_info_remove, 0x45)
        self.assertEqual(packets.clientbound_player_info_update, 0x46)
        self.assertEqual(packets.clientbound_position, 0x48)

    def test_unverified_on_1_21_11(self) -> None:
        packets = SUPPORTED_VERSIONS["1.21.11"].packets
        self.assertEqual(packets.clientbound_player_info_update, 0)
        self.assertEqual(packets.clientbound_player_info_remove, 0)


class RosterDecodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1", username="MyBot", version="26.2")
        self.capture = Capture(self.bot)

    async def test_full_entry_decodes_every_field(self) -> None:
        writer = PacketWriter().write_unsigned_byte(FULL_ACTIONS).write_varint(1)
        full_entry(
            writer, STEVE, "Steve", game_mode=1, latency=42,
            listed=True, display_name="[VIP] Steve",
        )
        await self.bot._handle_player_info_update(writer.to_bytes())
        entry = self.bot.players[STEVE]
        self.assertEqual(entry.name, "Steve")
        self.assertEqual(entry.game_mode, 1)
        self.assertEqual(entry.latency, 42)
        self.assertTrue(entry.listed)
        self.assertEqual(entry.display_name, "[VIP] Steve")
        self.assertEqual(entry.label, "[VIP] Steve")
        self.assertEqual(self.capture.unparsed, [])

    async def test_properties_are_skipped(self) -> None:
        writer = PacketWriter().write_unsigned_byte(ADD).write_varint(1)
        add_entry(writer, STEVE, "Steve", properties=2)
        await self.bot._handle_player_info_update(writer.to_bytes())
        self.assertEqual(self.bot.online_players, ("Steve",))

    async def test_signed_properties_are_skipped(self) -> None:
        writer = PacketWriter().write_unsigned_byte(ADD).write_varint(1)
        writer.write_uuid(STEVE).write_string("Steve").write_varint(1)
        writer.write_string("textures").write_string("base64").write_bool(True)
        writer.write_string("signature")
        await self.bot._handle_player_info_update(writer.to_bytes())
        self.assertEqual(self.bot.online_players, ("Steve",))

    async def test_chat_session_is_skipped(self) -> None:
        writer = PacketWriter().write_unsigned_byte(ADD | INIT_CHAT).write_varint(1)
        add_entry(writer, STEVE, "Steve")
        writer.write_bool(True).write_uuid(ALEX).write_long(123)
        writer.write_bytes(b"\x01\x02").write_bytes(b"\x03")
        await self.bot._handle_player_info_update(writer.to_bytes())
        self.assertEqual(self.bot.online_players, ("Steve",))

    async def test_partial_update_keeps_earlier_fields(self) -> None:
        await self.bot._handle_player_info_update(roster_payload((STEVE, "Steve")))
        writer = PacketWriter().write_unsigned_byte(LATENCY).write_varint(1)
        writer.write_uuid(STEVE).write_varint(99)
        await self.bot._handle_player_info_update(writer.to_bytes())
        entry = self.bot.players[STEVE]
        self.assertEqual(entry.name, "Steve")  # 名字没被清掉
        self.assertEqual(entry.latency, 99)

    async def test_latency_only_update_is_not_a_join(self) -> None:
        self.bot._roster_synced = True
        self.bot._roster_deadline = 0.0
        writer = PacketWriter().write_unsigned_byte(LATENCY).write_varint(1)
        writer.write_uuid(ALEX).write_varint(10)
        await self.bot._handle_player_info_update(writer.to_bytes())
        self.assertEqual(self.capture.joined, [])
        self.assertIn(ALEX, self.bot.players)  # 但列表里仍然记着这个 UUID

    async def test_unknown_action_bit_discards_the_packet(self) -> None:
        """动作位集将来变成两字节时，宁可没有事件，也不要错位的人名。"""
        payload = PacketWriter().write_unsigned_byte(0xFF).write_varint(1).write_uuid(
            STEVE
        ).write_string("Steve").write_varint(0).to_bytes()
        await self.bot._handle_player_info_update(payload)
        self.assertEqual(self.bot.players, {})
        self.assertEqual(len(self.capture.unparsed), 1)

    async def test_truncated_payload_is_reported_not_raised(self) -> None:
        await self.bot._handle_player_info_update(
            PacketWriter().write_unsigned_byte(ADD).write_varint(2).to_bytes()
        )
        self.assertEqual(self.bot.players, {})
        self.assertEqual(len(self.capture.unparsed), 1)

    async def test_trailing_bytes_are_reported(self) -> None:
        payload = roster_payload((STEVE, "Steve")) + b"\x00"
        await self.bot._handle_player_info_update(payload)
        self.assertEqual(self.bot.players, {})
        self.assertEqual(len(self.capture.unparsed), 1)

    async def test_empty_name_is_rejected(self) -> None:
        payload = PacketWriter().write_unsigned_byte(ADD).write_varint(1).write_uuid(
            STEVE
        ).write_string("").write_varint(0).to_bytes()
        await self.bot._handle_player_info_update(payload)
        self.assertEqual(self.capture.unparsed and self.bot.players, {})


class JoinLeaveEventTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1", username="MyBot", version="26.2")
        self.capture = Capture(self.bot)

    async def sync_roster(self, *players: tuple[uuid.UUID, str]) -> None:
        """走一遍进服时的初始名单，之后的更新才算加入。"""
        await self.bot._handle_player_info_update(roster_payload(*players))
        self.bot._roster_deadline = 0.0  # 宽限期结束

    async def test_initial_roster_is_not_a_flood_of_joins(self) -> None:
        await self.sync_roster((STEVE, "Steve"), (ALEX, "Alex"))
        self.assertEqual(self.capture.joined, [])
        self.assertEqual(len(self.capture.rosters), 1)
        self.assertEqual(self.bot.online_players, ("Alex", "Steve"))

    async def test_a_later_add_is_a_join(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        await self.bot._handle_player_info_update(add_payload((ALEX, "Alex")))
        self.assertEqual([entry.name for entry in self.capture.joined], ["Alex"])

    async def test_split_roster_within_the_grace_window_is_still_a_roster(self) -> None:
        """代理有时把在线玩家拆成几个包发。"""
        self.bot._roster_synced = False
        self.bot._roster_deadline = time.monotonic() + 1.0  # 刚进 PLAY
        await self.bot._handle_player_info_update(roster_payload((STEVE, "Steve")))
        await self.bot._handle_player_info_update(roster_payload((ALEX, "Alex")))
        self.assertEqual(self.capture.joined, [])
        self.assertEqual(len(self.capture.rosters), 2)

    async def test_rejoining_the_same_player_fires_again(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        await self.bot._handle_player_info_remove(remove_payload(STEVE))
        await self.bot._handle_player_info_update(add_payload((STEVE, "Steve")))
        self.assertEqual([entry.name for entry in self.capture.left], ["Steve"])
        self.assertEqual([entry.name for entry in self.capture.joined], ["Steve"])

    async def test_repeated_update_for_a_known_player_is_not_a_join(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        await self.bot._handle_player_info_update(add_payload((STEVE, "Steve")))
        self.assertEqual(self.capture.joined, [])

    async def test_leaving_removes_the_entry(self) -> None:
        await self.sync_roster((STEVE, "Steve"), (ALEX, "Alex"))
        await self.bot._handle_player_info_remove(remove_payload(ALEX))
        self.assertEqual([entry.name for entry in self.capture.left], ["Alex"])
        self.assertEqual(self.bot.online_players, ("Steve",))

    async def test_removing_an_unknown_uuid_is_quiet(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        await self.bot._handle_player_info_remove(remove_payload(ALEX))
        self.assertEqual(self.capture.left, [])

    async def test_own_entry_never_counts_as_join_or_leave(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        await self.bot._handle_player_info_update(add_payload((self.bot.uuid, "MyBot")))
        await self.bot._handle_player_info_remove(remove_payload(self.bot.uuid))
        self.assertEqual(self.capture.joined, [])
        self.assertEqual(self.capture.left, [])

    async def test_malformed_removal_is_reported_not_raised(self) -> None:
        await self.bot._handle_player_info_remove(
            PacketWriter().write_varint(3).to_bytes()
        )
        self.assertEqual(len(self.capture.unparsed), 1)

    async def test_find_player_is_case_insensitive(self) -> None:
        await self.sync_roster((STEVE, "Steve"))
        self.assertIsNotNone(self.bot.find_player("steve"))
        self.assertIsNone(self.bot.find_player("nobody"))


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_routes_both_packets(self) -> None:
        bot = Bot("127.0.0.1", username="MyBot", version="26.2")
        capture = Capture(bot)
        ids = bot.version.packets
        await bot._handle_play(
            RawPacket(
                ConnectionState.PLAY,
                ids.clientbound_player_info_update,
                roster_payload((STEVE, "Steve")),
            )
        )
        bot._roster_deadline = 0.0
        await bot._handle_play(
            RawPacket(
                ConnectionState.PLAY,
                ids.clientbound_player_info_update,
                add_payload((ALEX, "Alex")),
            )
        )
        await bot._handle_play(
            RawPacket(
                ConnectionState.PLAY,
                ids.clientbound_player_info_remove,
                remove_payload(STEVE),
            )
        )
        self.assertEqual([entry.name for entry in capture.joined], ["Alex"])
        self.assertEqual([entry.name for entry in capture.left], ["Steve"])

    async def test_1_21_11_does_not_route_packet_zero(self) -> None:
        """774 的 ID 留 0：0x00 是 add_entity，绝不能被当成玩家列表。"""
        bot = Bot("127.0.0.1", username="MyBot", version="1.21.11")
        capture = Capture(bot)
        await bot._handle_play(
            RawPacket(ConnectionState.PLAY, 0x00, roster_payload((STEVE, "Steve")))
        )
        self.assertEqual(bot.players, {})
        self.assertEqual(capture.rosters, [])

    async def test_relogin_clears_the_list(self) -> None:
        bot = Bot("127.0.0.1", username="MyBot", version="26.2")
        await bot._handle_player_info_update(roster_payload((STEVE, "Steve")))
        bot._reset_server_state()
        self.assertEqual(bot.players, {})
        self.assertFalse(bot._roster_synced)


if __name__ == "__main__":
    unittest.main()
