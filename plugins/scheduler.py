"""定时任务插件：按间隔、每天固定时间、游戏事件或状态条件触发。

任务存于本插件同目录的 ``scheduler.json``（首次启用自动生成，示例任务
默认禁用）：

.. code-block:: json

    {
      "tasks": [
        {"name": "整点报时", "time": "18:00", "action": "chat",
         "text": "晚上好！", "enabled": true},
        {"name": "清理提醒", "interval": 1800.0, "action": "command",
         "text": "say 该清理掉落物啦", "enabled": true},
        {"name": "迎新", "event": "player_join", "action": "chat",
         "text": "欢迎 {player}！", "enabled": true},
        {"name": "血量告警", "condition": "health < 8", "action": "remind",
         "text": "血量只剩 {health} 了，想想办法", "enabled": true}
      ]
    }

触发方式四选一（可组合）：``interval``（秒，≥5，循环执行）、``time``
（"HH:MM" 本地时间，每天一次）、``event``（游戏事件，见 ``TRIGGER_EVENTS``）
与 ``condition``（状态条件，见 ``CONDITION_VARS``）。**condition 单独出现时
是触发器**（条件由假变真的那一刻执行一次），**与其他触发方式同时出现时是
开关**（到点/事件发生时条件不成立就跳过）。``cooldown`` 是同一任务两次执行
的最小间隔（秒），``match`` 只在事件内容包含该子串时才触发。

``action`` 为 ``chat``（发聊天）、``command``（执行服务器命令）或 ``remind``
（把内容交给 LLM 智能体，由它决定说什么、做什么）；``enabled`` 为 false 时
暂停。``text`` 里的 ``{player}`` / ``{message}`` / ``{health}`` 等占位符会在
执行前替换成触发上下文与当前状态（未知占位符原样保留）。

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

from protobot import Plugin, log, plain_text

# 小时/分钟必须在合法范围内：只查位数会放过 "25:00" 这类越界时刻。
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

#: interval 下限（秒），防止刷屏
MIN_INTERVAL = 5.0

#: 支持的动作 -> 日志里的说法
ACTIONS: dict[str, str] = {
    "chat": "发送聊天",
    "command": "执行命令",
    "remind": "提醒 LLM",
}

#: 可作为 ``event`` 的核心事件 -> 日志里的说法。这些都是 bot 事件，插件在
#: ``__init__`` 里一次性订阅，任务改动无需重新绑定。
TRIGGER_EVENTS: dict[str, str] = {
    "player_join": "玩家加入",
    "player_leave": "玩家退出",
    "death": "死亡",
    "respawn": "重生",
}

#: 条件里可用的变量 -> 说明（也会写进给 LLM 的参数描述）
CONDITION_VARS: dict[str, str] = {
    "health": "health 0-20",
    "food": "food 0-20",
    "players": "players online in the tab list",
    "entities": "entities visible nearby",
    "x": "x coordinate",
    "y": "y coordinate",
    "z": "z coordinate",
    "dead": "true while on the death screen",
    "hour": "local hour 0-23",
    "minute": "local minute 0-59",
}

#: 条件子句：``变量 运算符 值``，用 " and " 连接多个（不支持 or——拆成两个任务）
CLAUSE_PATTERN = re.compile(r"^([a-z_]+)\s*(<=|>=|==|!=|<|>)\s*(\S+)$")

#: text 里的占位符：只替换已知键，其余原样保留（命令里的花括号不会被吃掉）
PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")

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
        "event": {
            "type": "string",
            "description": (
                "Game event that triggers the task: "
                + ", ".join(TRIGGER_EVENTS)
            ),
        },
        "condition": {
            "type": "string",
            "description": (
                "State condition such as 'health < 8' or 'players > 4 and "
                "dead == false'. On its own it triggers the task the moment it "
                "becomes true; combined with interval/time/event it only gates "
                "them. Variables: "
                + "; ".join(CONDITION_VARS.values())
            ),
        },
        "cooldown": {
            "type": "number",
            "description": "Minimum seconds between two runs of this task",
        },
        "match": {
            "type": "string",
            "description": (
                "Only trigger when the event text (player name, death message) "
                "contains this substring"
            ),
        },
        "action": {
            "type": "string",
            "description": "chat to send a message, command to run a server command, remind to wake the LLM agent with this text",
        },
        "text": {
            "type": "string",
            "description": (
                "Message body or command; {player}, {message}, {health}, "
                "{players}, {x}, {y}, {z} are filled in when it runs"
            ),
        },
        "enabled": {"type": "boolean", "description": "Pause or resume"},
    },
    "required": ["name"],
}

NAME_SCHEMA: dict = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "Task name"}},
    "required": ["name"],
}


def parse_condition(
    text: str,
) -> tuple[tuple[tuple[str, str, float], ...] | None, str]:
    """把 ``"health < 8 and dead == false"`` 解析成子句，或返回错误原因。

    故意不用 ``eval``：任务文件与 LLM 都能写这里的字符串，所以只认「变量
    运算符 数值/true/false」这一种形式，变量名还要在 :data:`CONDITION_VARS`
    里。写错的条件在 add/set 的那一刻就被拒绝，而不是到了执行时才炸。
    """

    clauses: list[tuple[str, str, float]] = []
    for raw in re.split(r"\s+and\s+", text.strip(), flags=re.IGNORECASE):
        clause = raw.strip()
        if not clause:
            return None, "condition has an empty clause"
        if re.search(r"\s+or\s+", clause, flags=re.IGNORECASE):
            return None, "condition does not support 'or' (use two tasks)"
        found = CLAUSE_PATTERN.match(clause)
        if found is None:
            return None, f"cannot parse condition clause {clause!r}"
        variable, operator, value = found.groups()
        if variable not in CONDITION_VARS:
            return None, f"unknown condition variable {variable!r}"
        lowered = value.lower()
        if lowered in ("true", "false"):
            number = 1.0 if lowered == "true" else 0.0
        else:
            try:
                number = float(value)
            except ValueError:
                return None, f"condition value must be a number: {value!r}"
        clauses.append((variable, operator, number))
    if not clauses:
        return None, "condition is empty"
    return tuple(clauses), ""


def compare(left: float, operator: str, right: float) -> bool:
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "!=":
        return left != right
    return left == right


class Scheduler(Plugin):
    name = "scheduler"

    def __init__(self) -> None:
        super().__init__()
        self._file: Path | None = None
        self._tasks: list[dict] = []
        self._next_run: dict[str, float] = {}  # 任务名 -> 下次运行的单调秒
        self._last_run: dict[str, float] = {}  # 任务名 -> 上次执行的单调秒（冷却）
        self._condition_state: dict[str, bool] = {}  # 任务名 -> 上次条件求值
        self._condition_text: dict[str, str] = {}  # 任务名 -> 上次加载时的条件
        self._mtime: float | None = None
        self._loop_task: asyncio.Task | None = None
        self._tick_count = 0
        # 事件触发：一次性订阅全部可用事件，任务表怎么改都不用重新绑定。
        self.subscribe("player_join", self._on_player_join)
        self.subscribe("player_leave", self._on_player_leave)
        self.subscribe("death", self._on_death)
        self.subscribe("respawn", self._on_respawn)
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
                "Add a scheduled task. It can repeat every interval seconds, "
                "run daily at a local HH:MM time, fire on a game event "
                "(player_join, player_leave, death, respawn), and/or fire when "
                "a state condition such as 'health < 8' becomes true. Takes "
                "effect within 5 seconds."
            ),
            parameters=TASK_SCHEMA,
            llm=True,
            admin=True,
        )
        self.expose(
            "set",
            self._service_set,
            description=(
                "Modify a scheduled task: pass any of interval/time/event/"
                "condition/cooldown/match/action/text/enabled to change just "
                "those fields."
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
            self._file = self.data_path("scheduler.json")
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
        names = {task["name"] for task in valid}
        self._last_run = {
            name: at for name, at in self._last_run.items() if name in names
        }
        # 条件的边沿状态也只对还存在的任务保留：改过条件的任务重新求值，
        # 否则「上次为真」会把新条件的第一次上升沿吃掉。
        self._condition_state = {
            task["name"]: self._condition_state[task["name"]]
            for task in valid
            if task["name"] in self._condition_state
            and task["condition"] == self._condition_text.get(task["name"])
        }
        self._condition_text = {
            task["name"]: task["condition"] for task in valid if task["condition"]
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
        if action not in ACTIONS:
            return None, f"action must be one of {', '.join(ACTIONS)}"
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
        event = str(task.get("event") or "").strip()
        if event and event not in TRIGGER_EVENTS:
            return None, f"event must be one of {', '.join(TRIGGER_EVENTS)}"
        condition = str(task.get("condition") or "").strip()
        if condition:
            clauses, error = parse_condition(condition)
            if clauses is None:
                return None, error
        cooldown = 0.0
        raw_cooldown = task.get("cooldown")
        if raw_cooldown is not None and str(raw_cooldown) != "":
            try:
                cooldown = float(raw_cooldown)
            except (TypeError, ValueError):
                return None, "cooldown must be a number of seconds"
            if cooldown < 0.0:
                return None, "cooldown cannot be negative"
        match = str(task.get("match") or "").strip()
        if match and not event:
            return None, "match only applies to event tasks"
        if interval is None and not time_spec and not event and not condition:
            return None, "provide interval, time, event, and/or condition"
        return {
            "name": name,
            "interval": interval,
            "time": time_spec or None,
            "event": event or None,
            "condition": condition or None,
            "cooldown": cooldown,
            "match": match or None,
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
        if task.get("event"):
            parts.append(f"on {task['event']}")
            if task.get("match"):
                parts.append(f"matching {task['match']!r}")
        if task.get("condition"):
            gated = task.get("interval") or task.get("time") or task.get("event")
            parts.append(
                f"only while {task['condition']}" if gated else f"when {task['condition']}"
            )
        schedule = " and ".join(parts) or "no schedule"
        if task.get("cooldown"):
            schedule += f", at most every {float(task['cooldown']):.0f}s"
        state = "" if task.get("enabled", True) else " (disabled)"
        return f"{task.get('action', 'chat')} {schedule}{state}"

    # ---- 条件与占位符 ----

    def _snapshot(self) -> dict[str, float]:
        """条件里可用的变量当前值（bool 也用 0/1 表示，比较起来一致）。"""

        bot = self.bot
        now = time.localtime()
        values = {
            "hour": float(now.tm_hour),
            "minute": float(now.tm_min),
            "health": 0.0,
            "food": 0.0,
            "players": 0.0,
            "entities": 0.0,
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "dead": 0.0,
        }
        if bot is None:
            return values
        player = getattr(bot, "player", None)
        if player is not None:
            values["health"] = float(getattr(player, "health", 0.0))
            values["food"] = float(getattr(player, "food", 0.0))
            values["x"] = float(getattr(player, "x", 0.0))
            values["y"] = float(getattr(player, "y", 0.0))
            values["z"] = float(getattr(player, "z", 0.0))
            values["dead"] = 1.0 if getattr(player, "dead", False) else 0.0
        values["players"] = float(len(getattr(bot, "players", ()) or ()))
        values["entities"] = float(len(getattr(bot, "entities", ()) or ()))
        return values

    def _condition_true(self, condition: str | None) -> bool:
        if not condition:
            return True
        clauses, _ = parse_condition(condition)
        if clauses is None:  # 已经过 _normalize，理论上到不了这里
            return False
        values = self._snapshot()
        for variable, operator, wanted in clauses:
            if not compare(values[variable], operator, wanted):
                return False
        return True

    def _context(self, context: dict[str, str] | None = None) -> dict[str, str]:
        """占位符替换表：当前状态 + 触发上下文（后者优先）。"""

        values = self._snapshot()
        text: dict[str, str] = {
            "health": f"{values['health']:.1f}".rstrip("0").rstrip("."),
            "food": f"{int(values['food'])}",
            "players": f"{int(values['players'])}",
            "entities": f"{int(values['entities'])}",
            "x": f"{values['x']:.1f}",
            "y": f"{values['y']:.1f}",
            "z": f"{values['z']:.1f}",
            "hour": f"{int(values['hour']):02d}",
            "minute": f"{int(values['minute']):02d}",
            "bot": getattr(self.bot, "username", "") or "",
            "player": "",
            "message": "",
            "trigger": "",
        }
        text.update(context or {})
        return text

    @staticmethod
    def _expand(text: str, context: dict[str, str]) -> str:
        return PLACEHOLDER_PATTERN.sub(
            lambda found: context.get(found.group(1), found.group(0)), text
        )

    def _cooldown_ok(self, task: dict, now: float) -> bool:
        cooldown = float(task.get("cooldown") or 0.0)
        if cooldown <= 0.0:
            return True
        last = self._last_run.get(task["name"])
        return last is None or now - last >= cooldown

    @staticmethod
    def _matches(task: dict, context: dict[str, str]) -> bool:
        wanted = task.get("match")
        if not wanted:
            return True
        haystack = " ".join(
            str(context.get(key, "")) for key in ("player", "message")
        ).lower()
        return str(wanted).lower() in haystack

    # ---- 事件触发 ----

    async def _on_player_join(self, entry) -> None:
        await self._fire_event(
            "player_join", {"player": getattr(entry, "name", str(entry))}
        )

    async def _on_player_leave(self, entry) -> None:
        await self._fire_event(
            "player_leave", {"player": getattr(entry, "name", str(entry))}
        )

    async def _on_death(self, message) -> None:
        text = plain_text(message) if message is not None else ""
        await self._fire_event("death", {"message": text})

    async def _on_respawn(self, session) -> None:
        await self._fire_event("respawn", {})

    async def _fire_event(self, event: str, context: dict[str, str]) -> None:
        if self.bot is None:
            return
        now = time.monotonic()
        for task in list(self._tasks):
            if not task["enabled"] or task["event"] != event:
                continue
            if not self._matches(task, context):
                continue
            if not self._condition_true(task["condition"]):
                continue
            if not self._cooldown_ok(task, now):
                continue
            await self._launch(task, {"trigger": event, **context}, now)

    async def _launch(self, task: dict, context: dict[str, str], now: float) -> None:
        """执行一个任务并统一记日志（定时、事件、条件三条路共用）。"""

        name = task["name"]
        self._last_run[name] = now
        try:
            error = await self._perform(task, context)
            if error:
                log.warn(f"[定时] 任务 {name}: {error}")
            else:
                log.info(f"[定时] 任务 {name}: 已{ACTIONS[task['action']]}。")
        except Exception as error:
            log.error(f"[定时] 任务 {name} 执行失败: {error}")


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
        events = sum(1 for task in tasks if task.get("event"))
        conditions = sum(1 for task in tasks if task.get("condition"))
        return (
            f"{len(tasks)} scheduled task(s), {enabled} enabled, "
            f"{events} event-triggered, {conditions} with a condition"
        )

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
            error = await self._perform(task)
            if error:
                return error
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
            if task["event"]:
                continue  # 事件任务由事件回调触发，不参与定时
            if task["interval"] is None and not task["time"]:
                await self._check_condition(task, now)
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
            # 带条件的定时任务：条件是开关，不成立就跳过这一次（照常排下一次）
            if not self._condition_true(task["condition"]):
                continue
            if not self._cooldown_ok(task, now):
                continue
            await self._launch(task, {"trigger": "schedule"}, now)

    async def _check_condition(self, task: dict, now: float) -> None:
        """只有条件的任务：在条件由假变真的那一刻执行一次。

        用上升沿而不是「条件为真就执行」——后者会在血量低的整段时间里每秒
        触发一次。冷却期内被挡掉的上升沿不会补发，得等条件再落回去。
        """

        name = task["name"]
        satisfied = self._condition_true(task["condition"])
        previous = self._condition_state.get(name, False)
        self._condition_state[name] = satisfied
        if not satisfied or previous:
            return
        if not self._cooldown_ok(task, now):
            return
        await self._launch(task, {"trigger": "condition"}, now)

    async def _perform(self, task: dict, context: dict[str, str] | None = None) -> str:
        """执行一个任务；返回空字符串表示成功，否则是给调用方看的原因。

        ``remind`` 不自己发话，而是把内容交给 LLM 智能体去决定怎么处理——
        通过暴露出来的 ``llm_agent.remind``，所以这里不需要知道它的内部，
        没装那个插件时也只是拿到一句「未加载」。
        """
        action = task["action"]
        text = self._expand(task["text"], self._context(context))
        if action == "remind":
            manager = self.manager
            if manager is None or manager.get_service("llm_agent.remind") is None:
                return "LLM 智能体未加载，提醒已跳过"
            result = await manager.call_service(
                "llm_agent.remind", text=text, source=task["name"]
            )
            return "" if "queued" in str(result) else str(result)
        bot = self.bot
        if bot is None:
            return "尚未连接服务器"
        if action == "command":
            await bot.send_command(text)
        else:
            await bot.send_message(text)
        return ""

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
