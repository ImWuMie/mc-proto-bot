"""Plugin companion files: JSON settings with defaults, merge, and hot reload.

Every plugin that has tuning knobs ends up wanting the same five things: a file
next to its own source, defaults written out on first run, user values merged
over those defaults, a reload when the file changes on disk, and a way to write
one value back without clobbering edits made since the last read.  Writing that
per plugin produced three copies that drifted -- and shared two bugs: a
``Path("plugins")`` fallback that wrote to the wrong directory when the process
was launched from elsewhere, and a self-write that the next poll mistook for an
external edit.

What stays with the caller is the part that is genuinely per plugin: the
defaults table and the coercion/clamping of values (passed as ``normalize``),
plus whatever it wants to do when a reload lands.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .log import info, warn

__all__ = ["PluginSettings", "deep_merge"]


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict:
    """Merge ``extra`` over ``base``, recursing into nested mappings.

    A user file that sets one key inside a section keeps the defaults for the
    rest of that section, which is why a flat ``dict.update`` will not do.
    """
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class PluginSettings:
    """One plugin's JSON settings file.

    ``normalize`` receives the merged dict and returns it with values coerced
    and clamped; it runs on every load, so the in-memory settings are always in
    the shape the plugin expects no matter what the file says.
    """

    def __init__(
        self,
        path: Path,
        defaults: Mapping[str, Any],
        *,
        label: str = "设置",
        normalize: Callable[[dict], dict] | None = None,
    ) -> None:
        self.path = Path(path)
        self._defaults = defaults
        self._label = label
        self._normalize = normalize
        self._mtime: float | None = None
        self.data: dict = self._merge({})

    # ---- loading ----

    def _merge(self, raw: Mapping[str, Any]) -> dict:
        merged = deep_merge(self._defaults, raw)
        return self._normalize(merged) if self._normalize is not None else merged

    def load(self) -> dict:
        """Read the file (writing defaults first if it is missing) and merge."""
        raw: Mapping[str, Any] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                warn(f"[{self._label}] 读取失败，使用默认值 ({error})")
                loaded = None
            if isinstance(loaded, Mapping):
                raw = loaded
            elif loaded is not None:
                warn(f"[{self._label}] 文件不是 JSON 对象，使用默认值。")
        else:
            self._write(self._defaults)
            info(f"[{self._label}] 已生成默认文件: {self.path}")
        self.data = self._merge(raw)
        self._snapshot()
        return self.data

    def reload_if_changed(self) -> bool:
        """Reload when the file changed since the last load; ``True`` if it did.

        A write through :meth:`patch` re-snapshots the mtime, so the plugin's
        own writes never come back as an external change.
        """
        if self._mtime is None or not self.path.exists():
            return False
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self.load()
        return True

    # ---- writing ----

    def patch(self, changes: Mapping[str, Any]) -> str:
        """Apply ``changes`` to the file on disk; ``""`` on success, else why not.

        The file is re-read first, so a value the user edited since the last
        load survives, and keys they never set are not expanded into the file.
        """
        raw: dict = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    raw = dict(loaded)
            except (OSError, ValueError) as error:
                return f"读取失败: {error}"
        error = self._write(deep_merge(raw, changes))
        if error:
            return error
        self.load()  # 重新归一化并刷新 mtime：自己写的不算外部改动
        return ""

    def _write(self, data: Mapping[str, Any]) -> str:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as error:
            warn(f"[{self._label}] 写入失败 ({error})")
            return f"写入失败: {error}"
        return ""

    def _snapshot(self) -> None:
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None
