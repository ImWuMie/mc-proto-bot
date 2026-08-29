"""Scheduled tasks: on an interval, at a daily time, on a game event, or
when a state condition becomes true.

Tasks live in ``scheduler.json`` next to this file (written on first enable,
with one disabled sample):

.. code-block:: json

    {
      "tasks": [
        {"name": "evening", "time": "18:00", "action": "chat",
         "text": "good evening!", "enabled": true},
        {"name": "cleanup", "interval": 1800.0, "action": "command",
         "text": "say time to clear the drops", "enabled": true},
        {"name": "greet", "event": "player_join", "action": "chat",
         "text": "welcome, {player}!", "enabled": true},
        {"name": "open up", "event": "player_chat", "match": "open the door",
         "action": "command", "text": "say coming", "enabled": true},
        {"name": "low health", "condition": "health < 8", "action": "remind",
         "text": "only {health} health left, do something", "enabled": true}
      ]
    }

Four ways to trigger, combinable: ``interval`` (seconds, >= 5, repeating),
``time`` ("HH:MM" local, once a day), ``event`` (a game event, see
``TRIGGER_EVENTS``), and ``condition`` (a state expression, see
``CONDITION_VARS``). **A condition on its own is a trigger** -- it runs the
task the moment the expression flips from false to true -- while **next to
any other trigger it is a gate** instead: a false condition skips that run.
``cooldown`` is the least time between two runs of one task, and ``match``
only triggers when the event text contains that substring -- the chat events
(``player_chat`` / ``system_chat``) require it, or every single line would
run the task.

``action`` is ``chat`` (say it), ``command`` (run it as a server command), or
``remind`` (hand the text to the LLM agent and let it decide what to say or
do); ``enabled: false`` pauses a task. Placeholders in ``text`` such as
``{player}`` / ``{message}`` / ``{health}`` are filled in from the trigger
context and the current state as the task runs (unknown ones are left alone).

The JSON's modification time is checked every 5 seconds, so edits reload on
their own. Tasks can also be maintained by other plugins or the LLM through
the exposed ``scheduler.list`` / ``add`` / ``set`` / ``remove`` / ``run`` /
``status``: those go through this plugin's own validation and take effect as
soon as they are written, so the rules live in exactly one place
(``_normalize``). While disconnected, due tasks are postponed rather than
consuming their slot.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from protobot import Plugin, log, plain_text

# Hours and minutes have to be in range: digit-counting alone would accept
# out-of-range times like "25:00".
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

#: Lower bound for interval (seconds), so a task cannot spam
MIN_INTERVAL = 5.0

#: Supported actions -> how the log describes them
ACTIONS: dict[str, str] = {
    "chat": "sent a chat message",
    "command": "ran a command",
    "remind": "reminded the LLM agent",
}

#: Core events usable as ``event`` -> what they mean. These are all bot
#: events, subscribed once in ``__init__``, so editing tasks never has to
#: rebind anything.
TRIGGER_EVENTS: dict[str, str] = {
    "player_chat": "someone said something",
    "system_chat": "the server broadcast something",
    "player_join": "a player joined",
    "player_leave": "a player left",
    "death": "this bot died",
    "respawn": "this bot respawned",
}

#: Chat events: ``match`` is required, or every line typed runs the task.
CHAT_EVENTS = ("player_chat", "system_chat")

#: How long (seconds) our own lines are remembered, to break the loop where a
#: task's output triggers the task again. The server echo is immediate, so the
#: window is short: a long one would also ignore someone else saying the same.
ECHO_MEMORY = 10.0

#: Variables usable in a condition -> their description (which also goes into
#: the parameter schema the LLM sees)
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

#: A condition clause: ``variable operator value``, joined by " and " (no or --
#: use two tasks for that)
CLAUSE_PATTERN = re.compile(r"^([a-z_]+)\s*(<=|>=|==|!=|<|>)\s*(\S+)$")

#: Placeholders in text: only known keys are replaced, so braces in a command
#: survive untouched
PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")

DEFAULT_TASKS: dict = {
    "tasks": [
        {
            "name": "sample broadcast",
            "interval": 3600.0,
            "action": "chat",
            "text": "sample task: sent once an hour. Edit scheduler.json to change me.",
            "enabled": False,
        }
    ]
}

#: Parameter schemas for the LLM: add/set take every field, remove/run a name.
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
                "Only trigger when the event text (chat line, player name, "
                "death message) contains this substring; required for "
                "player_chat and system_chat events"
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
    """Parse ``"health < 8 and dead == false"`` into clauses, or say why not.

    Deliberately not ``eval``: both the task file and the LLM write these
    strings, so the only accepted shape is ``variable operator number/bool``
    with a variable from :data:`CONDITION_VARS`. A broken condition is rejected
    the moment it is added rather than blowing up when the task fires.
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
        self._next_run: dict[str, float] = {}  # name -> next run (monotonic)
        self._last_run: dict[str, float] = {}  # name -> last run (for cooldown)
        self._condition_state: dict[str, bool] = {}  # name -> last evaluation
        self._condition_text: dict[str, str] = {}  # name -> condition as loaded
        self._mtime: float | None = None
        self._loop_task: asyncio.Task | None = None
        self._tick_count = 0
        # Event triggers: subscribe to every usable event once, so editing the
        # task list never has to rebind anything.
        self._sent: list[tuple[float, str]] = []  # Our own lines (loop guard)
        self.subscribe("player_chat", self._on_player_chat)
        self.subscribe("system_chat", self._on_system_chat)
        self.subscribe("chat_sent", self._on_chat_sent)
        self.subscribe("player_join", self._on_player_join)
        self.subscribe("player_leave", self._on_player_leave)
        self.subscribe("death", self._on_death)
        self.subscribe("respawn", self._on_respawn)
        # Exposed to other plugins and the LLM: list / add / set / remove / run
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
                "(player_chat, system_chat, player_join, player_leave, death, "
                "respawn), and/or fire when a state condition such as "
                "'health < 8' becomes true. Chat events need match set to the "
                "text to look for. Takes effect within 5 seconds."
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

    # ---- Lifecycle ----

    async def on_enable(self) -> None:
        if self._file is None:
            self._file = self.data_path("scheduler.json")
        self._load_tasks()
        self._loop_task = asyncio.create_task(
            self._loop(), name="protobot-scheduler"
        )
        log.info(f"[scheduler] enabled with {len(self._tasks)} task(s).")

    async def on_disable(self) -> None:
        task = self._loop_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        log.info("[scheduler] stopped.")

    # ---- Loading and validation ----

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
                log.info(f"[scheduler] wrote a default task file: {path} (the sample is disabled)")
            except OSError as error:
                log.warn(f"[scheduler] could not write the default task file ({error})")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data.get("tasks") if isinstance(data, dict) else None
        except (OSError, ValueError) as error:
            log.warn(f"[scheduler] could not read the task file, running with none ({error})")
            raw = None
        valid: list[dict] = []
        for task in raw or []:
            parsed = self._validate(task)
            if parsed is not None:
                valid.append(parsed)
        # Keep the timing of tasks that still exist; new ones start next cycle
        self._next_run = {
            task["name"]: self._next_run[task["name"]]
            for task in valid
            if task["name"] in self._next_run
        }
        names = {task["name"] for task in valid}
        self._last_run = {
            name: at for name, at in self._last_run.items() if name in names
        }
        # Edge state survives only for tasks that still exist, and only while
        # their condition is unchanged: otherwise a stale "was true" would eat
        # the first rising edge of the new condition.
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
        log.info(f"[scheduler] loaded {len(valid)} task(s).")

    def _validate(self, task) -> dict | None:
        """The file-loading path: log an invalid task and skip it."""
        parsed, error = self._normalize(task)
        if parsed is None:
            log.warn(f"[scheduler] skipping an invalid task: {error}")
        return parsed

    def _normalize(self, task) -> tuple[dict | None, str]:
        """Validate and normalize one task, returning ``(task, error)``.

        File loading and the exposed add/set share this one set of rules --
        writing them twice once meant the same field was rejected on one path
        and silently clamped on the other.
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
        if event in CHAT_EVENTS and not match:
            return None, (
                "chat events need match: the text to look for (without it every "
                "single chat line would run the task)"
            )
        # A task whose own output matches its own trigger fires forever. That is
        # visible the moment it is created, so there is no reason to find out by
        # being muted for spam.
        if (
            event in CHAT_EVENTS
            and action in ("chat", "command")
            and match.lower() in text.lower()
        ):
            return None, (
                "this task would trigger itself: its text contains the match "
                f"text {match!r}"
            )
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

    # ---- Conditions and placeholders ----

    def _snapshot(self) -> dict[str, float]:
        """Current values for the condition variables (bools as 0/1, so every
        comparison works the same way)."""

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
        if clauses is None:  # Already validated by _normalize; unreachable
            return False
        values = self._snapshot()
        for variable, operator, wanted in clauses:
            if not compare(values[variable], operator, wanted):
                return False
        return True

    def _context(self, context: dict[str, str] | None = None) -> dict[str, str]:
        """Placeholder values: the current state plus the trigger context,
        which wins."""

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

    # ---- Event triggers ----

    async def _on_chat_sent(self, message) -> None:
        """Remember what we (or any plugin) said, to recognise the echo."""

        text = str(message or "").strip()
        if text:
            self._sent.append((time.monotonic(), text))

    def _is_echo(self, text: str) -> bool:
        now = time.monotonic()
        self._sent = [
            (at, sent) for at, sent in self._sent if now - at <= ECHO_MEMORY
        ]
        lowered = text.lower()
        return any(sent.lower() in lowered for _, sent in self._sent)

    async def _on_player_chat(self, sender, name, message, chat_type_id, target) -> None:
        speaker = plain_text(name)
        text = plain_text(message) if message is not None else ""
        bot = self.bot
        if bot is not None and speaker == getattr(bot, "username", None):
            return  # Our own line coming back from the server
        if self._is_echo(text):
            return
        await self._fire_event("player_chat", {"player": speaker, "message": text})

    async def _on_system_chat(self, component, overlay) -> None:
        text = plain_text(component)
        if self._is_echo(text):
            return  # The server echoing a command or a chat line of ours
        await self._fire_event("system_chat", {"message": text})

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
        """Run one task and log it the same way, whichever path triggered it."""

        name = task["name"]
        self._last_run[name] = now
        try:
            error = await self._perform(task, context)
            if error:
                log.warn(f"[scheduler] task {name}: {error}")
            else:
                log.info(f"[scheduler] task {name}: {ACTIONS[task['action']]}.")
        except Exception as error:
            log.error(f"[scheduler] task {name} failed: {error}")


    # ---- Capabilities exposed to other plugins and the LLM ----

    def _read_tasks(self) -> tuple[list[dict] | None, str]:
        """Read the raw task list back (unknown fields kept, no normalizing)."""
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
        # Reload at once: the change takes effect immediately and mtime is
        # refreshed, so our own write is not seen as an external edit
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
            log.info(f"[scheduler] task {name}: run once by hand.")
            return f"Task {name} executed once"
        return f"Task not found: {name}"

    # ---- Main loop ----

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._tick_count += 1
            if self._tick_count % 5 == 0:
                self._maybe_reload()
            await self._run_due()

    def _maybe_reload(self) -> None:
        """Reload after an outside edit to the JSON (an LLM tool, an editor)."""
        path = self._file
        if path is None or not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._mtime is not None and mtime != self._mtime:
            log.info("[scheduler] the task file changed, reloading.")
            self._load_tasks()

    async def _run_due(self) -> None:
        bot = self.bot
        if bot is None:
            return  # Not connected: due tasks are postponed, not consumed
        now = time.monotonic()
        for task in self._tasks:
            if not task["enabled"]:
                continue
            if task["event"]:
                continue  # Event tasks fire from their handler, not the timer
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
            # A timed task with a condition: the condition gates it, so a false
            # one skips this run (the next one is scheduled as usual)
            if not self._condition_true(task["condition"]):
                continue
            if not self._cooldown_ok(task, now):
                continue
            await self._launch(task, {"trigger": "schedule"}, now)

    async def _check_condition(self, task: dict, now: float) -> None:
        """A condition-only task: runs once as the condition becomes true.

        The rising edge, not "run while true" -- the latter would fire every
        second for as long as health stayed low. An edge blocked by the cooldown
        is not replayed; the condition has to go false and true again.
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
        """Run one task; empty string means success, anything else is the reason
        to show the caller.

        ``remind`` does not speak by itself: it hands the text to the LLM agent
        through the exposed ``llm_agent.remind`` and lets the agent decide what
        to do. Nothing here needs to know how that works, and with the plugin
        absent the answer is simply "not loaded".
        """
        action = task["action"]
        text = self._expand(task["text"], self._context(context))
        if action == "remind":
            manager = self.manager
            if manager is None or manager.get_service("llm_agent.remind") is None:
                return "the LLM agent is not loaded, reminder skipped"
            result = await manager.call_service(
                "llm_agent.remind", text=text, source=task["name"]
            )
            return "" if "queued" in str(result) else str(result)
        bot = self.bot
        if bot is None:
            return "not connected to a server"
        if action == "command":
            await bot.send_command(text)
        else:
            await bot.send_message(text)
        return ""

    def _seconds_until(self, spec: str) -> float:
        """Seconds until the next local ``HH:MM`` (tomorrow if it passed)."""
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
