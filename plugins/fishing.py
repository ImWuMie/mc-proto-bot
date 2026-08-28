"""自动钓鱼插件：抛竿、判定咬钩、收杆，循环往复。

判定思路（本协议栈没有音效包，因此不依赖 ``entity.fishing_bobber.splash``）：

  1. **认领浮标**——优先从服务端下发的 ``minecraft:entity_type`` 注册表里查
     ``minecraft:fishing_bobber`` 的 type_id；查不到就把「抛竿后 2 秒内、
     身边 8 格内新生成的实体」当作浮标，并**记住它的 type_id**，之后每次
     抛竿都能精确认领。
  2. **等它静止**——浮标飞行落水期间 Y 一直在变，连续几次更新几乎不动才
     确立静止水面基准线（否则抛物线会被误判成咬钩）。
  3. **两路信号**——``entity_motion`` 报出向下速度（原版咬钩约 -0.4 格/tick），
     或位置从基准线下沉超过阈值；任一命中即视为咬钩，立刻收杆。
     两路都只在基准线确立之后生效（否则抛物线下落时的重力速度会被误判），
     且都可单独关闭、阈值可调。

超时兜底：``max_wait`` 秒没咬钩（浮标落在陆地上、线被打断等）就收杆重抛；
浮标被移除也会重抛。收杆与重抛之间只隔 ``recast_delay`` 秒（默认 0.4），
在不显得机械的前提下尽量少空转。

设置文件 ``fishing.json``（与本插件同目录，首次启用自动生成）修改后约 5 秒
内自动重新加载，**默认 enabled=false**：把它改成 true 才会开始钓。手持鱼竿
由你自己保证——本协议栈拿不到物品名称，插件无法校验手里是不是鱼竿。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from protobot import Plugin, log

DEFAULT_SETTINGS: dict = {
    "enabled": False,  # 改成 true 才开始自动钓鱼
    "hand": "main_hand",  # 用哪只手甩竿：main_hand / off_hand
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
        self._file: Path | None = None
        self._settings: dict = dict(DEFAULT_SETTINGS)
        self._mtime: float | None = None
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
        self.subscribe("entity_add", self._on_entity_add)
        self.subscribe("entity_motion", self._on_entity_motion)
        self.subscribe("entity_move", self._on_entity_move)
        self.subscribe("entity_teleport", self._on_entity_teleport)
        self.subscribe("entities_remove", self._on_entities_remove)
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
        """就地改开关并写回 fishing.json，避免 5 秒后被文件内容覆盖回去。"""
        self._settings["enabled"] = enabled
        if not enabled:
            self._reset()
        path = self._file
        if path is None:
            return
        try:
            data = dict(self._settings)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._mtime = path.stat().st_mtime  # 自己写的，不算「外部改动」
        except OSError as error:
            log.warn(f"[钓鱼] 开关状态写回失败 ({error})")
        log.info(f"[钓鱼] {'开始' if enabled else '停止'}自动钓鱼。")


    # ---- 生命周期 ----

    async def on_enable(self) -> None:
        if self._file is None:
            source = (
                self.manager.source_of(self.name)
                if self.manager is not None
                else None
            )
            base = source.parent if source is not None else Path("plugins")
            self._file = base / "fishing.json"
        self._load_settings()
        self._loop_task = asyncio.create_task(
            self._loop(), name="protobot-fishing"
        )
        if self._settings["enabled"]:
            log.info("[钓鱼] 已启用，等待连接后开始自动抛竿。")
        else:
            log.info(
                f"[钓鱼] 插件已加载但未开启：把 {self._file} 里的 "
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

    # ---- 设置：生成模板 + mtime 热重载 ----

    def _load_settings(self) -> None:
        path = self._file
        if path is None:
            return
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info(f"[钓鱼] 已生成默认设置: {path}")
            except OSError as error:
                log.warn(f"[钓鱼] 无法写入默认设置 ({error})")
        merged = dict(DEFAULT_SETTINGS)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged.update(loaded)
            else:
                log.warn("[钓鱼] 设置文件不是 JSON 对象，使用默认设置。")
        except (OSError, ValueError) as error:
            log.warn(f"[钓鱼] 设置读取失败，使用默认设置 ({error})")
        merged["enabled"] = bool(merged.get("enabled", False))
        if merged.get("hand") not in ("main_hand", "off_hand"):
            merged["hand"] = "main_hand"
        for key in (
            "bite_velocity", "bite_drop", "settle_epsilon",
            "recast_delay", "max_wait", "spawn_window", "spawn_radius",
        ):
            try:
                merged[key] = float(merged.get(key, DEFAULT_SETTINGS[key]))
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        try:
            merged["settle_updates"] = max(1, int(merged.get("settle_updates", 3)))
        except (TypeError, ValueError):
            merged["settle_updates"] = 3
        merged["recast_delay"] = max(0.05, merged["recast_delay"])
        merged["max_wait"] = max(5.0, merged["max_wait"])
        self._settings = merged
        try:
            self._mtime = path.stat().st_mtime
        except OSError:
            self._mtime = None

    def _maybe_reload(self) -> None:
        path = self._file
        if path is None or not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._mtime is not None and mtime != self._mtime:
            was_on = self._settings.get("enabled")
            self._load_settings()
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

    # ---- 咬钩判定（两路信号，任一命中立刻收杆） ----

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
