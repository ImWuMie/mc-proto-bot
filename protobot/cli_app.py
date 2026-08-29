"""The single ProtoBot command line: ``protobot login|run|plugins|setup``.

On first start (no config file yet) an interactive wizard runs: login method
-> server address -> protocol version, and the answers are written to a local
``config.yaml``. The credential cache always lives next to that config file
(``auth_cache.json``), so ``login`` and ``run`` point at the same one from any
working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import (
    MinecraftProfile,
    authorization_code_login,
    device_code_login,
    refresh_login,
)
from .config import load_config, save_config
from .log import info, set_sink
from .plugin import PluginError, PluginManager, PluginWatcher
from . import __version__
from .examples import plugins as _examples_plugins
from .protocol.versions import SUPPORTED_VERSIONS
from .session import BotContainer, BotSession, SessionConfig
from .tui import ProtoBotApp, StdoutProxy, tui_enabled

DEFAULT_CONFIG = Path("config.yaml")

__all__ = [
    "credentials_ready",
    "get_credentials",
    "list_plugins",
    "load_plugin_config",
    "load_profile",
    "load_session_config",
    "load_tui_autostart",
    "load_tui_config",
    "main",
    "run_bot_session",
    "run_login",
    "save_profile",
]


# ======================== Configuration loading ========================


def load_session_config(data: dict) -> SessionConfig:
    """Build a SessionConfig from the config.yaml mapping.

    Invalid values raise ValueError with a message meant for the console.
    """
    server = data.get("server", {})
    login = data.get("login", {})
    session = data.get("session", {})
    if not isinstance(server, dict) or not server.get("host"):
        raise ValueError("the config is missing server.host (the server address)")

    mode = login.get("mode", "online") if isinstance(login, dict) else "online"
    if mode not in ("online", "offline"):
        raise ValueError(f"login.mode must be online or offline, got {mode!r}")
    offline_username = str(login.get("offline_username", "ProtoBot")) \
        if isinstance(login, dict) else "ProtoBot"

    def _get(section: dict, key: str, default):
        return section[key] if isinstance(section, dict) and key in section else default

    return SessionConfig(
        host=str(server["host"]),
        port=int(_get(server, "port", 25565)),
        version=str(_get(server, "version", "26.2")),
        online_mode=mode == "online",
        offline_username=offline_username,
        reconnect=bool(_get(session, "reconnect", True)),
        reconnect_delay=float(_get(session, "reconnect_delay", 5.0)),
        reconnect_max_attempts=_get(session, "reconnect_max_attempts", None),
        connect_timeout=float(_get(session, "connect_timeout", 30.0)),
    )


@dataclass(frozen=True)
class PluginConfig:
    directory: Path
    disabled: tuple[str, ...] = ()
    watch: bool = True


def load_plugin_config(data: dict, base_dir: Path) -> PluginConfig:
    """Read the [plugins] section; relative paths resolve next to the config."""
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}
    directory = Path(str(plugins.get("directory", "plugins")))
    if not directory.is_absolute():
        directory = base_dir / directory
    disabled = plugins.get("disabled", []) or []
    if isinstance(disabled, str):
        disabled = [disabled]
    return PluginConfig(
        directory=directory,
        disabled=tuple(str(name) for name in disabled),
        watch=bool(plugins.get("watch", True)),
    )


def load_tui_config(data: dict) -> bool:
    """Read [tui] enabled from config.yaml (default True)."""
    tui = data.get("tui", {})
    return bool(tui.get("enabled", True)) if isinstance(tui, dict) else True


def load_tui_autostart(data: dict) -> bool:
    """Read [tui] autostart from config.yaml (default True).

    When true and the credentials are ready, the TUI runs .run for you as soon
    as it comes up instead of waiting for it to be typed.
    """
    tui = data.get("tui", {})
    return bool(tui.get("autostart", True)) if isinstance(tui, dict) else True


def credentials_ready(cache_file: Path, session_config: SessionConfig) -> bool:
    """Whether the credentials needed to connect are ready (gates autostart).

    Offline mode always is. Online mode needs a local cache whose token is
    either still valid or refreshable -- otherwise ``get_credentials`` raises
    SystemExit, and autostart should not walk into that.
    """
    if not session_config.online_mode:
        return True
    loaded = load_profile(cache_file)
    if loaded is None:
        return False
    profile, _ = loaded
    return not profile.expired or bool(profile.refresh_token)


# ================= Credential cache (same format as the old login.py) =================


def save_profile(
    cache_file: Path, profile: MinecraftProfile, refresh_options: dict
) -> None:
    cache_file.write_text(
        json.dumps(
            {
                "name": profile.name,
                "uuid": str(profile.id),
                "access_token": profile.access_token,
                "refresh_token": profile.refresh_token,
                "expires_at": profile.expires_at,
                "azure_ad": refresh_options.get("azure_ad", False),
                "client_id": refresh_options.get("client_id"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_profile(cache_file: Path) -> tuple[MinecraftProfile, dict] | None:
    """Read the cached online-mode credentials; None when missing or corrupt.

    Also returns what a refresh needs: a token issued through the device-code
    flow has to be refreshed against the Azure AD endpoints and one issued
    through the authorization-code flow against the MSA endpoints. The two
    cannot be mixed.
    """
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        profile = MinecraftProfile(
            id=uuid.UUID(data["uuid"]),
            name=data["name"],
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0)),
        )
    except (OSError, ValueError, KeyError) as error:
        info(f"[note] the credential cache is corrupt, signing in again ({error})")
        return None

    refresh_options: dict = {}
    if data.get("azure_ad") and data.get("client_id"):
        refresh_options = {"client_id": data["client_id"], "azure_ad": True}
    return profile, refresh_options


async def get_credentials(
    cache_file: Path, *, online_mode: bool, offline_username: str
) -> tuple[str, str | None, uuid.UUID | None]:
    """Get the credentials to connect with (online or offline).

    Online mode reuses the local cache when it can and refreshes an expired
    token with its refresh token. When no refresh is possible it asks for
    ``protobot login`` to be run again.
    """
    if not online_mode:
        return offline_username, None, None

    loaded = load_profile(cache_file)
    if loaded is None:
        raise SystemExit(
            "[error] no credential cache found. Run protobot login once to "
            "authorize your Microsoft account."
        )
    profile, refresh_options = loaded

    if not profile.expired:
        info(f"[auth] using the cached online account: {profile.name}")
        return profile.name, profile.access_token, profile.id

    if not profile.refresh_token:
        raise SystemExit(
            "[error] the cached token expired and there is no refresh token; "
            "run protobot login again."
        )

    info(f"[auth] the cached token expired, refreshing it for {profile.name} ...")
    try:
        profile = await refresh_login(profile.refresh_token, **refresh_options)
    except Exception as error:
        raise SystemExit(
            f"[error] the refresh failed, run protobot login again. Reason: {error}"
        ) from error

    save_profile(cache_file, profile, refresh_options)
    info(f"[auth] refreshed: {profile.name}")
    return profile.name, profile.access_token, profile.id


# ==================== Sign-in prompts (moved from login.py) ====================


def _open_browser(url: str) -> None:
    try:
        if webbrowser.open(url):
            print("    (a browser was opened for you)")
    except Exception:
        pass


def show_device_code(user_code: str, verification_uri: str) -> None:
    print(f"\n[1] Open this in a browser (the code is prefilled):\n\n    {verification_uri}\n")
    _open_browser(verification_uri)
    print(f"[2] Code: {user_code}")
    print("    Type it in yourself if the page did not prefill it.\n")
    print("[3] Click all the way through to the final confirmation page;")
    print("    do not close the window early. This script then continues on")
    print("    its own -- nothing to do back here.\n")
    print("[..] waiting for you to finish in the browser (up to 15 minutes) ...")


def prompt_for_code(url: str) -> str:
    print("\n[1] Open this link in a browser and sign in:\n")
    print(f"    {url}\n")
    _open_browser(url)
    print("[2] Microsoft then shows a warning page along the lines of:")
    print('    "You\'ve reached a page you normally would not see. Microsoft'
          ' will never ask you to copy or share this URL."')
    print("    That is an anti-phishing notice. You are pasting into this")
    print("    script on your own machine; the token does not leave it.\n")
    print("[3] Copy the **whole** address from the browser bar. It looks like:")
    print("    https://login.live.com/oauth20_desktop.srf?code=M.C5xx...&lc=2052\n")
    return input("[4] Paste the redirect address (or just the code) and press Enter: ")


# ======================== First-run configuration wizard ========================


def _parse_address(value: str) -> tuple[str, int]:
    """Parse ``host`` or ``host:port`` (IPv6 literals are not supported)."""
    if ":" in value:
        host, _, port_text = value.rpartition(":")
        if not host:
            raise ValueError("the host name is missing")
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"invalid port: {port_text!r}") from None
    else:
        host, port = value, 25565
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {port}")
    return host, port


def run_setup(config_path: Path) -> int:
    """Interactive wizard: login method -> server -> version, into config.yaml."""
    print("=" * 60)
    print("        ProtoBot first-run setup")
    print("=" * 60)
    print("(press Enter to accept a default; Ctrl+C quits at any point)\n")

    # [1/3] Login method
    while True:
        choice = input(
            "[1/3] Login method: [1] offline  [2] online/Microsoft (default 2): "
        ).strip() or "2"
        if choice == "1":
            mode = "offline"
        elif choice == "2":
            mode = "online"
        else:
            print("    Please enter 1 or 2.")
            continue
        break
    offline_username = "ProtoBot"
    if mode == "offline":
        name = input("[ ] Offline username (default ProtoBot): ").strip() or "ProtoBot"
        offline_username = name[:16] or "ProtoBot"

    # [2/3] Server address
    while True:
        address = input(
            "[2/3] Server address host[:port] (port defaults to 25565): "
        ).strip()
        if not address:
            print("    The address cannot be empty.")
            continue
        try:
            host, port = _parse_address(address)
        except ValueError as error:
            print(f"    {error}")
            continue
        break

    # [3/3] Protocol version
    versions = list(SUPPORTED_VERSIONS)
    print("[3/3] Protocol version:")
    for index, version in enumerate(versions, start=1):
        marker = " (default)" if version == "26.2" else ""
        print(f"    [{index}] {version}{marker}")
    while True:
        choice = input("[ ] Number or version (default 26.2): ").strip() or "26.2"
        if choice.isdigit():
            index = int(choice)
            if 1 <= index <= len(versions):
                version = versions[index - 1]
                break
        if choice in versions:
            version = choice
            break
        print(f"    Please enter 1-{len(versions)} or a version number.")

    data = {
        "server": {"host": host, "port": port, "version": version},
        "login": {"mode": mode, "offline_username": offline_username},
        "session": {
            "reconnect": True,
            "reconnect_delay": 5.0,
            "reconnect_max_attempts": None,
        },
        "plugins": {"directory": "plugins", "disabled": [], "watch": True},
        "tui": {"enabled": True, "autostart": True},
    }
    save_config(config_path, data)
    plugin_count = _write_starter_plugins(config_path)

    print("\n" + "=" * 60)
    print("[done] setup complete")
    print(f"Login: {'online (Microsoft)' if mode == 'online' else 'offline'}")
    print(f"Server: {host}:{port}  Version: {version}")
    print(f"Config written to: {config_path}")
    if plugin_count:
        print(f"Starter plugins written to: {config_path.parent / 'plugins'}")
        print(
            f"  ({plugin_count} example plugin(s); [plugins] directory in the "
            "config decides where they load from)"
        )
    print("=" * 60)
    print("Next:")
    if mode == "online":
        print("  1. run protobot login once to authorize your Microsoft account")
    print("  2. run protobot run (or right-click run_bot.py in PyCharm)")
    return 0


def _write_starter_plugins(config_path: Path) -> int:
    """Copy the bundled example plugins into a starter plugins/ directory.

    Only fills a directory that does not exist yet -- a folder the user already
    created, or one configured elsewhere, is left alone. The examples are the
    same files shipped in the repository's plugins/ directory, so the portable
    zip and a pip install behave identically.
    """
    directory = config_path.parent / "plugins"
    if directory.exists():
        return 0
    source = Path(_examples_plugins.__file__).parent
    copied = 0
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.iterdir()):
            if path.suffix != ".py" or path.name == "__init__.py":
                continue
            (directory / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            copied += 1
    except OSError as error:
        print(f"[note] could not write the starter plugins directory ({error})")
        return 0
    return copied


# ======================== Subcommands ========================


async def run_login(args: argparse.Namespace) -> int:
    print("=" * 60)
    print("      ProtoBot Microsoft account sign-in")
    print("=" * 60)

    if args.auth_code:
        print("[flow] authorization code (you paste the redirect address)")
        profile = await authorization_code_login(prompt_callback=prompt_for_code)
        client_id = None
        azure_ad = False
    elif args.azure_client_id:
        print("[flow] device code (with your own Azure application)")
        profile = await device_code_login(
            args.azure_client_id, prompt_callback=show_device_code
        )
        client_id = args.azure_client_id
        azure_ad = True
    else:
        print("[flow] device code (enter a code, no registration needed)")
        profile = await device_code_login(prompt_callback=show_device_code)
        client_id = None
        azure_ad = False

    cache_file = args.config.with_name("auth_cache.json")
    save_profile(cache_file, profile, {"azure_ad": azure_ad, "client_id": client_id})

    print("\n" + "=" * 60)
    print("[done] signed in")
    print(f"Name: {profile.name}")
    print(f"UUID: {profile.id}")
    print(f"Credentials saved to: {cache_file.name}")
    if profile.refresh_token:
        print("A refresh token was saved, so later connections renew on their own.")
    else:
        print("Note: no refresh token this time; rerun this command once it expires.")
    print("Careful: that file holds an account access token. Do not share or commit it.")
    print("You can now run protobot run (or right-click run_bot.py in PyCharm).")
    print("=" * 60)
    return 0


async def run_bot_session(args: argparse.Namespace) -> int:
    config_path = args.config
    try:
        data = load_config(config_path)
    except OSError:
        print(f"[error] no config file at {config_path}. Pass --config to point elsewhere.")
        return 2
    except ValueError as error:
        print(f"[error] {error}")
        return 2

    try:
        session_config = load_session_config(data)
        plugin_config = load_plugin_config(data, config_path.parent)
    except ValueError as error:
        print(f"[error] {error}")
        return 2

    manager = PluginManager(
        [plugin_config.directory], disabled=plugin_config.disabled
    )
    try:
        manager.discover()
    except PluginError as error:
        print(error)
        return 1
    if manager.load_order():
        print(
            f"[plugin] {len(manager.load_order())} plugin(s) loaded: "
            + ", ".join(plugin.name for plugin in manager.load_order())
        )

    print("=" * 60)
    print("           ProtoBot launcher")
    print("=" * 60)

    cache_file = config_path.with_name("auth_cache.json")
    session = BotSession(
        session_config,
        credentials=lambda: get_credentials(
            cache_file,
            online_mode=session_config.online_mode,
            offline_username=session_config.offline_username,
        ),
        plugin_manager=manager,
    )

    watcher = PluginWatcher(manager) if plugin_config.watch else None
    watcher_task: asyncio.Task | None = None
    try:
        if tui_enabled(load_tui_config(data)):
            # One event loop: the Textual app and the session tasks share it.
            # Textual swallows stdout while it runs (print would vanish), so
            # protobot.log's sink feeds the proxy queue and lands in the log
            # pane. With credentials ready the UI runs .run as it comes up, and
            # leaving it (Ctrl+C) means request_stop plus a graceful shutdown.
            proxy = StdoutProxy()
            ready = credentials_ready(cache_file, session_config)
            autostart = load_tui_autostart(data) and ready
            if not ready:
                print(
                    "[note] no usable online credentials, so the bot will not "
                    "start on its own. Run protobot login, or type .run here."
                )
            app = ProtoBotApp(session, manager, proxy, autostart=autostart)
            await manager.enable_all()
            watcher_task = _start_watcher(watcher)
            set_sink(lambda line: proxy.write(line + "\n"))
            try:
                await app.run_async()
            finally:
                session.request_stop()
                if app.session_task is not None:
                    await app.session_task
                set_sink(None)  # Back to plain print
                await _stop_watcher(watcher, watcher_task)
                watcher_task = None
                await manager.disable_all()
        else:
            container = BotContainer(plugin_manager=manager)
            container.add_session("default", session)
            # The container does enable_all/disable_all itself; the watcher only
            # has to cover the time in between
            watcher_task = _start_watcher(watcher)
            await container.run()
    finally:
        await _stop_watcher(watcher, watcher_task)
    return 0


def _start_watcher(watcher: PluginWatcher | None) -> asyncio.Task | None:
    """Start watching plugin files -- only ever after enable_all.

    Otherwise the watcher can hot-load a plugin before enable_all runs, and
    enable_all then calls on_enable on that same instance a second time: every
    task on_enable started exists twice, while on_disable cancels only the one
    it remembers.
    """
    if watcher is None:
        return None
    task = asyncio.create_task(watcher.run(), name="protobot-plugin-watcher")
    print("[plugin] watching for changes (editing a file in the plugin directory applies it).")
    return task


async def _stop_watcher(
    watcher: PluginWatcher | None, task: asyncio.Task | None
) -> None:
    """Stop the watcher -- before disable_all, or an edit during shutdown
    would enable a plugin again."""
    if watcher is None or task is None:
        return
    watcher.request_stop()
    await task


def list_plugins(args: argparse.Namespace) -> int:
    config_path = args.config
    data: dict = {}
    if config_path.exists():
        try:
            data = load_config(config_path)
        except ValueError as error:
            print(f"[error] {error}")
            return 2
    plugin_config = load_plugin_config(data, config_path.parent)

    manager = PluginManager(
        [plugin_config.directory], disabled=plugin_config.disabled
    )
    try:
        manager.discover()
    except PluginError as error:
        print(error)
        return 1

    if not manager.plugins:
        print("[plugin] no plugins found.")
        return 0

    config_disabled = set(plugin_config.disabled)
    print("[plugin] plugins found:")
    for name in sorted(manager.plugins):
        plugin = manager.plugins[name]
        source = manager.source_of(name)
        if name in config_disabled:
            status = "disabled (config)"
        elif name in manager.disabled_names():
            status = "disabled (dependency)"
        else:
            status = "enabled"
        deps = ", ".join(plugin.dependencies) or "-"
        print(f"  {name:<24s} needs: {deps:<28s} {status}  ({source})")
    order = [plugin.name for plugin in manager.load_order()]
    print(f"[plugin] load order: {' -> '.join(order) if order else '(none)'}")
    return 0


# ======================== Entry point ========================


def _root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protobot", description="ProtoBot command-line tool"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{login,run,plugins,setup}"
    )

    login_parser = sub.add_parser(
        "login", help="authorize a Microsoft account and cache the credentials"
    )
    login_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="config file path (also fixes where the credential cache lives; default config.yaml)",
    )
    login_parser.add_argument(
        "--auth-code",
        action="store_true",
        help="use the authorization-code flow (paste the redirect address)",
    )
    login_parser.add_argument(
        "--azure-client-id",
        default="",
        help="device-code flow with your own Azure application id",
    )

    run_parser = sub.add_parser("run", help="connect to the server and run plugins")
    run_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="config file path (default config.yaml)",
    )

    plugins_parser = sub.add_parser("plugins", help="list the discovered plugins")
    plugins_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="config file path (default config.yaml)",
    )

    setup_parser = sub.add_parser("setup", help="run the interactive setup again")
    setup_parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="config file path (default config.yaml)",
    )
    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "setup":
        return run_setup(args.config)
    if not args.config.exists():
        print("[note] no config file found, starting first-run setup ...\n")
        code = run_setup(args.config)
        if code != 0:
            return code
    if args.command == "login":
        return await run_login(args)
    if args.command == "run":
        return await run_bot_session(args)
    return list_plugins(args)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: ``protobot login|run|plugins|setup``.

    argv defaults to ``sys.argv[1:]``; the run_bot.py shim calls
    ``main(["run", ...])`` directly.
    """
    parser = _root_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("\n[exit] interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
