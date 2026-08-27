"""LLM 智能体插件：把大语言模型接入游戏内聊天（类 Hermes Agent）。

功能：
  - LLM 上下文为 agent 对话上下文（系统提示 + 对话轮次，条数可配）；游戏内
    聊天记录最近 N 条（默认 200），通过 read_chat 工具按参数过滤查询
  - 按服务器分开的长期记忆，以 Markdown 文件保存
    （``llm_agent_memory/<host>_<port>/MEMORY.md``，可以有多个 .md 文件），
    LLM 通过 read_memory / save_memory / write_memory / clear_memory 工具
    自主维护
  - 工具调用（OpenAI function-calling 兼容）：发消息、执行命令、直线移动、
    A* 寻路、查看状态、启用/禁用插件、编写新插件（写入独立的 plugins_llm/
    目录并立即热加载）、读写记忆
  - 回复策略可配：只回应提及自己名字、特殊前缀（默认 "hey,claude"）或命中
    关键词列表的消息，或回应每一条聊天
  - 管理员名单（admins）：只有名单内的玩家能让 LLM 写插件 / 开关插件；
    留空表示不限制
  - 设置文件 ``llm_agent.json``（与本插件同目录，首次启用自动生成）：自定义
    API 端点（base_url）、模型、系统提示词、回复策略等

LLM 看到的内容（系统提示词、工具描述、工具返回）均为英文；控制台日志保持
中文 [LLM] 风格。修改 llm_agent.json 后，保存一次本文件（llm_agent.py）
触发热重载即可生效，无需重启。生成目录里的插件由 LLM 维护，与手工编写的
plugins/ 目录分开。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from protobot import Plugin, PluginError, log, plain_text

# ======================== 默认设置 ========================


DEFAULT_SYSTEM_PROMPT = """\
You are an AI agent living inside a Minecraft server. You interact with players through chat and use tools to observe and affect the game world.

Behavior rules:
- Respond in Chinese by default (the players on this server speak Chinese), short and natural, like a fellow player; do not parrot raw tool output.
- When players mention you or talk to you directly, respond like a friend; use tools such as get_status first when you are unsure of the situation.
- Proactively use save_memory (append a note) or write_memory (rewrite the whole file) for anything worth remembering long-term (server rules, player identities, agreements, todos, your goals). Memory is stored per server as MEMORY.md and other Markdown files and is provided to you in every future conversation.
- Each incoming in-game chat message that triggers you appears in this conversation as a user message of the form "<PlayerName>: message".
- The in-game chat stream is NOT part of your context. Use the read_chat tool to look up recent chat (the latest 200 lines are kept; filter by players, keyword, or include_system) whenever you need to know what others said.
- Keep a single chat message under 250 characters. If you decide not to respond, output exactly NO_REPLY and nothing else.
- set_plugin and write_plugin are admin-only (players listed in the admins setting). Calls from non-admins return a permission-denied result; tell them politely and do not retry.

When writing plugins (write_plugin), follow the ProtoBot plugin rules:
1. A plugin is a Plugin subclass with a unique `name`; optional `dependencies = ("other_plugin",)` declares prerequisites.
2. Register bot events in __init__ with self.subscribe("event", handler), and session events with self.subscribe_session("event", handler) (e.g. session_ready). Common events: player_chat(sender_uuid, name, message, chat_type_id, target_name), system_chat(component, overlay).
3. Chat components (message/component) must be converted with plain_text(...).
4. Do not wrap handlers in your own try/except — the framework isolates handler exceptions; they cannot drop the connection.
5. Re-read self.bot on every call (reconnects replace the bot object); it may be None.
6. Only import the standard library and protobot; plugins cannot import each other.
7. Log via protobot.log (log.info/warn/error/debug, call format identical to print) — never print(), the TUI swallows print output.
8. send_message is capped at 256 characters; send_command is unlimited.
9. Tasks created in on_enable must be cancelled in on_disable.
10. Save files as UTF-8; module-level globals do not survive hot reload — persist state to files.
11. Minimal example:

