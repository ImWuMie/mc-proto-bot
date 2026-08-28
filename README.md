# ProtoBot

English | [简体中文](README_zh.md)

A modern Python 3.12+ Minecraft protocol client supporting both **offline-mode** and **online-mode** (Mojang / Microsoft authenticated) servers.

ProtoBot implements the vanilla protocol stack directly on asyncio TCP sockets — handshake, login, configuration, and play states — with zero required third-party dependencies for offline use and optional `cryptography` for authenticated encryption. It ships a deterministic client-side physics engine (walking, sprinting, jumping, sneaking, boats, spectator flight), an A\* pathfinder over exact collision shapes, and an event-driven high-level `Bot` API.

> 🙏 **Credits** — the bot core and protocol foundation build on the work of
> [ImAlexBlock](https://github.com/ImAlexBlock).

## Features

- **Full protocol stack** — handshake → login → configuration → play, keep-alive, teleport confirmation, chunk decoding, and server transfers, all bounds-checked and deterministic.
- **Online & offline mode** — full support for Mojang session-server authenticated login (RSA/AES-CFB8 stream encryption) and Microsoft OAuth sign-in (authorization-code by default, device-code with your own Azure app), as well as offline-mode servers.
- **SRV records** — `_minecraft._tcp` lookup like a vanilla client, so an address that publishes a backend host and port resolves to it.
- **Multiple releases** — Minecraft `1.21.11`, `26.1`, `26.1.1`, `26.1.2`, and `26.2` out of the box (bundled per-version block-state tables).
- **Client-side physics** — a 20 Hz deterministic physics engine that mirrors vanilla movement, including boats and hard entity collision.
- **Navigation** — A\* path planning and execution over the decoded world with automatic replanning.
- **Mod loader handshakes** — Forge, NeoForge, and Fabric client mod declarations, plus Velocity modern forwarding.
- **Event bus** — subscribe to chat, chunk, entity, container, and raw packet events.
- **Plugin system & unified CLI** — plugins auto-discovered from `plugins/` with prerequisite declarations, dependency-ordered loading, exception isolation, and hot load/reload/close; `protobot login|run|plugins|setup` covers sign-in, connecting, and plugin management, with automatic reconnects.
- **Diagnostic CLI** — live regression checks and movement traces against a local server.

## Installation

Requires Python 3.12+.

```bash
# Offline-only (zero third-party dependencies)
python -m pip install -e .

# With online-mode authentication support
python -m pip install -e ".[online]"

# With TUI interface support
python -m pip install -e ".[tui]"

# Or with uv
uv sync --extra online --extra tui
```

## Quick start

The simplest way in is the `connect()` helper, which returns a spawned bot:

```python
import asyncio
from protobot import connect, MovementInput

async def main():
    bot = await connect("127.0.0.1", username="MyBot", version="26.2")
    print(f"logged in as {bot.username} at ({bot.player.x:.1f}, {bot.player.y:.1f}, {bot.player.z:.1f})")

    # Run 40 forward ticks through the physics engine
    for _ in range(40):
        await bot.tick(MovementInput(forward=1.0))
        await asyncio.sleep(0.05)

    await bot.close()

asyncio.run(main())
```

### Walking and pathfinding

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect("127.0.0.1", username="MyBot")

    # Wait for the first decoded chunk, then walk somewhere
    await bot.wait_world()
    await bot.walk_to(10.5, 20.5, sprint=True)          # straight-line walk
    await bot.navigate_to(50.0, -30.0, sprint=True)     # A* route with replanning

    await bot.close()

asyncio.run(main())
```

### Events

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect("127.0.0.1", username="MyBot")

    @bot.on("system_chat")
    async def on_chat(component, overlay):
        print("chat:", component)

    # Player messages arrive separately from server broadcasts: signed messages
    # via the player-chat packet, unsigned ones via the profileless packet.
    # Both are decoded and emitted here.
    @bot.on("player_chat")
    async def on_player_chat(sender_uuid, name, message, chat_type_id, target_name):
        print("player:", name, "says", message)

    @bot.on("close")
    async def on_close(reason):
        print("disconnected:", reason)

    await bot.send_message("hello from ProtoBot")
    await bot.send_command("say hello from ProtoBot")
    await asyncio.sleep(5)
    await bot.close()

asyncio.run(main())
```

### Server addresses and SRV records

Most public servers publish a backend host and port through a `_minecraft._tcp`
SRV record, so what players type is not what they connect to. `connect()` follows
those records the way a vanilla client does — only when no port was given, since
an explicit port always wins:

```python
bot = await connect("play.example.com")                    # follows the SRV record
bot = await connect("play.example.com", port=25565)         # explicit: connects as given
bot = await connect("play.example.com", resolve_srv=False)   # never look up SRV
```

Without this, a server that only answers on its SRV target accepts the TCP
connection at the typed address (or a DNS placeholder) and then closes it, which
surfaces as `ConnectionClosed: server closed the connection`. Subscribe to
`srv_resolved` to see the redirect, or read `bot.connected_host` /
`bot.connected_port` for the address actually dialled:

```python
@bot.on("srv_resolved")
def on_srv(original, host, port):
    print(f"{original} -> {host}:{port}")
```

`resolve_minecraft_srv(host)` is exported if you want the lookup on its own. It
uses the operating system resolver (so split-horizon and VPN DNS apply) and
returns `None` — connect directly — for IP literals, missing records, and
unreachable resolvers.

### Online-mode (Mojang / Microsoft authentication)

Pass an `access_token` and `profile_uuid` directly:

```python
import asyncio
from protobot import connect

async def main():
    bot = await connect(
        "mc.example.com",
        username="PlayerName",
        access_token="<your_minecraft_access_token>",
        profile_uuid="<your_player_uuid>",
        version="26.2",
    )
    print("Online-mode connected:", bot.username, bot.uuid)
    await bot.close()

asyncio.run(main())
```

Or sign in interactively with a Microsoft account. `device_code_login()` is the
default: the user opens a link with the code already filled in and approves in a
browser, with nothing to copy back. It works with the public launcher client ID,
so no Azure application is needed:

```python
import asyncio
from protobot import connect, device_code_login

async def main():
    # Prints a link (code pre-filled) and waits for the browser approval
    profile = await device_code_login()

    bot = await connect(
        "mc.example.com",
        username=profile.name,
        access_token=profile.access_token,
        profile_uuid=profile.id,
        version="26.2",
    )
    print("Logged in as:", bot.username)
    await bot.close()

asyncio.run(main())
```

The sign-in has to be carried through to the final confirmation page. Abandoning
it part-way leaves the device code unauthorized, and Microsoft answers the next
poll with *"the user could not be authenticated or user interaction is
required"* — retry and complete the browser flow.

Minecraft access tokens last roughly a day. The login also returns a refresh
token, so a stored credential can be renewed without asking the user for
anything:

```python
from protobot import refresh_login

if profile.expired and profile.refresh_token:
    profile = await refresh_login(profile.refresh_token)
```

`refresh_login` raises `AuthenticationError` once the refresh token itself is
revoked or expired; sign in again at that point. The command-line tool
implements exactly this (see `protobot login` and `protobot run` in the next
section): authorize once, then reconnect indefinitely with automatic renewal.

### Alternative: authorization-code flow

`authorization_code_login()` also needs no registration. It prints a sign-in URL
and takes back the redirect URL the browser lands on:

```python
profile = await authorization_code_login()                       # prompts on stdin
profile = await authorization_code_login(prompt_callback=my_ui)  # or your own UI
```

Microsoft shows an anti-phishing interstitial on that redirect page — *"You've
reached a page that normally isn't shown. Microsoft will never ask you to copy or
share this URL."* The warning targets scammers who ask victims to forward the URL;
pasting it into a script on your own machine keeps the token local. Loopback
redirects (`http://localhost:...`) would avoid the copy entirely but are rejected
for the public launcher client ID, so prefer the device-code flow above.

### Alternative: device code with your own Azure application

This is the path Microsoft officially supports for the device-code grant. Pass an
Azure AD application ID and the Azure endpoints are used automatically:

```python
profile = await device_code_login("<your-azure-application-id>")
...
profile = await refresh_login(profile.refresh_token, "<your-azure-application-id>")
```

Registering one is free: in the [Azure portal](https://portal.azure.com) go to
*Microsoft Entra ID → App registrations → New registration*, choose "Personal
Microsoft accounts only", then under *Authentication* enable "Allow public client
flows".

A refresh must return to the endpoint family that issued the token. Passing the
same `client_id` selects it automatically (the launcher ID means MSA, anything
else means Azure AD); `azure_ad=True/False` overrides the choice. `protobot
login` records this in its cache so `protobot run` renews correctly.

## Bot CLI and plugin system

Besides the library API, ProtoBot ships one unified command that merges the
former standalone scripts into subcommands:

```bash
protobot login     # Microsoft sign-in (device-code flow; caches credentials)
protobot run       # connect, run plugins, auto-reconnect after disconnects
protobot plugins   # list discovered plugins and their load order
protobot setup     # re-enter the interactive configuration wizard
```

**On first launch** of any subcommand, if there is no local `config.yaml`, an
interactive wizard walks through login mode (offline/online), server address,
and protocol version, then writes the configuration:

```yaml
server:
  host: "wolfx.jp"
  port: 25565
  version: "26.2"
login:
  mode: online              # online | offline
  offline_username: "ProtoBot"
session:
  reconnect: true           # reconnect after disconnects
  reconnect_delay: 5.0      # seconds between attempts (default: every 5 s)
  reconnect_max_attempts: null   # optional cap (null = forever)
plugins:
  directory: "plugins"      # relative to this config file
  disabled: []              # e.g. ["hello_reply"]
  watch: true               # hot load/reload/close on file changes
```

The sign-in cache lives next to the config file (`auth_cache.json`), so `login`
and `run` agree on it from any working directory. The `run_bot.py` at the repo
root is a thin shim for PyCharm's right-click Run — equivalent to
`protobot run`.

### Writing plugins

A plugin is a `Plugin` subclass dropped in `plugins/` (configurable via
`[plugins] directory`). It declares `name` and optional prerequisite plugins
in `dependencies`; the framework loads them in topological order:

```python
from protobot import Plugin, plain_text

class HelloReply(Plugin):
    name = "hello_reply"
    dependencies = ("chat_logger",)   # prerequisite: loaded before this one

    def __init__(self):
        super().__init__()
        # Bot protocol events (exceptions are isolated: never kill the link)
        self.subscribe("player_chat", self._on_player_chat)
        # Session lifecycle events
        self.subscribe_session("session_disconnected", self._on_disconnect)

    async def _on_player_chat(self, sender, name, message, chat_type_id, target):
        if plain_text(message).startswith("hey,claude"):
            await self.bot.send_message("1")
```

Notes:

- **Events** — `subscribe()` registers bot protocol events (same names and
  arguments as `bot.on`: `player_chat`, `system_chat`, `world`, `entity_add`,
  …); `subscribe_session()` registers session lifecycle events
  (`session_start`, `session_connecting`, `session_ready`,
  `session_disconnected`, `session_stop`). A raising handler prints a
  traceback and **cannot** drop the connection.
- **Re-read `self.bot` on every call** — a reconnect spawns a fresh Bot object;
  the framework rebinds subscriptions to it automatically, so a cached
  reference would point at the closed predecessor. `on_bot_ready()` fires once
  per spawned bot and is the place for per-connection state.
- **Lifecycle** — `on_enable()` / `on_disable()` run once per process. Tasks
  created in `on_enable` outlive individual bots; cancel them yourself in
  `on_disable` (the framework never cancels plugin tasks).
- **Hot updates** — with `plugins.watch = true` (default), saving a file under
  `plugins/` hot-reloads it: new files hot-load, modified files hot-reload,
  deleted files hot-close. A broken edit (syntax error, missing dependency) is
  rejected and the **old plugin keeps running** — the online bot is untouched.
- **Config switches** — `[plugins] disabled = ["hello_reply"]` disables a
  plugin; anything depending on it is disabled too, with a notice.

### LLM agent plugin (`llm_agent`)

`plugins/llm_agent.py` turns the bot into an in-game LLM agent (Hermes-style):
it keeps an **agent conversation context**, looks up the recent **200 chat
lines through a `read_chat` tool** (filter by players / keyword / system
broadcasts), and acts through OpenAI-compatible function calling — send chat,
run commands, walk/navigate, turn/look, look up player positions, check
status, toggle/patch/read plugins, write new plugins, manage scheduled tasks,
and maintain **per-server Markdown memory** (`MEMORY.md`, multiple files
allowed) that it updates autonomously.

**On first run** the plugin generates `plugins/llm_agent.json` — fill in the
endpoint and key, then save `llm_agent.py` once to hot-reload the settings:

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",   // any OpenAI-compatible endpoint
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "max_tokens": 1000000,          // model context window
    "compact_reserve_ratio": 0.05   // reserve 5%; older turns auto-compact
  },
  "reply": {
    "all": false,               // true = reply to every chat line
    "name_mention": true,       // reply when chat contains the bot's name
    "prefix": "hey,claude",     // special prefix ("" disables it)
    "keywords": ["claude"]      // extra keyword triggers (case-insensitive)
  },
  "admins": ["your_name"],      // only admins may write/toggle plugins ([] = anyone)
  "system_prompt": "...",       // optional, overrides the built-in prompt
  "history_limit": 200,         // chat lines kept for the read_chat tool
  "memory_dir": "llm_agent_memory",
  "generated_dir": "../plugins_llm"
}
```

- **Memory** is stored per server at `llm_agent_memory/<host>_<port>/MEMORY.md`;
  the agent maintains it with the `read_memory` / `save_memory` / `write_memory`
  / `clear_memory` tools, and every conversation includes it.
- **Admin tools** (`write_plugin`, `set_plugin`) are restricted to the `admins`
  list. Generated plugins go to the separate `plugins_llm/` directory — never
  the hand-written `plugins/` folder — and are re-registered after restarts.
- **Whispers** — system messages of the form `[Player -> me] ...` (private
  messages to the bot) always trigger a reply; the sender is what admin
  checks compare against.
- `llm_agent.json`, the memory directory, and `plugins_llm/` are gitignored:
  the settings file holds an API key, keep it out of version control.

### Scheduled tasks plugin (`scheduler`)

`plugins/scheduler.py` runs chat messages and server commands on a schedule.
Tasks live in `plugins/scheduler.json` (generated on first run with one
disabled sample):

```json
{
  "tasks": [
    {"name": "evening", "time": "18:00", "action": "chat",
     "text": "Good evening!", "enabled": true},
    {"name": "cleanup", "interval": 1800, "action": "command",
     "text": "say time to clear the drops", "enabled": true}
  ]
}
```

- `interval` (seconds, minimum 5) repeats forever; `time` (`HH:MM`, 24-hour
  local) fires once a day — give at least one. `action` is `chat` or
  `command`; `enabled: false` pauses a task.
- The file is re-read within 5 seconds of any change, so edits apply without
  restarting or hot-reloading the plugin. While the bot is disconnected, due
  tasks are postponed rather than dropped.
- **The LLM agent can manage these tasks** through `schedule_list`,
  `schedule_add`, `schedule_set`, `schedule_remove`, and `schedule_run`
  (run once now) — everything except `schedule_list` is admin-only. So
  "every 30 minutes remind people to eat" is enough to create a task in
  game, and "cancel the reminder" removes it.

### Full-screen TUI (optional)

`protobot run` also ships a Claude-Code-style full-screen interface: a
scrolling log area on top (everything the session and plugins print), a
three-column status bar above the input (bot name / coordinates / server ·
version · mode · connection uptime), and a **bottom input row**. It is built
on [Textual](https://github.com/Textualize/textual) as an optional extra, so
the core stays dependency-free:

```bash
uv sync --extra tui              # or pip install -e ".[tui]"
uv run protobot run              # in a real terminal (Windows Terminal etc.)
```

The input accepts three kinds of content, with a live suggestion dropdown
while typing `.`:

| Input | Action |
| --- | --- |
| plain text | sends a chat message |
| `/command` | runs a server command (e.g. `/say hi`) |
| `.run` | starts the bot (the TUI starts with the bot stopped) |
| `.stop` | stops the bot (keeps the UI) |
| `.plugins` | lists loaded plugins |
| `.help` | shows available commands |

- **Real terminals** (Windows Terminal, VS Code terminal, macOS, Linux) get
  the full-screen UI; **Ctrl+C exits**.
- **PyCharm consoles, pipes, and CI** fall back to plain line logging
  automatically (the bot then auto-starts as before, no `.run` needed); a
  missing extra prints a one-time hint and falls back too.
- Config switch: `[tui] enabled = false` turns it off entirely (default `true`).
- **Plugin logging**: while the TUI runs, Textual swallows `print()` output —
  plugins should use `from protobot import log` with `log.info/warn/error/
  debug(...)` (print-style calls), which land in the TUI log area and degrade
  to plain prints outside the TUI.

## Diagnostic CLI

Three console commands are installed with the package:

```bash
# Exercise login, world loading, keep-alive, and optional movement against
# an existing server (or start one from a jar with --jar / --accept-eula)
protobot-live-regression 127.0.0.1 --version 26.2 --movement-ticks 40

