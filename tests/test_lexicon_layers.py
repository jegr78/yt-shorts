"""The lexicon's four additive layers: default -> workspace -> channel -> event."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from yt_shorts import lexicon, profile
from yt_shorts.profile import merge_lexicons

FIXTURE_ERF_DIR = Path(__file__).parent / "fixtures" / "channels" / "erf"
EVENT_NAME = "community-clips-back-catalogue"


def test_layers_accumulate():
    merged = merge_lexicons([{"crash": 3.0}, {"pole": 0.3}, {"purple": 2.0}])
    assert merged == {"crash": 3.0, "pole": 0.3, "purple": 2.0}


def test_the_more_specific_layer_wins_a_collision():
    merged = merge_lexicons([{"crash": 3.0}, {"crash": 1.0}, {"crash": 0.5}])
    assert merged == {"crash": 0.5}


def test_a_zero_weight_drops_an_inherited_marker():
    """Weight 0 is how a channel or event disables something inherited."""
    merged = merge_lexicons([{"crash": 3.0, "pole": 0.3}, {"pole": 0.0}])
    assert merged == {"crash": 3.0}


def test_a_zero_in_the_least_specific_layer_is_also_dropped():
    assert merge_lexicons([{"pole": 0.0}]) == {}


def test_a_later_layer_can_re_enable_what_an_earlier_one_disabled():
    merged = merge_lexicons([{"crash": 3.0}, {"crash": 0.0}, {"crash": 2.0}])
    assert merged == {"crash": 2.0}


def test_empty_layers_are_skipped():
    assert merge_lexicons([{}, {"crash": 3.0}, {}]) == {"crash": 3.0}


def test_no_layers_is_empty():
    assert merge_lexicons([]) == {}


@pytest.fixture
def erf_profile_factory(tmp_path, monkeypatch):
    """Builds a throwaway copy of the ERF channel fixture under tmp_path,
    points profile.CHANNELS_DIR at it (so writing moments.json here never
    touches the committed tests/fixtures/ tree), and returns a factory that
    writes the given workspace/channel/event marker payloads to the three
    moments.json layers, loads erf/community-clips-back-catalogue, and
    returns either the loaded config or - when expect_problems is True -
    the collected problem strings from a raised ProfileError.
    """
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir()
    shutil.copytree(FIXTURE_ERF_DIR, channels_dir / "erf",
                    ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(profile, "CHANNELS_DIR", channels_dir)

    # The committed ERF fixture ships its own channel-level moments.json
    # (predating this feature - stage D2b). These tests need to control
    # exactly which of the four layers exist, so start from a clean slate
    # and let the `channel` argument below put one back when a test wants it.
    (channels_dir / "erf" / "moments.json").unlink()

    def factory(*, workspace=None, channel=None, event=None, expect_problems=False):
        if workspace is not None:
            (tmp_path / "moments.json").write_text(json.dumps(workspace), encoding="utf-8")
        if channel is not None:
            (channels_dir / "erf" / "moments.json").write_text(
                json.dumps(channel), encoding="utf-8")
        if event is not None:
            (channels_dir / "erf" / "events" / EVENT_NAME / "moments.json").write_text(
                json.dumps(event), encoding="utf-8")

        if expect_problems:
            try:
                profile.load(f"erf/{EVENT_NAME}")
            except profile.ProfileError as error:
                lines = str(error).splitlines()[1:]  # drop the "N problem(s):" header
                return [line.strip().removeprefix("- ").strip() for line in lines if line.strip()]
            raise AssertionError("expected profile.ProfileError, none was raised")

        return profile.load(f"erf/{EVENT_NAME}").config

    return factory


class TestProfileLayering:
    """The wiring: every layer present on disk reaches the loaded profile."""

    def test_the_default_alone_applies_when_no_file_exists(self, erf_profile_factory):
        config = erf_profile_factory()
        assert config["lexicon"].markers["crash"] == lexicon.DEFAULT_MARKERS["crash"]

    def test_a_workspace_file_adds_to_the_default(self, erf_profile_factory):
        config = erf_profile_factory(workspace={"markers": {"kesselchen": 2.0}})
        assert config["lexicon"].markers["kesselchen"] == 2.0
        assert "crash" in config["lexicon"].markers          # default still there

    def test_a_channel_file_overrides_the_workspace(self, erf_profile_factory):
        config = erf_profile_factory(workspace={"markers": {"purple": 1.0}},
                                     channel={"markers": {"purple": 2.5}})
        assert config["lexicon"].markers["purple"] == 2.5

    def test_an_event_file_overrides_the_channel(self, erf_profile_factory):
        config = erf_profile_factory(channel={"markers": {"purple": 2.5}},
                                     event={"markers": {"purple": 0.5}})
        assert config["lexicon"].markers["purple"] == 0.5

    def test_an_event_no_longer_replaces_the_channel_wholesale(self, erf_profile_factory):
        """The behaviour change: before this feature an event's file discarded
        every channel marker. Now it adds to them."""
        config = erf_profile_factory(channel={"markers": {"kesselchen": 2.0}},
                                     event={"markers": {"karussell": 2.0}})
        assert config["lexicon"].markers["kesselchen"] == 2.0
        assert config["lexicon"].markers["karussell"] == 2.0

    def test_an_event_can_disable_a_default_marker(self, erf_profile_factory):
        config = erf_profile_factory(event={"markers": {"big": 0}})
        assert "big" not in config["lexicon"].markers

    def test_a_malformed_layer_is_a_reported_defect_not_a_raise(self, erf_profile_factory):
        problems = erf_profile_factory(channel={"markers": {"crash": -1}},
                                       expect_problems=True)
        assert any("between 0" in p for p in problems)
