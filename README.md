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
- **Event bus** — subscribe to chat, chunk, entity, container, health/death, join/leave, and raw packet events.
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
    await bot.fly_to(50.0, 90.0, -30.0)                 # collision-aware 3D flight
    await bot.fly_to(50.0, 10.0, -30.0, vclip=True)     # optional vertical clip

    await bot.close()

asyncio.run(main())
```

`fly_to` uses the original flight physics by default while suppressing
serverbound abilities packets. It temporarily enables only the local physics
state, so `player.abilities` remains the server snapshot. The legacy
`force_flight` and `bypass_permission` flags are accepted for compatibility but
do not perform an abilities check or send an abilities packet.

The optional `no_fall` plugin forces the on-ground bit to `false` on every
outgoing player and vehicle movement packet. It only changes the wire flag;
local physics continues to track real landings. It is enabled by default when
discovered and can be toggled in `plugins/no_fall.json` or with the LLM tools
`no_fall_status` and `no_fall_set`.

Flight navigation is path-quality-first by default. It plans the complete
route on a worker thread before moving, scores turns, vertical changes, and
VClip actions in addition to geometric distance, and compresses clear flight
segments into continuous steering operations. Use `realtime=True` to opt into
rolling planning when navigating beyond the loaded world; its segment length
is controlled by `planning_horizon` (default 32 blocks), and `lookahead=False`
disables background prefetch.

`timeout` bounds the complete flight operation (default 180 seconds), including
waiting for initial world data, planning, lookahead, and movement. Each individual background A*
plan is bounded by `planning_timeout` (default: `min(timeout, 30)` seconds).
When either deadline expires, `NavigationTimeout` is raised and any pending
lookahead task is cancelled and retrieved before flight state is cleaned up.

The client only has the chunks currently sent by the server. The live
`get_status` tool reports the loaded chunk bounds and approximate radius;
`view_distance` and server chunk-send/unload policy determine that radius.

For servers or proxies that kick idle flying players, the anti-kick heartbeat
is enabled by default and can be configured locally:

```yaml
navigation:
  anti_kick: true
  anti_kick_interval: 1.0
```

Packet anti-kick follows Meteor's packet mode: at the configured interval it
rewrites the outgoing movement packet's wire Y to `lastPacketY - 0.03130` while
leaving the local predictor position unchanged. It may reduce idle-flight
kicks, but it cannot grant server flight permission or bypass server-side
movement validation. It does not create an additional heartbeat packet. Flight
navigation also combines heading and position into one `PositionRotation`
packet per physics tick to avoid duplicate movement traffic.

When the server disconnects the client, the reason is rendered from the
disconnect component and logged as `[disconnect] server kick reason=...` along
with a bounded `payload_hex=...` field. Unexpected socket/protocol failures are
logged with their exception type and reason as well.

VClip is enabled by default. Configure vertical clip distance locally in
`config.yaml` (in blocks); the limits apply separately to upward and downward
vertical wall passage:

```yaml
navigation:
  vclip: true
  vclip_up_limit: 3.0
  vclip_down_limit: 2.0
```

Only vertical segments use VClip; horizontal segments continue to require
clearance. `vclip_up_limit` and `vclip_down_limit` are cumulative distances for
one continuous wall passage. Consecutive half-block VClip search nodes in one
vertical passage are merged into a single endpoint action, so execution sends
one clip position packet for that passage instead of one packet per node.

The high-level inventory helpers expose the latest server snapshot and common
player-container actions. Slots `0..8` are the hotbar; `36..44` are the
corresponding player inventory slots.

```python
await bot.switch_hotbar_slot(2)
held = bot.held_item
await bot.click_slot(36, click_type="quick_move")
await bot.drop_item(37, whole_stack=True)
```

The bundled LLM agent exposes these operations as `select_slot`,
`get_inventory`, `inventory_action`, and `close_container` tools.

Navigation paths are node-oriented. Each `PathWaypoint` exposes an
`operation`/`action` value describing the edge from the previous node:
`walk`, `jump`, `fly`, or `vclip`. Use `path.nodes` and `path.operations` when
an integration needs to inspect or execute the route one operation at a time.

### Events

```python
import asyncio
from protobot import connect, plain_text

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

    # Who is online, and when that changes. player_list is the roster the
    # server sends at login, so join/leave really mean someone came or went.
    @bot.on("player_join")
    async def on_join(entry):
        print("joined:", entry.name, "->", bot.online_players)

    @bot.on("player_leave")
    async def on_leave(entry):
        print("left:", entry.name)

    # This bot's own life cycle: death carries the death-message component
    # (None when the signal came from health reaching 0), and the bot stays on
    # the death screen until something calls bot.respawn() -- plugins/respawn.py
    # does that for you.
    @bot.on("death")
    async def on_death(message):
        print("died:", plain_text(message) if message else "?")

    @bot.on("respawn")
    async def on_respawn(session):
        print("respawned in", session.dimension_name)

    await bot.send_message("hello from ProtoBot")
    await bot.send_command("say hello from ProtoBot")
    await asyncio.sleep(5)
    await bot.close()

