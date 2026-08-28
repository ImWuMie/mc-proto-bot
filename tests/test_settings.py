"""Tests for protobot/settings.py (plugin companion settings files)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from protobot.settings import PluginSettings, deep_merge

DEFAULTS = {
    "enabled": False,
    "limits": {"count": 10, "ratio": 0.5},
    "names": [],
}


def clamp(merged: dict) -> dict:
    merged["limits"]["count"] = max(1, min(100, int(merged["limits"]["count"])))
    return merged


class DeepMergeTest(unittest.TestCase):
    def test_nested_sections_keep_untouched_keys(self) -> None:
        self.assertEqual(
            deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}}),
            {"a": {"x": 1, "y": 9}},
        )

    def test_scalars_and_lists_are_replaced_whole(self) -> None:
        self.assertEqual(
            deep_merge({"a": [1, 2], "b": 1}, {"a": [3], "b": 2}),
            {"a": [3], "b": 2},
        )

    def test_inputs_are_not_mutated(self) -> None:
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": {"x": 2}})
        self.assertEqual(base, {"a": {"x": 1}})


class PluginSettingsTest(unittest.TestCase):
    def _settings(self, tmp: str, name: str = "s.json") -> PluginSettings:
        return PluginSettings(
            Path(tmp) / name, DEFAULTS, label="测试", normalize=clamp
        )

    def test_missing_file_is_created_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.load()
            saved = json.loads(settings.path.read_text(encoding="utf-8"))
            self.assertEqual(saved, DEFAULTS)
            self.assertFalse(settings.data["enabled"])

    def test_user_values_merge_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text(
                json.dumps({"enabled": True, "limits": {"count": 3}}),
                encoding="utf-8",
            )
            settings.load()
            self.assertTrue(settings.data["enabled"])
            self.assertEqual(settings.data["limits"]["count"], 3)
            self.assertEqual(settings.data["limits"]["ratio"], 0.5)  # 默认保留

    def test_normalize_runs_on_every_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text(
                json.dumps({"limits": {"count": 9999}}), encoding="utf-8"
            )
            settings.load()
            self.assertEqual(settings.data["limits"]["count"], 100)

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text("not json", encoding="utf-8")
            settings.load()
            self.assertEqual(settings.data["limits"]["count"], 10)

    def test_non_object_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text("[1, 2]", encoding="utf-8")
            settings.load()
            self.assertEqual(settings.data["limits"]["count"], 10)

    def test_reload_if_changed_detects_an_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.load()
            self.assertFalse(settings.reload_if_changed())
            settings.path.write_text(
                json.dumps({"enabled": True}), encoding="utf-8"
            )
            os.utime(settings.path, (0, 0))
            self.assertTrue(settings.reload_if_changed())
            self.assertTrue(settings.data["enabled"])
            self.assertFalse(settings.reload_if_changed())  # 只报一次

    def test_patch_writes_one_key_and_keeps_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text(
                json.dumps({"names": ["keep"]}), encoding="utf-8"
            )
            settings.load()
            self.assertEqual(settings.patch({"enabled": True}), "")
            saved = json.loads(settings.path.read_text(encoding="utf-8"))
            # 用户写过的键保留，没写过的键不会被展开成默认值
            self.assertEqual(saved, {"names": ["keep"], "enabled": True})
            self.assertTrue(settings.data["enabled"])

    def test_patch_does_not_look_like_an_external_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.load()
            settings.patch({"enabled": True})
            self.assertFalse(settings.reload_if_changed())

    def test_patch_preserves_a_concurrent_user_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.load()
            # 用户在本进程加载之后改了另一个键
            settings.path.write_text(
                json.dumps({"limits": {"count": 7}}), encoding="utf-8"
            )
            settings.patch({"enabled": True})
            saved = json.loads(settings.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["limits"]["count"], 7)
            self.assertTrue(saved["enabled"])

    def test_patch_merges_into_nested_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            settings.path.write_text(
                json.dumps({"limits": {"count": 4, "ratio": 0.9}}),
                encoding="utf-8",
            )
            settings.load()
            settings.patch({"limits": {"count": 5}})
            saved = json.loads(settings.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["limits"], {"count": 5, "ratio": 0.9})

    def test_data_is_usable_before_any_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(tmp)
            self.assertEqual(settings.data["limits"]["count"], 10)
            self.assertFalse(settings.path.exists())  # 构造时不落盘

    def test_parent_directories_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = PluginSettings(
                Path(tmp) / "nested" / "deep" / "s.json", DEFAULTS
            )
            settings.load()
            self.assertTrue(settings.path.exists())


if __name__ == "__main__":
    unittest.main()
