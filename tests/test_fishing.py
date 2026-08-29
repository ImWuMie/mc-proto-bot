"""Tests for plugins/fishing.py (auto-fishing).

The real plugin file is loaded through PluginManager; the bot, the entity
stream, and the settings file are all faked, so nothing touches a server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from protobot.plugin import PluginManager
from protobot.protocol import PacketWriter
from protobot.settings import PluginSettings

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


class FakeRegistries:
    def __init__(self, entity_types: list[str] | None = None) -> None:
        self._entity_types = entity_types

    def get(self, registry_id: str):
        if registry_id == "minecraft:entity_type" and self._entity_types:
            return tuple(
                SimpleNamespace(key=key, value=None) for key in self._entity_types
            )
        return ()


class FakeBot:
    def __init__(self, entity_types: list[str] | None = None) -> None:
        self.username = "FakeBot"
        self.player = SimpleNamespace(x=0.0, y=64.0, z=0.0, yaw=0.0, pitch=0.0)
        self.registries = FakeRegistries(entity_types)
        # 26.x 的包表里音效包是 117；插件默认从这里取，不再硬编码
        self.version = SimpleNamespace(
            packets=SimpleNamespace(clientbound_sound=117)
        )
        self.entities: dict[int, object] = {}
        self.use_calls: list[str] = []
        self.fail_use = False

    async def use_item(self, *, hand: str = "main_hand", **kwargs) -> int:
        if self.fail_use:
            raise RuntimeError("not in play state")
        self.use_calls.append(hand)
        return len(self.use_calls)


def bobber(entity_id: int = 7, type_id: int = 130, x=0.5, y=63.0, z=0.5):
    return SimpleNamespace(entity_id=entity_id, type_id=type_id, x=x, y=y, z=z)


def plugin_module(plugin):
    """插件被 exec 进匿名模块，取它的模块级常量只能这样拿。"""
    return sys.modules[type(plugin).__module__]


def load_plugin(tmp: str, settings: dict | None = None):
    manager = PluginManager([PLUGIN_DIR])
    manager.discover()
    plugin = manager.plugins["fishing"]
    plugin.manager = manager
    plugin._config = PluginSettings(
        Path(tmp) / "fishing.json",
        plugin_module(plugin).DEFAULT_SETTINGS,
        label="钓鱼",
        normalize=type(plugin)._normalize,
    )
    if settings is not None:
        plugin._config.path.write_text(json.dumps(settings), encoding="utf-8")
    plugin._load_settings()
    return manager, plugin


class ArmingTest(unittest.IsolatedAsyncioTestCase):
    """落水门控按时间来，不再依赖位置包（那是「一小时只钓上 6 条」的根因）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_type = 130
        self.plugin._state = "casting"
        self.plugin._cast_at = time.monotonic()
        self.plugin._next_cast_at = time.monotonic() + 999.0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _claim_and_wait(self) -> None:
        await self.plugin._on_entity_add(bobber(entity_id=7, y=63.0))
        self.bot.entities[7] = bobber(entity_id=7, y=63.0)
        self.plugin._claim_at -= 10.0  # 落水延时已过
        await self.plugin._step()

    async def test_arms_without_a_single_position_packet(self) -> None:
        # 水面上的浮标不动，服务端就不发位置包；以前基准线因此永远建立不起来
        await self._claim_and_wait()
        self.assertTrue(self.plugin._armed)
        self.assertEqual(self.plugin._baseline, 63.0)

    async def test_bite_after_arming_reels(self) -> None:
        await self._claim_and_wait()
        await self.plugin._on_entity_motion(7, (0.0, -0.3, 0.0), None)
        self.assertEqual(self.bot.use_calls, ["main_hand"])
        self.assertEqual(self.plugin._catches, 1)

    async def test_vanilla_bite_velocity_is_inside_the_threshold(self) -> None:
        # 原版咬钩把 vy 设成 -0.4 × [0.6, 1.0]，最弱的一档也要判出来
        await self._claim_and_wait()
        await self.plugin._on_entity_motion(7, (0.0, -0.24, 0.0), None)
        self.assertEqual(self.plugin._catches, 1)

    async def test_cast_impulse_before_arming_is_ignored(self) -> None:
        # 朝下抛竿时初速度也是负的，门控没过就不能算咬钩
        await self.plugin._on_entity_add(bobber(entity_id=7, y=70.0))
        await self.plugin._on_entity_motion(7, (0.2, -0.6, 0.1), None)
        self.assertEqual(self.bot.use_calls, [])
        self.assertEqual(self.plugin._catches, 0)

    async def test_dip_uses_the_armed_baseline(self) -> None:
        await self._claim_and_wait()
        await self.plugin._on_entity_move(7, bobber(entity_id=7, y=62.8))
        self.assertEqual(self.plugin._catches, 1)


