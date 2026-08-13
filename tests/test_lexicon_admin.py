"""Reading, updating and adopting the lexicon layers (the studio's write path)."""

import os
import json

import pytest

from yt_shorts import lexicon, lexicon_admin


@pytest.fixture
def root(tmp_path):
    (tmp_path / "channels" / "erf" / "events" / "ev").mkdir(parents=True)
    return tmp_path


def _own(root, *parts):
    return json.loads((root.joinpath(*parts) / "moments.json").read_text(encoding="utf-8"))


class TestRead:
    def test_the_workspace_scope_starts_with_only_the_default(self, root):
        got = lexicon_admin.read(root)
        assert got["scope"] == "workspace"
        assert got["own"] == {}
        assert got["effective"]["crash"] == {"weight": lexicon.DEFAULT_MARKERS["crash"],
                                            "source": "default"}

    def test_own_entries_are_reported_with_their_source(self, root):
        lexicon_admin.update(root, {"kesselchen": 2.0})
        got = lexicon_admin.read(root)
        assert got["own"] == {"kesselchen": 2.0}
        assert got["effective"]["kesselchen"] == {"weight": 2.0, "source": "workspace"}

    def test_a_channel_sees_the_workspace_as_inherited(self, root):
        lexicon_admin.update(root, {"kesselchen": 2.0})
        got = lexicon_admin.read(root, channel="erf")
        assert got["scope"] == "channel"
        assert got["own"] == {}
        assert got["effective"]["kesselchen"]["source"] == "workspace"

    def test_an_event_sees_the_channel_as_inherited(self, root):
        lexicon_admin.update(root, {"purple": 1.0}, channel="erf")
        got = lexicon_admin.read(root, channel="erf", event="ev")
        assert got["scope"] == "event"
        assert got["effective"]["purple"] == {"weight": 1.0, "source": "channel"}

    def test_the_most_specific_source_wins(self, root):
        lexicon_admin.update(root, {"purple": 1.0})
        lexicon_admin.update(root, {"purple": 2.0}, channel="erf")
        lexicon_admin.update(root, {"purple": 0.5}, channel="erf", event="ev")
        got = lexicon_admin.read(root, channel="erf", event="ev")
        assert got["effective"]["purple"] == {"weight": 0.5, "source": "event"}

    def test_a_disabled_marker_survives_into_effective_so_the_ui_can_show_it(self, root):
        """merge_lexicons drops a 0 for SCORING; the editor still needs to see
        that this layer disabled something it inherited."""
        lexicon_admin.update(root, {"big": 0}, channel="erf")
        got = lexicon_admin.read(root, channel="erf")
        assert got["effective"]["big"] == {"weight": 0.0, "source": "channel"}

    def test_an_unknown_channel_is_not_found(self, root):
        with pytest.raises(lexicon_admin.LexiconAdminError) as caught:
            lexicon_admin.read(root, channel="nope")
        assert caught.value.kind == "not_found"

    def test_an_unknown_event_is_not_found(self, root):
        with pytest.raises(lexicon_admin.LexiconAdminError) as caught:
            lexicon_admin.read(root, channel="erf", event="nope")
        assert caught.value.kind == "not_found"

    @pytest.mark.parametrize("bad", ["..", "a/b", ".hidden", ""])
    def test_an_unsafe_segment_is_refused_before_any_filesystem_touch(self, root, bad):
        with pytest.raises(lexicon_admin.LexiconAdminError) as caught:
            lexicon_admin.read(root, channel=bad)
        assert caught.value.kind == "bad_name"


class TestUpdate:
    def test_it_writes_only_its_own_layer(self, root):
        lexicon_admin.update(root, {"purple": 2.0}, channel="erf")
        assert _own(root, "channels", "erf") == {"markers": {"purple": 2.0}}
        assert not (root / "moments.json").exists()

    def test_an_empty_payload_clears_the_layer_explicitly(self, root):
        lexicon_admin.update(root, {"purple": 2.0}, channel="erf")
        lexicon_admin.update(root, {}, channel="erf")
        assert _own(root, "channels", "erf") == {"markers": {}}

    def test_what_it_accepts_profile_load_accepts(self, root):
        lexicon_admin.update(root, {"Safety Car": 2.5})
        path = root / "moments.json"
        assert lexicon.load(path).markers == {"safety car": 2.5}

    @pytest.mark.parametrize("markers", [
        {"crash": -1}, {"crash": 300}, {"crash": "high"}, {"": 1.0}, {"crash": None},
    ])
    def test_a_bad_payload_never_reaches_disk(self, root, markers):
        with pytest.raises(lexicon_admin.LexiconAdminError) as caught:
            lexicon_admin.update(root, markers)
        assert caught.value.kind == "bad_markers"
        assert not (root / "moments.json").exists()


