"""Command-line entry points shipped with ProtoBot.

The project keeps the small diagnostic programs in :mod:`tools` so that they
are easy to read from a source checkout.  This module contains the same
entrypoints inside the package, which means an installed wheel does not need
the repository's ``tools/`` directory or a manually configured ``PYTHONPATH``.

The commands are intentionally diagnostic rather than a second high-level API:

``protobot-export-block-states``
    Convert Mojang's ``reports/blocks.json`` into ProtoBot's compact table.
``protobot-movement-matrix``
    Connect to an already-running offline server and write a deterministic
    movement trace.
``protobot-live-regression``
    Run the same check against an optional local server jar, taking care of
    startup, readiness, and shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import struct
import subprocess
import time
import zipfile
from contextlib import closing
from pathlib import Path

from .client import connect
from .physics import MovementInput
from .world import BlockStateRegistry


def _movement_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protobot-movement-matrix",
        description="Run a deterministic movement trace against an offline server.",
    )
    parser.add_argument("host")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.2")
    parser.add_argument("--username", default="ProtoBotMatrix")
    parser.add_argument("--walk-ticks", type=int, default=20)
    parser.add_argument("--sprint-ticks", type=int, default=20)
    parser.add_argument("--sneak-ticks", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--grim-db",
        type=Path,
        help="GrimAC history.v1.db to correlate violations with this run",
    )
    parser.add_argument(
        "--grim-wait",
        type=float,
        default=2.0,
        help="seconds to wait for GrimAC to close and flush the player session",
    )
    parser.add_argument(
        "--require-grim-clean",
        action="store_true",
        help="exit unsuccessfully when GrimAC records a violation or no matching session",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _read_grim_audit(
    database: Path,
    username: str,
    started_after_ms: int,
) -> dict[str, object]:
    """Read the newest matching GrimAC v2 history session without mutating it."""

    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"GrimAC history database not found: {database}")
    uri = f"file:{database.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
        session = connection.execute(
            """
            SELECT hex(s.session_id), s.started_at, s.closed_at,
                   s.client_version_pvn, s.server_version, s.grim_version
              FROM grim_sessions AS s
              JOIN grim_players AS p ON p.uuid = s.player_uuid
             WHERE p.current_name = ? AND s.started_at >= ?
             ORDER BY s.started_at DESC
             LIMIT 1
            """,
            (username, started_after_ms),
        ).fetchone()
        if session is None:
            return {
                "database": str(database),
                "session_found": False,
                "clean": False,
                "violation_count": 0,
                "violations": [],
            }

        session_id, started_at, closed_at, client_protocol, server_version, grim_version = session
        rows = connection.execute(
            """
            SELECT v.occurred_at, c.stable_key, c.display, v.vl, v.verbose
              FROM grim_violations AS v
              JOIN grim_checks AS c ON c.check_id = v.check_id
             WHERE hex(v.session_id) = ?
             ORDER BY v.occurred_at, v.vl
            """,
            (session_id,),
        ).fetchall()

    violations: list[dict[str, object]] = []
    for occurred_at, stable_key, display, violation_level, verbose in rows:
        encoded = bytes(verbose or b"")
        violation: dict[str, object] = {
            "occurred_at_ms": occurred_at,
            "session_offset_ms": occurred_at - started_at,
            "check": stable_key,
            "display": display,
            "violation_level": violation_level,
            "verbose_hex": encoded.hex(),
        }
        if stable_key == "grim.prediction.simulation" and len(encoded) == 8:
            violation["offset"] = struct.unpack("<d", encoded)[0]
        violations.append(violation)

    return {
        "database": str(database),
        "session_found": True,
        "clean": bool(closed_at) and not violations,
        "session": {
            "id": session_id.lower(),
            "started_at_ms": started_at,
            "closed_at_ms": closed_at,
            "client_protocol": client_protocol,
            "server_version": server_version,
            "grim_version": grim_version,
        },
        "violation_count": len(violations),
        "violations": violations,
    }


async def _wait_for_grim_audit(
    database: Path,
    username: str,
    started_after_ms: int,
    timeout: float,
) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + timeout
    audit: dict[str, object] | None = None
    while True:
        audit = await asyncio.to_thread(
            _read_grim_audit,
            database,
            username,
            started_after_ms,
        )
        session = audit.get("session")
        if isinstance(session, dict) and session.get("closed_at_ms"):
            # The session close and its final violation batch may use separate
            # transactions. Give the datastore one short flush interval, then
            # take the report that will be returned to the caller.
            await asyncio.sleep(0.1)
            return await asyncio.to_thread(
                _read_grim_audit,
                database,
                username,
                started_after_ms,
            )
        if asyncio.get_running_loop().time() >= deadline:
            assert audit is not None
            return audit
        await asyncio.sleep(0.1)


async def run_movement_matrix(args: argparse.Namespace) -> dict[str, object]:
    """Collect a movement trace using the public :func:`connect` API."""

    if min(args.walk_ticks, args.sprint_ticks, args.sneak_ticks) < 0:
        raise ValueError("tick counts cannot be negative")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    grim_wait = float(getattr(args, "grim_wait", 2.0))
    if grim_wait < 0:
        raise ValueError("GrimAC wait must be non-negative")
    started_at_ms = time.time_ns() // 1_000_000
    bot = await connect(
        args.host,
        port=args.port,
        username=args.username,
        version=args.version,
        timeout=args.timeout,
    )
    try:
        await bot.wait_world(timeout=args.timeout)
        trace: list[dict[str, object]] = []
        sequence = (
            [MovementInput(forward=1.0)] * args.walk_ticks
            + [MovementInput(forward=1.0, sprint=True, jump=True)]
            + [MovementInput(forward=1.0, sprint=True)] * args.sprint_ticks
            + [MovementInput(sneak=True)] * args.sneak_ticks
        )
        for tick, controls in enumerate(sequence):
            state = await bot.tick(controls)
            trace.append(
                {
                    "tick": tick,
                    "x": state.position.x,
                    "y": state.position.y,
                    "z": state.position.z,
                    "vx": state.velocity.x,
                    "vy": state.velocity.y,
                    "vz": state.velocity.z,
                    "on_ground": state.on_ground,
                    "pose": state.pose,
                    "completed_at_ms": time.time_ns() // 1_000_000,
                }
            )
            # A vanilla client sends one movement tick every 50 ms.  Keeping
            # this delay configurable in a future API is unnecessary for the
            # regression command, whose purpose is to exercise server checks.
            await asyncio.sleep(0.05)
        result: dict[str, object] = {
            "version": args.version,
            "host": args.host,
            "port": args.port,
            "username": args.username,
            "started_at_ms": started_at_ms,
            "trace": trace,
        }
    finally:
        await bot.close()
    grim_database = getattr(args, "grim_db", None)
    if grim_database is not None:
        result["grimac"] = await _wait_for_grim_audit(
            Path(grim_database),
            args.username,
            started_at_ms,
            grim_wait,
        )
    return result


def movement_matrix() -> None:
    """Console-script entry point for ``protobot-movement-matrix``."""

    parser = _movement_parser()
    args = parser.parse_args()
    if args.require_grim_clean and args.grim_db is None:
        parser.error("--require-grim-clean requires --grim-db")
    result = asyncio.run(run_movement_matrix(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"wrote {len(result['trace'])} ticks to {args.output}")
    audit = result.get("grimac")
    if isinstance(audit, dict):
        print(f"GrimAC violations: {audit['violation_count']}")
        if args.require_grim_clean and not audit["clean"]:
            raise SystemExit("GrimAC did not produce a clean, closed session")


def _export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protobot-export-block-states",
        description="Convert Mojang reports/blocks.json to a ProtoBot table.",
    )
    parser.add_argument("report", help="path to Mojang reports/blocks.json")
    parser.add_argument("--output", required=True, help="output JSON or JSON.GZ table")
    parser.add_argument("--version", help="Minecraft release label")
    return parser


def export_block_states() -> None:
    """Console-script entry point for ``protobot-export-block-states``."""

    args = _export_parser().parse_args()
    registry = BlockStateRegistry.from_report(args.report)
    count = registry.export_table(args.output, version=args.version)
    print(f"exported {count} block states to {args.output}")


def _port_is_open(host: str, port: int) -> bool:
    """Return whether a TCP connect succeeds without retaining the socket."""

    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (ConnectionError, OSError, TimeoutError):
        return False


async def _wait_for_port(
    host: str,
    port: int,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_open(host, port):
            return
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"server process exited with status {process.returncode}")
        await asyncio.sleep(0.25)
    raise TimeoutError(f"server did not listen on {host}:{port} within {timeout:g}s")


def _write_server_properties(path: Path, port: int) -> None:
    """Create minimal offline properties when a jar has no configuration yet."""

    if path.exists():
        return
    path.write_text(
        "\n".join(
            (
                f"server-port={port}",
                "server-ip=",
                "online-mode=false",
                "enable-status=false",
                "spawn-protection=0",
                "view-distance=4",
                "simulation-distance=4",
                "sync-chunk-writes=false",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _jar_main_class(jar: Path) -> str | None:
    """Read a jar's declared main class without invoking Java.

    Mojang distributes both a self-contained launcher (whose main class is the
    bundler) and a version jar (whose ``net.minecraft.server.Main`` class
    expects the libraries directory on the Java class path).  Keeping this
    small probe here lets the live diagnostic support either distribution.
    """

    try:
        with zipfile.ZipFile(jar) as archive:
            raw = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    for line in raw.splitlines():
        if line.lower().startswith("main-class:"):
            return line.split(":", 1)[1].strip()
    return None


def _nearby_library_jars(jar: Path, server_dir: Path) -> list[Path]:
    """Find a Mojang ``libraries`` tree near a version jar.

    A downloaded server layout normally places ``libraries`` beside
    ``versions``; a caller may instead copy it into ``server_dir``.  Search a
    bounded set of ancestors and never scan an unrelated filesystem root.
    """

    roots: list[Path] = [server_dir / "libraries", jar.parent / "libraries"]
    # A version jar may live several levels below a launcher cache (for
    # example ``versions/26.2/server-26.2.jar``), but walking all the way to a
    # filesystem root can accidentally inspect an unrelated ``libraries``
    # directory and make startup unexpectedly expensive.  Six levels covers
    # the official layout and a couple of common wrapper directories while
    # keeping the probe bounded.
    for ancestor in list(jar.parents)[:6]:
        roots.append(ancestor / "libraries")
    seen: set[Path] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        try:
            jars = sorted(resolved.rglob("*.jar"))
        except OSError:
            continue
        if jars:
            return jars
    return []


def _server_command(
    java: str,
    jvm_args: list[str],
    jar: Path,
    server_dir: Path,
) -> list[str]:
    """Build a Java command for launcher and dependency-split server jars.

    Mojang version jars and modern Paper version jars both declare their real
    entry point while keeping dependencies in a neighboring ``libraries``
    tree.  ``java -jar`` ignores that tree unless the manifest contains a
    (usually enormous) ``Class-Path`` entry, so invoke the declared main class
    directly whenever those libraries are available.  Self-contained launcher
    jars continue to use ``-jar``.
    """

    main_class = _jar_main_class(jar)
    if main_class in {"io.papermc.paperclip.Main", "net.minecraft.bundler.Main"}:
        return [java, *jvm_args, "-jar", str(jar), "--nogui"]
    if main_class is not None:
        libraries = _nearby_library_jars(jar, server_dir)
        if libraries:
            classpath = os.pathsep.join(str(path) for path in (jar, *libraries))
            return [java, *jvm_args, "-cp", classpath, main_class, "--nogui"]
    return [java, *jvm_args, "-jar", str(jar), "--nogui"]


def _live_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protobot-live-regression",
        description=(
            "Exercise offline login, world loading, keep-alive, and movement "
            "against a running server or a local server jar."
        ),
    )
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--version", default="26.2")
    parser.add_argument("--username", default="ProtoBotLive")
    parser.add_argument(
        "--loader",
        choices=("vanilla", "forge", "neoforge", "fabric"),
        default="vanilla",
    )
    parser.add_argument(
        "--mod",
        action="append",
        default=[],
        type=_mod_assignment,
        metavar="ID=VERSION",
        help="client mod declaration; may be specified more than once",
    )
    parser.add_argument("--loader-protocol", type=int, default=0)
    parser.add_argument(
        "--velocity-secret",
        help="Velocity modern-forwarding secret (UTF-8 text; do not include the newline)",
    )
    parser.add_argument(
        "--velocity-player-ip",
        default="127.0.0.1",
        help="IP address to put in the Velocity forwarding payload",
    )
    parser.add_argument(
        "--fabric-accept-registry",
        action="store_true",
        help=(
            "blindly acknowledge and count Fabric registry sync payloads in this "
            "disposable diagnostic"
        ),
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--movement-ticks", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--jar",
        type=Path,
        help="start this server jar before connecting (requires --accept-eula)",
    )
    parser.add_argument(
        "--server-dir",
        type=Path,
        help="working directory for --jar (defaults to the jar's directory)",
    )
    parser.add_argument("--java", default="java", help="Java executable")
    parser.add_argument(
        "--jvm-arg",
        action="append",
        default=[],
        help="extra JVM argument; may be specified more than once",
    )
    parser.add_argument(
        "--accept-eula",
        action="store_true",
        help="write eula=true when starting a local jar",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="server log path (default: <server-dir>/protobot-live.log)",
    )
    return parser


def _mod_assignment(value: str) -> tuple[str, str]:
    mod_id, separator, version = value.partition("=")
    if not separator or not mod_id or not version:
        raise argparse.ArgumentTypeError("mod must use non-empty ID=VERSION syntax")
    return mod_id, version


async def run_live_regression(args: argparse.Namespace) -> dict[str, object]:
    """Run a small live-server health and movement regression.

    The server jar is optional.  Without ``--jar`` this function only connects
    to an existing server, making it suitable for CI where the server is
    managed by a container or service fixture.  With ``--jar`` the caller must
    explicitly pass ``--accept-eula``; no EULA file is created implicitly.
    """

    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if not 0 < args.port < 65536:
        raise ValueError("port must be between 1 and 65535")
    if args.movement_ticks < 0:
        raise ValueError("movement tick count cannot be negative")
    loader = getattr(args, "loader", "vanilla")
    loader_protocol = getattr(args, "loader_protocol", 0)
    if loader_protocol < 0:
        raise ValueError("loader protocol version cannot be negative")
    velocity_secret = getattr(args, "velocity_secret", None)
    velocity_player_ip = getattr(args, "velocity_player_ip", "127.0.0.1")
    mod_entries = getattr(args, "mod", [])
    mods: dict[str, str] = {}
    for mod_id, version in mod_entries:
        if mod_id in mods:
            raise ValueError(f"duplicate mod id: {mod_id}")
        mods[mod_id] = version
    fabric_accept_registry = getattr(args, "fabric_accept_registry", False)
    if fabric_accept_registry and loader != "fabric":
        raise ValueError("--fabric-accept-registry requires --loader fabric")
    fabric_registry_syncs = 0
    fabric_registry_bytes = 0

    def accept_fabric_registry(snapshot: bytes) -> bool:
        nonlocal fabric_registry_syncs, fabric_registry_bytes
        fabric_registry_syncs += 1
        fabric_registry_bytes += len(snapshot)
        return True
    process: subprocess.Popen[bytes] | None = None
    log_stream = None
    server_dir: Path | None = None
    start_time = time.monotonic()
    try:
        if args.jar is not None:
            jar = args.jar.expanduser().resolve()
            if not jar.is_file():
                raise FileNotFoundError(f"server jar not found: {jar}")
            server_dir = (args.server_dir or jar.parent).expanduser().resolve()
            server_dir.mkdir(parents=True, exist_ok=True)
            eula = server_dir / "eula.txt"
            if args.accept_eula:
                eula.write_text(
                    "# Generated for ProtoBot live regression\neula=true\n",
                    encoding="utf-8",
                )
            elif not eula.exists() or "eula=true" not in eula.read_text(encoding="utf-8").lower():
                raise RuntimeError(
                    "refusing to start a Minecraft server without eula=true; "
                    "pass --accept-eula explicitly"
                )
            if _port_is_open(args.host, args.port):
                raise RuntimeError(
                    f"refusing to start a second server: {args.host}:{args.port} is in use"
                )
            _write_server_properties(server_dir / "server.properties", args.port)
            log_path = args.log or (server_dir / "protobot-live.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_stream = log_path.open("ab")
            command = _server_command(args.java, args.jvm_arg, jar, server_dir)
            process = subprocess.Popen(
                command,
                cwd=server_dir,
                stdin=subprocess.PIPE,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
            )
            await _wait_for_port(args.host, args.port, args.timeout, process)

        bot = await connect(
            args.host,
            port=args.port,
            username=args.username,
            version=args.version,
            timeout=args.timeout,
            loader=loader,
            mods=mods,
            loader_protocol=loader_protocol,
            fabric_registry_handler=accept_fabric_registry if fabric_accept_registry else None,
            velocity_secret=velocity_secret,
            velocity_player_ip=velocity_player_ip,
        )
        try:
            await bot.wait_world(timeout=args.timeout)
            trace: list[dict[str, object]] = []
            for tick in range(args.movement_ticks):
                state = await bot.tick(MovementInput(forward=1.0))
                trace.append(
                    {
                        "tick": tick,
                        "x": state.position.x,
                        "y": state.position.y,
                        "z": state.position.z,
                        "on_ground": state.on_ground,
                        "pose": state.pose,
                    }
                )
                await asyncio.sleep(0.05)
            result: dict[str, object] = {
                "ok": True,
                "version": args.version,
                "protocol": bot.version.protocol,
                "host": args.host,
                "port": args.port,
                "username": bot.username,
                "loader": bot.modlist.loader.value,
                "mods": dict(bot.modlist.mods),
                "fabric_registry_syncs": fabric_registry_syncs,
                "fabric_registry_bytes": fabric_registry_bytes,
                "uuid": str(bot.uuid),
                "session_id": str(bot.session_id) if bot.session_id else None,
                "state": bot.state.value,
                "chunks": len(bot.world.chunks),
                "position": {
                    "x": bot.player.x,
                    "y": bot.player.y,
                    "z": bot.player.z,
                },
                "trace": trace,
                "elapsed_seconds": round(time.monotonic() - start_time, 3),
            }
        finally:
            await bot.close()
        return result
    finally:
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.write(b"stop\n")
                    process.stdin.flush()
                except OSError:
                    pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if log_stream is not None:
            log_stream.close()


def live_regression() -> None:
    """Console-script entry point for ``protobot-live-regression``."""

    args = _live_parser().parse_args()
    result = asyncio.run(run_live_regression(args))
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


__all__ = [
    "export_block_states",
    "live_regression",
    "movement_matrix",
    "run_live_regression",
    "run_movement_matrix",
]
