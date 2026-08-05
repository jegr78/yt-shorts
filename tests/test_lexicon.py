"""The excitement lexicon: both file shapes, weights, and their validation."""

import json

import pytest

from yt_shorts import lexicon


def _write(tmp_path, payload):
    path = tmp_path / "moments.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert lexicon.load(tmp_path / "nope.json") is lexicon.EMPTY


def test_the_weighted_dict_form_is_read_as_given(tmp_path):
    path = _write(tmp_path, {"markers": {"crash": 3.0, "pole": 0.3}})
    assert lexicon.load(path).markers == {"crash": 3.0, "pole": 0.3}


def test_the_old_flat_list_form_means_weight_one(tmp_path):
    path = _write(tmp_path, {"markers": ["crash", "safety car"]})
    assert lexicon.load(path).markers == {"crash": 1.0, "safety car": 1.0}


def test_markers_are_lowercased(tmp_path):
    path = _write(tmp_path, {"markers": {"Safety Car": 2.5}})
    assert lexicon.load(path).markers == {"safety car": 2.5}


def test_a_duplicate_after_lowercasing_is_refused(tmp_path):
    path = _write(tmp_path, {"markers": {"Crash": 3.0, "crash": 1.0}})
    with pytest.raises(ValueError, match="duplicate"):
        lexicon.load(path)


def test_an_integer_weight_is_accepted_as_a_float(tmp_path):
    path = _write(tmp_path, {"markers": {"crash": 3}})
    markers = lexicon.load(path).markers
    assert markers == {"crash": 3.0}
    assert type(markers["crash"]) is float


def test_zero_is_a_valid_weight_it_means_disabled(tmp_path):
    """A layer disables an inherited marker by giving it weight 0; the value
    must survive load so the merge can act on it (see profile._load_lexicon)."""
    path = _write(tmp_path, {"markers": {"pole": 0}})
    assert lexicon.load(path).markers == {"pole": 0.0}


@pytest.mark.parametrize("weight", [-1, -0.5])
def test_a_negative_weight_is_refused(tmp_path, weight):
    path = _write(tmp_path, {"markers": {"crash": weight}})
    with pytest.raises(ValueError, match="between 0"):
        lexicon.load(path)


def test_a_weight_above_the_cap_is_refused(tmp_path):
    path = _write(tmp_path, {"markers": {"crash": 300}})
    with pytest.raises(ValueError, match="between 0"):
        lexicon.load(path)


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
def test_a_non_finite_weight_is_refused(tmp_path, literal):
    """Python's json accepts these literals as an extension, so they can really
    reach us from a hand-edited file."""
    path = tmp_path / "moments.json"
    path.write_text('{"markers": {"crash": %s}}' % literal, encoding="utf-8")
    with pytest.raises(ValueError, match="finite|between 0"):
        lexicon.load(path)


def test_a_non_numeric_weight_is_refused(tmp_path):
    path = _write(tmp_path, {"markers": {"crash": "high"}})
    with pytest.raises(ValueError, match="number"):
        lexicon.load(path)


@pytest.mark.parametrize("payload", [
    {"markers": {"": 1.0}},
    {"markers": [""]},
    {"markers": [3]},
    {"markers": "crash"},
    {"markers": 7},
    {"markers": {"crash": True}},
    {"markers": None},
    ["crash", "spin"],
])
def test_a_malformed_marker_set_is_refused(tmp_path, payload):
    with pytest.raises(ValueError):
        lexicon.load(_write(tmp_path, payload))


def test_a_payload_without_markers_is_empty(tmp_path):
    assert lexicon.load(_write(tmp_path, {})).markers == {}


def test_a_marker_at_the_length_cap_is_accepted(tmp_path):
    marker = "a" * lexicon.MAX_MARKER_LENGTH
    path = _write(tmp_path, {"markers": {marker: 1.0}})
    assert lexicon.load(path).markers == {marker: 1.0}


def test_a_marker_over_the_length_cap_is_refused(tmp_path):
    marker = "a" * (lexicon.MAX_MARKER_LENGTH + 1)
    path = _write(tmp_path, {"markers": {marker: 1.0}})
    with pytest.raises(ValueError, match="character"):
        lexicon.load(path)


def test_a_one_megabyte_marker_is_refused(tmp_path):
    marker = "a" * 1_000_000
    path = _write(tmp_path, {"markers": {marker: 1.0}})
    with pytest.raises(ValueError, match="character"):
        lexicon.load(path)


@pytest.mark.parametrize("bad_char", ["\n", "\t", "\x00", "\x1f", "\x7f"])
def test_a_marker_with_a_control_character_is_refused(tmp_path, bad_char):
    path = _write(tmp_path, {"markers": {f"crash{bad_char}now": 1.0}})
    with pytest.raises(ValueError, match="control character"):
        lexicon.load(path)


class TestTheConstructorAcceptsBothShapes:
    """Lexicon(markers=[...]) is the call shape the suite has used since D2b;
    it must keep working now that markers are weighted."""

    def test_a_list_becomes_weight_one(self):
        assert Lexicon_markers(["crash", "spin"]) == {"crash": 1.0, "spin": 1.0}

    def test_a_dict_is_kept(self):
        assert Lexicon_markers({"crash": 3.0}) == {"crash": 3.0}

    def test_the_default_is_empty(self):
        assert lexicon.Lexicon().markers == {}


def Lexicon_markers(markers):
    return lexicon.Lexicon(markers=markers).markers


class TestTheRacingDefault:
    def test_it_is_a_weighted_dict(self):
        assert isinstance(lexicon.DEFAULT_MARKERS, dict)
        assert all(isinstance(m, str) and m == m.lower() and m
                   for m in lexicon.DEFAULT_MARKERS)
        assert all(isinstance(w, float) and 0 < w <= lexicon.MAX_WEIGHT
                   for w in lexicon.DEFAULT_MARKERS.values())

    def test_it_survives_its_own_validation(self):
        """Whatever ships must be something load() would accept."""
        assert lexicon.Lexicon(markers=lexicon.DEFAULT_MARKERS).markers == lexicon.DEFAULT_MARKERS

    def test_incidents_outweigh_ambient_vocabulary(self):
        """The whole point of weights: measured on a real 98-min qualifying
        transcript, 'pole' occurs 19x as ordinary chatter while a crash is an
        event. An unweighted list made every 'pole' mention a candidate."""
        assert lexicon.DEFAULT_MARKERS["crash"] > lexicon.DEFAULT_MARKERS["pole"] * 5

    def test_it_covers_all_three_bands(self):
        for marker in ("crash", "safety car", "purple", "overtake", "oh my", "wow"):
            assert marker in lexicon.DEFAULT_MARKERS, marker
