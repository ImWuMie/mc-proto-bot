"""Build everything a ProtoBot release ships.

Two kinds of artifacts land in dist/:

- **pip/uv packages** (``packages``): sdist + wheel. The wheel carries the
  block-state tables and the bundled example plugins; ``protobot setup``
  writes a starter plugins/ directory next to the config for pip users.
- **Self-contained portable** (``portable``): a PyInstaller onedir build --
  protobot.exe plus its own Python runtime -- zipped up with the example
  plugins and the READMEs. Extract it anywhere and run protobot.exe: nothing
  to install, no Python required. Built on Windows this is a Windows exe; the
  zip name carries the platform.

Usage: ``python release.py [all|packages|portable]`` (default: all).
Requires git, uv, and PyInstaller (installed through the dev dependency
group). The tree should be committed and clean so what is built matches HEAD.
"""

from __future__ import annotations

import argparse
import pathlib
import platform
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXAMPLE_PLUGINS = (
    "chat_logger.py",
    "fishing.py",
    "llm_agent.py",
    "no_fall.py",
    "respawn.py",
    "scheduler.py",
)


def version() -> str:
    sys.path.insert(0, str(ROOT))
    import protobot

    return protobot.__version__


def run(command: list[str], **kwargs) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True, **kwargs)


def sync_examples() -> None:
    """Refresh the bundled example plugins from the canonical sources.

    Committed copies keep pip installs from a plain checkout complete;
    overwriting here means a release can never ship stale examples.
    """
    bundled = ROOT / "protobot" / "examples" / "plugins"
    for name in EXAMPLE_PLUGINS:
        shutil.copyfile(ROOT / "plugins" / name, bundled / name)
    print(f"synced {len(EXAMPLE_PLUGINS)} example plugin(s)")


def warn_if_dirty() -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    if status:
        print(
            "warning: the working tree has uncommitted changes; the release is "
            "built from HEAD and will not include them"
        )


def build_packages() -> None:
    DIST.mkdir(exist_ok=True)
    if shutil.which("uv"):
        run(["uv", "build"])
    else:
        run([sys.executable, "-m", "build"])


def build_portable(ver: str) -> None:
    if shutil.which("uv"):
        run(["uv", "run", "python", "-m", "PyInstaller", "--noconfirm", "--clean",
             "protobot.spec"])
    else:
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             "protobot.spec"])
    # Stage the onedir output together with the example plugins and the docs,
    # then zip it. Only tracked plugin files are copied, so local runtime data
    # (settings, memory) never leaks into a release.
    staging = DIST / "portable"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for path in (DIST / "protobot").iterdir():
        (shutil.copytree if path.is_dir() else shutil.copy2)(path, staging / path.name)
    plugin_dir = staging / "plugins"
    plugin_dir.mkdir()
    for name in EXAMPLE_PLUGINS:
        shutil.copyfile(ROOT / "plugins" / name, plugin_dir / name)
    for name in ("README.md", "README_zh.md", "LICENSE"):
        shutil.copyfile(ROOT / name, staging / name)
    platform_tag = f"{platform.system().lower()}-{platform.machine().lower()}"
    zip_base = DIST / f"protobot-{ver}-{platform_tag}-portable"
    shutil.make_archive(str(zip_base), "zip", root_dir=staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step", nargs="?", default="all",
        choices=("all", "packages", "portable"),
        help="what to build (default: everything)",
    )
    args = parser.parse_args()

    ver = version()
    DIST.mkdir(exist_ok=True)
    sync_examples()
    warn_if_dirty()

    if args.step in ("all", "packages"):
        build_packages()
    if args.step in ("all", "portable"):
        build_portable(ver)

    print("\nrelease artifacts:")
    for path in sorted(DIST.iterdir()):
        if path.name.startswith(("protobot-",)) and (
            path.is_file() or (path.is_dir() and path.name == "protobot")
        ):
            size = (
                path.stat().st_size / 1024 / 1024
                if path.is_file()
                else sum(p.stat().st_size for p in path.rglob("*")) / 1024 / 1024
            )
            print(f"  {path.name}  ({size:.1f} MB)")
    print("\nnext: tag and publish, see the README section 'Building a release'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
