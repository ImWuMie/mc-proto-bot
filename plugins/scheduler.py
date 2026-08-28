"""定时任务插件：按间隔或每天固定时间自动发送聊天/执行命令。

任务存于本插件同目录的 ``scheduler.json``（首次启用自动生成，示例任务
默认禁用）：

.. code-block:: json

    {
      "tasks": [
        {"name": "整点报时", "time": "18:00", "action": "chat",
         "text": "晚上好！", "enabled": true},
        {"name": "清理提醒", "interval": 1800.0, "action": "command",
         "text": "say 该清理掉落物啦", "enabled": true}
      ]
    }

字段：``interval``（秒，≥5，循环执行）与 ``time``（"HH:MM" 本地时间，每天
执行一次）至少给一个；``action`` 为 ``chat``（发聊天）或 ``command``
（执行服务器命令）；``enabled`` 为 false 时暂停。

运行时每 5 秒检查一次 JSON 的修改时间，改动自动重新加载——llm_agent 的
schedule_* 工具就是直接编辑这个文件，无需热重载本插件。未连接服务器时
任务顺延，不消耗本次调度。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from protobot import Plugin, log

# 小时/分钟必须在合法范围内：只查位数会放过 "25:00" 这类越界时刻。
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

#: interval 下限（秒），防止刷屏
MIN_INTERVAL = 5.0

DEFAULT_TASKS: dict = {
    "tasks": [
        {
            "name": "示例广播",
            "interval": 3600.0,
            "action": "chat",
            "text": "示例任务：每小时发送一次。编辑 scheduler.json 修改我。",
            "enabled": False,
        }
    ]
}


class Scheduler(Plugin):
    name = "scheduler"

    def __init__(self) -> None:
        super().__init__()
        self._file: Path | None = None
        self._tasks: list[dict] = []
        self._next_run: dict[str, float] = {}  # 任务名 -> 下次运行的单调秒
        self._mtime: float | None = None
        self._loop_task: asyncio.Task | None = None
        self._tick_count = 0

    # ---- 生命周期 ----

    async def on_enable(self) -> None:
        if self._file is None:
            source = (
                self.manager.source_of(self.name)
                if self.manager is not None
                else None
            )
            base = source.parent if source is not None else Path("plugins")
            self._file = base / "scheduler.json"
        self._load_tasks()
        self._loop_task = asyncio.create_task(
            self._loop(), name="protobot-scheduler"
        )
        log.info(f"[定时] 已启用，共 {len(self._tasks)} 个任务。")

    async def on_disable(self) -> None:
        task = self._loop_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        log.info("[定时] 已关闭。")

    # ---- 加载与校验 ----

    def _load_tasks(self) -> None:
        path = self._file
        if path is None:
            return
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(DEFAULT_TASKS, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info(f"[定时] 已生成默认任务文件: {path}（示例任务默认禁用）")
            except OSError as error:
                log.warn(f"[定时] 无法写入默认任务文件 ({error})")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("tasks") if isinstance(data, dict) else None
        except (OSError, ValueError) as error:
            log.warn(f"[定时] 任务文件读取失败，使用空任务列表 ({error})")
            raw = None
        valid: list[dict] = []
        for task in raw or []:
            parsed = self._validate(task)
            if parsed is not None:
                valid.append(parsed)
        # 保留已有任务的计时；新增任务从下次周期开始
        self._next_run = {
            task["name"]: self._next_run[task["name"]]
            for task in valid
            if task["name"] in self._next_run
        }
        self._tasks = valid
        try:
            self._mtime = path.stat().st_mtime
        except OSError:
            self._mtime = None
        log.info(f"[定时] 已加载 {len(valid)} 个任务。")

    def _validate(self, task) -> dict | None:
        """校验并归一化一个任务；非法任务返回 None 并打日志。"""
        if not isinstance(task, dict):
            log.warn("[定时] 跳过非法任务（非对象）。")
            return None
        name = str(task.get("name") or "").strip()
        if not name:
            log.warn("[定时] 跳过无名任务。")
            return None
        action = str(task.get("action") or "chat")
        if action not in ("chat", "command"):
            log.warn(f"[定时] 任务 {name} 的 action 非法，已跳过。")
            return None
        text = str(task.get("text") or "").strip()
        if not text:
            log.warn(f"[定时] 任务 {name} 缺少 text，已跳过。")
            return None
        interval = None
        if task.get("interval") is not None:
            try:
                interval = float(task["interval"])
                if interval < MIN_INTERVAL:
                    interval = MIN_INTERVAL
            except (TypeError, ValueError):
                log.warn(f"[定时] 任务 {name} 的 interval 非法，已跳过。")
                return None
        time_spec = str(task.get("time") or "").strip()
        if time_spec and not TIME_PATTERN.match(time_spec):
            log.warn(f"[定时] 任务 {name} 的 time 非法（需 HH:MM），已跳过。")
            return None
        if interval is None and not time_spec:
            log.warn(f"[定时] 任务 {name} 既没有 interval 也没有 time，已跳过。")
            return None
        return {
            "name": name,
            "interval": interval,
            "time": time_spec or None,
            "action": action,
            "text": text,
            "enabled": bool(task.get("enabled", True)),
        }

    # ---- 主循环 ----

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._tick_count += 1
            if self._tick_count % 5 == 0:
                self._maybe_reload()
            await self._run_due()

    def _maybe_reload(self) -> None:
        """JSON 被外部修改（如 llm_agent 的 schedule_* 工具）后自动重载。"""
        path = self._file
        if path is None or not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._mtime is not None and mtime != self._mtime:
            log.info("[定时] 任务文件已修改，重新加载。")
            self._load_tasks()

    async def _run_due(self) -> None:
        bot = self.bot
        if bot is None:
            return  # 尚未连接：到期任务顺延，不消耗本次调度
        now = time.monotonic()
        for task in self._tasks:
            if not task["enabled"]:
                continue
            name = task["name"]
            if name not in self._next_run:
                delay = (
                    task["interval"]
                    if task["interval"] is not None
                    else self._seconds_until(task["time"])
                )
                self._next_run[name] = now + delay
                continue
            if now < self._next_run[name]:
                continue
            if task["interval"] is not None:
                self._next_run[name] = now + task["interval"]
            else:
                self._next_run[name] = now + self._seconds_until(task["time"])
            try:
                if task["action"] == "command":
                    await bot.send_command(task["text"])
                    log.info(f"[定时] 任务 {name}: 已执行命令。")
                else:
                    await bot.send_message(task["text"])
                    log.info(f"[定时] 任务 {name}: 已发送聊天。")
            except Exception as error:
                log.error(f"[定时] 任务 {name} 执行失败: {error}")

    def _seconds_until(self, spec: str) -> float:
        """到下一个 ``HH:MM`` 本地时刻的秒数（已过则算到明天）。"""
        now = time.localtime()
        hour, minute = spec.split(":")
        target = time.mktime(
            (
                now.tm_year, now.tm_mon, now.tm_mday,
                int(hour), int(minute), 0, 0, 0, now.tm_isdst,
            )
        )
        delta = target - time.time()
        if delta <= 0:
            delta += 86400.0
        return max(delta, MIN_INTERVAL)