asyncio.run(main())
```

Chat components carry translation keys rather than sentences, and `plain_text()`
formats them with the built-in `en_us` patterns — `{"translate": "chat.type.text",
"with": ["Steve", "hi"]}` renders as `<Steve> hi`, and the whole vanilla death
message set is included. A server-supplied `fallback` wins over the table; an
unknown key is shown with its arguments appended rather than dropped. Add
server-specific keys with `register_translations({...})`, or point
`load_translations()` at a Mojang `en_us.json`.

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
tui:
  enabled: true             # full-screen interface in a real terminal
  autostart: true           # connect on launch when credentials are ready
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
- **Exposing capabilities** — `self.expose("name", handler, llm=True)`
  publishes a function as `"<plugin>.<name>"`. Other plugins call it with
  `await self.call("fishing.status")`; with `llm=True` it also joins the LLM
  agent's tool list automatically (as `fishing_status`, using the
  `description` and a JSON-Schema `parameters`), and `admin=True` makes the
  agent refuse it for non-admin players. Exposures are withdrawn on disable
  or hot-reload, so a stale instance can never be called, and handler
  exceptions propagate to the caller instead of being swallowed.

### LLM agent plugin (`llm_agent`)

`plugins/llm_agent.py` turns the bot into an in-game LLM agent (Hermes-style):
it keeps an **agent conversation context**, looks up the recent **200 chat
lines through a `read_chat` tool** (filter by players / keyword / system
broadcasts), and acts through OpenAI-compatible function calling — send chat,
run commands, walk/navigate, turn/look, look up player positions, check
status, inspect its own runtime (`get_system_info`: model, context budget in
use, uptime, plugin/task counts), toggle/patch/read/delete plugins, write new
plugins, manage scheduled tasks, start the configured bot session with the
admin-only `start_bot` tool, optionally hand a chat line to a second,
context-free **speaker model**, and maintain **per-server Markdown memory**
(`MEMORY.md`, multiple files allowed) that it updates autonomously.

**On first run** the plugin generates `plugins/llm_agent.json` — fill in the
endpoint and key, then save `llm_agent.py` once to hot-reload the settings:

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",   // any OpenAI-compatible endpoint
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "max_tokens": 1000000,          // model context window
    "compact_reserve_ratio": 0.05,  // reserve 5%; older turns auto-compact
    "system_blocks": true,          // send the system prompt as content blocks
    "cache_control": false          // tag the last block {"type":"ephemeral"}
  },
  "speaker": {                  // optional second model that answers chat lines
    "enabled": false,
    "base_url": "",             // blank = same endpoint as above
    "api_key": "",              // blank = same key
    "model": "",                // blank = same model
    "max_tokens": 300,          // generation limit for one answer
    "temperature": 1.0
  },
  "reply": {
    "all": false,               // true = reply to every chat line
    "name_mention": true,       // reply when chat contains the bot's name
    "prefix": "hey,claude",     // special prefix ("" disables it)
    "keywords": ["claude"],     // extra keyword triggers (case-insensitive)
    "attention_seconds": 15,    // keep listening to a player this long after replying (0 = off, the default)
    "duplicate_window": 10      // ignore the same line from the same player again within this many seconds
  },
  "admins": ["your_name"],      // only admins may write/toggle plugins ([] = anyone)
  "system_prompt": "...",       // optional, overrides the built-in prompt
  "history_limit": 200,         // chat lines kept for the read_chat tool
  "persona_file": "llm_agent_persona.md",
  "skills_dir": "../.claude/skills",
  "memory_dir": "llm_agent_memory",
  "generated_dir": "../plugins_llm",
  "qq": {                       // optional QQ bot bridge (protobot[qq])
    "enabled": false,
    "appid": "",                // from the QQ open platform
    "token": "",
    "sandbox": false,           // true when the bot lives in the sandbox env
    "admin_ids": [],            // QQ openids treated as admins ([] = none)
    "trust_players": []         // MC players whose chat may be relayed to you on QQ
  }
}
```

