"""Tests for the print-style leveled logging module."""

from __future__ import annotations

import contextlib
import io
import unittest

from protobot import log


class FormatTest(unittest.TestCase):
    def test_join_and_newline_like_print(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            log.info("a", "b", "c")
        self.assertEqual(captured.getvalue(), "a b c\n")

    def test_sep_and_end_arguments(self) -> None:
        # Sinks receive complete lines, so the default print sink always
        # terminates with a newline even when end="!" is given.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            log.info("a", "b", sep=",", end="!")
        self.assertEqual(captured.getvalue(), "a,b!\n")

    def test_level_prefixes(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            log.warn("w")
            log.error("e")
            log.debug("d")
        self.assertEqual(
            captured.getvalue(),
            "[警告] w\n[错误] e\n[调试] d\n",
        )

    def test_multi_line_values_are_split(self) -> None:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            log.info("x\ny")
        self.assertEqual(captured.getvalue(), "x\ny\n")


class SinkTest(unittest.TestCase):
    def tearDown(self) -> None:
        log.set_sink(None)

    def test_set_sink_routes_lines_and_restores(self) -> None:
        lines: list[str] = []
        log.set_sink(lines.append)
        log.info("hello", 42)
        self.assertEqual(lines, ["hello 42"])

        captured = io.StringIO()
        log.set_sink(None)
        with contextlib.redirect_stdout(captured):
            log.info("back")
        self.assertEqual(captured.getvalue(), "back\n")

    def test_sink_receives_complete_lines_without_newlines(self) -> None:
        lines: list[str] = []
        log.set_sink(lines.append)
        log.info("a\nb\nc")
        self.assertEqual(lines, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
