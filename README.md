# ProtoBot

English | [简体中文](README_zh.md)

A modern Python 3.12+ Minecraft protocol client supporting both **offline-mode** and **online-mode** (Mojang / Microsoft authenticated) servers.

ProtoBot implements the vanilla protocol stack directly on asyncio TCP sockets — handshake, login, configuration, and play states — with zero required third-party dependencies for offline use and optional `cryptography` for authenticated encryption. It ships a deterministic client-side physics engine (walking, sprinting, jumping, sneaking, boats, spectator flight), an A\* pathfinder over exact collision shapes, and an event-driven high-level `Bot` API.

## Features

- **Full protocol stack** — handshake → login → configuration → play, keep-alive, teleport confirmation, chunk decoding, and server transfers, all bounds-checked and deterministic.
- **Online & offline mode** — full support for Mojang session-server authenticated login (RSA/AES-CFB8 stream encryption) and Microsoft Device Code OAuth, as well as offline-mode servers.
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

    @bot.on("close")
    async def on_close(reason):
        print("disconnected:", reason)

    await bot.send_command("say hello from ProtoBot")
    await asyncio.sleep(5)
    await bot.close()

asyncio.run(main())
```

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

Or perform interactive Microsoft Device Code login:

```python
import asyncio
from protobot import connect, device_code_login

async def main():
    # Prompts to visit https://www.microsoft.com/link and enter a code
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

Minecraft access tokens last roughly a day. `device_code_login()` also returns a
refresh token, so a stored credential can be renewed without asking the user to
enter another code:

```python
from protobot import refresh_login

if profile.expired and profile.refresh_token:
    profile = await refresh_login(profile.refresh_token)
```

`refresh_login` raises `AuthenticationError` once the refresh token itself is
revoked or expired; fall back to `device_code_login()` at that point. The
bundled `login.py` and `run_bot.py` scripts implement exactly this: authorize
once, then reconnect indefinitely with automatic renewal.

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
| `auth.py` | Mojang session join, RSA/AES-CFB8 encryption, Microsoft OAuth device flow |
| `protocol/` | Wire codec, framing, NBT, connection state machine, version tables |
| `physics/` | Deterministic movement engine, collision geometry, boat physics |
| `navigation.py` | A\* pathfinder |
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
