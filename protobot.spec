# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the self-contained Windows build.

Produces dist/protobot/ with protobot.exe + _internal/, a full Python runtime
of its own -- the user needs nothing installed. Everything the app reads off
disk at runtime (block-state tables, the example plugins written by
``protobot setup``) is collected as data; textual/rich/cryptography are
collected wholesale so the TUI and online-mode auth keep working frozen.
"""

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

textual_datas, textual_binaries, textual_hidden = collect_all("textual")
rich_datas, rich_binaries, rich_hidden = collect_all("rich")
# The QQ bridge (llm_agent's optional qq extra). The plugin files are data,
# not imported modules, so PyInstaller's static scan never sees "import botpy"
# -- it has to be collected explicitly, together with its dependencies.
# botpy.ext.cog_apscheduler cannot be imported without a running event loop,
# so it is collected module-by-module with that one excluded; the PyPI
# distribution is named qq-botpy, which is what copy_metadata wants.
botpy_datas = collect_data_files("botpy")
botpy_binaries = collect_dynamic_libs("botpy")
botpy_hidden = [
    name for name in collect_submodules("botpy")
    if "cog_apscheduler" not in name
]
aiohttp_datas, aiohttp_binaries, aiohttp_hidden = collect_all("aiohttp")

a = Analysis(
    ["protobot_launcher.py"],
    pathex=["."],
    binaries=textual_binaries + rich_binaries + botpy_binaries + aiohttp_binaries,
    datas=(
        textual_datas
        + rich_datas
        + botpy_datas
        + aiohttp_datas
        + copy_metadata("textual")
        + copy_metadata("rich")
        + copy_metadata("cryptography")
        + copy_metadata("qq-botpy")
        + copy_metadata("aiohttp")
        + [
            ("protobot/data", "protobot/data"),
            ("protobot/examples/plugins", "protobot/examples/plugins"),
        ]
    ),
    hiddenimports=textual_hidden + rich_hidden + botpy_hidden + aiohttp_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="protobot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="protobot",
)