- **QQ bridge** (`qq`, off by default; needs `pip install protobot[qq]`; the
  portable builds already include it) — the
  same agent answers QQ messages: C2C private chats and group @-mentions reach
  the queue like any other trigger, and the reply goes back through QQ instead
  of Minecraft chat. The agent keeps working even while disconnected from the
  server. Requires an appid/token from the QQ open platform; configure
  `enabled`, `appid` and `token` in `llm_agent.json` (reloads within ~3
  seconds). Requester names look like `[QQ] <openid>`; QQ users are **never**
  admins unless their openid is in `admin_ids` (an unrestricted `admins` list
  does not leak rights to strangers on QQ). The bot also **learns** every
  user/group that messages it (the openid is logged as "QQ contact
  learned"), and exposes `send_qq` (admin-only) plus `qq_contacts` so it
  can reach out proactively -- paste your own openid into `admin_ids`
  and scheduled tasks can ping you on QQ. Players listed in
  `trust_players` may also have their chat relayed to you (`send_qq` with
  `to='owner'`); they are not admins and can reach no one else.
- **A second model for the small talk** (`speaker`, off by default) — turning it
  on adds a `speak` tool that forwards a player's line, **word for word**, to a
  second model and sends back whatever it answers. That model gets **nothing
  else**: the request is one bare user message, with no system prompt, no
  persona, no memory, no chat history, and no tool list. That is the point — it
  is cheap, it never touches the main model's context, and nothing in chat can
  steer it anywhere, because there is nowhere to steer. The cost is that it
  knows nothing about the server, so the main model decides when a line is
  worth handing over: anything needing a looked-up fact, a tool, or knowing who
  is asking gets answered with `send_message` instead. Blank fields inherit the
  main endpoint, key, model, and timeout, so pointing `model` at something small
  is usually the whole configuration; `max_tokens` bounds how long an answer can
  get. A failed `speak` returns an error telling the main model to answer it
  itself, and `send_message` is never routed through the speaker.
  `get_system_info` names the speaking model without naming the endpoint.
- **Talking to it from the console** — `.llm <text>` in the TUI runs one full
  agent turn whose reply prints in the log area and **never goes to game
  chat**; tools still work, so asking it to greet the server makes it call
  `send_message`. The console counts as an **admin** (anyone who can start the
  process can already edit the config), and its identity is an internal marker
  no player name can match, so nobody in chat can impersonate it. With
  `llm_agent` absent or disabled the command says so instead of doing nothing.
- **Prompt caching** — the system prompt is sent as ordered content blocks,
  most stable first: the static prompt, then the skill list, then identity
  (name, server), the character sheet, memory, and the todo list. Nothing
  per-request goes in it; the wall clock rides on the trigger message instead
  (`[HH:MM] <Player>: text`), so the whole system prompt plus the conversation
  history stays a byte-identical prefix from one call to the next, which is
  what endpoints match on when they cache. Set `system_blocks: false` for an
  endpoint that only accepts a plain string, and `cache_control: true` to tag
  the last block `{"type": "ephemeral"}` on endpoints that want an explicit
  cache breakpoint (most do not, hence the default). `get_system_info` reports
  the block count and marker state.
- **Sustained attention** (off by default) — set `attention_seconds` to a
  number and, after the agent actually replies to someone, that player stays
  in an attention window for that long. Their next lines reach the agent even
  without naming it, marked `(follow-up)`, and the agent decides whether the
  line was aimed at it — answering if so, staying silent (`NO_REPLY`) if the
  conversation moved on. A reply refreshes the window; `NO_REPLY` never opens
  one. Note that every line inside the window costs an API call.
- **Character sheet** — `plugins/llm_agent_persona.md` (a template is written
  on first run) is free-form Markdown describing who the bot is: personality,
  backstory, interests, speech habits. It is re-read every time a prompt is
  built, so **saving the file is enough** — no restart, no plugin reload. It
  shapes voice only: it grants no permissions and cannot loosen the trust
  rules.
- **Skills** — the authoritative contract for writing plugins is
  `.claude/skills/protobot-plugin/SKILL.md`, which the agent reads at the time
  with `list_skills` / `read_skill`. The system prompt keeps only the
  irreducible core; the detailed rules are no longer inlined, because the
  inlined copy had already drifted from the framework. `skills_dir` points at
  the directory.
- **Interjections** — writing a plugin takes several tool rounds, and during
  them a new line **from the same player** is folded into the running turn
  (marked `(interjection)`), so they can change their mind or add a
  requirement mid-task. Everyone else keeps their own turn, and their words
  never extend the running turn's permissions.
- **Todo list** — `TODO.md` sits beside the memory files and is a Markdown
  checklist the agent maintains with `todo_add` / `todo_list` / `todo_done` /
  `todo_remove`. Open items are injected into every prompt, so something it
  agreed to do survives a restart; finished ones stop taking up context. Items
  are matched by substring rather than index, and an ambiguous match is
  refused instead of guessed.
- **Duplicate triggers are filtered** — the same line from the same player is
  dropped while an identical trigger is still queued, and again if it was
  handled within `duplicate_window` seconds (10 by default). Players
  double-tapping enter, or several trigger rules matching at once, would
  otherwise each cost an API call.
- **Memory** is stored per server at `llm_agent_memory/<host>_<port>/MEMORY.md`;
  the agent maintains it with the `read_memory` / `save_memory` / `write_memory`
  / `clear_memory` tools, and every conversation includes it.
- **Admin tools** (`write_plugin`, `patch_plugin`, `set_plugin`,
  `remove_plugin`) are restricted to the `admins` list. Generated plugins go to
  the separate `plugins_llm/` directory — never the hand-written `plugins/`
  folder — and are re-registered after restarts. `remove_plugin` closes a plugin
  and deletes its source file for good (`set_plugin` with `enabled: false` is
  the reversible one); it refuses to remove `llm_agent` itself, since that would
  take the agent down with it.
- **Whispers** — system messages of the form `[Player -> me] ...` (private
  messages to the bot) always trigger a reply; the sender is what admin
  checks compare against.
- `llm_agent.json`, the memory directory, and `plugins_llm/` are gitignored:
  the settings file holds an API key, keep it out of version control.

### Scheduled tasks plugin (`scheduler`)

`plugins/scheduler.py` runs chat messages and server commands on a schedule, on
a game event, or when a state condition becomes true. Tasks live in
`plugins/scheduler.json` (generated on first run with one disabled sample):

```json
{
  "tasks": [
    {"name": "evening", "time": "18:00", "action": "chat",
     "text": "Good evening!", "enabled": true},
    {"name": "cleanup", "interval": 1800, "action": "command",
     "text": "say time to clear the drops", "enabled": true},
    {"name": "greet", "event": "player_join", "action": "chat",
     "text": "welcome, {player}!", "cooldown": 5, "enabled": true},
    {"name": "open up", "event": "player_chat", "match": "open the door",
     "action": "command", "text": "say coming", "enabled": true},
    {"name": "low health", "condition": "health < 8", "action": "remind",
     "text": "only {health} health left, do something", "enabled": true}
  ]
}
```

- Four ways to trigger, combinable: `interval` (seconds, minimum 5) repeats
  forever; `time` (`HH:MM`, 24-hour local) fires once a day; `event` fires on a
  game event (`player_chat`, `system_chat`, `player_join`, `player_leave`,
  `death`, `respawn`); `condition` fires on a state expression. At least one is
  required.
- **A `condition` on its own is a trigger** — it runs the task at the moment the
  expression flips from false to true, once, not every second it stays true.
  **Combined with `interval`/`time`/`event` it is a gate** instead: the task
  fires on its own schedule, and a false condition skips that run.
  Conditions are comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`) joined by
  `and` over `health`, `food`, `players` (tab-list size), `entities`, `x`, `y`,
  `z`, `dead`, `hour`, `minute` — for example `players > 4 and dead == false`.
  There is no `or` and no arbitrary code: an expression is parsed, not
  evaluated, and a bad one is rejected when the task is created rather than at
  run time.
- `cooldown` is the minimum number of seconds between two runs of the same task
  — worth setting on a join greeter so ten people arriving at once do not
  produce ten messages. `match` only triggers an event task when the event text
  (chat line, player name, death message) contains that substring, and the two
  chat events **require** it: without it every line anyone types would run the
  task. A task whose own text contains its own `match` is refused, because it
  would trigger itself forever; the bot also ignores its own lines coming back
  from the server (by name, and by anything it said in the last 10 seconds).
- `text` may use placeholders, filled in as the task runs: `{player}`,
  `{message}` (the death message), `{bot}`, `{health}`, `{food}`, `{players}`,
  `{x}`, `{y}`, `{z}`, `{hour}`, `{minute}`. Anything else in braces is left
  alone, so command syntax survives.
- `action` is `chat` (say it), `command` (run it), or `remind` (hand the text to
  the LLM agent and let it decide what to do); `enabled: false` pauses a task.
- The file is re-read within 5 seconds of any change, so edits apply without
  restarting or hot-reloading the plugin. While the bot is disconnected, due
  tasks are postponed rather than dropped.
- **The LLM agent can manage these tasks** — the plugin exposes
  `scheduler.list`, `add`, `set`, `remove`, `run` (fire once now), and
  `status`, which reach the agent as tools automatically; everything except
  `list` and `status` is admin-only. So "every 30 minutes remind people to
  eat" is enough to create a task in game, and "greet anyone who joins",
  "answer whenever someone says open the door", or "tell me when my health
  drops below 8" creates an event or condition task the same way. Validation lives in the plugin, so the file, the services, and the
  agent all obey one set of rules.
- **Waking the agent** — `action: remind` calls the exposed
  `llm_agent.remind`, so the text reaches the LLM as a reminder turn rather
  than being said verbatim: "every hour, check whether anyone needs help" lets
  it decide what to do, or stay quiet. Reminders carry no admin rights, and
  the task is skipped with a notice when no agent is loaded.
- On protocol 774 (1.21.11) the tab-list and combat-death packet ids are
  unverified, so `player_join`, `player_leave`, and `death` never fire there;
  the chat, `system_chat`, and `respawn` triggers work on every version.

### Auto-fishing plugin (`fishing`)

`plugins/fishing.py` casts, detects the bite, and reels, in a loop. Three
signals feed the detector, any one of which reels:

1. **The bite sound** (most accurate — it is what vanilla cues players with).
   `entity.fishing_bobber.splash` arrives on the positional sound packet
   (0x75 = 117 on protocols 775/776) at the **bobber's** coordinates, while
   casting and retrieving play at the **player's**, so "within
   `sound_radius` of the bobber" both recognises the bite and rules out your
   own cast and other people's fishing. Sound ids shift between versions and
   are not sent by the server (`minecraft:sound_event` is a built-in
   registry), so nothing is hardcoded: the first bite recognised by position
   **teaches** the plugin the id, which it then requires. The packet id comes
   from the connected version's table — a version whose id is not verified
   (1.21.11) leaves this path off rather than parsing on a guess. Pin either
   with `sound_id` / `sound_packet_id`.
2. **Downward velocity** — `entity_motion` on the bobber. Vanilla sets the
   hook's Y velocity to `-0.4 × [0.6, 1.0]` at the moment of the bite and
   plays the splash in the same instant, so this signal and the sound are two
   views of one event.
3. **Position drop** — the bobber pulled `bite_drop` below its resting
   baseline.

The bobber is claimed by looking `minecraft:fishing_bobber` up in the
server's `minecraft:entity_type` registry, or else by adopting the first
entity spawned within `spawn_window` seconds of the cast and `spawn_radius`
blocks of the bot — **remembering its type id** so later casts are exact. If
that id turns out to be wrong (a stray item or orb got adopted, or the
registry index does not line up), the next cast that fails to claim anything
adopts its candidate after `spawn_window` and **corrects the id**; without
that, one bad guess would blind the plugin for the rest of the session.

Signals 2 and 3 arm `settle_delay` seconds after the bobber appears (1.2 s by
default — a cast is in the air for about one). The gate is needed because a
downward cast starts with negative velocity too. It is deliberately **not**
"wait until consecutive position updates stop changing": a bobber resting on
the water sends no position updates at all, so that baseline often never
formed, both signals stayed gated off forever, and everything rode on the
sound path. The sound needs no gate, since it only plays on a real bite.

`water_check` (on by default) looks at the two blocks under the bobber once it
has settled and re-casts as soon as they read "not water" five checks in a
row, instead of waiting out `max_wait` with the line on dry land. An unloaded
chunk reads as unknown, never as dry.

`max_wait` seconds without a bite triggers a re-cast, as does the bobber being
removed. `recast_delay` is the only gap between reeling and casting again.

Settings live in `plugins/fishing.json` (generated on first run, re-read
within 5 seconds of an edit) and it starts **`enabled: false`** — flip that to
begin, or just ask the agent: the plugin exposes `fishing.start`,
`fishing.stop`, and `fishing.status`, so "start fishing" in chat works
(start/stop are admin-only). Holding a rod is up to you: the stack exposes no
item names, so the plugin cannot verify what is in the bot's hand.

### Auto-respawn plugin (`respawn`)

`plugins/respawn.py` gets the bot back on its feet after it dies. A dead
player stays on the death screen until the **client** asks to respawn — the
server never does it on its own, not even with `doImmediateRespawn` on (that
gamerule only stops the vanilla client from showing the screen first). Without
this, a dead bot just lies there: no physics, no movement, no chat.

Death detection comes from the core `death` event, which merges two signals
and fires **once** per death:

1. **Combat Death** (0x44 = 68 on protocols 775/776) — the packet whose job is
   to open the death screen, so it is the one the server always sends. It
   carries the death message, which the plugin logs. Filtered on the bot's own
   entity id, so other players dying nearby means nothing.
2. **Health reaching zero** (`set_health`, 0x68 = 104) — a fallback. The server
   sends it at a tick boundary with no ordering guarantee, and server plugins
   can scale health, so it is never the sole basis for a decision.

The respawn request is Client Status (serverbound 0x0C, one VarInt, 0 =
perform respawn), sent by `await bot.respawn()`. Afterwards the server sends
the respawn packet plus a position sync carrying a teleport id; confirming
that teleport and re-sending Player Loaded is handled by the core, since the
respawn handler clears `player.loaded`. The plugin waits for the `respawn`
event as its confirmation and retries `max_retries` times if it never comes.

Settings live in `plugins/respawn.json` (generated on first run, re-read
within 5 seconds of an edit) and it is **on by default**:

```json
{
  "enabled": true,
  "delay": 1.0,
  "retry_delay": 2.0,
  "max_retries": 2,
  "announce": "",
  "return_to_death_point": false,
  "return_max_distance": 200.0
}
```

- `announce` sends that line in chat after respawning; empty says nothing.
- `return_to_death_point` pathfinds back to where the bot died, unless the
  respawn point is further than `return_max_distance` blocks away.
- The plugin exposes `respawn.status`, `respawn.now` (skip the delay) and
  `respawn.set` (admin-only), so "did you die?" or "stop respawning
  automatically" work in chat. `get_status` also reports health, food, and
  whether the bot is currently dead.
- On protocol 774 (1.21.11) these three packet ids are **unverified**, so
  `bot.respawn()` raises `UnsupportedVersion` and the plugin says so once and
  stops rather than sending a guessed packet id.

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
| `.run` | starts the bot |
| `.stop` | stops the bot (keeps the UI) |
| `.plugins` | lists loaded plugins |
| `.llm <text>` | hands the text to the LLM agent; its reply prints in the log area instead of going to chat |
| `.help` | shows available commands |
| ↑ / ↓ | walks the input history (the line you were typing comes back) |
| PageUp / PageDown | scrolls the log; paging up pauses auto-follow so new lines stop yanking the view back, paging to the bottom resumes it |
| Ctrl+L | jumps back to the newest line and resumes following |

- **Scrolling needs the keyboard, not the wheel.** The wheel only works if the
  terminal forwards mouse events, and a multiplexer usually does not: GNU
  `screen` drops them unless `mousetrack on` is set, tmux needs
  `set -g mouse on`, so over SSH inside `screen` the wheel does nothing. The
  terminal's own scrollback cannot reach the log either, because a full-screen
  app runs in the alternate screen buffer. PageUp/PageDown are plain key
  sequences that survive every layer, which is why they are the answer here.
  Submitting anything in the input box also returns you to the newest line.

- **Autostart** — when the configuration is complete enough to connect
  (offline mode always, online mode once `protobot login` has cached a token
  that is valid or refreshable) the session starts by itself, so `.run` is
  only needed after a `.stop` or when credentials are missing. Turn it off
  with `[tui] autostart = false`.
- **Real terminals** (Windows Terminal, VS Code terminal, macOS, Linux) get
  the full-screen UI; **Ctrl+C exits**.
- **PyCharm consoles, pipes, and CI** fall back to plain line logging
  automatically (the bot auto-starts there too); a missing extra prints a
  one-time hint and falls back as well.
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
| `plugin.py` | Plugin framework: discovery, dependency ordering, exception isolation, hot load/reload/close, `expose()` services |
| `settings.py` | Plugin companion files: defaults, deep merge, mtime hot reload, single-key writes |
| `session.py` | `BotSession` reconnect loop and `BotContainer` |
| `text.py` | Chat-component plain-text rendering (`plain_text`) |
| `translations.py` | Built-in `en_us` patterns for translate keys, plus `register_translations` / `load_translations` |
| `config.py` | Dependency-free YAML-subset codec for `config.yaml` |
| `cli_app.py` | Unified CLI: `protobot login|run|plugins|setup` |
| `tui.py` | Textual full-screen TUI (optional `tui` extra) with plain-log fallback |
| `data/` | Bundled per-version block-state tables |
| `cli.py` | Diagnostic console commands |
| `plugins/` | Example plugins (chat_logger, llm_agent, scheduler, fishing, no_fall, respawn) |
| `config.yaml` | Local configuration (generated by the first-launch wizard; not committed) |

## Development

```bash
uv sync --extra online          # install runtime extras plus the dev tools
python -m compileall protobot plugins run_bot.py   # fast syntax check
```

## Building a release

The version lives in one place, `protobot/__init__.py` (`__version__`), and the
CLI echoes it: `protobot --version`. One command builds the whole release:

```bash
uv sync --extra online --extra tui   # once: dev tools incl. PyInstaller
python release.py                    # -> everything below, into dist/
```

What a release ships:

- **`protobot-x.y.z.tar.gz` / `.whl`** -- the pip/uv packages. The wheel
  carries the block-state tables and the bundled example plugins;
  `protobot setup` writes a starter `plugins/` directory next to the config,
  so a pip install gets the plugin system and the same examples.
- **`protobot-x.y.z-<platform>-portable.zip`** (windows-amd64 and
  linux-x86_64) -- the self-contained build: `protobot.exe` / `protobot`
  with its own Python runtime, plus the example plugins and the READMEs.
  Extract it anywhere, run the binary in a terminal -- **nothing to
  install, no Python required**. Launched bare it defaults to
  `protobot run`, so double-clicking starts the bot; the first run walks
  through the setup wizard first.

`release.py packages` / `release.py portable` build just one of the two.
`dist/` is gitignored. The GitHub Actions workflow builds on every `v*` tag
and attaches the artifacts to the GitHub release, so publishing is:

```bash
git tag v1.0.0
git push origin main --tags
```

(create the GitHub release for that tag afterwards; the workflow fills in the
artifacts). Offline mode stays dependency-free in the wheel -- `[online]` and
`[tui]` remain optional extras and are never required to import `protobot`.

## Notes and limitations

- **Online & offline mode.** Offline mode has zero third-party dependencies. For online-mode servers, install `protobot[online]` (requires `cryptography`).
- **Secure chat is supported for online accounts.** ProtoBot fetches the account's ephemeral player certificate, registers a fresh chat session, signs `send_message()` payloads, tracks last-seen acknowledgements, and refreshes the certificate while connected. Offline-mode connections keep the unsigned fallback. `send_command()` continues to use the plain command packet.
- Physics prediction mirrors vanilla 26.2 defaults; servers with heavy movement anti-cheat customisation may still issue corrections.
- `python -m compileall protobot plugins run_bot.py` is the standing sanity check.

## License

MIT. See [LICENSE](LICENSE). You can use, modify, and redistribute ProtoBot
freely, including in closed-source and commercial projects, as long as the
copyright and permission notices stay attached.
