"""自动重生插件：死了就自己站起来，不用人盯着。

死亡信号来自核心的 ``death`` 事件，它由两路合并而成（``protobot.client``
已按「一次死亡只发一次」去重）：

  1. **Combat Death**（协议 775/776 的 0x44 = 68）——这个包的用途就是
     「让客户端弹出死亡界面」，是服务端在玩家死亡时必然发出的通知，也是
     本插件依赖的主信号。它带着死亡消息组件，会一并记下来。
  2. **血量归零**（``set_health`` 0x68 = 104 里 health ≤ 0）——次要信号。
     服务端在 tick 边界补发，顺序不受协议保证，而且服务器插件可以缩放血量，
     所以它只当兜底，不作为唯一依据。

重生动作是 **Client Status**（服务端方向 0x0C = 12，载荷只有一个 VarInt，
0 = perform respawn）。服务端从不自己让玩家重生：即便开了 ``doImmediateRespawn``
也只是原版客户端不显示死亡界面、直接把这个包发出去而已。所以不发它，bot 就会
一直躺在死亡界面里，位置不动、物理不跑。

发出请求后服务端回 ``respawn`` 包，紧接一次带 teleport id 的位置同步；确认传送
与补发 Player Loaded 都由核心处理（``_handle_respawn`` 会清掉 ``player.loaded``，
下一个位置包便会重新确认并补发），这里只等 ``respawn`` 事件当作确认。

包 ID 未经核实的版本（如 1.21.11 / 协议 774）上 ``bot.respawn()`` 会抛
``UnsupportedVersion``——插件提示一次就退出，绝不拿推断值乱发包。

设置文件 ``respawn.json``（与本插件同目录，首次启用自动生成）改动后约 5 秒生效。
默认只做重生本身；``return_to_death_point`` 打开后还会寻路走回死亡坐标。
"""

from __future__ import annotations

import asyncio
import math
import time

from protobot import Plugin, PluginSettings, log, plain_text
from protobot.errors import UnsupportedVersion

DEFAULT_SETTINGS: dict = {
    "enabled": True,  # 改成 false 可只保留 respawn.status / respawn.now
    "delay": 1.0,  # 死亡到发出重生请求的间隔（秒）
    "retry_delay": 2.0,  # 多久没等到重生确认就重试（秒）
    "max_retries": 2,  # 最多重试几次（0 = 只发一次）
    "announce": "",  # 非空则重生后发这句聊天；留空不发
    "return_to_death_point": False,  # 重生后寻路走回死亡坐标
    "return_max_distance": 200.0,  # 超过这个直线距离就不走回去（格）
}

#: 设置文件的轮询间隔（秒）。重生本身走事件，不靠轮询。
RELOAD_INTERVAL = 5.0

#: 走回死亡点前等世界解码的上限（秒）。等不到就放弃，不能挂死在这里。
WORLD_TIMEOUT = 30.0


