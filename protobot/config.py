"""A small YAML-subset codec for ``config.yaml`` (zero dependencies).

Only the constructs ProtoBot generates and reads are supported: comments,
two-level ``key: value`` maps with deeper nesting, scalar strings (optionally
quoted), integers, floats, ``true``/``false``, ``null``, and inline ``[a, b]``
lists.  Anchors, aliases, block scalars, and flow maps are intentionally
unsupported -- the wizard writes this file and errors name the offending line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["load_config", "save_config"]

_LINE_PATTERN = re.compile(r"^([A-Za-z0-9_.\-]+):\s*(.*)$")


def load_config(path: Path) -> dict:
    """Parse a YAML-subset file into a dict; ``ValueError`` names the bad line."""
    return _parse(path.read_text(encoding="utf-8"), path.name)


def save_config(path: Path, data: dict) -> None:
    """Write a dict as YAML-subset text (nested maps are indented two spaces)."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict) and value:
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {_format_scalar(sub_value)}")
        elif isinstance(value, dict):
            lines.append(f"{key}: {{}}")
        else:
            lines.append(f"{key}: {_format_scalar(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse(text: str, filename: str) -> dict:
    root: dict = {}
    # Each stack entry: (indent of the map's first key, map, indent of this
    # map's own keys, or None until the first key pins it).  All keys of one
    # map must share the same indent, so a value that would attach to the
    # wrong level is caught instead of silently re-parented.
    stack: list[tuple[int, dict, int | None]] = [(-1, root, 0)]
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ValueError(f"{filename} 第 {lineno} 行: 不允许使用制表符缩进")
        indent = len(leading)
        match = _LINE_PATTERN.match(stripped)
        if match is None:
            raise ValueError(f"{filename} 第 {lineno} 行: 无法解析 `{stripped}`")
        key = match.group(1)
        rest = _strip_comment(match.group(2)).strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ValueError(f"{filename} 第 {lineno} 行: 缩进错误")
        container_indent, container, key_indent = stack[-1]
        if key_indent is not None and indent != key_indent:
            raise ValueError(f"{filename} 第 {lineno} 行: 缩进错误（键 `{key}`）")
        if key in container:
            raise ValueError(f"{filename} 第 {lineno} 行: 重复的键 `{key}`")
        if rest == "":
            nested: dict = {}
            container[key] = nested
            stack.append((indent, nested, None))
        else:
            if key_indent is None:
                stack[-1] = (container_indent, container, indent)
            container[key] = _parse_scalar(rest, filename, lineno)
    return root


def _parse_scalar(value: str, filename: str, lineno: int):
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        items = _split_list(value[1:-1])
        return [_parse_scalar(item, filename, lineno) for item in items]
    if value.startswith('"') or value.startswith("'"):
        quote = value[0]
        if not value.endswith(quote) or len(value) < 2:
            raise ValueError(f"{filename} 第 {lineno} 行: 引号未闭合 `{value}`")
        body = value[1:-1]
        if quote == '"':
            body = (
                body.replace('\\"', '"')
                .replace("\\\\", "\\")
                .replace("\\n", "\n")
                .replace("\\t", "\t")
            )
        return body
    if value == "true":
        return True
    if value == "false":
        return False
    if value in ("null", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _split_list(body: str) -> list[str]:
    """Split ``a, "b, c", d`` on top-level commas (quotes may contain commas)."""
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quote == '"':
            current.append(char)
            escaped = True
        elif quote is not None:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
            current.append(char)
        elif char == ",":
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if quote is not None:
        raise ValueError("列表元素中的引号未闭合")
    if current or items:
        items.append("".join(current).strip())
    return [item for item in items if item]


def _strip_comment(text: str) -> str:
    """Strip a trailing ``#`` comment, ignoring ``#`` inside quotes."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote is not None:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return text[:index]
    return text


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_scalar(item) for item in value) + "]"
    text = str(value)
    if (
        not text
        or text != text.strip()
        or any(char in text for char in ':#"\'[],{}')
        or text in ("true", "false", "null", "~")
        or _looks_numeric(text)
    ):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _looks_numeric(text: str) -> bool:
    """True when the bare text would round-trip as int/float (e.g. "26.2")."""
    try:
        int(text)
        return True
    except ValueError:
        pass
    try:
        float(text)
        return True
    except ValueError:
        return False
