---
name: protobot-plugin
description: >-
  Guide for authoring ProtoBot plugins: the Plugin base-class contract
  (name/dependencies), event inventory with exact signatures, self.bot turnover
  rules across reconnects, hot-reload semantics, the Bot API available to
  plugins, and writing constraints. Use when the user asks to write, modify, or
  debug a plugin under plugins/, or asks how the plugin system works.
---

# Authoring ProtoBot Plugins

Follow this contract when writing `plugins/*.py` plugins. The authoritative
sources are `protobot/plugin.py`, `protobot/session.py`, `protobot/client.py`,
and `protobot/text.py`.

## Minimal skeleton

```python
from protobot import Plugin, plain_text

class MyPlugin(Plugin):
    name = "my_plugin"                 # required, unique across the repo
    dependencies = ("chat_logger",)    # optional prerequisites, loaded first

    def __init__(self):
        super().__init__()
        self.subscribe("player_chat", self._on_player_chat)      # bot protocol events
        self.subscribe_session("session_ready", self._on_ready)  # session lifecycle events

    async def _on_player_chat(self, sender, name, message, chat_type_id, target):
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("1")
```

## Event inventory (signatures match the emit sites in client.py)

### Session lifecycle events (`subscribe_session`, on the session's own bus)

| Event | Arguments |
| --- | --- |
| `session_start` | () |
| `session_connecting` | (attempt: int) |
| `session_ready` | (bot: Bot) — after connect and plugin binding |
| `session_disconnected` | (reason: str \| None, attempt: int) |
| `session_stop` | () |

### Bot protocol events (`subscribe`, on bot.events — same names/args as `bot.on`)

Chat:
- `system_chat` (component, overlay) — component is decoded NBT (str/dict/list)
- `player_chat` (sender_uuid\|None, name, message, chat_type_id\|None, target_name\|None)
  — sender is None for profileless chat; render with `plain_text()`

World/chunks:
- `world` (WorldSessionState) · `respawn` (WorldSessionState)
- `world_ready` (world) · `chunk` (Chunk) · `chunk_unload` (chunk_x, chunk_z)
- `chunk_batch` (batch_size) · `section_blocks_update` (updates)
- `block_update` (x, y, z, state_id)

Entities:
- `entity_add` (EntityState) · `entity_move` (entity_id, entity\|None)
- `entity_teleport` (entity_id, entity\|None, relative) · `entities_remove` (ids, removed)
- `entity_motion` (entity_id, velocity, entity\|None) · `entity_data` (entity_id, updates, entity\|None)
- `equipment` (entity_id, updates) · `passengers` (vehicle_id, passenger_ids)
- `effect_update` (entity_id, effect_id, identifier, effect) · `effect_remove` (...)

Containers/inventory:
- `inventory` (slot, item) · `container_open` (ContainerState)
- `container_content` (ContainerState) · `container_slot` (ContainerState, slot)
- `container_close` (ContainerState)

State/misc:
- `position` (PlayerState) · `abilities` (PlayerAbilities) · `game_mode` (int)
- `game_event` (event_id, value) · `attributes` (entity_id, updates)
- `login` (bot) · `ready` (bot) · `reconfiguration` (bot) · `transfer` (host, port)
- `error` (BaseException) · `close` (reason: str\|None)
- `packet` (RawPacket) · `packet:{state}:{id}` (RawPacket) — raw fallback
- `path` (NavigationPath, attempt) · `gliding_collision` (damage)

`login_plugin_request` / `cookie_request` / `configuration_payload` /
`mod_payload` / `play_payload` / `registry` are configuration-phase and
mod-loader events; ordinary plugins do not need them.

## Hard rules (each one prevents a real bug)

1. **Handler exceptions are already isolated**: `subscribe`/`subscribe_session`
   wrap every handler; an exception only prints `[插件] <name> 处理事件时出错`
   plus a traceback and **cannot** drop the connection. Do not swallow
   exceptions or add your own try/except inside handlers — it hides problems.
2. **Re-read `self.bot` on every call**: a reconnect spawns a fresh Bot object
   and the framework rebinds subscriptions to it automatically; a cached
   reference points at the closed predecessor. `self.bot` is None during
   backoff gaps. Put per-connection state in `on_bot_ready()`, which fires once
   per spawned bot after binding.
