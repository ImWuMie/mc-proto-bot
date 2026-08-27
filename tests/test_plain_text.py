"""Regression tests for run_bot's chat-component plain-text rendering."""

from __future__ import annotations

import unittest

import run_bot


class PlainTextTest(unittest.TestCase):
    def test_string_and_list_components(self) -> None:
        self.assertEqual(run_bot._plain_text("hi"), "hi")
        self.assertEqual(run_bot._plain_text([{"text": "a"}, "b"]), "ab")

    def test_text_and_extra(self) -> None:
        self.assertEqual(run_bot._plain_text({"text": "", "extra": [{"text": "x"}]}), "x")

    def test_translate_without_fallback(self) -> None:
        self.assertEqual(
            run_bot._plain_text(
                {"translate": "multiplayer.player.joined", "with": ["Steve"]}
            ),
            "multiplayer.player.joinedSteve",
        )

    def test_fallback_wins_over_translation_key(self) -> None:
        self.assertEqual(
            run_bot._plain_text(
                {
                    "translate": "commands.message.display.incoming",
                    "fallback": "[_ImWuMie -> me] 123",
                    "with": ["_ImWuMie", "me", {"text": "123"}],
                }
            ),
            "[_ImWuMie -> me] 123",
        )

    def test_empty_key_text_is_rendered(self) -> None:
        """A live server put message content under an empty key ('').

        The wire data was {'': '123'} instead of {'text': '123'}; the text
        must still be shown rather than dropped.
        """
        component = {
            "extra": [
                {
                    "color": "gold",
                    "extra": [
                        {
                            "color": "red",
                            "extra": [
                                {
                                    "color": "gold",
                                    "extra": [
                                        {
                                            "color": "red",
                                            "extra": [{"color": "gold", "text": "] "}],
                                            "text": "me",
                                        }
                                    ],
                                    "text": " -> ",
                                }
                            ],
                            "text": "_ImWuMie",
                        }
                    ],
                    "text": "[",
                },
                {"": "123"},
            ],
            "text": "",
        }
        self.assertEqual(run_bot._plain_text(component), "[_ImWuMie -> me] 123")

    def test_empty_key_renders_alongside_other_parts(self) -> None:
        self.assertEqual(run_bot._plain_text({"": "tail"}), "tail")


if __name__ == "__main__":
    unittest.main()
