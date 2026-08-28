"""自动钓鱼插件：抛竿、判定咬钩、收杆，循环往复。

判定按可靠性排序，三路信号任一命中即收杆：

  1. **咬钩音效**（最准，原版就是靠它提示玩家）——``entity.fishing_bobber.splash``
     由位置型音效包（协议 775/776 的 0x75 = 117）发出，坐标是**浮标所在处**；
     甩竿/收杆的音效发在**玩家所在处**，因此「音效位置离浮标 1.5 格内」既能
     认出咬钩，也能排除自己甩竿和别人钓鱼的动静。音效的数字 ID 逐版本变动
     且不由服务端下发（``minecraft:sound_event`` 是内置注册表），所以这里
     **不硬编码**：第一次靠位置认出咬钩时把 ID 学下来，之后要求 ID 也匹配。
  2. **向下速度**——``entity_motion`` 报出浮标的向下速度（原版咬钩约 -0.4 格/tick）。
  3. **位置下沉**——浮标从静止水面基准线被拽下超过阈值。

浮标怎么认领：优先从服务端下发的 ``minecraft:entity_type`` 注册表里查
``minecraft:fishing_bobber`` 的 type_id；查不到就把「抛竿后 2 秒内、身边
8 格内新生成的实体」当作浮标，并**记住它的 type_id**，之后每次抛竿都精确认领。

速度与下沉两路都必须等浮标落稳、基准线确立后才生效——飞行途中重力速度同样
是负的、Y 也在持续下降，不做这个门控会一抛竿就误判。音效路不受此限制，它本身
就只在鱼咬钩时才响。

超时兜底：``max_wait`` 秒没咬钩（浮标落在陆地上、线被打断等）就收杆重抛；
浮标被移除也会重抛。收杆与重抛之间只隔 ``recast_delay`` 秒（默认 0.4）。

设置文件 ``fishing.json``（与本插件同目录，首次启用自动生成）修改后约 5 秒
内自动重新加载，**默认 enabled=false**：把它改成 true 才会开始钓，也可以让
LLM 调用暴露出来的 ``fishing.start`` / ``fishing.stop`` / ``fishing.status``。
手持鱼竿由你自己保证——本协议栈拿不到物品名称，插件无法校验手里是不是鱼竿。
"""

from __future__ import annotations

import asyncio
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
    "bite_velocity": -0.15,  # 向下速度阈值（格/tick，原版咬钩约 -0.4）；0 关闭该路
    "bite_drop": 0.12,  # 相对静止基准线的下沉阈值（格）；0 关闭该路
    "settle_updates": 3,  # 连续多少次位置更新几乎不动才算落稳
    "settle_epsilon": 0.02,  # 「几乎不动」的判定（格）
    "recast_delay": 0.4,  # 收杆到重抛的间隔（秒）
    "max_wait": 45.0,  # 抛竿后多久没咬钩就重抛（秒）
    "spawn_window": 2.0,  # 认领浮标：抛竿后多少秒内生成的实体才算
    "spawn_radius": 8.0,  # 认领浮标：距自己多少格内
}

#: 主循环步长（秒）。判定走事件、不靠轮询，这里只管超时与重抛。
TICK = 0.1


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
        self._stable = 0
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
        if self._state == "waiting" and self._baseline is None:
            detail = "bobber in flight, not settled yet"
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
        self._stable = 0
        self._state = "idle"
        self._reeling = False

    # ---- 设置：默认值与钳制由本插件负责，读写/热重载交给框架 ----

    @staticmethod
    def _normalize(merged: dict) -> dict:
        merged["enabled"] = bool(merged.get("enabled", False))
        if merged.get("hand") not in ("main_hand", "off_hand"):
            merged["hand"] = "main_hand"
        for key in (
            "bite_velocity", "bite_drop", "settle_epsilon",
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
        try:
            merged["settle_updates"] = max(1, int(merged.get("settle_updates", 3)))
        except (TypeError, ValueError):
            merged["settle_updates"] = 3
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
        if now - self._cast_at > self._settings["max_wait"]:
            # 浮标可能落在陆地上或线已断：收回来重抛
            log.info("[钓鱼] 久未咬钩，重新抛竿。")
            await self._reel(caught=False)

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
        self._stable = 0
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
        self._stable = 0
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
        if self._bobber_type is None:
            self._bobber_type = self._registry_bobber_type()
        type_id = getattr(entity, "type_id", None)
        if self._bobber_type is not None:
            if type_id != self._bobber_type:
                return
        else:
            # 还不知道浮标的 type_id：用「刚抛竿 + 就在身边」认领，并记下来
            if time.monotonic() - self._cast_at > self._settings["spawn_window"]:
                return
            player = self.bot.player
            radius = self._settings["spawn_radius"]
            if (
                abs(entity.x - player.x) > radius
                or abs(entity.y - player.y) > radius
                or abs(entity.z - player.z) > radius
            ):
                return
            self._bobber_type = type_id
            log.debug(f"[钓鱼] 已认定浮标实体类型 type_id={type_id}。")
        self._bobber_id = entity.entity_id
        self._baseline = None
        self._last_y = entity.y
        self._stable = 0
        self._state = "waiting"

    async def _on_entities_remove(self, entity_ids, removed) -> None:
        if self._bobber_id is not None and self._bobber_id in tuple(entity_ids):
            if self._state == "waiting" and not self._reeling:
                # 浮标凭空消失（线断/换维度）：不算钓到，重抛
                log.debug("[钓鱼] 浮标消失，重新抛竿。")
                self._bobber_id = None
                self._state = "cooldown"
                self._next_cast_at = (
                    time.monotonic() + self._settings["recast_delay"]
                )

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
        if self._baseline is None:
            return  # 还在飞行途中：重力带来的向下速度不是咬钩
        threshold = self._settings["bite_velocity"]
        if threshold < 0 and velocity[1] <= threshold:
            log.debug(f"[钓鱼] 速度信号命中 vy={velocity[1]:.3f}。")
            await self._reel(caught=True)

    async def _on_entity_move(self, entity_id, entity) -> None:
        if entity is not None and self._watching(entity_id):
            await self._check_dip(entity.y)

    async def _on_entity_teleport(self, entity_id, entity, relative) -> None:
        if entity is not None and self._watching(entity_id):
            await self._check_dip(entity.y)

    def _watching(self, entity_id: int) -> bool:
        return (
            self._state == "waiting"
            and not self._reeling
            and entity_id == self._bobber_id
            and self._settings["enabled"]
        )

    async def _check_dip(self, y: float) -> None:
        """先等浮标落稳确立基准线，再看它是否被拽下水面。

        抛竿后的抛物线会让 Y 连续大幅变化，必须等「连续几次几乎不动」才建立
        基准，否则下落过程本身就会被当成咬钩。
        """
        drop = self._settings["bite_drop"]
        last = self._last_y
        self._last_y = y
        if self._baseline is None:
            if last is not None and abs(y - last) <= self._settings["settle_epsilon"]:
                self._stable += 1
                if self._stable >= self._settings["settle_updates"]:
                    self._baseline = y
                    log.debug(f"[钓鱼] 浮标已落稳，基准 Y={y:.3f}。")
            else:
                self._stable = 0
            return
        if drop > 0 and self._baseline - y >= drop:
            log.debug(
                f"[钓鱼] 下沉信号命中 {self._baseline - y:.3f} 格 "
                f"(基准 {self._baseline:.3f} -> {y:.3f})。"
            )
            await self._reel(caught=True)
