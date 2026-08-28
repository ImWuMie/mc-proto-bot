"""自动钓鱼插件：抛竿、判定咬钩、收杆，循环往复。

判定按可靠性排序，三路信号任一命中即收杆：

  1. **咬钩音效**（最准，原版就是靠它提示玩家）——``entity.fishing_bobber.splash``
     由位置型音效包（协议 775/776 的 0x75 = 117）发出，坐标是**浮标所在处**；
     甩竿/收杆的音效发在**玩家所在处**，因此「音效位置离浮标 1.5 格内」既能
     认出咬钩，也能排除自己甩竿和别人钓鱼的动静。音效的数字 ID 逐版本变动
     且不由服务端下发（``minecraft:sound_event`` 是内置注册表），所以这里
     **不硬编码**：第一次靠位置认出咬钩时把 ID 学下来，之后要求 ID 也匹配。
  2. **向下速度**——``entity_motion`` 报出浮标的向下速度。原版咬钩那一刻把
     浮标的 Y 速度设成 ``-0.4 × [0.6, 1.0]``（即 -0.24 ~ -0.4 格/tick）并同时
     播放溅水音效，所以这一路和音效路本是同一个瞬间的两种表现。
  3. **位置下沉**——浮标从静止水面基准线被拽下超过阈值。

浮标怎么认领：优先从服务端下发的 ``minecraft:entity_type`` 注册表里查
``minecraft:fishing_bobber`` 的 type_id；查不到就把「抛竿后 2 秒内、身边
8 格内新生成的实体」当作浮标，并**记住它的 type_id**，之后每次抛竿都精确认领。
注册表算出来的下标万一对不上（版本或服务端差异），``spawn_window`` 过后会用
这个候选兜底并把 type_id 纠正过来——否则一次算错就再也认不出浮标，整小时空转。

速度与下沉两路要等 ``settle_delay``（默认 1.2 秒，抛出去到落水就这么点时间）
之后才生效：抛竿的初速度本身可能就是向下的，不做门控会一抛竿就误判。**不能**
改用「连续几次位置更新几乎不动」来判断落稳——浮标停在水面上时服务端根本不再
发位置包，那样基准线常常永远建立不起来，速度与下沉两路被永久门控住，只能靠
音效路，音效再有一点问题就整小时钓不上鱼。音效路不需要门控，它只在鱼真咬钩时响。

落点检查：``water_check``（默认开）在浮标就位时查一下脚下那两格是不是水，不是
就立刻重抛，而不是干等满 ``max_wait``。区块还没收到时判为「未知」，不会瞎重抛。

超时兜底：``max_wait`` 秒没咬钩（浮标落在陆地上、线被打断等）就收杆重抛，
日志会带上这一竿到底看到了什么（是否认领到浮标、收到几个速度/位置包、是否在
水里、音效路是否开着），便于对症下药；浮标被移除也会重抛。收杆与重抛之间只
隔 ``recast_delay`` 秒（默认 0.4）。

设置文件 ``fishing.json``（与本插件同目录，首次启用自动生成）修改后约 5 秒
内自动重新加载，**默认 enabled=false**：把它改成 true 才会开始钓，也可以让
LLM 调用暴露出来的 ``fishing.start`` / ``fishing.stop`` / ``fishing.status``。
手持鱼竿由你自己保证——本协议栈拿不到物品名称，插件无法校验手里是不是鱼竿。
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from protobot import Plugin, PluginSettings, log
from protobot.protocol import PacketReader

DEFAULT_SETTINGS: dict = {
    "enabled": False,  # 改成 true 才开始自动钓鱼
    "hand": "main_hand",  # 用哪只手甩竿：main_hand / off_hand
    "sound_packet_id": None,  # 音效包 ID；null = 按连接的协议版本自动取（26.x 为 117）
    "sound_id": None,  # 咬钩音效的数字 ID；null = 自动学习并记住
    "sound_radius": 1.5,  # 音效位置与浮标的最大距离（格）
    "bite_velocity": -0.15,  # 向下速度阈值（格/tick，原版咬钩为 -0.24 ~ -0.4）；0 关闭该路
    "bite_drop": 0.12,  # 相对静止基准线的下沉阈值（格）；0 关闭该路
    "settle_delay": 1.2,  # 浮标生成后多少秒才开始看速度/下沉（等它飞完落水）
    "water_check": True,  # 落点不是水就立刻重抛，不白等 max_wait
    "recast_delay": 0.4,  # 收杆到重抛的间隔（秒）
    "max_wait": 45.0,  # 抛竿后多久没咬钩就重抛（秒）
    "spawn_window": 2.0,  # 认领浮标：抛竿后多少秒内生成的实体才算
    "spawn_radius": 8.0,  # 认领浮标：距自己多少格内
}

#: 主循环步长（秒）。判定走事件、不靠轮询，这里只管超时与重抛。
TICK = 0.1

#: 连续读到「浮标不在水里」多少次才重抛（× TICK 秒）。抛得远时浮标可能
#: 还在水面上方飞，只看一眼会把好竿误杀。
DRY_CONFIRM = 5


class AutoFishing(Plugin):
    name = "fishing"

    def __init__(self) -> None:
        super().__init__()
        self._config: PluginSettings | None = None
        self._settings: dict = AutoFishing._normalize(dict(DEFAULT_SETTINGS))
        self._loop_task: asyncio.Task | None = None
        self._tick_count = 0
        # 浮标追踪
        self._bobber_id: int | None = None
        self._bobber_type: int | None = None  # 认领成功后学到的 type_id
        self._baseline: float | None = None  # 静止水面基准 Y
        self._last_y: float | None = None
        self._armed = False  # 落水延时已过：速度/下沉两路开始生效
        self._claim_at = 0.0  # 认领到浮标的时刻
        self._candidate: tuple[int, int | None, float] | None = None  # 兜底认领
        self._dry_reads = 0  # 连续读到「不在水里」的次数
        self._motion_seen = 0  # 诊断：本次抛竿收到过几个速度包
        self._move_seen = 0  # 诊断：本次抛竿收到过几个位置包
        # 状态机：idle -> casting -> waiting -> (咬钩/超时) -> cooldown -> ...
        self._state = "idle"
        self._cast_at = 0.0
        self._next_cast_at = 0.0
        self._reeling = False
        self._catches = 0
        self._learned_sound: int | None = None  # 学到的咬钩音效 ID
        self._warned_no_sound = False  # 版本没有已核实的音效包 ID 时只提示一次
        self.subscribe("entity_add", self._on_entity_add)
        self.subscribe("entity_motion", self._on_entity_motion)
        self.subscribe("entity_move", self._on_entity_move)
        self.subscribe("entity_teleport", self._on_entity_teleport)
        self.subscribe("entities_remove", self._on_entities_remove)
        self.subscribe("packet", self._on_packet)
        self.subscribe_session("session_ready", self._on_session_ready)
        # 暴露给其他插件与 LLM：fishing.start / fishing.stop / fishing.status
        self.expose(
            "start",
            self._service_start,
            description=(
                "Start auto-fishing: cast, watch the bobber, reel on a bite, "
                "repeat. The bot must be holding a fishing rod and facing water."
            ),
            llm=True,
            admin=True,
        )
        self.expose(
            "stop",
            self._service_stop,
            description="Stop auto-fishing and leave the rod alone.",
            llm=True,
            admin=True,
        )
        self.expose(
            "status",
            self._service_status,
            description=(
                "Auto-fishing status: whether it is running, what it is doing "
                "right now, and how many fish have been reeled in."
            ),
            llm=True,
        )

    # ---- 暴露给其他插件 / LLM 的能力 ----

    async def _service_start(self) -> str:
        if self._settings["enabled"]:
            return f"Already fishing ({self._catches} caught so far)"
        self._set_enabled(True)
        return "Auto-fishing started (rod in hand and water in front are on you)"

    async def _service_stop(self) -> str:
        if not self._settings["enabled"]:
            return "Auto-fishing was not running"
        self._set_enabled(False)
        return f"Auto-fishing stopped ({self._catches} caught this session)"

    async def _service_status(self) -> str:
        states = {
            "idle": "idle",
            "casting": "cast, waiting for the bobber to appear",
            "waiting": "line in the water, watching the bobber",
            "cooldown": "between casts",
        }
        if not self._settings["enabled"]:
            return f"Auto-fishing is off ({self._catches} caught this session)"
        detail = states.get(self._state, self._state)
        if self._state == "waiting" and not self._armed:
            detail = "bobber in flight, not watching yet"
        elif self._state == "waiting" and self._bobber_in_water() is False:
            detail = "bobber is not in water -- recasting shortly"
        return (
            f"Auto-fishing is on: {detail}; {self._catches} caught this session"
        )

    def _set_enabled(self, enabled: bool) -> None:
        """改开关并只把这一个键写回 fishing.json。

        只 patch 一个键，不整份回写：否则会覆盖用户在这期间改过的其他值，
        还会把一个只写了一行的文件展开成全部默认项。
        """
        if not enabled:
            self._reset()
        error = self._config.patch({"enabled": enabled})
        self._settings = self._config.data
        if error:
            log.warn(f"[钓鱼] 开关状态写回失败 ({error})")
            self._settings["enabled"] = enabled  # 至少让本进程内生效
        log.info(f"[钓鱼] {'开始' if enabled else '停止'}自动钓鱼。")

    # ---- 生命周期 ----

    async def on_enable(self) -> None:
        if self._config is None:
            self._config = self.settings_file(
                "fishing.json", DEFAULT_SETTINGS,
                label="钓鱼", normalize=self._normalize,
            )
        self._load_settings()
        self._loop_task = asyncio.create_task(
            self._loop(), name="protobot-fishing"
        )
        if self._settings["enabled"]:
            log.info("[钓鱼] 已启用，等待连接后开始自动抛竿。")
        else:
            log.info(
                f"[钓鱼] 插件已加载但未开启：把 {self._config.path} 里的 "
                'enabled 改成 true 即可（约 5 秒生效）。'
            )

    async def on_disable(self) -> None:
        task = self._loop_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        log.info(f"[钓鱼] 已关闭（本次共 {self._catches} 条）。")

    async def _on_session_ready(self, bot) -> None:
        self._reset()  # 重连后旧浮标已不存在

    def _reset(self) -> None:
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._state = "idle"
        self._reeling = False

    # ---- 设置：默认值与钳制由本插件负责，读写/热重载交给框架 ----

    @staticmethod
    def _normalize(merged: dict) -> dict:
        merged["enabled"] = bool(merged.get("enabled", False))
        if merged.get("hand") not in ("main_hand", "off_hand"):
            merged["hand"] = "main_hand"
        for key in (
            "bite_velocity", "bite_drop", "settle_delay",
            "recast_delay", "max_wait", "spawn_window", "spawn_radius",
            "sound_radius",
        ):
            try:
                merged[key] = float(merged.get(key, DEFAULT_SETTINGS[key]))
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        for key in ("sound_packet_id", "sound_id"):
            value = merged.get(key)
            if value is None or str(value) == "":
                merged[key] = None  # 交给版本表决定 / 自动学习
                continue
            try:
                merged[key] = int(value)
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        merged["water_check"] = bool(merged.get("water_check", True))
        merged["settle_delay"] = min(10.0, max(0.2, merged["settle_delay"]))
        merged["recast_delay"] = max(0.05, merged["recast_delay"])
        merged["max_wait"] = max(5.0, merged["max_wait"])
        return merged

    def _load_settings(self) -> None:
        self._config.load()
        self._settings = self._config.data

    def _maybe_reload(self) -> None:
        was_on = self._settings.get("enabled")
        if not self._config.reload_if_changed():
            return
        self._settings = self._config.data
        now_on = self._settings.get("enabled")
        if was_on != now_on:
            log.info(f"[钓鱼] 设置已更新：{'开启' if now_on else '关闭'}。")
            if not now_on:
                self._reset()
        else:
            log.info("[钓鱼] 设置已更新。")

    # ---- 主循环：只管抛竿、超时与重抛（咬钩判定在事件里） ----

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK)
            self._tick_count += 1
            if self._tick_count % int(5 / TICK) == 0:
                self._maybe_reload()
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(f"[钓鱼] 循环出错: {error!r}")

    async def _step(self) -> None:
        if not self._settings["enabled"] or self.bot is None:
            return
        now = time.monotonic()
        if self._state in ("idle", "cooldown"):
            if now >= self._next_cast_at:
                await self._cast()
            return
        if self._state == "casting":
            # 注册表给的 type_id 可能对不上（版本/服务端差异），那就用
            # 「刚抛竿 + 就在身边」的候选兜底，并把 type_id 纠正过来。
            if now - self._cast_at > self._settings["spawn_window"]:
                self._claim_candidate()
        if self._state == "waiting" and not self._armed:
            if now - self._claim_at >= self._settings["settle_delay"]:
                self._arm()
        if self._state == "waiting" and self._armed:
            self._check_water()
        if now - self._cast_at > self._settings["max_wait"]:
            # 浮标可能落在陆地上或线已断：收回来重抛
            log.info(f"[钓鱼] 久未咬钩，重新抛竿（{self._diagnosis()}）。")
            await self._reel(caught=False)

    def _arm(self) -> None:
        """落水延时已过：把当前 Y 定为基准，速度/下沉两路开始生效。

        以前这里要求「连续几次位置更新几乎不动」才算落稳，但浮标在水面上
        基本不动，服务端**就不再发位置包**，于是基准线常常永远建立不起来，
        速度与下沉两路被永久门控住——咬钩自然判不出来。改按时间：抛出去到
        落水就一秒出头，等这么久之后浮标要么在水里、要么这一竿本来就废了。
        """
        self._armed = True
        position = self._bobber_position()
        if position is not None:
            self._baseline = position[1]
            self._last_y = position[1]
        elif self._last_y is not None:
            self._baseline = self._last_y
        log.debug(f"[钓鱼] 浮标已就位，基准 Y={self._baseline}。")

    def _check_water(self) -> None:
        """落点不是水就重抛。要连着读到 DRY_CONFIRM 次才动手。

        只看一眼是不够的：抛得远时浮标可能还在水面上方飞着，那一眼读到的是
        空气。连读几次（每次隔一个 TICK）既能等它落定，也不会误杀好竿。
        """
        if not self._settings["water_check"]:
            return
        if self._bobber_in_water() is False:
            self._dry_reads += 1
        else:
            self._dry_reads = 0
            return
        if self._dry_reads >= DRY_CONFIRM:
            log.info("[钓鱼] 浮标没落在水里，立刻重抛。")
            self._recast_soon()

    def _bobber_in_water(self) -> bool | None:
        """浮标是否在水里。区块没加载或读不到方块时返回 None（不知道）。

        未加载的区块所有格都读成 0 号状态（空气），不做这个区分就会把
        「还没收到区块」当成「不是水」，于是不停重抛。
        """
        position = self._bobber_position()
        world = getattr(self.bot, "world", None)
        if position is None or world is None:
            return None
        x, y, z = position
        block_x, block_z = math.floor(x), math.floor(z)
        chunks = getattr(world, "chunks", None)
        if not chunks or (block_x >> 4, block_z >> 4) not in chunks:
            return None
        for block_y in (math.floor(y), math.floor(y) - 1):
            try:
                properties = world.block_properties(block_x, block_y, block_z)
            except Exception:
                return None
            if getattr(properties, "fluid", None) == "water":
                return True
        return False

    def _claim_candidate(self) -> None:
        """spawn_window 过了还没认领到浮标：用候选兜底，并纠正 type_id。"""
        candidate = self._candidate
        if candidate is None:
            return
        entity_id, type_id, _ = candidate
        if type_id is not None and type_id != self._bobber_type:
            log.info(
                f"[钓鱼] 注册表给的浮标 type_id={self._bobber_type} 没对上，"
                f"改用实际生成的 type_id={type_id}。"
            )
            self._bobber_type = type_id
        self._claim(entity_id, self._last_y)

    def _claim(self, entity_id: int, y: float | None) -> None:
        self._bobber_id = entity_id
        self._baseline = None
        self._last_y = y
        self._armed = False
        self._dry_reads = 0
        self._candidate = None
        self._claim_at = time.monotonic()
        self._state = "waiting"

    def _recast_soon(self) -> None:
        self._bobber_id = None
        self._baseline = None
        self._armed = False
        self._dry_reads = 0
        self._state = "cooldown"
        self._next_cast_at = time.monotonic() + self._settings["recast_delay"]

    def _diagnosis(self) -> str:
        """超时时说清楚我们到底看到了什么，免得只能靠猜。"""
        water = self._bobber_in_water()
        return (
            f"浮标={'已认领' if self._bobber_id is not None else '未认领'}, "
            f"速度包={self._motion_seen}, 位置包={self._move_seen}, "
            f"在水里={'是' if water else ('否' if water is False else '未知')}, "
            f"音效路={'开' if self._sound_packet_id() else '关'}"
            + (f", 已学音效 ID={self._learned_sound}" if self._learned_sound else "")
        )

    async def _cast(self) -> None:
        bot = self.bot
        if bot is None:
            return
        try:
            await bot.use_item(hand=self._settings["hand"])
        except Exception as error:
            log.error(f"[钓鱼] 抛竿失败: {error}")
            self._next_cast_at = time.monotonic() + 2.0
            return
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._dry_reads = 0
        self._motion_seen = 0
        self._move_seen = 0
        self._reeling = False
        self._state = "casting"
        self._cast_at = time.monotonic()
        log.debug("[钓鱼] 已抛竿。")

    async def _reel(self, *, caught: bool) -> None:
        """收杆。``caught`` 区分「钓上来」与「超时/断线重抛」，只影响日志计数。"""
        bot = self.bot
        if bot is None or self._reeling:
            return
        self._reeling = True
        try:
            await bot.use_item(hand=self._settings["hand"])
        except Exception as error:
            log.error(f"[钓鱼] 收杆失败: {error}")
        if caught:
            self._catches += 1
            waited = time.monotonic() - self._cast_at
            log.info(f"[钓鱼] 咬钩，已收杆（第 {self._catches} 条，等待 {waited:.1f}s）。")
        self._bobber_id = None
        self._baseline = None
        self._last_y = None
        self._armed = False
        self._candidate = None
        self._state = "cooldown"
        self._next_cast_at = time.monotonic() + self._settings["recast_delay"]

    # ---- 认领浮标 ----

    def _registry_bobber_type(self) -> int | None:
        """从服务端注册表查 fishing_bobber 的 type_id（协议 id 即条目下标）。"""
        registries = getattr(self.bot, "registries", None)
        if registries is None:
            return None
        try:
            entries = registries.get("minecraft:entity_type") or ()
        except Exception:
            return None
        for index, entry in enumerate(entries):
            if getattr(entry, "key", None) == "minecraft:fishing_bobber":
                return index
        return None

    async def _on_entity_add(self, entity) -> None:
        if self._state != "casting" or self.bot is None:
            return
        if time.monotonic() - self._cast_at > self._settings["spawn_window"]:
            return
        player = self.bot.player
        radius = self._settings["spawn_radius"]
        near = (
            abs(entity.x - player.x) <= radius
            and abs(entity.y - player.y) <= radius
            and abs(entity.z - player.z) <= radius
        )
        if not near:
            return
        type_id = getattr(entity, "type_id", None)
        if self._bobber_type is None:
            self._bobber_type = self._registry_bobber_type()
        if self._bobber_type is not None and type_id != self._bobber_type:
            # 类型对不上就先记成候选：注册表下标算错时靠它兜底（见 _claim_candidate）
            if self._candidate is None:
                self._candidate = (entity.entity_id, type_id, entity.y)
                self._last_y = entity.y
            return
        if self._bobber_type is None:
            self._bobber_type = type_id
            log.debug(f"[钓鱼] 已认定浮标实体类型 type_id={type_id}。")
        self._claim(entity.entity_id, entity.y)

    async def _on_entities_remove(self, entity_ids, removed) -> None:
        if self._bobber_id is not None and self._bobber_id in tuple(entity_ids):
            if self._state == "waiting" and not self._reeling:
                # 浮标凭空消失（线断/换维度）：不算钓到，重抛
                log.debug("[钓鱼] 浮标消失，重新抛竿。")
                self._recast_soon()

    # ---- 咬钩判定：音效（最准） ----

    async def _on_packet(self, packet) -> None:
        """位置型音效包：坐标落在浮标上就是咬钩。

        ``packet`` 事件对每个入站包都会触发，所以第一步只做一次整数比较。
        """
        wanted = self._sound_packet_id()
        if not wanted or packet.packet_id != wanted:
            return
        if not self._watching_sound():
            return
        decoded = self._decode_sound(packet.payload)
        if decoded is None:
            return  # 包 ID 配错或布局不符：当作没有音效路，交给另外两路
        sound_id, x, y, z = decoded
        position = self._bobber_position()
        if position is None:
            return
        radius = self._settings["sound_radius"]
        if (
            abs(x - position[0]) > radius
            or abs(y - position[1]) > radius
            or abs(z - position[2]) > radius
        ):
            return  # 甩竿/收杆的音效在玩家身上，别人钓鱼的在别处
        pinned = self._settings["sound_id"]
        expected = pinned if pinned is not None else self._learned_sound
        if expected is not None and sound_id != expected:
            return
        if expected is None and sound_id is not None:
            self._learned_sound = sound_id
            log.info(f"[钓鱼] 已学到咬钩音效 ID={sound_id}，之后按它精确判定。")
        log.debug(f"[钓鱼] 音效信号命中 id={sound_id}。")
        await self._reel(caught=True)

    def _sound_packet_id(self) -> int:
        """音效包 ID：设置里写死的优先，否则问当前版本的包表。

        版本表里为 0 表示这个版本的 ID 未经核实（例如 1.21.11），此时音效
        这一路直接关掉——拿推断值去解析只会误判，另外两路信号足够兜住。
        """
        configured = self._settings.get("sound_packet_id")
        if configured:
            return int(configured)
        version = getattr(self.bot, "version", None)
        packets = getattr(version, "packets", None)
        resolved = int(getattr(packets, "clientbound_sound", 0) or 0)
        if not resolved and not self._warned_no_sound:
            self._warned_no_sound = True
            log.info(
                "[钓鱼] 当前版本没有已核实的音效包 ID，音效判定已关闭"
                "（改用速度/下沉两路；可在 fishing.json 里手填 sound_packet_id）。"
            )
        return resolved

    def _watching_sound(self) -> bool:
        return (
            self._settings["enabled"]
            and self._state == "waiting"
            and not self._reeling
            and self._bobber_id is not None
        )

    def _bobber_position(self) -> tuple[float, float, float] | None:
        bot = self.bot
        if bot is None or self._bobber_id is None:
            return None
        entity = getattr(bot, "entities", {}).get(self._bobber_id)
        if entity is None:
            return None
        return entity.x, entity.y, entity.z

    @staticmethod
    def _decode_sound(payload: bytes):
        """解析位置型音效包；布局不符返回 None（宁可不判也不误判）。

        字段顺序：音效 holder（varint，0 = 内联 名称+bool+可选 float）、
        分类 varint、x/y/z 定点整数（÷8）、音量 float、音调 float、种子 long。
        """
        try:
            reader = PacketReader(payload)
            raw = reader.read_varint()
            if raw == 0:
                reader.read_string()
                if reader.read_bool():
                    reader.read_float()
                sound_id = None
            else:
                sound_id = raw - 1
            reader.read_varint()  # 分类
            x = reader.read_int() / 8.0
            y = reader.read_int() / 8.0
            z = reader.read_int() / 8.0
            reader.read_float()  # 音量
            reader.read_float()  # 音调
            reader.read_long()  # 随机种子
            reader.expect_end()  # 严格校验：包 ID 配错时几乎必然在这里失败
        except Exception:
            return None
        return sound_id, x, y, z

    # ---- 咬钩判定（速度 / 下沉，均需先落稳） ----

    async def _on_entity_motion(self, entity_id, velocity, entity) -> None:
        if not self._watching(entity_id):
            return
        self._motion_seen += 1
        if not self._armed:
            return  # 还在飞行途中：抛出去时的初速度同样可能是向下的
        threshold = self._settings["bite_velocity"]
        if threshold < 0 and velocity[1] <= threshold:
            log.debug(f"[钓鱼] 速度信号命中 vy={velocity[1]:.3f}。")
            await self._reel(caught=True)

    async def _on_entity_move(self, entity_id, entity) -> None:
        if entity is not None and self._watching(entity_id):
            self._move_seen += 1
            await self._check_dip(entity.y)

    async def _on_entity_teleport(self, entity_id, entity, relative) -> None:
        if entity is not None and self._watching(entity_id):
            self._move_seen += 1
            await self._check_dip(entity.y)

    def _watching(self, entity_id: int) -> bool:
        return (
            self._state == "waiting"
            and not self._reeling
            and entity_id == self._bobber_id
            and self._settings["enabled"]
        )

    async def _check_dip(self, y: float) -> None:
        """看浮标是否被从静止水面拽了下去。

        基准线由 :meth:`_arm` 在落水延时之后确立，不再依赖「连续几次几乎
        不动」——水面上的浮标根本不发位置包，那种判定常常永远等不到。
        """
        drop = self._settings["bite_drop"]
        self._last_y = y
        if not self._armed or self._baseline is None:
            return
        if drop > 0 and self._baseline - y >= drop:
            log.debug(
                f"[钓鱼] 下沉信号命中 {self._baseline - y:.3f} 格 "
                f"(基准 {self._baseline:.3f} -> {y:.3f})。"
            )
            await self._reel(caught=True)