3. **Own the tasks you create in `on_enable`**: they outlive individual bots;
   cancel them in `on_disable` (the framework never cancels plugin tasks).
   `on_enable` / `on_disable` run once per process each.
4. **Hot reload means a fresh instance**: saving a file hot-reloads it
   (`[plugins] watch`, on by default) — the old instance's `on_disable` and the
   new instance's `on_enable` both run again; deleting a file hot-closes it
   (dependents close too). **Module-level globals do not survive a reload**
   (each import gets a fresh module name) — persist state to a file instead.
   A reload that fails (syntax error, missing dependency) is rejected and the
   old plugin keeps running.
5. **Dependencies reference names only**: the framework orders plugins with a
   Kahn topological sort (deterministic, name-ordered); cycles and missing
   dependencies are rejected at load. Disabling a plugin via
   `[plugins] disabled` also disables its dependents, with a notice.
6. **Zero third-party dependencies**: plugins may import only the stdlib and
   `protobot`; plugin files cannot import each other (the plugin directory is
   not on sys.path). Keep files UTF-8 with Chinese comments and Chinese console
   output in the existing `[标签]` style. **Log via `protobot.log`, not
   `print()`**: while the TUI runs, Textual captures stdout and plain prints
   are lost — `from protobot import log; log.info("[聊天]", text)` routes to
   the TUI log area (and falls back to print outside the TUI). `warn` /
   `error` / `debug` add `[警告]` / `[错误]` / `[调试]` prefixes; call
   signatures match `print` (positional args, `sep`, `end`).
7. **Chat-sending limits**: `send_message()` is capped at 256 characters and is
   unsigned (dropped by enforce-secure-profile servers); `send_command()` is
   unaffected.

## Bot API available to plugins (public)

- Logging: `from protobot import log` → `log.info(*args, sep=" ", end="\n")`,
  `log.warn(...)`, `log.error(...)`, `log.debug(...)` — print-style calls that
  reach the TUI log area (plain `print()` output is swallowed by the TUI)
- Chat: `await self.bot.send_message(text)` / `await self.bot.send_command(cmd)` (leading `/` stripped)
- Movement: `await self.bot.tick(MovementInput())` (one 20 Hz physics tick),
  `walk_to(x, z, sprint=False)`, `navigate_to(x, z, sprint=False)` (A*),
  `set_flying(flag)`, `start_gliding()`
- Interaction: `click_container(slot, ...)`, `close_container()`, `use_item()`,
  `select_hotbar_slot(slot)`
- State: `bot.player` (PlayerState: x/y/z, health, yaw/pitch), `bot.world`
  (chunks), `bot.entities`, `bot.containers`, `bot.session`, `bot.username`,
  `bot.uuid`, `bot.closed` (asyncio.Event), `bot.disconnect_reason`
- Manager: `self.manager` (PluginManager, bound while the plugin is enabled):
  `load_order()`, `plugins` (name → Plugin), `source_of(name)`,
  `set_enabled(name, bool)` (runtime toggle; disables dependents too, keeps
  the source so it can be re-enabled), `hot_load_file(path)`,
  `hot_reload_file(path)`, `hot_close(name)` — a plugin can list, toggle, or
  hot-load other plugins (see plugins/llm_agent.py for a full example)
- Text: `from protobot.text import plain_text` (str/dict/list component →
  plain text; handles translate+fallback and the empty-key `{'': '123'}`
  server-plugin quirk)

## Pre-delivery checklist

- [ ] `name` is non-empty and unique; `dependencies` lists only real plugin names, no cycles
- [ ] Subscriptions happen in `__init__` (or `on_enable`); event names and signatures match the tables above
- [ ] Handlers guard against `self.bot` being None, or only touch it after `on_bot_ready`
- [ ] No third-party imports, no cross-plugin imports
- [ ] Any long-lived task is cancelled in `on_disable`
- [ ] `uv run pytest tests/test_plugin.py` passes; new behavior has a regression
  test (see the temp-dir + FakeBot pattern in tests/test_plugin.py)
