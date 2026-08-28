"""死亡检测、重生请求，以及 plugins/respawn.py 自动重生插件的测试。

协议部分核对的是本仓库版本表里 775/776 的三个包：
``clientbound_set_health`` 0x68、``clientbound_player_combat_kill`` 0x44、
``serverbound_client_command`` 0x0C（载荷只有一个 VarInt，0 = perform respawn）。
1.21.11（774）这三个 ID 未经核实，表里留 0，核心必须跳过而不是错认 0x00。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from protobot.client import Bot
from protobot.errors import UnsupportedVersion
from protobot.plugin import PluginManager
from protobot.protocol.codec import PacketReader, PacketWriter
from protobot.protocol.connection import ConnectionState, RawPacket
from protobot.protocol.nbt import encode_nbt_string
from protobot.protocol.versions import SUPPORTED_VERSIONS
from protobot.settings import PluginSettings

RESPAWN_FILE = Path(__file__).resolve().parent.parent / "plugins" / "respawn.py"


def component(value: str) -> bytes:
    """把纯文本组件编成匿名 NBT StringTag（与 test_chat.py 同一套）。"""
    return b"\x08" + encode_nbt_string(value)


def health_payload(
    health: float = 20.0, food: int = 20, saturation: float = 5.0
) -> bytes:
    return (
        PacketWriter()
        .write_float(health)
        .write_varint(food)
        .write_float(saturation)
        .to_bytes()
    )


def combat_kill_payload(entity_id: int, message: str = "Steve was slain") -> bytes:
    return (
        PacketWriter().write_varint(entity_id).write_raw(component(message)).to_bytes()
    )


def respawn_payload() -> bytes:
    """Respawn 包 = SpawnInfo + 「保留哪些数据组件」一个字节。"""
    return (
        PacketWriter()
        .write_varint(0)  # dimension type id
        .write_string("minecraft:overworld")
        .write_long(0)  # hashed seed
        .write_byte(0)  # game mode
        .write_byte(-1)  # previous game mode
        .write_bool(False)  # is debug
        .write_bool(False)  # is flat
        .write_bool(False)  # 没有死亡地点
        .write_varint(0)  # portal cooldown
        .write_varint(63)  # sea level
        .write_unsigned_byte(0)  # data components to retain（死亡重生为 0）
        .to_bytes()
    )


class DeathCapture:
    def __init__(self, bot: Bot) -> None:
        self.deaths: list = []
        self.health: list[tuple[float, int, float]] = []
        bot.events.on("death", self._on_death)
        bot.events.on("health", self._on_health)

    async def _on_death(self, message) -> None:
        self.deaths.append(message)

    async def _on_health(self, health, food, saturation) -> None:
        self.health.append((health, food, saturation))


class PacketIdTableTest(unittest.TestCase):
    def test_verified_ids_for_775_and_776(self) -> None:
        """MCProtocolLib 的注册顺序与 minecraft.wiki 的 776 表交叉核对一致。"""
        for version in ("26.1", "26.1.1", "26.1.2", "26.2"):
            packets = SUPPORTED_VERSIONS[version].packets
            self.assertEqual(packets.clientbound_set_health, 0x68)
            self.assertEqual(packets.clientbound_player_combat_kill, 0x44)
            self.assertEqual(packets.serverbound_client_command, 0x0C)

    def test_unverified_on_1_21_11(self) -> None:
        # 774 上这三个 ID 没核实过，留 0 让功能自行降级，别拿推断值发包。
        packets = SUPPORTED_VERSIONS["1.21.11"].packets
        self.assertEqual(packets.clientbound_set_health, 0)
        self.assertEqual(packets.clientbound_player_combat_kill, 0)
        self.assertEqual(packets.serverbound_client_command, 0)


class HealthDecodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1", version="26.2")
        self.capture = DeathCapture(self.bot)

    async def test_fields_decode_in_order(self) -> None:
        await self.bot._handle_set_health(health_payload(7.5, 13, 2.25))
        self.assertAlmostEqual(self.bot.player.health, 7.5, places=5)
        self.assertEqual(self.bot.player.food, 13)
        self.assertAlmostEqual(self.bot.player.saturation, 2.25, places=5)
        self.assertEqual(len(self.capture.health), 1)
        self.assertFalse(self.bot.player.dead)

    async def test_zero_health_is_a_death(self) -> None:
        await self.bot._handle_set_health(health_payload(0.0, 0, 0.0))
        self.assertTrue(self.bot.player.dead)
        self.assertEqual(self.capture.deaths, [None])  # 血量路没有死亡消息

    async def test_repeated_zero_health_fires_once(self) -> None:
        # 死亡窗口里服务端会反复补发血量 0；一次死亡只该有一个 death 事件。
        for _ in range(3):
            await self.bot._handle_set_health(health_payload(0.0, 0, 0.0))
        self.assertEqual(len(self.capture.deaths), 1)

    async def test_dispatch_routes_the_packet(self) -> None:
        ids = self.bot.version.packets
        await self.bot._handle_play(
            RawPacket(ConnectionState.PLAY, ids.clientbound_set_health,
                      health_payload(11.0, 9, 1.0))
        )
        self.assertAlmostEqual(self.bot.player.health, 11.0, places=5)

    async def test_1_21_11_does_not_mistake_packet_zero(self) -> None:
        """774 的 set_health ID 是 0；派发必须跳过，别把 0x00 当血量包。"""
        bot = Bot("127.0.0.1", version="1.21.11")
        capture = DeathCapture(bot)
        await bot._handle_play(RawPacket(ConnectionState.PLAY, 0x00, b""))
        self.assertEqual(capture.health, [])
        self.assertEqual(capture.deaths, [])
        self.assertEqual(bot.player.health, 20.0)


class CombatKillTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1", version="26.2")
        self.bot.session.entity_id = 42
        self.capture = DeathCapture(self.bot)

    async def test_own_death_carries_the_message(self) -> None:
        await self.bot._handle_player_combat_kill(combat_kill_payload(42, "被苦力怕炸死"))
        self.assertTrue(self.bot.player.dead)
        self.assertEqual(self.bot.player.health, 0.0)
        self.assertEqual(self.capture.deaths, ["被苦力怕炸死"])

    async def test_another_players_death_is_ignored(self) -> None:
        await self.bot._handle_player_combat_kill(combat_kill_payload(99))
        self.assertFalse(self.bot.player.dead)
        self.assertEqual(self.capture.deaths, [])

    async def test_unknown_own_entity_id_still_counts(self) -> None:
        # 登录包还没到（entity_id 为 None）时不该把死亡信号丢掉。
        self.bot.session.entity_id = None
        await self.bot._handle_player_combat_kill(combat_kill_payload(7))
        self.assertTrue(self.bot.player.dead)

    async def test_health_zero_after_combat_kill_does_not_refire(self) -> None:
        await self.bot._handle_player_combat_kill(combat_kill_payload(42))
        await self.bot._handle_set_health(health_payload(0.0, 0, 0.0))
        self.assertEqual(len(self.capture.deaths), 1)

    async def test_dispatch_routes_the_packet(self) -> None:
        ids = self.bot.version.packets
        await self.bot._handle_play(
            RawPacket(ConnectionState.PLAY, ids.clientbound_player_combat_kill,
                      combat_kill_payload(42))
        )
        self.assertTrue(self.bot.player.dead)

    async def test_respawn_packet_clears_the_dead_flag(self) -> None:
        await self.bot._handle_player_combat_kill(combat_kill_payload(42))
        self.bot._handle_respawn(respawn_payload())
        self.assertFalse(self.bot.player.dead)
        self.assertFalse(self.bot.player.loaded)  # 之后的位置包会补发 Player Loaded


class RespawnRequestTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Bot("127.0.0.1", version="26.2")
        self.bot.state = ConnectionState.PLAY
        self.bot._connection.send_packet = AsyncMock()

    async def test_sends_client_command_with_action_zero(self) -> None:
        await self.bot.respawn()
        packet_id, payload = self.bot._connection.send_packet.call_args[0]
        self.assertEqual(packet_id, 0x0C)
        reader = PacketReader(payload)
        self.assertEqual(reader.read_varint(), 0)  # 0 = perform respawn
        self.assertEqual(reader.remaining, 0)

    async def test_unverified_version_refuses_to_guess(self) -> None:
        bot = Bot("127.0.0.1", version="1.21.11")
        bot.state = ConnectionState.PLAY
        bot._connection.send_packet = AsyncMock()
        with self.assertRaises(UnsupportedVersion):
            await bot.respawn()
        bot._connection.send_packet.assert_not_called()

    async def test_requires_play_state(self) -> None:
        self.bot.state = ConnectionState.CONFIGURATION
        with self.assertRaises(Exception):
            await self.bot.respawn()


class FakeBot:
    """插件只需要这些：player 状态、respawn()、发消息、寻路。"""

    def __init__(self, *, dead: bool = True, unsupported: bool = False) -> None:
        self.username = "FakeBot"
        self.player = SimpleNamespace(
            x=10.0, y=64.0, z=-20.0, health=0.0 if dead else 20.0,
            food=17, dead=dead, position=(10.0, 64.0, -20.0),
        )
        self.respawn_calls = 0
        self.sent_messages: list[str] = []
        self.navigated: list[tuple[float, float]] = []
        self.unsupported = unsupported
        self.auto_confirm: object | None = None  # 设成插件后自动回 respawn 事件

    async def respawn(self) -> None:
        self.respawn_calls += 1
        if self.unsupported:
            raise UnsupportedVersion("未核实")
        if self.auto_confirm is not None:
            self.player.dead = False
            await self.auto_confirm._on_respawn(None)

    async def send_message(self, text: str) -> None:
        self.sent_messages.append(text)

    async def wait_world(self, *, timeout: float = 30.0) -> None:
        return None

    async def navigate_to(self, x: float, z: float, **kwargs) -> None:
        self.navigated.append((x, z))


def load_plugin(tmp: str, settings: dict | None = None):
    manager = PluginManager([RESPAWN_FILE.parent])
    manager.discover()
    plugin = manager.plugins["respawn"]
    plugin.manager = manager
    plugin._config = PluginSettings(
        Path(tmp) / "respawn.json",
        sys.modules[type(plugin).__module__].DEFAULT_SETTINGS,
        label="自动重生",
        normalize=type(plugin)._normalize,
    )
    if settings is not None:
        plugin._config.path.write_text(json.dumps(settings), encoding="utf-8")
    plugin._config.load()
    plugin._settings = plugin._config.data
    return manager, plugin


async def drain(plugin) -> None:
    """等插件的重生任务跑完（测试里 delay/retry_delay 都设成极小值）。"""
    task = plugin._respawn_task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)


FAST = {"delay": 0.0, "retry_delay": 0.05, "max_retries": 1}


class PluginSettingsTest(unittest.TestCase):
    def test_template_is_written_and_on_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp)
            saved = json.loads(plugin._config.path.read_text(encoding="utf-8"))
            self.assertTrue(saved["enabled"])
            self.assertFalse(saved["return_to_death_point"])
            self.assertEqual(saved["announce"], "")

    def test_bad_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(
                tmp,
                {"delay": "soon", "max_retries": -3, "return_max_distance": None},
            )
            self.assertEqual(plugin._settings["delay"], 1.0)
            self.assertEqual(plugin._settings["max_retries"], 0)
            self.assertEqual(plugin._settings["return_max_distance"], 200.0)

    def test_negative_delay_is_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"delay": -5})
            self.assertEqual(plugin._settings["delay"], 0.0)

    def test_announce_is_capped_at_the_chat_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"announce": "啊" * 400})
            self.assertEqual(len(plugin._settings["announce"]), 256)


class DeathFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plugin(self, settings: dict | None = None, **bot_kwargs):
        merged = dict(FAST)
        merged.update(settings or {})
        _, plugin = load_plugin(self._tmp.name, merged)
        plugin.bot = FakeBot(**bot_kwargs)
        plugin.bot.auto_confirm = plugin
        return plugin

    async def test_death_triggers_a_respawn_request(self) -> None:
        plugin = self._plugin()
        await plugin._on_death("被苦力怕炸死")
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 1)
        self.assertEqual(plugin._deaths, 1)
        self.assertEqual(plugin._last_message, "被苦力怕炸死")
        self.assertEqual(plugin._last_death, (10.0, 64.0, -20.0))

    async def test_disabled_does_not_request(self) -> None:
        plugin = self._plugin({"enabled": False})
        await plugin._on_death(None)
        self.assertIsNone(plugin._respawn_task)
        self.assertEqual(plugin.bot.respawn_calls, 0)

    async def test_retries_when_no_confirmation_arrives(self) -> None:
        plugin = self._plugin({"max_retries": 2})
        plugin.bot.auto_confirm = None  # 服务器一直不回 respawn 包
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 3)  # 首发 + 2 次重试

    async def test_no_retry_when_max_retries_is_zero(self) -> None:
        plugin = self._plugin({"max_retries": 0})
        plugin.bot.auto_confirm = None
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 1)

    async def test_stops_retrying_once_confirmed(self) -> None:
        plugin = self._plugin({"max_retries": 3})
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 1)

    async def test_unverified_version_warns_once_and_gives_up(self) -> None:
        plugin = self._plugin({"max_retries": 2}, unsupported=True)
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 1)  # 不重试，也不乱发包
        self.assertTrue(plugin._warned_unsupported)

    async def test_without_a_bot_the_request_is_dropped(self) -> None:
        plugin = self._plugin()
        plugin.bot = None
        await plugin._on_death(None)
        await drain(plugin)  # 不应抛出

    async def test_a_second_death_signal_does_not_double_up(self) -> None:
        plugin = self._plugin({"delay": 0.2})
        await plugin._on_death(None)
        first = plugin._respawn_task
        await plugin._on_death(None)  # 血量路紧跟 combat_kill 的情况
        self.assertIs(plugin._respawn_task, first)
        await drain(plugin)
        self.assertEqual(plugin.bot.respawn_calls, 1)

    async def test_announce_is_sent_after_respawning(self) -> None:
        plugin = self._plugin({"announce": "我回来了"})
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.sent_messages, ["我回来了"])

    async def test_nothing_is_said_by_default(self) -> None:
        plugin = self._plugin()
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.sent_messages, [])

    async def test_walks_back_to_the_death_point_when_asked(self) -> None:
        plugin = self._plugin({"return_to_death_point": True})
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.navigated, [(10.0, -20.0)])

    async def test_does_not_walk_back_by_default(self) -> None:
        plugin = self._plugin()
        await plugin._on_death(None)
        await drain(plugin)
        self.assertEqual(plugin.bot.navigated, [])

    async def test_far_death_point_is_not_walked_to(self) -> None:
        plugin = self._plugin(
            {"return_to_death_point": True, "return_max_distance": 50.0}
        )
        await plugin._on_death(None)
        # 重生点离死亡点 1000 格：超过上限就不走
        plugin.bot.player.position = (1000.0, 64.0, -20.0)
        await drain(plugin)
        self.assertEqual(plugin.bot.navigated, [])

    async def test_a_failed_walk_back_is_only_logged(self) -> None:
        plugin = self._plugin({"return_to_death_point": True})

        async def failing(x, z, **kwargs):
            raise RuntimeError("no path")

        plugin.bot.navigate_to = failing
        await plugin._on_death(None)
        await drain(plugin)  # 不应抛出


class ExposedServiceTest(unittest.IsolatedAsyncioTestCase):
    """respawn.status / respawn.now / respawn.set（其他插件与 LLM 共用）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, dict(FAST))
        self.manager = self.plugin.manager
        self.plugin.bot = FakeBot(dead=False)
        self.plugin.bot.auto_confirm = self.plugin

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _saved(self) -> dict:
        return json.loads(self.plugin._config.path.read_text(encoding="utf-8"))

    def test_exposures_declared(self) -> None:
        exposed = {service.name: service for service in self.plugin.exposed()}
        self.assertEqual(sorted(exposed), ["now", "set", "status"])
        self.assertFalse(exposed["status"].admin)  # 只读，不限管理员
        self.assertTrue(exposed["now"].admin)
        self.assertTrue(exposed["set"].admin)
        self.assertEqual(exposed["status"].tool_name, "respawn_status")

    async def test_status_while_alive(self) -> None:
        status = await self.plugin._service_status()
        self.assertIn("Auto-respawn is on", status)
        self.assertIn("alive", status)
        self.assertIn("health 20.0/20", status)
        self.assertIn("food 17", status)

    async def test_status_while_dead_reports_the_last_death(self) -> None:
        self.plugin.bot.player.dead = True
        await self.plugin._on_death("摔死了")
        await drain(self.plugin)
        status = await self.plugin._service_status()
        self.assertIn("1 death(s)", status)
        self.assertIn("last died at 10.0 64.0 -20.0", status)
        self.assertIn("摔死了", status)

    async def test_status_without_a_bot(self) -> None:
        self.plugin.bot = None
        self.assertIn("not connected", await self.plugin._service_status())

    async def test_now_requires_being_dead(self) -> None:
        result = await self.plugin._service_now()
        self.assertIn("nothing to respawn from", result)
        self.assertEqual(self.plugin.bot.respawn_calls, 0)

    async def test_now_respawns_immediately(self) -> None:
        self.plugin.bot.player.dead = True
        result = await self.plugin._service_now()
        self.assertIn("Respawn requested", result)
        await drain(self.plugin)
        self.assertEqual(self.plugin.bot.respawn_calls, 1)

    async def test_now_skips_a_pending_delay(self) -> None:
        self.plugin._settings["delay"] = 30.0  # 本来要等 30 秒
        self.plugin.bot.player.dead = True
        await self.plugin._on_death(None)
        waiting = self.plugin._respawn_task
        await self.plugin._service_now()
        await asyncio.sleep(0)  # 让取消落地
        self.assertIsNot(self.plugin._respawn_task, waiting)
        self.assertTrue(waiting.cancelled() or waiting.cancelling())
        await drain(self.plugin)
        self.assertEqual(self.plugin.bot.respawn_calls, 1)

    async def test_now_without_a_bot(self) -> None:
        self.plugin.bot = None
        self.assertIn("Not connected", await self.plugin._service_now())

    async def test_set_writes_only_the_given_keys(self) -> None:
        result = await self.plugin._service_set(enabled=False)
        self.assertIn("enabled=False", result)
        self.assertFalse(self.plugin._settings["enabled"])
        self.assertFalse(self._saved()["enabled"])
        self.assertEqual(self._saved()["retry_delay"], 0.05)  # 未提及的保持原样

    async def test_set_normalizes_before_saving(self) -> None:
        await self.plugin._service_set(delay="3")
        self.assertEqual(self._saved()["delay"], 3.0)

    async def test_set_rejects_unknown_fields(self) -> None:
        result = await self.plugin._service_set(sprint=True)
        self.assertIn("Unknown field(s): sprint", result)

    async def test_set_with_nothing_to_do(self) -> None:
        self.assertIn("Nothing to change", await self.plugin._service_set())

    async def test_set_can_turn_the_walk_back_on(self) -> None:
        await self.plugin._service_set(return_to_death_point=True)
        self.assertTrue(self._saved()["return_to_death_point"])

    async def test_llm_arguments_are_filtered_to_the_schema(self) -> None:
        # 模型常常多塞一个 reason 之类的键，框架按 schema 过滤后再调用
        service = {s.name: s for s in self.plugin.exposed()}["set"]
        filtered = service.filter_arguments({"enabled": False, "reason": "怕刷屏"})
        self.assertEqual(filtered, {"enabled": False})
        self.assertIn("updated", await self.plugin._service_set(**filtered))

    async def test_callable_through_the_manager(self) -> None:
        await self.manager.enable_all()
        try:
            self.assertIn("respawn.status", self.manager.services())
            result = await self.manager.call_service("respawn.status")
            self.assertIn("Auto-respawn is", result)
        finally:
            await self.manager.disable_all()


class BotReadyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, dict(FAST))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_dead_on_arrival_gets_a_respawn(self) -> None:
        # 热重载或重连时正躺在死亡界面上：没有 death 事件可等，得自己补一次
        self.plugin.bot = FakeBot(dead=True)
        self.plugin.bot.auto_confirm = self.plugin
        await self.plugin.on_bot_ready()
        await drain(self.plugin)
        self.assertEqual(self.plugin.bot.respawn_calls, 1)

    async def test_alive_on_arrival_does_nothing(self) -> None:
        self.plugin.bot = FakeBot(dead=False)
        await self.plugin.on_bot_ready()
        self.assertIsNone(self.plugin._respawn_task)

    async def test_a_stale_flow_from_the_old_connection_is_cancelled(self) -> None:
        self.plugin._settings["delay"] = 30.0
        self.plugin.bot = FakeBot(dead=True)
        await self.plugin._on_death(None)
        stale = self.plugin._respawn_task
        self.plugin.bot = FakeBot(dead=False)  # 重连后的新 bot，活着
        await self.plugin.on_bot_ready()
        await asyncio.sleep(0)  # 让取消落地
        self.assertTrue(stale.cancelled() or stale.cancelling())
        self.assertIsNone(self.plugin._respawn_task)


if __name__ == "__main__":
    unittest.main()