```python
from protobot import Plugin, plain_text

class Hello(Plugin):
    name = "hello"

    def __init__(self):
        super().__init__()
        self.subscribe("player_chat", self._on_player_chat)

    async def _on_player_chat(self, sender, name, message, chat_type_id, target):
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("Hello!")
```
"""


DEFAULT_SETTINGS: dict = {
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 120.0,
        "max_tool_rounds": 5,
    },
    "reply": {
        "all": False,  # true = 回应每一条玩家聊天；false = 仅按下面几种方式触发
        "name_mention": True,  # 聊天内容包含自己名字时触发
        "prefix": "hey,claude",  # 特殊前缀（留空 "" 表示不使用）
        "keywords": [],  # 关键词列表：聊天命中任一关键词即触发（忽略大小写）
    },
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "admins": [],  # 管理员玩家名列表：只有名单内玩家能让 LLM 写插件/开关插件；留空不限制
    "history_limit": 200,  # 游戏内聊天日志保留条数（read_chat 工具查询范围）
    "context_limit": 40,  # agent 对话上下文保留的消息条数
    "memory_dir": "llm_agent_memory",  # 记忆根目录（每服务器一个子目录，记忆为 MEMORY.md 等 Markdown 文件）
    "generated_dir": "../plugins_llm",  # LLM 生成插件的目录（与 plugins/ 分开）
}


# ======================== 工具定义（OpenAI function-calling 格式） ========================


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a chat message (auto-split at 250 chars, up to 4 parts)",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The message to send"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_command",
            "description": "Execute a Minecraft server command (no leading /, e.g. 'say hi'; success must be observed via chat or get_status)",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The server command"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "Check system status: your position, dimension, game mode, world loading, visible entities, and plugin list",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_chat",
            "description": "Read recent in-game chat logs (the latest 200 lines are kept) with optional filters",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to return, default 20 (1-100)",
                    },
                    "players": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only lines from these player names (case-insensitive); omit for all players",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Only lines containing this text (case-insensitive)",
                    },
                    "include_system": {
                        "type": "boolean",
                        "description": "Include server system broadcasts, default false",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Walk in a straight line to X/Z coordinates (30 s timeout, may be blocked by terrain; use navigate_to to go around obstacles)",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Target X coordinate"},
                    "z": {"type": "number", "description": "Target Z coordinate"},
                    "sprint": {"type": "boolean", "description": "Whether to sprint, default false"},
                },
                "required": ["x", "z"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_to",
            "description": "A* pathfind to X/Z coordinates (routes around obstacles, 60 s timeout)",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Target X coordinate"},
                    "z": {"type": "number", "description": "Target Z coordinate"},
                },
                "required": ["x", "z"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_plugin",
            "description": "Enable or disable a plugin by its name (cannot touch llm_agent itself; disabling also closes plugins that depend on it; admin only)",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Plugin name, e.g. chat_logger"},
                    "enabled": {"type": "boolean", "description": "true to enable / false to disable"},
                },
                "required": ["name", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_plugin",
            "description": "Write a new ProtoBot plugin and load it immediately (stored in the separate plugins_llm/ directory; must follow the plugin rules in the system prompt; admin only)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name, e.g. hello.py (letters/digits/underscores only)",
                    },
                    "code": {
                        "type": "string",
                        "description": "Full Python source containing at least one Plugin subclass",
                    },
                },
                "required": ["filename", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read all Markdown memory files (MEMORY.md etc.) of this server",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Append one note to MEMORY.md (long-term memory, provided to you in every future conversation)",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "The note to remember"}
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": "Rewrite MEMORY.md entirely with the given Markdown content (use to restructure or clean up memory)",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The new full MEMORY.md content (Markdown)"}
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_memory",
            "description": "Clear this server's memory (delete all .md files in the memory directory)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ======================== 辅助函数 ========================


def _deep_merge(base: dict, extra: dict) -> dict:
    """递归合并两个字典；extra 覆盖 base，嵌套字典逐层合并。"""
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _http_post_json(
    url: str, payload: dict, headers: dict, timeout: float
) -> dict:
    """同步 POST JSON（在 asyncio.to_thread 中运行，不阻塞事件循环）。

    失败时抛 RuntimeError，错误信息包含 HTTP 状态码与响应片段，供上层
    记录日志；仅依赖标准库 urllib（沿用 auth.py 的模式）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers)
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"网络错误: {error.reason}") from error
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"响应不是合法 JSON: {error}") from error


