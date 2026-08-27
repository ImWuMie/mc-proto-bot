# ProtoBot

English | [简体中文](README_zh.md)

A modern Python 3.12+ Minecraft protocol client supporting both **offline-mode** and **online-mode** (Mojang / Microsoft authenticated) servers.

ProtoBot implements the vanilla protocol stack directly on asyncio TCP sockets — handshake, login, configuration, and play states — with zero required third-party dependencies for offline use and optional `cryptography` for authenticated encryption. It ships a deterministic client-side physics engine (walking, sprinting, jumping, sneaking, boats, spectator flight), an A\* pathfinder over exact collision shapes, and an event-driven high-level `Bot` API.

## Features

- **Full protocol stack** — handshake → login → configuration → play, keep-alive, teleport confirmation, chunk decoding, and server transfers, all bounds-checked and deterministic.
- **Online & offline mode** — full support for Mojang session-server authenticated login (RSA/AES-CFB8 stream encryption) and Microsoft OAuth sign-in (authorization-code by default, device-code with your own Azure app), as well as offline-mode servers.
- **SRV records** — `_minecraft._tcp` lookup like a vanilla client, so an address that publishes a backend host and port resolves to it.
- **Multiple releases** — Minecraft `1.21.11`, `26.1`, `26.1.1`, `26.1.2`, and `26.2` out of the box (bundled per-version block-state tables).
- **Client-side physics** — a 20 Hz deterministic physics engine that mirrors vanilla movement, including boats and hard entity collision.
- **Navigation** — A\* path planning and execution over the decoded world with automatic replanning.
- **Mod loader handshakes** — Forge, NeoForge, and Fabric client mod declarations, plus Velocity modern forwarding.
- **Event bus** — subscribe to chat, chunk, entity, container, and raw packet events.
- **Diagnostic CLI** — live regression checks and movement traces against a local server.

## Installation

Requires Python 3.12+.

```bash
# Offline-only (zero third-party dependencies)
python -m pip install -e .

# With online-mode authentication support
python -m pip install -e ".[online]"

# Or with uv
uv sync --extra online
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
revoked or expired; sign in again at that point. The bundled `login.py` and
`run_bot.py` scripts implement exactly this: authorize once, then reconnect
indefinitely with automatic renewal.

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
else means Azure AD); `azure_ad=True/False` overrides the choice. `login.py`
records this in its cache so `run_bot.py` renews correctly.

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
| `data/` | Bundled per-version block-state tables |
| `cli.py` | Diagnostic console commands |

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
- Physics prediction mirrors vanilla 26.2 defaults; servers with heavy movement anti-cheat customisation may still issue corrections.
- `python -m compileall .` is the standing sanity check; unit tests are located under `tests/`.

## License

All rights reserved by the repository owner.