class TestAdoptDefault:
    def test_it_copies_the_default_into_the_workspace_layer(self, root):
        lexicon_admin.adopt_default(root)
        own = _own(root)["markers"]
        assert own == {m: w for m, w in lexicon.DEFAULT_MARKERS.items()}

    def test_it_is_idempotent(self, root):
        lexicon_admin.adopt_default(root)
        first = (root / "moments.json").read_text(encoding="utf-8")
        lexicon_admin.adopt_default(root)
        assert (root / "moments.json").read_text(encoding="utf-8") == first

    def test_the_adopted_entries_are_then_reported_as_own(self, root):
        lexicon_admin.adopt_default(root)
        got = lexicon_admin.read(root)
        assert got["effective"]["crash"]["source"] == "workspace"

    def test_it_preserves_an_existing_own_marker_not_in_the_default(self, root):
        """The critical regression: adopt must be ADDITIVE, never a whole-layer
        overwrite - a custom marker already saved at this layer must survive
        adopting the default alongside it."""
        lexicon_admin.update(root, {"kesselchen": 2.0})
        lexicon_admin.adopt_default(root)
        own = _own(root)["markers"]
        assert own["kesselchen"] == 2.0
        assert own["crash"] == lexicon.DEFAULT_MARKERS["crash"]

    def test_it_preserves_an_existing_own_disable(self, root):
        """A weight-0 own entry (an explicit disable of an inherited default)
        must not be silently re-enabled by adopting the default."""
        lexicon_admin.update(root, {"big": 0})
        lexicon_admin.adopt_default(root)
        assert _own(root)["markers"]["big"] == 0.0

    def test_an_own_weight_wins_over_the_default_for_a_shared_marker(self, root):
        lexicon_admin.update(root, {"crash": 9.0})
        lexicon_admin.adopt_default(root)
        assert _own(root)["markers"]["crash"] == 9.0

    def test_it_is_still_idempotent_with_existing_own_entries(self, root):
        lexicon_admin.update(root, {"kesselchen": 2.0, "big": 0})
        lexicon_admin.adopt_default(root)
        first = (root / "moments.json").read_text(encoding="utf-8")
        lexicon_admin.adopt_default(root)
        assert (root / "moments.json").read_text(encoding="utf-8") == first


class TestReadDegradesAMalformedLayer:
    """Mirrors profile._load_lexicon's own handling of the same failure mode:
    a hand-broken moments.json at one layer is a reported problem, never an
    uncaught raise that would 500 every moments route at this scope."""

    def test_a_malformed_workspace_layer_is_reported_not_raised(self, root):
        (root / "moments.json").write_text("not json", encoding="utf-8")
        got = lexicon_admin.read(root)
        assert got["own"] == {}
        assert got["effective"]["crash"]["source"] == "default"
        assert len(got["problems"]) == 1
        assert "moments.json" in got["problems"][0]

    def test_a_malformed_channel_layer_is_reported_not_raised(self, root):
        (root / "channels" / "erf" / "moments.json").write_text(
            "not json", encoding="utf-8")
        got = lexicon_admin.read(root, channel="erf")
        assert got["own"] == {}
        assert len(got["problems"]) == 1

    def test_a_malformed_event_layer_is_reported_not_raised(self, root):
        (root / "channels" / "erf" / "events" / "ev" / "moments.json").write_text(
            "not json", encoding="utf-8")
        got = lexicon_admin.read(root, channel="erf", event="ev")
        assert got["own"] == {}
        assert len(got["problems"]) == 1

    def test_a_healthy_layer_reports_no_problems(self, root):
        lexicon_admin.update(root, {"crash": 5.0})
        got = lexicon_admin.read(root)
        assert got["problems"] == []

    def test_an_update_still_succeeds_when_a_different_layer_is_broken(self, root):
        """A write to the event layer must not fail just because the CHANNEL
        layer (a layer this call inherits from, but does not write) happens
        to be malformed."""
        (root / "channels" / "erf" / "moments.json").write_text(
            "not json", encoding="utf-8")
        lexicon_admin.update(root, {"pole": 4.0}, channel="erf", event="ev")
        got = lexicon_admin.read(root, channel="erf", event="ev")
        assert got["own"] == {"pole": 4.0}
        assert len(got["problems"]) == 1
        assert "channels" in got["problems"][0]


class TestTheWriteIsAtomic:
    def test_a_failed_update_leaves_the_previous_moments_json_complete(self, root, monkeypatch):
        """Writes through `atomicwrite`, so a reader can never find this file
    empty (see that module's docstring for the CI failure that measured
    the alternative). `os.replace` is the only step that can fail after
    the new bytes exist and before they are in place - failing anything
    earlier would pass under a truncating write too."""
        lexicon_admin.update(root, {"kesselchen": 2.0})
        path = root / "moments.json"
        before = path.read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            lexicon_admin.update(root, {"karussell": 3.0})

        assert path.read_bytes() == before
