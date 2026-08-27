"""Print-style leveled logging, routable to the TUI log area.

``info`` / ``warn`` / ``error`` / ``debug`` take the same arguments as
``print`` (positional values joined with ``sep``, terminated by ``end``).
By default lines go to stdout; while the TUI runs, cli_app installs a sink
that feeds its log area directly -- unlike ``print``, whose output Textual
captures and swallows while its message loop is running.

Usage:

    from protobot import log
    log.info("[聊天]", text)          # like print("[聊天]", text)
    log.warn("配置项缺失:", key)
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["debug", "error", "info", "set_sink", "warn"]

#: Receives complete, newline-free lines.  Default routes back to print.
_sink: Callable[[str], None] = lambda line: print(line)


def set_sink(sink: Callable[[str], None] | None) -> None:
    """Route log lines to a custom sink, or back to stdout when ``None``."""
    global _sink
    _sink = sink if sink is not None else lambda line: print(line)


def _emit(*args: Any, sep: str = " ", end: str = "\n") -> None:
    text = sep.join(str(arg) for arg in args) + end
    pieces = text.split("\n")
    for line in pieces[:-1]:
        _sink(line)
    if pieces[-1]:  # a trailing partial line (end without newline)
        _sink(pieces[-1])


def info(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit(*args, sep=sep, end=end)


def warn(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit("[警告]", *args, sep=sep, end=end)


def error(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit("[错误]", *args, sep=sep, end=end)


def debug(*args: Any, sep: str = " ", end: str = "\n") -> None:
    _emit("[调试]", *args, sep=sep, end=end)
