"""Regression tests for run_bot's chat-component plain-text rendering."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from protobot.text import format_translation, plain_text
from protobot.translations import (
    TRANSLATIONS,
    load_translations,
    register_translations,
)


class PlainTextTest(unittest.TestCase):
    def test_string_and_list_components(self) -> None:
        self.assertEqual(plain_text("hi"), "hi")
        self.assertEqual(plain_text([{"text": "a"}, "b"]), "ab")

    def test_text_and_extra(self) -> None:
        self.assertEqual(plain_text({"text": "", "extra": [{"text": "x"}]}), "x")

    def test_translate_uses_the_builtin_table(self) -> None:
        """The key is a pattern, not a prefix: arguments go *into* it.

        This used to render "multiplayer.player.joinedSteve" -- key and
        arguments concatenated -- which is what the vanilla client would show
        as "Steve joined the game".
        """
        self.assertEqual(
            plain_text(
                {"translate": "multiplayer.player.joined", "with": ["Steve"]}
            ),
            "Steve joined the game",
        )

    def test_chat_decoration(self) -> None:
        self.assertEqual(
            plain_text(
                {
                    "translate": "chat.type.text",
                    "with": [{"text": "Steve"}, {"text": "hello"}],
                }
            ),
            "<Steve> hello",
        )

    def test_fallback_wins_over_translation_key(self) -> None:
        self.assertEqual(
            plain_text(
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
        self.assertEqual(plain_text(component), "[_ImWuMie -> me] 123")

    def test_empty_key_renders_alongside_other_parts(self) -> None:
        self.assertEqual(plain_text({"": "tail"}), "tail")


class TranslateFormattingTest(unittest.TestCase):
    def test_positional_arguments(self) -> None:
        self.assertEqual(
            plain_text(
                {
                    "translate": "death.attack.thorns.item",
                    "with": ["mie_233", "a Zombie", "an Iron Sword"],
                }
            ),
            "mie_233 was killed by an Iron Sword while trying to hurt a Zombie",
        )

    def test_death_message_with_two_arguments(self) -> None:
        self.assertEqual(
            plain_text({"translate": "death.attack.mob", "with": ["mie_233", "Zombie"]}),
            "mie_233 was slain by Zombie",
        )

    def test_unknown_key_keeps_its_arguments(self) -> None:
        """An unknown key still has to show the name and the message."""
        self.assertEqual(
            plain_text({"translate": "myserver.greeting", "with": ["Steve", "hi"]}),
            "myserver.greeting Steve hi",
        )

    def test_unknown_key_without_arguments(self) -> None:
        self.assertEqual(plain_text({"translate": "myserver.quiet"}), "myserver.quiet")

    def test_missing_argument_renders_as_nothing(self) -> None:
        self.assertEqual(
            plain_text({"translate": "chat.type.text", "with": ["Steve"]}),
            "<Steve> ",
        )

    def test_nested_component_arguments(self) -> None:
        self.assertEqual(
            plain_text(
                {
                    "translate": "chat.type.text",
                    "with": [
                        {"text": "Steve", "extra": [{"text": "!"}]},
                        {"translate": "multiplayer.player.left", "with": ["Alex"]},
                    ],
                }
            ),
            "<Steve!> Alex left the game",
        )

    def test_extra_still_follows_a_translation(self) -> None:
        self.assertEqual(
            plain_text(
                {
                    "translate": "multiplayer.player.joined",
                    "with": ["Steve"],
                    "extra": [{"text": " (again)"}],
                }
            ),
            "Steve joined the game (again)",
        )

    def test_single_argument_not_wrapped_in_a_list(self) -> None:
        self.assertEqual(
            plain_text({"translate": "multiplayer.player.left", "with": "Alex"}),
            "Alex left the game",
        )

    def test_percent_escape_and_stray_percent(self) -> None:
        self.assertEqual(format_translation("100%% sure", []), "100% sure")
        self.assertEqual(format_translation("50% off %s", ["today"]), "50% off today")

    def test_arguments_are_not_appended_to_a_known_pattern(self) -> None:
        """A pattern that ignores an argument must not grow a tail.

        Only the unknown-key fallback appends leftovers; doing it for real
        patterns would duplicate the player name in every whisper.
        """
        self.assertEqual(
            format_translation("%s said something", ["Steve", "hello"]),
            "Steve said something",
        )

    def test_per_call_table_overrides_the_builtin(self) -> None:
        self.assertEqual(
            plain_text(
                {"translate": "chat.type.text", "with": ["Steve", "hi"]},
                translations={"chat.type.text": "%s: %s"},
            ),
            "Steve: hi",
        )

    def test_fallback_is_a_pattern_when_arguments_come_with_it(self) -> None:
        self.assertEqual(
            plain_text(
                {
                    "translate": "chat.type.text",
                    "fallback": "%s says %s",
                    "with": ["Steve", "hi"],
                }
            ),
            "Steve says hi",
        )

    def test_argumentless_fallback_is_verbatim(self) -> None:
        """Server text with no arguments keeps a literal percent sequence."""
        self.assertEqual(
            plain_text({"translate": "x.y", "fallback": "100%s sure"}),
            "100%s sure",
        )


class TranslationTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = dict(TRANSLATIONS)

    def tearDown(self) -> None:
        TRANSLATIONS.clear()
        TRANSLATIONS.update(self.saved)

    def test_register_adds_server_specific_keys(self) -> None:
        register_translations({"myserver.welcome": "Welcome, %s!"})
        self.assertEqual(
            plain_text({"translate": "myserver.welcome", "with": ["Steve"]}),
            "Welcome, Steve!",
        )

    def test_load_language_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "en_us.json"
            path.write_text(
                json.dumps({"a.key": "A %s", "a.number": 5}), encoding="utf-8"
            )
            added = load_translations(path)
        self.assertEqual(added, 1)  # non-string values are ignored
        self.assertEqual(plain_text({"translate": "a.key", "with": ["x"]}), "A x")

    def test_the_whole_vanilla_death_set_is_present(self) -> None:
        for key in ("death.attack.player", "death.fell.accident.generic"):
            self.assertIn(key, TRANSLATIONS)


if __name__ == "__main__":
    unittest.main()
