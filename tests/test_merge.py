"""Tests for yt_shorts.merge.deep_merge: the generic per-key deep merge used
to layer an event profile over a channel profile (event wins)."""

from __future__ import annotations

from yt_shorts.merge import deep_merge


class TestDeepMerge:
    def test_override_key_replaces_base_key(self):
        base = {"colors": {"accent": "#000000"}}
        override = {"colors": {"accent": "#FF0000"}}
        assert deep_merge(base, override) == {"colors": {"accent": "#FF0000"}}

    def test_override_naming_only_one_nested_key_keeps_the_others(self):
        """An event naming colors.accent must keep the channel's colors.base."""
        base = {"colors": {"accent": "#000000", "base": "#101010", "text": "#FFFFFF"}}
        override = {"colors": {"accent": "#FF0000"}}
        result = deep_merge(base, override)
        assert result == {"colors": {"accent": "#FF0000", "base": "#101010", "text": "#FFFFFF"}}

    def test_empty_override_returns_base_unchanged_in_value(self):
        base = {"colors": {"accent": "#000000"}, "output": {"width": 1080}}
        assert deep_merge(base, {}) == base

    def test_new_top_level_key_in_override_is_added(self):
        base = {"colors": {"accent": "#000000"}}
        override = {"logo": {"file": "assets/logo.png"}}
        result = deep_merge(base, override)
        assert result == {"colors": {"accent": "#000000"}, "logo": {"file": "assets/logo.png"}}

    def test_deeply_nested_keys_merge_recursively(self):
        base = {"output": {"width": 1080, "height": 1920, "video_y": 600}}
        override = {"output": {"video_y": 700}}
        result = deep_merge(base, override)
        assert result == {"output": {"width": 1080, "height": 1920, "video_y": 700}}

    def test_list_values_are_replaced_wholesale_not_merged(self):
        base = {"tags": ["a", "b", "c"]}
        override = {"tags": ["z"]}
        assert deep_merge(base, override) == {"tags": ["z"]}

    def test_neither_input_is_mutated(self):
        base = {"colors": {"accent": "#000000"}}
        override = {"colors": {"accent": "#FF0000"}}
        base_copy = {"colors": {"accent": "#000000"}}
        override_copy = {"colors": {"accent": "#FF0000"}}
        deep_merge(base, override)
        assert base == base_copy
        assert override == override_copy

    def test_scalar_override_replaces_dict_in_base(self):
        """Not a case the profile format uses today, but the merge must not
        crash if types disagree - override simply wins."""
        base = {"fonts": {"hook": "fonts/A.ttf"}}
        override = {"fonts": "not-a-dict-anymore"}
        assert deep_merge(base, override) == {"fonts": "not-a-dict-anymore"}