# Record a deterministic movement trace (walk, sprint, sneak) to a JSON file
protobot-movement-matrix 127.0.0.1 --output trace.json

# Convert Mojang's reports/blocks.json into ProtoBot's compact block table
protobot-export-block-states reports/blocks.json --output data/blocks-26.2.json.gz --version 26.2
```

## Project layout

| Path | Contents |
| --- | --- |
| `client.py` | `Bot` high-level API and `connect()` |
| `auth.py` | Mojang session join, RSA/AES-CFB8 encryption, Microsoft OAuth sign-in and token refresh |
| `protocol/` | Wire codec, framing, NBT, connection state machine, version tables |
| `physics/` | Deterministic movement engine, collision geometry, boat physics |
| `navigation.py` | A\* pathfinder |
| `srv.py` | Dependency-free `_minecraft._tcp` SRV lookup |
| `world.py` / `state.py` | World/chunk decoding, block-state registry, entity/inventory state |
| `modlist.py` | Forge/NeoForge/Fabric loader adapters, Velocity forwarding |
| `plugin.py` | Plugin framework: discovery, dependency ordering, exception isolation, hot load/reload/close |
| `session.py` | `BotSession` reconnect loop and `BotContainer` |
| `text.py` | Chat-component plain-text rendering (`plain_text`) |
| `config.py` | Dependency-free YAML-subset codec for `config.yaml` |
| `cli_app.py` | Unified CLI: `protobot login|run|plugins|setup` |
| `tui.py` | Textual full-screen TUI (optional `tui` extra) with plain-log fallback |
| `data/` | Bundled per-version block-state tables |
| `cli.py` | Diagnostic console commands |
| `plugins/` | Example plugins (chat_logger, llm_agent, scheduler) |
| `config.yaml` | Local configuration (generated by the first-launch wizard; not committed) |

## Development

```bash
uv sync --extra online          # install runtime extras plus the dev tools
uv run python -m compileall .   # fast syntax check
uv run pytest                   # unit tests under tests/
```

The suite is written against the standard library's `unittest`, so it also runs
with nothing installed beyond the package itself:

```bash
python -m unittest discover -s tests -t .
```

Tests that cover online-mode authentication skip automatically when the
optional `cryptography` extra is not present.

## Notes and limitations

- **Online & offline mode.** Offline mode has zero third-party dependencies. For online-mode servers, install `protobot[online]` (requires `cryptography`).
- **Chat sending is unsigned.** `send_message()` works on servers that do not enforce secure chat (most plugin servers). A server with `enforce-secure-profile=true` will drop or reject the message — signing requires the account's local chat keypair, which a bot holding only an access token cannot access. Commands are unaffected: `send_command()` uses the plain command packet.
- Physics prediction mirrors vanilla 26.2 defaults; servers with heavy movement anti-cheat customisation may still issue corrections.
- `python -m compileall .` is the standing sanity check; unit tests are located under `tests/`.

## License

All rights reserved by the repository owner.