class AutoRespawn(Plugin):
    name = "respawn"

    def __init__(self) -> None:
        super().__init__()
        self._config: PluginSettings | None = None
        self._settings: dict = AutoRespawn._normalize(dict(DEFAULT_SETTINGS))
        self._reload_task: asyncio.Task | None = None
        self._respawn_task: asyncio.Task | None = None
        self._respawned = asyncio.Event()
        self._deaths = 0
        self._last_death: tuple[float, float, float] | None = None
        self._last_death_at: float | None = None
        self._last_message = ""
        self._warned_unsupported = False
        self.subscribe("death", self._on_death)
        self.subscribe("respawn", self._on_respawn)
        # 暴露给其他插件与 LLM：respawn.status / respawn.now / respawn.set
        self.expose(
            "status",
            self._service_status,
            description=(
                "Auto-respawn status: whether it is on, whether the bot is "
                "currently alive, its health and food, how many times it has "
                "died on this connection and where it died last."
            ),
            llm=True,
        )
        self.expose(
            "now",
            self._service_now,
            description=(
                "Respawn right now instead of waiting out the configured "
                "delay. Only useful while the bot is dead."
            ),
            llm=True,
            admin=True,
        )
        self.expose(
            "set",
            self._service_set,
            description=(
                "Turn auto-respawn on or off, or change how it behaves: "
                "delay before respawning, and whether to walk back to the "
                "place of death afterwards."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "Respawn automatically on death",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds to wait before respawning",
                    },
                    "return_to_death_point": {
                        "type": "boolean",
                        "description": "Walk back to where the bot died",
                    },
                    "announce": {
                        "type": "string",
                        "description": (
                            "Chat message to send after respawning; empty to "
                            "say nothing"
                        ),
                    },
                },
            },
            llm=True,
            admin=True,
        )

    # ---- 生命周期 ----

    async def on_enable(self) -> None:
        if self._config is None:
            self._config = self.settings_file(
                "respawn.json", DEFAULT_SETTINGS,
                label="自动重生", normalize=self._normalize,
            )
        self._config.load()
        self._settings = self._config.data
        self._reload_task = asyncio.create_task(
            self._reload_loop(), name="protobot-respawn-settings"
        )
        state = "已启用" if self._settings["enabled"] else "已加载但未启用"
        log.info(f"[重生] {state}（设置：{self._config.path}）。")

    async def on_disable(self) -> None:
        for task in (self._reload_task, self._respawn_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._reload_task = None
        self._respawn_task = None
        log.info(f"[重生] 已关闭（本进程共处理 {self._deaths} 次死亡）。")

    async def on_bot_ready(self) -> None:
        """新连接：作废上一条连接遗留的重生流程。"""

        task = self._respawn_task
        if task is not None and not task.done():
            task.cancel()
        self._respawn_task = None
        self._respawned.clear()
        # 热重载或重连时可能正躺在死亡界面上，这种情况直接补一次重生。
        bot = self.bot
        if bot is not None and bot.player.dead and self._settings["enabled"]:
            log.info("[重生] 连接就绪时仍处于死亡状态，补发重生请求。")
            self._start_respawn()

    async def _reload_loop(self) -> None:
        while True:
            await asyncio.sleep(RELOAD_INTERVAL)
            try:
                if self._config.reload_if_changed():
                    was_on = self._settings.get("enabled")
                    self._settings = self._config.data
                    now_on = self._settings.get("enabled")
                    if was_on != now_on:
                        log.info(
                            f"[重生] 设置已更新：{'开启' if now_on else '关闭'}自动重生。"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error(f"[重生] 读取设置出错: {error!r}")

    # ---- 事件 ----

    async def _on_death(self, message) -> None:
        self._deaths += 1
        self._last_death_at = time.time()
        self._last_message = plain_text(message).strip() if message else ""
        bot = self.bot
        if bot is not None:
            self._last_death = bot.player.position
        where = ""
        if self._last_death is not None:
            x, y, z = self._last_death
            where = f"，死于 {x:.1f} {y:.1f} {z:.1f}"
        reason = f"：{self._last_message}" if self._last_message else ""
        log.info(f"[重生] 检测到死亡{where}{reason}")
        if not self._settings["enabled"]:
            log.info("[重生] 自动重生已关闭，留在死亡界面。")
            return
        self._start_respawn()

    async def _on_respawn(self, session) -> None:
        self._respawned.set()

    # ---- 重生流程 ----

    def _start_respawn(self, *, delay: float | None = None) -> None:
        task = self._respawn_task
        if task is not None and not task.done():
            return  # 同一次死亡不重复排程
        self._respawned.clear()
        if delay is None:
            delay = float(self._settings["delay"])
        self._respawn_task = asyncio.create_task(
            self._respawn_flow(delay), name="protobot-respawn"
        )

    async def _respawn_flow(self, delay: float) -> None:
        settings = self._settings
        await asyncio.sleep(max(0.0, delay))
        attempts = 1 + max(0, int(settings["max_retries"]))
        timeout = max(0.5, float(settings["retry_delay"]))
        for attempt in range(1, attempts + 1):
            bot = self.bot
            if bot is None:
                log.warn("[重生] 尚未连接，本次重生请求作废。")
                return
            if not bot.player.dead:
                # 手动重生过了，或者服务器已经把我们拉起来了。
                await self._after_respawn()
                return
            try:
                await bot.respawn()
            except UnsupportedVersion as error:
                if not self._warned_unsupported:
                    self._warned_unsupported = True
                    log.warn(f"[重生] {error}；这个版本上自动重生不可用。")
                return
            except Exception as error:
                log.error(f"[重生] 发送重生请求失败: {error!r}")
                return  # 连接本身有问题，重试也没有意义
            try:
                await asyncio.wait_for(self._respawned.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                if attempt < attempts:
                    log.warn(f"[重生] 第 {attempt} 次请求没等到重生确认，重试。")
                continue
            await self._after_respawn()
            return
        log.warn(f"[重生] {attempts} 次请求都没等到重生确认，放弃。")

    async def _after_respawn(self) -> None:
        log.info(f"[重生] 已重生（本进程第 {self._deaths} 次）。")
        bot = self.bot
        announce = str(self._settings["announce"]).strip()
        if announce and bot is not None:
            try:
                await bot.send_message(announce)
            except Exception as error:
                log.warn(f"[重生] 重生播报发送失败: {error!r}")
        if not self._settings["return_to_death_point"]:
            return
        if bot is None or self._last_death is None:
            return
        x, y, z = self._last_death
        distance = math.dist(bot.player.position, (x, y, z))
        limit = float(self._settings["return_max_distance"])
        if distance > limit:
            log.info(
                f"[重生] 死亡点在 {distance:.0f} 格外（上限 {limit:.0f} 格），不走回去。"
            )
            return
        log.info(f"[重生] 正在走回死亡点 {x:.1f} {z:.1f}（{distance:.0f} 格）。")
        try:
            await bot.wait_world(timeout=WORLD_TIMEOUT)
            await bot.navigate_to(x, z, sprint=True)
        except TimeoutError:
            log.warn("[重生] 等世界解码超时，放弃走回死亡点。")
        except Exception as error:
            log.warn(f"[重生] 走回死亡点失败: {error!r}")
        else:
            log.info("[重生] 已回到死亡点附近。")

    # ---- 暴露给其他插件 / LLM 的能力 ----

    async def _service_status(self) -> str:
        parts = [
            "Auto-respawn is " + ("on" if self._settings["enabled"] else "off")
        ]
        bot = self.bot
        if bot is None:
            parts.append("not connected")
        else:
            parts.append(
                f"health {bot.player.health:.1f}/20, food {bot.player.food}"
            )
            parts.append("currently DEAD" if bot.player.dead else "alive")
        parts.append(f"{self._deaths} death(s) so far")
        if self._last_death is not None:
            x, y, z = self._last_death
            ago = ""
            if self._last_death_at is not None:
                ago = f" {time.time() - self._last_death_at:.0f}s ago"
            parts.append(f"last died at {x:.1f} {y:.1f} {z:.1f}{ago}")
        if self._last_message:
            parts.append(f'death message: "{self._last_message}"')
        if self._settings["return_to_death_point"]:
            parts.append("walks back to the death point after respawning")
        return "; ".join(parts)

    async def _service_now(self) -> str:
        bot = self.bot
        if bot is None:
            return "Not connected"
        if not bot.player.dead:
            return "Not dead, so there is nothing to respawn from"
        task = self._respawn_task
        if task is not None and not task.done():
            task.cancel()  # 取消还在等 delay 的那次，立刻重发
            self._respawn_task = None
        self._start_respawn(delay=0.0)
        return "Respawn requested"

    async def _service_set(self, **changes) -> str:
        allowed = ("enabled", "delay", "return_to_death_point", "announce")
        unknown = sorted(key for key in changes if key not in allowed)
        if unknown:
            return f"Unknown field(s): {', '.join(unknown)}"
        if not changes:
            return "Nothing to change"
        merged = dict(self._settings)
        merged.update(changes)
        merged = self._normalize(merged)
        patch = {key: merged[key] for key in changes}
        error = self._config.patch(patch)
        self._settings = self._config.data
        if error:
            self._settings.update(patch)  # 至少让本进程内生效
            return f"Applied in memory but could not be saved: {error}"
        described = ", ".join(f"{key}={patch[key]!r}" for key in sorted(patch))
        return f"Auto-respawn updated: {described}"

    # ---- 设置校验 ----

    @staticmethod
    def _normalize(merged: dict) -> dict:
        for key in ("enabled", "return_to_death_point"):
            merged[key] = bool(merged.get(key, DEFAULT_SETTINGS[key]))
        for key in ("delay", "retry_delay", "return_max_distance"):
            try:
                merged[key] = max(0.0, float(merged.get(key, DEFAULT_SETTINGS[key])))
            except (TypeError, ValueError):
                merged[key] = DEFAULT_SETTINGS[key]
        try:
            merged["max_retries"] = max(0, int(merged.get("max_retries", 2)))
        except (TypeError, ValueError):
            merged["max_retries"] = DEFAULT_SETTINGS["max_retries"]
        announce = merged.get("announce") or ""
        merged["announce"] = str(announce)[:256]
        return merged
