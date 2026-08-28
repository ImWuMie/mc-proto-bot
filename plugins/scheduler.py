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

运行时每 5 秒检查一次 JSON 的修改时间，改动自动重新加载。任务也可以通过
暴露出去的 ``scheduler.list`` / ``add`` / ``set`` / ``remove`` / ``run`` /
``status`` 由其他插件或 LLM 直接维护——增删改经由本插件自己的校验，写回后
立即生效，因此校验规则只有这一份（``_normalize``）。未连接服务器时任务顺延，
不消耗本次调度。
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

#: 暴露给 LLM 的参数表：add/set 用完整字段，remove/run 只要名字。
TASK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Unique task name"},
        "interval": {
            "type": "number",
            "description": "Seconds between runs (at least 5)",
        },
        "time": {
            "type": "string",
            "description": "Daily local time HH:MM (24-hour)",
        },
        "action": {
            "type": "string",
            "description": "chat to send a message, command to run one",
        },
        "text": {"type": "string", "description": "Message body or command"},
        "enabled": {"type": "boolean", "description": "Pause or resume"},
    },
    "required": ["name"],
}

NAME_SCHEMA: dict = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "Task name"}},
    "required": ["name"],
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
        # 暴露给其他插件与 LLM：scheduler.list / add / set / remove / run
        self.expose(
            "list",
            self._service_list,
            description="List scheduled tasks: when each runs, what it does, and whether it is enabled.",
            llm=True,
        )
        self.expose(
            "add",
            self._service_add,
            description=(
                "Add a scheduled task that repeats every interval seconds "
                "and/or runs daily at a local HH:MM time. Takes effect within "
                "5 seconds."
            ),
            parameters=TASK_SCHEMA,
            llm=True,
            admin=True,
        )
        self.expose(
            "set",
            self._service_set,
            description=(
                "Modify a scheduled task: pass any of interval/time/action/"
                "text/enabled to change just those fields."
            ),
            parameters=TASK_SCHEMA,
            llm=True,
            admin=True,
        )
        self.expose(
            "remove",
            self._service_remove,
            description="Delete a scheduled task by name.",
            parameters=NAME_SCHEMA,
            llm=True,
            admin=True,
        )
        self.expose(
            "run",
            self._service_run,
            description=(
                "Run a scheduled task once right now without changing its "
                "schedule."
            ),
            parameters=NAME_SCHEMA,
            llm=True,
            admin=True,
        )
        self.expose(
            "status",
            self._service_status,
            description="How many scheduled tasks exist and how many are enabled.",
            llm=True,
        )

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
        """文件加载路径：非法任务打日志并跳过。"""
        parsed, error = self._normalize(task)
        if parsed is None:
            log.warn(f"[定时] 跳过非法任务：{error}")
        return parsed

    def _normalize(self, task) -> tuple[dict | None, str]:
        """校验并归一化一个任务，返回 ``(任务, 错误信息)``。

        文件加载与暴露出去的 add/set 走同一份规则——两处各写一遍曾经
        造成同一个字段一边报错、一边静默钳制。
        """
        if not isinstance(task, dict):
            return None, "task must be an object"
        name = str(task.get("name") or "").strip()
        if not name:
            return None, "missing task name"
        action = str(task.get("action") or "chat")
        if action not in ("chat", "command"):
            return None, "action must be chat or command"
        text = str(task.get("text") or "").strip()
        if not text:
            return None, "missing text (message body or command)"
        interval = None
        raw_interval = task.get("interval")
        if raw_interval is not None and str(raw_interval) != "":
            try:
                interval = float(raw_interval)
            except (TypeError, ValueError):
                return None, "interval must be a number of seconds"
            interval = max(interval, MIN_INTERVAL)
        time_spec = str(task.get("time") or "").strip()
        if time_spec and not TIME_PATTERN.match(time_spec):
            return None, "time must be HH:MM (24-hour, local)"
        if interval is None and not time_spec:
            return None, "provide interval (seconds) and/or time (HH:MM)"
        return {
            "name": name,
            "interval": interval,
            "time": time_spec or None,
            "action": action,
            "text": text,
            "enabled": bool(task.get("enabled", True)),
        }, ""

    @staticmethod
    def _describe(task: dict) -> str:
        parts = []
        if task.get("interval"):
            parts.append(f"every {float(task['interval']):.0f}s")
        if task.get("time"):
            parts.append(f"daily at {task['time']}")
        schedule = " and ".join(parts) or "no schedule"
        state = "" if task.get("enabled", True) else " (disabled)"
        return f"{task.get('action', 'chat')} {schedule}{state}"


    # ---- 暴露给其他插件 / LLM 的能力 ----

    def _read_tasks(self) -> tuple[list[dict] | None, str]:
        """读回文件里的原始任务列表（保留未知字段，不做归一化）。"""
        path = self._file
        if path is None:
            return None, "Scheduler file is not resolved yet"
        if not path.exists():
            return [], ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, f"Failed to read scheduler.json: {error}"
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return None, "scheduler.json has an invalid format"
        return list(data["tasks"]), ""

    def _write_tasks(self, tasks: list[dict]) -> str:
        path = self._file
        if path is None:
            return "Scheduler file is not resolved yet"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            return f"Failed to write scheduler.json: {error}"
        # 立刻重载：既让改动即时生效，也刷新 mtime（自己写的不算外部改动）
        self._load_tasks()
        return ""

    async def _service_list(self) -> str:
        tasks, error = self._read_tasks()
        if error:
            return error
        if not tasks:
            return "No scheduled tasks"
        return "\n".join(
            f"- {task.get('name')}: {self._describe(task)}: {task.get('text')}"
            for task in tasks
        )

    async def _service_status(self) -> str:
        tasks, error = self._read_tasks()
        if error:
            return error
        enabled = sum(1 for task in tasks if task.get("enabled", True))
        return f"{len(tasks)} scheduled task(s), {enabled} enabled"

    async def _service_add(self, **fields) -> str:
        task, error = self._normalize(fields)
        if task is None:
            return f"Cannot add task: {error}"
        tasks, error = self._read_tasks()
        if error:
            return error
        if any(str(other.get("name")) == task["name"] for other in tasks):
            return f"Task already exists: {task['name']} (use set to modify)"
        tasks.append(task)
        error = self._write_tasks(tasks)
        if error:
            return error
        return f"Scheduled task added: {task['name']} ({self._describe(task)})"

    async def _service_set(self, name: str = "", **fields) -> str:
        name = str(name).strip()
        if not name:
            return "Missing task name"
        unknown = set(fields) - set(TASK_SCHEMA["properties"])
        if unknown:
            return f"Unknown field(s): {', '.join(sorted(unknown))}"
        tasks, error = self._read_tasks()
        if error:
            return error
        for index, task in enumerate(tasks):
            if str(task.get("name")) != name:
                continue
            merged = dict(task)
            merged.update(fields)
            merged["name"] = name
            updated, error = self._normalize(merged)
            if updated is None:
                return f"Cannot update task: {error}"
            tasks[index] = updated
            error = self._write_tasks(tasks)
            if error:
                return error
            return f"Scheduled task updated: {name} ({self._describe(updated)})"
        return f"Task not found: {name}"

    async def _service_remove(self, name: str = "") -> str:
        name = str(name).strip()
        if not name:
            return "Missing task name"
        tasks, error = self._read_tasks()
        if error:
            return error
        remaining = [task for task in tasks if str(task.get("name")) != name]
        if len(remaining) == len(tasks):
            return f"Task not found: {name}"
        error = self._write_tasks(remaining)
        if error:
            return error
        return f"Scheduled task removed: {name}"

    async def _service_run(self, name: str = "") -> str:
        name = str(name).strip()
        if not name:
            return "Missing task name"
        tasks, error = self._read_tasks()
        if error:
            return error
        for raw in tasks:
            if str(raw.get("name")) != name:
                continue
            task, error = self._normalize(raw)
            if task is None:
                return f"Task {name} is not runnable: {error}"
            bot = self.bot
            if bot is None:
                return "Not connected to a server"
            if task["action"] == "command":
                await bot.send_command(task["text"])
            else:
                await bot.send_message(task["text"])
            log.info(f"[定时] 任务 {name}: 手动执行一次。")
            return f"Task {name} executed once"
        return f"Task not found: {name}"

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