# ======================== 插件本体 ========================


class LLMAgent(Plugin):
    name = "llm_agent"

    def __init__(self) -> None:
        super().__init__()
        self._settings: dict = copy.deepcopy(DEFAULT_SETTINGS)
        self._settings_file: Path | None = None
        self._memory_dir: Path | None = None
        self._generated_dir: Path | None = None
        self._generated: list[str] = []  # LLM 生成插件的文件名登记
        self._memory_loaded = False
        self._chat_log: list[dict] = []  # 最近 N 条游戏内聊天（read_chat 工具查询）
        self._conversation: list[dict] = []  # agent 对话上下文（system 之外的消息轮次）
        self._queue: asyncio.Queue | None = None
        self._worker_task: asyncio.Task | None = None
        self._requester: str | None = None  # 当前触发聊天的玩家名（权限判定用）
        self._post_json = _http_post_json  # 测试可替换为假实现
        self.subscribe("player_chat", self._on_player_chat)
        self.subscribe("system_chat", self._on_system_chat)
        self.subscribe_session("session_ready", self._on_session_ready)

    # ---- 生命周期 ----

    async def on_enable(self) -> None:
        self._resolve_settings_file()
        self._load_settings()
        self._resolve_dirs()
        api_key = str(self._settings["llm"].get("api_key") or "")
        if not api_key:
            log.warn(
                f"[LLM] 未配置 api_key，将不会回应聊天。请编辑 {self._settings_file} "
                "填写后保存一次本插件文件触发热重载。"
            )
        reply = self._settings["reply"]
        mode = "回应每条聊天" if reply.get("all") else "仅回应名字提及/特殊前缀"
        self._queue = asyncio.Queue(maxsize=16)
        self._worker_task = asyncio.create_task(
            self._worker(), name="protobot-llm-agent-worker"
        )
        log.info(f"[LLM] 智能体插件已启用（回复策略: {mode}）。")

    async def on_disable(self) -> None:
        task = self._worker_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        self._queue = None
        log.info("[LLM] 智能体插件已关闭。")

    def _resolve_settings_file(self) -> None:
        if self._settings_file is not None:
            return
        source = (
            self.manager.source_of(self.name)
            if self.manager is not None
            else None
        )
        base = source.parent if source is not None else Path("plugins")
        self._settings_file = base / "llm_agent.json"

    def _load_settings(self) -> None:
        """读取设置文件；缺失时写出默认设置，损坏时回退默认并警告。"""
        merged = copy.deepcopy(DEFAULT_SETTINGS)
        path = self._settings_file
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = _deep_merge(merged, loaded)
                else:
                    log.warn("[LLM] 设置文件不是 JSON 对象，使用默认设置。")
            except (OSError, ValueError) as error:
                log.warn(f"[LLM] 设置文件读取失败，使用默认设置 ({error})")
        elif path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info(f"[LLM] 已生成默认设置文件: {path}（请填写 api_key）")
            except OSError as error:
                log.warn(f"[LLM] 无法写入默认设置文件 ({error})")
        self._settings = merged
        try:
            limit = int(merged.get("history_limit", 200))
            self._settings["history_limit"] = max(10, min(2000, limit))
        except (TypeError, ValueError):
            self._settings["history_limit"] = 200
        try:
            context = int(merged.get("context_limit", 40))
            self._settings["context_limit"] = max(4, min(200, context))
        except (TypeError, ValueError):
            self._settings["context_limit"] = 40
        if not isinstance(merged.get("reply"), dict):
            self._settings["reply"] = dict(DEFAULT_SETTINGS["reply"])
        self._settings["admins"] = [
            str(admin) for admin in (merged.get("admins") or [])
        ]

    def _resolve_dirs(self) -> None:
        base = self._settings_file.parent
        self._memory_dir = (
            base / str(self._settings.get("memory_dir") or "llm_agent_memory")
        ).resolve()
        self._generated_dir = (
            base / str(self._settings.get("generated_dir") or "../plugins_llm")
        ).resolve()

    # ---- 会话事件：按服务器加载记忆 ----

    async def _on_session_ready(self, bot) -> None:
        if self._memory_loaded:
            return
        self._memory_loaded = True
        self._load_state()
        await self._reload_generated_plugins()

    # ---- 事件处理：记录聊天 + 触发判定 ----

    async def _on_player_chat(
        self, sender_uuid, name, message, chat_type_id, target_name
    ) -> None:
        text = plain_text(message)
        bot = self.bot
        if bot is not None and name == bot.username:
            return  # 自己消息的服务器回显：发送时已记录，且不能自我触发
        self._record_chat(system=False, name=name or "?", text=text)
        if self._should_reply(name, text):
            self._enqueue(name, text)

    async def _on_system_chat(self, component, overlay) -> None:
        text = plain_text(component)
        if text:
            self._record_chat(system=True, name="", text=text)

    def _should_reply(self, name: str, text: str) -> bool:
        """回复策略：reply.all 全回；否则名字提及/特殊前缀/关键词任一命中。"""
        reply = self._settings.get("reply", {})
        if reply.get("all"):
            return True
        lowered = text.lower()
        bot = self.bot
        if bot is not None and reply.get("name_mention", True):
            username = bot.username
            if username and username.lower() in lowered:
                return True
        prefix = str(reply.get("prefix") or "")
        if prefix and lowered.startswith(prefix.lower()):
            return True
        keywords = [str(k).lower() for k in (reply.get("keywords") or [])]
        return any(keyword and keyword in lowered for keyword in keywords)

    def _record_chat(self, *, system: bool, name: str, text: str) -> None:
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "system": system,
            "name": name,
            "text": text,
        }
        self._chat_log.append(entry)
        limit = int(self._settings.get("history_limit", 200))
        while len(self._chat_log) > limit:
            self._chat_log.pop(0)

    def _format_chat_entry(self, entry: dict) -> str:
        if entry["system"]:
            return f"[{entry['time']}] [system] {entry['text']}"
        return f"[{entry['time']}] <{entry['name']}> {entry['text']}"

    def _enqueue(self, name: str, text: str) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait((name, text))
        except asyncio.QueueFull:
            log.warn("[LLM] 待处理队列已满，丢弃一条触发。")

    # ---- 后台任务：串行处理触发 ----

    async def _worker(self) -> None:
        while True:
            name, text = await self._queue.get()
            try:
                await self._handle_trigger(name, text)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # 双保险：队列任务不应拖垮插件
                log.error(f"[LLM] 处理聊天时出错: {error!r}")

    # ---- LLM 调用链 ----

    async def _handle_trigger(self, name: str, text: str) -> None:
        # 记录触发玩家：write_plugin / set_plugin 按 admins 名单做权限判定。
        # worker 串行处理，不会与并发触发交错。
        self._requester = name
        try:
            await self._process_trigger(name, text)
        finally:
            self._requester = None

    def _is_admin(self, name: str | None) -> bool:
        """admins 名单判定；名单为空表示不限制，比较忽略大小写。"""
        admins = self._settings.get("admins") or []
        if not admins:
            return True
        if not name:
            return False
        lowered = [str(admin).lower() for admin in admins]
        return str(name).lower() in lowered

    def _persist_turn(self, turn: list[dict]) -> None:
        """把一轮对话（触发消息 + 助手消息 + 工具消息）并入 agent 上下文。"""
        limit = int(self._settings.get("context_limit", 40))
        self._conversation.extend(turn)
        if len(self._conversation) > limit:
            del self._conversation[:-limit]

    async def _process_trigger(self, name: str, text: str) -> None:
        bot = self.bot
        if bot is None:
            log.info("[LLM] 尚未连接服务器，跳过本轮处理。")
            return
        settings = self._settings["llm"]
        if not str(settings.get("api_key") or ""):
            log.warn("[LLM] 未配置 api_key，跳过处理。")
            return
        context_limit = int(self._settings.get("context_limit", 40))
        context = list(self._conversation[-context_limit:])
        messages = [
            {"role": "system", "content": self._build_system_prompt(bot)}
        ] + context
        prefix_len = len(messages)  # 本轮新增消息（触发 + 助手 + 工具）从这之后开始
        messages.append({"role": "user", "content": f"<{name}>: {text}"})
        rounds = max(1, int(settings.get("max_tool_rounds", 5)))
        for _ in range(rounds):
            try:
                reply = await self._complete_chat(messages)
            except Exception as error:
                log.error(f"[LLM] API 调用失败: {error}")
                return
            if not isinstance(reply, dict):
                log.error(f"[LLM] API 响应异常: {reply!r}")
                return
            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                content = str(reply.get("content") or "").strip()
                messages.append(reply)
                self._persist_turn(messages[prefix_len:])
                if content and content.upper() != "NO_REPLY":
                    try:
                        await self._send_chat(content)
                    except Exception as error:
                        log.error(f"[LLM] 发送回复失败: {error}")
                return
            messages.append(reply)
            for call in tool_calls:
                function = call.get("function") or {}
                tool_name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                result = await self._run_tool(tool_name, arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": str(result),
                    }
                )
        log.warn("[LLM] 工具调用轮数达到上限，放弃本轮。")

    async def _complete_chat(self, messages: list[dict]) -> dict:
        llm = self._settings["llm"]
        url = str(llm.get("base_url") or "").rstrip("/") + "/chat/completions"
        payload = {
            "model": str(llm.get("model") or "gpt-4o-mini"),
            "messages": messages,
            "tools": TOOLS,
        }
        headers = {}
        api_key = str(llm.get("api_key") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = float(llm.get("timeout", 120.0))
        data = await asyncio.to_thread(
            self._post_json, url, payload, headers, timeout
        )
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError(f"响应格式异常: {str(data)[:300]}") from error

    def _build_system_prompt(self, bot) -> str:
        parts = [str(self._settings.get("system_prompt") or "")]
        parts.append(f"\nCurrent time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        session = self.session
        if session is not None:
            config = session.config
            parts.append(f"Server: {config.host}:{config.port}  Version: {config.version}")
        parts.append(f"Your in-game name: {bot.username}")
        parts.append(
            "\n## Long-term memory (this server; maintain it autonomously "
            "with the memory tools)\n" + self._read_memory_text()
        )
        return "\n".join(parts)

    async def _send_chat(self, text: str) -> str:
        """分段发送聊天（250 字/段，最多 4 段）；失败向上抛，由调用方记录。"""
        bot = self.bot
        if bot is None:
            raise RuntimeError("Not connected to a server")
        chunks = [text[i : i + 250] for i in range(0, len(text), 250)]
        if len(chunks) > 4:
            chunks = chunks[:4]
            log.warn("[LLM] 回复过长，只发送前 4 段。")
        for chunk in chunks:
            await bot.send_message(chunk)
            self._record_chat(system=False, name=bot.username, text=chunk)
        return f"Sent {len(chunks)} message(s)"

    # ---- 工具分发 ----

    async def _run_tool(self, name: str, arguments: dict) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"Unknown tool: {name}"
        try:
            return str(await handler(arguments) or "")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return f"Tool {name} failed: {error!r}"

    async def _tool_send_message(self, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "Message content is empty"
        return await self._send_chat(text)

    async def _tool_send_command(self, args: dict) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            return "Command is empty"
        bot = self.bot
        if bot is None:
            return "Not connected to a server"
        await bot.send_command(command)
        return f"Command executed: {command} (observe chat or get_status for the result)"

    async def _tool_get_status(self, args: dict) -> str:
        bot = self.bot
        if bot is None:
            return "Not connected to a server"
        player = bot.player
        lines = [
            f"Position: X={player.x:.1f} Y={player.y:.1f} Z={player.z:.1f}"
            f" (yaw={player.yaw:.1f}, pitch={player.pitch:.1f}, "
            f"{'on ground' if player.on_ground else 'in air'})"
        ]
        session = getattr(bot, "session", None)
        dimension = getattr(session, "dimension_name", None) or "?"
        mode_names = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
        mode = mode_names.get(getattr(session, "game_mode", -1), "?")
        lines.append(f"Dimension: {dimension}  Game mode: {mode}")
        world = getattr(bot, "world", None)
        chunk_count = len(getattr(world, "chunks", ())) if world is not None else "?"
        entity_count = len(getattr(bot, "entities", ()))
        lines.append(f"World: {chunk_count} chunks loaded, {entity_count} entities visible")
        manager = self.manager
        if manager is not None:
            enabled = [plugin.name for plugin in manager.load_order()]
            disabled = [n for n in manager.plugins if n not in enabled]
            lines.append(
                "Plugins: enabled " + (", ".join(enabled) or "-")
                + "; disabled " + (", ".join(disabled) or "-")
            )
        return "\n".join(lines)

    async def _tool_read_chat(self, args: dict) -> str:
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(100, limit))
        players = {
            str(player).lower()
            for player in (args.get("players") or [])
            if str(player).strip()
        }
        keyword = str(args.get("keyword") or "").strip().lower()
        include_system = bool(args.get("include_system", False))
        matched: list[str] = []
        for entry in reversed(self._chat_log):
            if entry["system"]:
                if not include_system or players:  # 按玩家过滤时不含系统广播
                    continue
            elif players and entry["name"].lower() not in players:
                continue
            if keyword and keyword not in entry["text"].lower():
                continue
            matched.append(self._format_chat_entry(entry))
            if len(matched) >= limit:
                break
        if not matched:
            return "No matching chat entries"
        return (
            f"Latest {len(matched)} matching chat line(s), newest last:\n"
            + "\n".join(reversed(matched))
        )

    async def _tool_move_to(self, args: dict) -> str:
        bot = self.bot
        if bot is None:
            return "Not connected to a server"
        try:
            x = float(args.get("x"))
            z = float(args.get("z"))
        except (TypeError, ValueError):
            return "Arguments x/z must be numbers"
        sprint = bool(args.get("sprint", False))
        try:
            await bot.walk_to(x, z, sprint=sprint, timeout=30.0)
        except TimeoutError:
            return "Failed to reach the target within 30 s (possibly blocked by terrain)"
        player = bot.player
        return f"Arrived at X={player.x:.1f} Z={player.z:.1f}"

    async def _tool_navigate_to(self, args: dict) -> str:
        bot = self.bot
        if bot is None:
            return "Not connected to a server"
        try:
            x = float(args.get("x"))
            z = float(args.get("z"))
        except (TypeError, ValueError):
            return "Arguments x/z must be numbers"
        try:
            await bot.navigate_to(x, z, timeout=60.0)
        except TimeoutError:
            return "Failed to reach the target within 60 s"
        player = bot.player
        return f"Arrived at X={player.x:.1f} Z={player.z:.1f}"

    async def _tool_set_plugin(self, args: dict) -> str:
        if not self._is_admin(self._requester):
            return "Permission denied: only admins can manage plugins"
        name = str(args.get("name") or "").strip()
        enabled = bool(args.get("enabled", True))
        if not name:
            return "Missing plugin name"
        if name == self.name:
            return f"Refused: cannot disable {self.name} itself"
        manager = self.manager
        if manager is None:
            return "Plugin manager unavailable"
        source = manager.source_of(name)
        try:
            plugin = await manager.set_enabled(name, enabled)
        except PluginError as error:
            return f"Operation failed: {error}"
        if plugin is None:
            return f"Plugin not found: {name}"
        # 生成目录里的插件被禁用时移出登记（重启不再加载），启用时加回。
        if source is not None and source.parent == self._generated_dir:
            if not enabled and source.name in self._generated:
                self._generated.remove(source.name)
            elif enabled and source.name not in self._generated:
                self._generated.append(source.name)
            self._save_state()
        action = "enabled" if enabled else "disabled"
        extra = "" if enabled else " (its dependents were closed too)"
        return f"Plugin {name} {action}{extra}"

    async def _tool_write_plugin(self, args: dict) -> str:
        if not self._is_admin(self._requester):
            return "Permission denied: only admins can write plugins"
        filename = str(args.get("filename") or "").strip()
        code = str(args.get("code") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}\.py", filename):
            return "Invalid filename: only letters/digits/underscores, ending in .py"
        if not code.strip():
            return "Code must not be empty"
        if self._generated_dir is None:
            return "Generated directory not configured"
        target = self._generated_dir / filename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
        except OSError as error:
            return f"Failed to write file: {error}"
        manager = self.manager
        if manager is None:
            return f"File saved to {target}, but the plugin manager is unavailable"
        loaded_here = any(
            manager.source_of(plugin_name) == target
            for plugin_name in manager.plugins
        )
        try:
            if loaded_here:
                plugins = await manager.hot_reload_file(target)
            else:
                plugins = await manager.hot_load_file(target)
        except PluginError as error:
            return f"Plugin load failed: {error} (file saved to {target}; edit and retry)"
        if not plugins:
            return "File saved, but it contains no Plugin subclass"
        if filename not in self._generated:
            self._generated.append(filename)
        self._save_state()
        names = ", ".join(plugin.name for plugin in plugins)
        action = "reloaded" if loaded_here else "loaded"
        return f"Saved and {action} plugin(s): {names} ({target})"

    # ---- 记忆工具（MEMORY.md 等 Markdown 文件） ----

    async def _tool_read_memory(self, args: dict) -> str:
        files = self._memory_files()
        if not files:
            return "No memory files for this server yet (MEMORY.md does not exist)"
        lines = [f"Memory directory: {self._server_dir()}"]
        for file in files:
            try:
                content = file.read_text(encoding="utf-8")
            except OSError as error:
                lines.append(f"- {file.name}: read failed ({error})")
                continue
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            lines.append(f"--- {file.name} ---\n{content.strip()}")
        return "\n".join(lines)

    async def _tool_save_memory(self, args: dict) -> str:
        note = str(args.get("note") or "").strip()
        if not note:
            return "Memory note is empty"
        directory = self._server_dir()
        if directory is None:
            return "Server info not available yet"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            file = directory / "MEMORY.md"
            if file.exists():
                lines = file.read_text(encoding="utf-8").splitlines()
            else:
                lines = []
            if not lines:
                lines = ["# Server Memory", ""]
            lines.append(f"- {note}")
            file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as error:
            return f"Failed to save memory: {error}"
        count = sum(1 for line in lines if line.strip().startswith("-"))
        return f"Appended to MEMORY.md ({count} note(s) total)"

    async def _tool_write_memory(self, args: dict) -> str:
        content = str(args.get("content") or "").strip()
        directory = self._server_dir()
        if directory is None:
            return "Server info not available yet"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "MEMORY.md").write_text(content + "\n", encoding="utf-8")
        except OSError as error:
            return f"Failed to write memory: {error}"
        return f"Rewrote MEMORY.md ({len(content)} chars)"

    async def _tool_clear_memory(self, args: dict) -> str:
        count = 0
        for file in self._memory_files():
            try:
                file.unlink()
                count += 1
            except OSError:
                pass
        return f"Cleared server memory (deleted {count} file(s))"

    # ---- 记忆文件与生成插件登记 ----

    def _server_dir(self) -> Path | None:
        """本服务器专属记忆目录：<memory_dir>/<host>_<port>/。"""
        if self._memory_dir is None or self.session is None:
            return None
        config = self.session.config
        host = re.sub(r"[^A-Za-z0-9_\-]", "_", config.host)
        return self._memory_dir / f"{host}_{config.port}"

    def _memory_files(self) -> list[Path]:
        """服务器记忆目录里的全部 Markdown 文件（MEMORY.md 排最前）。"""
        directory = self._server_dir()
        if directory is None or not directory.is_dir():
            return []
        files = sorted(directory.glob("*.md"))
        files.sort(key=lambda path: (path.name != "MEMORY.md", path.name))
        return files

    def _read_memory_text(self, limit: int = 8000) -> str:
        """把全部记忆文件拼成给 LLM 看的文本（超长截断）。"""
        files = self._memory_files()
        if not files:
            return "(none yet)"
        sections: list[str] = []
        total = 0
        for file in files:
            try:
                content = file.read_text(encoding="utf-8")
            except OSError:
                continue
            section = f"## Memory file: {file.name}\n{content.strip()}"
            sections.append(section)
            total += len(section)
            if total >= limit:
                break
        text = "\n\n".join(sections)
        if total >= limit:
            text += "\n\n(memory too long, truncated; use read_memory for the full content)"
        return text

    def _state_file(self) -> Path | None:
        """生成插件登记文件（与记忆分开：记忆是 MEMORY.md 等 Markdown）。"""
        if self._generated_dir is None:
            return None
        return self._generated_dir / ".llm_agent_state.json"

    def _load_state(self) -> None:
        self._generated = []
        file = self._state_file()
        if file is None or not file.exists():
            return
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._generated = [
                    str(name)
                    for name in (data.get("generated_plugins") or [])
                    if re.fullmatch(r"[A-Za-z0-9_]{1,64}\.py", str(name))
                ]
            else:
                log.warn("[LLM] 生成插件登记文件格式异常，已重置。")
        except (OSError, ValueError) as error:
            log.warn(f"[LLM] 生成插件登记文件损坏，已重置 ({error})")

    def _save_state(self) -> None:
        file = self._state_file()
        if file is None:
            return
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(
                json.dumps(
                    {"generated_plugins": self._generated},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            log.warn(f"[LLM] 生成插件登记保存失败 ({error})")

    async def _reload_generated_plugins(self) -> None:
        """把登记的生成插件重新热加载（重启/重载后恢复）。"""
        manager = self.manager
        if manager is None or self._generated_dir is None:
            return
        for filename in list(self._generated):
            target = self._generated_dir / filename
            if not target.exists():
                self._generated.remove(filename)
                continue
            loaded_here = any(
                manager.source_of(name) == target for name in manager.plugins
            )
            try:
                if loaded_here:
                    await manager.hot_reload_file(target)
                else:
                    await manager.hot_load_file(target)
                log.info(f"[LLM] 已重新加载生成插件: {filename}")
            except PluginError as error:
                log.error(f"[LLM] 重新加载生成插件 {filename} 失败: {error}")
        self._save_state()
