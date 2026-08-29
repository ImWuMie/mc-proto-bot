"""Build everything a ProtoBot release ships, in one command.

What comes out of dist/:

- ``protobot-<version>.tar.gz`` and ``.whl`` -- the pip/uv packages. The wheel
  carries the block-state tables and the bundled example plugins, and exposes
  the ``protobot`` console command with all its subcommands.
- ``protobot-<version>-portable.zip`` -- the whole repository at HEAD (git
  archive), launchers and example plugins included. Extract it anywhere, open
  a terminal in that folder and run ``protobot.bat`` / ``./protobot.sh``; the
  first-run wizard writes config.yaml and a starter plugins/ directory next to
  it. Only Python 3.12+ is needed -- no install step.

Usage: ``python release.py``. Requires git and uv on PATH (uv falls back to
``python -m build``). The tree should be committed and clean so the zip and
the version stamp match what was built.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXAMPLE_PLUGINS = (
    "chat_logger.py",
    "fishing.py",
    "llm_agent.py",
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


def main() -> int:
    ver = version()

    # 1. Refresh the bundled example plugins from the canonical sources.
    #    Committed copies keep pip installs from a plain checkout complete;
    #    overwriting here means a release can never ship stale examples.
    bundled = ROOT / "protobot" / "examples" / "plugins"
    for name in EXAMPLE_PLUGINS:
        shutil.copyfile(ROOT / "plugins" / name, bundled / name)
    print(f"synced {len(EXAMPLE_PLUGINS)} example plugin(s)")

    # 2. sdist + wheel.
    DIST.mkdir(exist_ok=True)
    if shutil.which("uv"):
        run(["uv", "build"])
    else:
        run([sys.executable, "-m", "build"])

    # 3. The portable zip: exactly the tracked files at HEAD, so .gitignore
    #    already keeps config.yaml, credentials, dist/ and runtime data out.
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    if status:
        print("warning: the working tree has uncommitted changes; the portable "
              "zip is built from HEAD and will not include them")
    zip_path = DIST / f"protobot-{ver}-portable.zip"
    if zip_path.exists():
        zip_path.unlink()
    run(["git", "archive", "--format=zip", "--output", str(zip_path), "HEAD"])

    print("\nrelease artifacts:")
    for path in sorted(DIST.glob(f"protobot-{ver}*")):
        size = path.stat().st_size / 1024 / 1024
        print(f"  {path.name}  ({size:.1f} MB)")
    print("\nnext: tag and publish, see the README section 'Building a release'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