class WaterCheckTest(unittest.IsolatedAsyncioTestCase):
    """落点不是水就立刻重抛，而不是干等满 max_wait。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_type = 130
        self.plugin._state = "waiting"
        self.plugin._cast_at = time.monotonic()
        self.plugin._bobber_id = 7
        self.plugin._claim_at = time.monotonic() - 10.0
        self.bot.entities[7] = bobber(entity_id=7, x=0.5, y=63.0, z=0.5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _world(self, fluid: str | None, *, loaded: bool = True) -> None:
        self.bot.world = SimpleNamespace(
            chunks={(0, 0): object()} if loaded else {},
            block_properties=lambda x, y, z: SimpleNamespace(fluid=fluid),
        )

    async def _look(self, times: int) -> None:
        for _ in range(times):
            await self.plugin._step()

    async def test_water_keeps_fishing(self) -> None:
        self._world("water")
        await self._look(10)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_dry_land_recasts_immediately(self) -> None:
        self._world(None)
        await self._look(plugin_module(self.plugin).DRY_CONFIRM)
        self.assertEqual(self.plugin._state, "cooldown")
        self.assertIsNone(self.plugin._bobber_id)

    async def test_one_dry_read_is_not_enough(self) -> None:
        # 抛得远时浮标可能还在水面上方飞，看一眼就重抛会误杀好竿
        self._world(None)
        await self._look(1)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_landing_in_water_clears_earlier_dry_reads(self) -> None:
        self._world(None)
        await self._look(plugin_module(self.plugin).DRY_CONFIRM - 1)
        self._world("water")  # 落水了
        await self._look(10)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_unloaded_chunk_is_unknown_not_dry(self) -> None:
        # 没收到区块时所有格都读成空气，不能因此不停重抛
        self._world(None, loaded=False)
        await self._look(20)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_no_world_is_unknown(self) -> None:
        await self._look(20)  # FakeBot 没有 world 属性
        self.assertEqual(self.plugin._state, "waiting")

    async def test_check_can_be_turned_off(self) -> None:
        self.plugin._settings["water_check"] = False
        self._world(None)
        await self._look(20)
        self.assertEqual(self.plugin._state, "waiting")


class CandidateClaimTest(unittest.IsolatedAsyncioTestCase):
    """注册表算出的 type_id 对不上时，用候选兜底并纠正——否则整小时空转。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_type = 999  # 假装注册表给了个错下标
        self.plugin._state = "casting"
        self.plugin._cast_at = time.monotonic()
        self.plugin._next_cast_at = time.monotonic() + 999.0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_mismatched_entity_is_kept_as_a_candidate(self) -> None:
        await self.plugin._on_entity_add(bobber(entity_id=7, type_id=130))
        self.assertIsNone(self.plugin._bobber_id)  # 先不认领
        self.assertIsNotNone(self.plugin._candidate)

    async def test_candidate_is_claimed_after_the_window(self) -> None:
        await self.plugin._on_entity_add(bobber(entity_id=7, type_id=130))
        self.plugin._cast_at -= 10.0  # spawn_window 已过
        await self.plugin._step()
        self.assertEqual(self.plugin._bobber_id, 7)
        self.assertEqual(self.plugin._bobber_type, 130)  # 已纠正
        self.assertEqual(self.plugin._state, "waiting")

    async def test_far_entities_never_become_candidates(self) -> None:
        await self.plugin._on_entity_add(bobber(entity_id=7, type_id=130, x=500.0))
        self.assertIsNone(self.plugin._candidate)


class SettingsTest(unittest.TestCase):
    def test_template_written_and_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp)
            saved = json.loads(plugin._config.path.read_text(encoding="utf-8"))
            self.assertFalse(saved["enabled"])
            self.assertEqual(saved["hand"], "main_hand")
            self.assertFalse(plugin._settings["enabled"])

    def test_custom_settings_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(
                tmp, {"enabled": True, "bite_drop": 0.3, "hand": "off_hand"}
            )
            self.assertTrue(plugin._settings["enabled"])
            self.assertEqual(plugin._settings["bite_drop"], 0.3)
            self.assertEqual(plugin._settings["hand"], "off_hand")
            self.assertEqual(plugin._settings["max_wait"], 45.0)  # 默认保留

    def test_invalid_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(
                tmp,
                {"hand": "foot", "bite_drop": "abc", "settle_delay": 0.0,
                 "recast_delay": 0.0, "max_wait": 1.0},
            )
            self.assertEqual(plugin._settings["hand"], "main_hand")
            self.assertEqual(plugin._settings["bite_drop"], 0.12)
            self.assertEqual(plugin._settings["settle_delay"], 0.2)
            self.assertEqual(plugin._settings["recast_delay"], 0.05)
            self.assertEqual(plugin._settings["max_wait"], 5.0)

    def test_corrupt_file_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp)
            plugin._config.path.write_text("not json", encoding="utf-8")
            plugin._load_settings()
            self.assertEqual(plugin._settings["bite_drop"], 0.12)

    def test_hot_reload_toggles_and_resets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True})
            plugin._state = "waiting"
            plugin._bobber_id = 7
            plugin._config.path.write_text(
                json.dumps({"enabled": False}), encoding="utf-8"
            )
            os.utime(plugin._config.path, (0, 0))  # 让 mtime 明确不同
            plugin._maybe_reload()
            self.assertFalse(plugin._settings["enabled"])
            self.assertEqual(plugin._state, "idle")  # 关闭时清空追踪
            self.assertIsNone(plugin._bobber_id)


class CastTest(unittest.IsolatedAsyncioTestCase):
    async def test_casts_when_enabled_and_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True})
            plugin.bot = FakeBot()
            await plugin._step()
            self.assertEqual(plugin.bot.use_calls, ["main_hand"])
            self.assertEqual(plugin._state, "casting")

    async def test_does_not_cast_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": False})
            plugin.bot = FakeBot()
            await plugin._step()
            self.assertEqual(plugin.bot.use_calls, [])

    async def test_does_not_cast_without_a_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True})
            plugin.bot = None
            await plugin._step()  # 不应抛出
            self.assertEqual(plugin._state, "idle")

    async def test_uses_the_configured_hand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True, "hand": "off_hand"})
            plugin.bot = FakeBot()
            await plugin._step()
            self.assertEqual(plugin.bot.use_calls, ["off_hand"])

    async def test_cast_failure_backs_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True})
            plugin.bot = FakeBot()
            plugin.bot.fail_use = True
            await plugin._cast()
            self.assertEqual(plugin._state, "idle")
            self.assertGreater(plugin._next_cast_at, time.monotonic() + 1.0)

    async def test_recast_waits_for_the_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True, "recast_delay": 5.0})
            plugin.bot = FakeBot()
            plugin._state = "cooldown"
            plugin._next_cast_at = time.monotonic() + 5.0
            await plugin._step()
            self.assertEqual(plugin.bot.use_calls, [])
            plugin._next_cast_at = time.monotonic() - 0.1
            await plugin._step()
            self.assertEqual(len(plugin.bot.use_calls), 1)


class BobberClaimTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _casting(self, bot: FakeBot) -> None:
        self.plugin.bot = bot
        self.plugin._state = "casting"
        self.plugin._cast_at = time.monotonic()

    async def test_registry_lookup_gives_the_type_id(self) -> None:
        types = ["minecraft:allay", "minecraft:fishing_bobber", "minecraft:pig"]
        self._casting(FakeBot(entity_types=types))
        await self.plugin._on_entity_add(bobber(type_id=1))
        self.assertEqual(self.plugin._bobber_type, 1)
        self.assertEqual(self.plugin._bobber_id, 7)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_registry_rejects_other_entity_types(self) -> None:
        types = ["minecraft:allay", "minecraft:fishing_bobber"]
        self._casting(FakeBot(entity_types=types))
        await self.plugin._on_entity_add(bobber(type_id=0))  # allay
        self.assertIsNone(self.plugin._bobber_id)
        self.assertEqual(self.plugin._state, "casting")

    async def test_heuristic_claim_learns_the_type_id(self) -> None:
        self._casting(FakeBot())  # 注册表里没有实体类型
        await self.plugin._on_entity_add(bobber(type_id=130))
        self.assertEqual(self.plugin._bobber_type, 130)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_heuristic_ignores_far_away_entities(self) -> None:
        self._casting(FakeBot())
        await self.plugin._on_entity_add(bobber(x=500.0))
        self.assertIsNone(self.plugin._bobber_id)
        self.assertIsNone(self.plugin._bobber_type)

    async def test_heuristic_ignores_late_spawns(self) -> None:
        self._casting(FakeBot())
        self.plugin._cast_at -= 10.0  # 早就抛完了
        await self.plugin._on_entity_add(bobber())
        self.assertIsNone(self.plugin._bobber_id)

    async def test_learned_type_is_used_on_later_casts(self) -> None:
        self._casting(FakeBot())
        self.plugin._bobber_type = 130
        await self.plugin._on_entity_add(bobber(entity_id=9, type_id=130))
        self.assertEqual(self.plugin._bobber_id, 9)

    async def test_spawn_window_bounds_every_claim(self) -> None:
        # 类型已知也要在窗口内、在身边：几分钟后生成的同类实体不是我的浮标
        self._casting(FakeBot())
        self.plugin._bobber_type = 130
        self.plugin._cast_at -= 10.0
        await self.plugin._on_entity_add(bobber(entity_id=9, type_id=130))
        self.assertIsNone(self.plugin._bobber_id)

    async def test_entities_added_while_idle_are_ignored(self) -> None:
        self.plugin.bot = FakeBot()
        self.plugin._state = "idle"
        await self.plugin._on_entity_add(bobber())
        self.assertIsNone(self.plugin._bobber_id)


class BiteDetectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_type = 130
        self.plugin._bobber_id = 7
        self.plugin._state = "waiting"
        self.plugin._cast_at = time.monotonic()
        self.plugin._last_y = 63.0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _settle(self, y: float = 63.0) -> None:
        """让浮标就位：落水延时已过，基准线取当前 Y。"""
        self.bot.entities[7] = bobber(y=y)
        self.plugin._claim_at = time.monotonic() - 10.0
        await self.plugin._step()

    async def test_falling_arc_does_not_trigger(self) -> None:
        # 抛物线下落：每次 Y 变化都很大，绝不能被当成咬钩
        for y in (70.0, 68.5, 66.0, 64.2, 63.4):
            await self.plugin._on_entity_move(7, bobber(y=y))
        self.assertIsNone(self.plugin._baseline)
        self.assertEqual(self.bot.use_calls, [])

    async def test_baseline_established_once_settled(self) -> None:
        await self._settle(63.0)
        self.assertEqual(self.plugin._baseline, 63.0)
        self.assertEqual(self.bot.use_calls, [])

    async def test_dip_below_baseline_reels(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_move(7, bobber(y=62.8))  # 下沉 0.2 > 0.12
        self.assertEqual(self.bot.use_calls, ["main_hand"])
        self.assertEqual(self.plugin._catches, 1)
        self.assertEqual(self.plugin._state, "cooldown")

    async def test_small_wobble_does_not_reel(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_move(7, bobber(y=62.95))  # 只沉 0.05
        self.assertEqual(self.bot.use_calls, [])

    async def test_rising_does_not_reel(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_move(7, bobber(y=63.5))
        self.assertEqual(self.bot.use_calls, [])

    async def test_teleport_also_feeds_the_dip_check(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_teleport(7, bobber(y=62.7), False)
        self.assertEqual(self.plugin._catches, 1)

    async def test_downward_motion_reels(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_motion(7, (0.0, -0.4, 0.0), bobber())
        self.assertEqual(self.bot.use_calls, ["main_hand"])
        self.assertEqual(self.plugin._catches, 1)

    async def test_gravity_during_flight_does_not_reel(self) -> None:
        # 抛物线下落时重力速度也是负的，基准线未确立前必须无视
        self.assertIsNone(self.plugin._baseline)
        await self.plugin._on_entity_motion(7, (0.0, -0.9, 0.0), bobber())
        self.assertEqual(self.bot.use_calls, [])
        self.assertEqual(self.plugin._catches, 0)

    async def test_gentle_motion_does_not_reel(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_motion(7, (0.0, -0.05, 0.0), bobber())
        self.assertEqual(self.bot.use_calls, [])

    async def test_upward_motion_does_not_reel(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_motion(7, (0.0, 0.5, 0.0), bobber())
        self.assertEqual(self.bot.use_calls, [])

    async def test_other_entities_are_ignored(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_motion(999, (0.0, -0.9, 0.0), bobber())
        await self.plugin._on_entity_move(999, bobber(entity_id=999, y=1.0))
        self.assertEqual(self.bot.use_calls, [])

    async def test_velocity_signal_can_be_disabled(self) -> None:
        self.plugin._settings["bite_velocity"] = 0.0
        await self._settle(63.0)
        await self.plugin._on_entity_motion(7, (0.0, -0.9, 0.0), bobber())
        self.assertEqual(self.bot.use_calls, [])

    async def test_drop_signal_can_be_disabled(self) -> None:
        self.plugin._settings["bite_drop"] = 0.0
        await self._settle(63.0)
        await self.plugin._on_entity_move(7, bobber(y=60.0))
        self.assertEqual(self.bot.use_calls, [])

    async def test_no_double_reel(self) -> None:
        await self._settle(63.0)
        await self.plugin._on_entity_move(7, bobber(y=62.5))
        await self.plugin._on_entity_motion(7, (0.0, -0.9, 0.0), bobber())
        self.assertEqual(len(self.bot.use_calls), 1)
        self.assertEqual(self.plugin._catches, 1)

    async def test_disabled_mid_wait_stops_detection(self) -> None:
        await self._settle(63.0)
        self.plugin._settings["enabled"] = False
        await self.plugin._on_entity_move(7, bobber(y=62.5))
        self.assertEqual(self.bot.use_calls, [])


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(
            self._tmp.name, {"enabled": True, "max_wait": 30.0}
        )
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_id = 7
        self.plugin._state = "waiting"
        self.plugin._cast_at = time.monotonic()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_timeout_reels_without_counting_a_catch(self) -> None:
        self.plugin._cast_at -= 31.0
        await self.plugin._step()
        self.assertEqual(self.bot.use_calls, ["main_hand"])
        self.assertEqual(self.plugin._catches, 0)
        self.assertEqual(self.plugin._state, "cooldown")

    async def test_no_timeout_before_max_wait(self) -> None:
        self.plugin._cast_at -= 10.0
        await self.plugin._step()
        self.assertEqual(self.bot.use_calls, [])

    async def test_removed_bobber_schedules_a_recast(self) -> None:
        await self.plugin._on_entities_remove((7,), ())
        self.assertIsNone(self.plugin._bobber_id)
        self.assertEqual(self.plugin._state, "cooldown")
        self.assertEqual(self.bot.use_calls, [])  # 不额外甩一次竿

    async def test_removing_other_entities_is_ignored(self) -> None:
        await self.plugin._on_entities_remove((1, 2, 3), ())
        self.assertEqual(self.plugin._bobber_id, 7)
        self.assertEqual(self.plugin._state, "waiting")

    async def test_reconnect_resets_tracking(self) -> None:
        self.plugin._baseline = 63.0
        await self.plugin._on_session_ready(self.bot)
        self.assertIsNone(self.plugin._bobber_id)
        self.assertIsNone(self.plugin._baseline)
        self.assertEqual(self.plugin._state, "idle")

    async def test_learned_type_survives_a_reconnect(self) -> None:
        self.plugin._bobber_type = 130
        await self.plugin._on_session_ready(self.bot)
        self.assertEqual(self.plugin._bobber_type, 130)


def sound_payload(
    sound_id: int | None = 640,
    x: float = 0.5,
    y: float = 63.0,
    z: float = 0.5,
    *,
    category: int = 6,
    volume: float = 0.25,
    pitch: float = 1.0,
    seed: int = 12345,
) -> bytes:
    """位置型音效包的载荷（协议 775/776 布局）。

    holder：varint，0 = 内联（名称 + bool + 可选 float），否则 注册表 ID + 1。
    坐标是 ÷8 定点整数。
    """
    writer = PacketWriter()
    if sound_id is None:
        writer.write_varint(0)
        writer.write_string("minecraft:entity.fishing_bobber.splash")
        writer.write_bool(False)
    else:
        writer.write_varint(sound_id + 1)
    writer.write_varint(category)
    for value in (x, y, z):
        writer.write_int(int(value * 8))
    writer.write_float(volume)
    writer.write_float(pitch)
    writer.write_long(seed)
    return writer.to_bytes()


class SoundBiteTest(unittest.IsolatedAsyncioTestCase):
    """音效判定：坐标落在浮标上才算咬钩，音效 ID 自动学习。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        _, self.plugin = load_plugin(self._tmp.name, {"enabled": True})
        self.bot = FakeBot()
        self.plugin.bot = self.bot
        self.plugin._bobber_id = 7
        self.plugin._bobber_type = 130
        self.plugin._state = "waiting"
        self.plugin._cast_at = time.monotonic()
        # 浮标停在 (0.5, 63, 0.5)
        self.bot.entities[7] = bobber(y=63.0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _packet(self, payload: bytes, packet_id: int = 117):
        return SimpleNamespace(packet_id=packet_id, payload=payload)

    async def test_splash_at_the_bobber_reels(self) -> None:
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.bot.use_calls, ["main_hand"])
        self.assertEqual(self.plugin._catches, 1)

    async def test_sound_id_is_learned_then_enforced(self) -> None:
        await self.plugin._on_packet(self._packet(sound_payload(640)))
        self.assertEqual(self.plugin._learned_sound, 640)
        # 换一条不同 ID 的音效（同样在浮标上）不再触发
        self.plugin._state = "waiting"
        self.plugin._reeling = False
        await self.plugin._on_packet(self._packet(sound_payload(999)))
        self.assertEqual(len(self.bot.use_calls), 1)

    async def test_pinned_sound_id_rejects_others(self) -> None:
        self.plugin._settings["sound_id"] = 641
        await self.plugin._on_packet(self._packet(sound_payload(640)))
        self.assertEqual(self.bot.use_calls, [])
        await self.plugin._on_packet(self._packet(sound_payload(641)))
        self.assertEqual(self.bot.use_calls, ["main_hand"])

    async def test_sound_far_from_the_bobber_is_ignored(self) -> None:
        # 甩竿/收杆音效发在玩家身上，不在浮标上
        await self.plugin._on_packet(self._packet(sound_payload(x=40.0)))
        self.assertEqual(self.bot.use_calls, [])
        self.assertIsNone(self.plugin._learned_sound)

    async def test_other_packet_ids_are_ignored(self) -> None:
        await self.plugin._on_packet(self._packet(sound_payload(), packet_id=99))
        self.assertEqual(self.bot.use_calls, [])

    async def test_zero_packet_id_disables_the_sound_path(self) -> None:
        # 设置里显式写 0 = 关闭这一路（版本表也不再参与）
        self.plugin._settings["sound_packet_id"] = 0
        self.bot.version.packets.clientbound_sound = 0
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.bot.use_calls, [])

    async def test_inline_sound_is_accepted_by_position(self) -> None:
        # 内联音效没有注册表 ID，只能靠位置判定
        await self.plugin._on_packet(self._packet(sound_payload(None)))
        self.assertEqual(self.plugin._catches, 1)
        self.assertIsNone(self.plugin._learned_sound)  # 没 ID 可学

    async def test_malformed_payload_is_ignored(self) -> None:
        await self.plugin._on_packet(self._packet(b"\x01\x02"))
        self.assertEqual(self.bot.use_calls, [])

    async def test_trailing_bytes_are_rejected(self) -> None:
        # 包 ID 配错时载荷通常长度不符，严格校验能把它挡下来
        await self.plugin._on_packet(self._packet(sound_payload() + b"\x00"))
        self.assertEqual(self.bot.use_calls, [])

    async def test_sound_before_the_bobber_exists_is_ignored(self) -> None:
        self.plugin._bobber_id = None
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.bot.use_calls, [])

    async def test_sound_works_without_a_settled_baseline(self) -> None:
        # 音效本身只在咬钩时才响，不需要等基准线
        self.assertIsNone(self.plugin._baseline)
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.plugin._catches, 1)

    async def test_disabled_plugin_ignores_sounds(self) -> None:
        self.plugin._settings["enabled"] = False
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.bot.use_calls, [])

    async def test_no_double_reel_from_sound_then_dip(self) -> None:
        await self.plugin._on_packet(self._packet(sound_payload()))
        await self.plugin._on_entity_motion(7, (0.0, -0.9, 0.0), bobber())
        self.assertEqual(len(self.bot.use_calls), 1)

    def test_decoder_returns_position_and_id(self) -> None:
        decoded = self.plugin._decode_sound(sound_payload(640, 1.25, 62.5, -3.0))
        self.assertEqual(decoded, (640, 1.25, 62.5, -3.0))

    def test_packet_id_comes_from_the_version_table(self) -> None:
        self.assertEqual(self.plugin._sound_packet_id(), 117)

    def test_explicit_setting_overrides_the_version_table(self) -> None:
        self.plugin._settings["sound_packet_id"] = 200
        self.assertEqual(self.plugin._sound_packet_id(), 200)

    async def test_unverified_version_disables_the_sound_path(self) -> None:
        # 1.21.11 那一类版本的音效包 ID 未核实，表里是 0：宁可不判也不误判
        self.bot.version.packets.clientbound_sound = 0
        self.assertEqual(self.plugin._sound_packet_id(), 0)
        await self.plugin._on_packet(self._packet(sound_payload()))
        self.assertEqual(self.bot.use_calls, [])

    async def test_missing_version_disables_the_sound_path(self) -> None:
        self.plugin.bot = SimpleNamespace(entities={})  # 没有 version 属性
        self.assertEqual(self.plugin._sound_packet_id(), 0)


class ExposedServiceTest(unittest.IsolatedAsyncioTestCase):
    """fishing.start / stop / status：供其他插件与 LLM 调用。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.manager, self.plugin = load_plugin(self._tmp.name, {"enabled": False})
        self.plugin.bot = FakeBot()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _saved(self) -> dict:
        return json.loads(self.plugin._config.path.read_text(encoding="utf-8"))

    def test_exposures_declared(self) -> None:
        exposed = {service.name: service for service in self.plugin.exposed()}
        self.assertEqual(sorted(exposed), ["start", "status", "stop"])
        self.assertTrue(exposed["start"].llm)
        self.assertTrue(exposed["start"].admin)
        self.assertTrue(exposed["status"].llm)
        self.assertFalse(exposed["status"].admin)  # 查状态不限管理员
        self.assertEqual(exposed["start"].tool_name, "fishing_start")

    async def test_start_enables_and_persists(self) -> None:
        result = await self.plugin._service_start()
        self.assertIn("started", result)
        self.assertTrue(self.plugin._settings["enabled"])
        self.assertTrue(self._saved()["enabled"])  # 写回文件，重启后仍开着

    async def test_start_when_already_running(self) -> None:
        await self.plugin._service_start()
        result = await self.plugin._service_start()
        self.assertIn("Already fishing", result)

    async def test_stop_disables_and_resets(self) -> None:
        await self.plugin._service_start()
        self.plugin._state = "waiting"
        self.plugin._bobber_id = 7
        result = await self.plugin._service_stop()
        self.assertIn("stopped", result)
        self.assertFalse(self._saved()["enabled"])
        self.assertEqual(self.plugin._state, "idle")
        self.assertIsNone(self.plugin._bobber_id)

    async def test_stop_when_not_running(self) -> None:
        result = await self.plugin._service_stop()
        self.assertIn("was not running", result)

    async def test_toggle_does_not_look_like_an_external_edit(self) -> None:
        # 自己写回文件后必须刷新 mtime，否则 5 秒后会被当成外部改动重载
        await self.plugin._service_start()
        self.plugin._maybe_reload()
        self.assertTrue(self.plugin._settings["enabled"])

    async def test_status_when_off(self) -> None:
        result = await self.plugin._service_status()
        self.assertIn("off", result)

    async def test_status_reports_the_current_stage(self) -> None:
        await self.plugin._service_start()
        self.plugin._state = "waiting"
        self.plugin._armed = False
        self.assertIn("not watching yet", await self.plugin._service_status())
        self.plugin._armed = True
        self.plugin._baseline = 63.0
        self.assertIn("watching the bobber", await self.plugin._service_status())
        self.plugin._state = "cooldown"
        self.assertIn("between casts", await self.plugin._service_status())

    async def test_status_counts_catches(self) -> None:
        await self.plugin._service_start()
        self.plugin._catches = 4
        self.assertIn("4 caught", await self.plugin._service_status())

    async def test_callable_through_the_manager(self) -> None:
        await self.manager.enable_all()
        try:
            result = await self.manager.call_service("fishing.status")
            self.assertIn("Auto-fishing", result)
            self.assertIn("fishing.start", self.manager.services())
        finally:
            await self.manager.disable_all()


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_enable_starts_the_loop_and_disable_cancels_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, plugin = load_plugin(tmp, {"enabled": True})
            await plugin.on_enable()
            try:
                self.assertIsNotNone(plugin._loop_task)
                self.assertFalse(plugin._loop_task.done())
            finally:
                await plugin.on_disable()
            self.assertIsNone(plugin._loop_task)

    async def test_loop_survives_a_step_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, plugin = load_plugin(tmp, {"enabled": True})
            plugin.bot = FakeBot()
            plugin.bot.fail_use = True
            await plugin.on_enable()
            try:
                await asyncio.sleep(0.25)
                self.assertFalse(plugin._loop_task.done())
            finally:
                await plugin.on_disable()


if __name__ == "__main__":
    unittest.main()

