import json

import os
import pytest

from yt_shorts import editorial
from yt_shorts.editorial import (
    CANDIDATE, DISCARDED, EDIT_FILENAME, Edit, EditError,
    checksum, effective_title, effective_words, load, save)


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


DERIVED = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
CORRECTED = [w(0.0, 1.0, " Rei Racing")]


class TestAnUntouchedClip:
    def test_no_file_means_defaults(self, tmp_path):
        edit = load(tmp_path)
        assert edit.title is None
        assert edit.status == CANDIDATE
        assert edit.transcript is None

    def test_loading_does_not_create_the_file(self, tmp_path):
        load(tmp_path)
        assert not (tmp_path / EDIT_FILENAME).exists()


class TestSaving:
    def test_saving_writes_only_what_was_set(self, tmp_path):
        save(tmp_path, Edit(title="Abschied von Speedy", status=CANDIDATE,
                            transcript=None))
        payload = json.loads((tmp_path / EDIT_FILENAME).read_text(encoding="utf-8"))
        assert payload == {"title": "Abschied von Speedy", "status": CANDIDATE}

    def test_a_saved_edit_reloads_unchanged(self, tmp_path):
        original = Edit(title="Titel", status=DISCARDED,
                        transcript={"based_on": checksum(DERIVED),
                                    "words": CORRECTED})
        save(tmp_path, original)
        assert load(tmp_path) == original


class TestRejectingBrokenFiles:
    def test_invalid_json_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(EditError):
            load(tmp_path)

    def test_an_unknown_status_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({"status": "maybe"}), encoding="utf-8")
        with pytest.raises(EditError) as error:
            load(tmp_path)
        assert "maybe" in str(error.value)

    def test_a_transcript_without_based_on_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({"transcript": {"words": CORRECTED}}), encoding="utf-8")
        with pytest.raises(EditError):
            load(tmp_path)

    def test_a_non_dict_word_entry_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({
                "transcript": {
                    "based_on": checksum(CORRECTED),
                    "words": [" very"]  # string instead of dict
                }
            }), encoding="utf-8")
        with pytest.raises(EditError) as error:
            load(tmp_path)
        assert "entry 0" in str(error.value).lower()

    def test_a_word_entry_missing_a_required_key_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({
                "transcript": {
                    "based_on": checksum(CORRECTED),
                    "words": [{"start": 0.0, "end": 0.5}]  # missing 'text'
                }
            }), encoding="utf-8")
        with pytest.raises(EditError) as error:
            load(tmp_path)
        assert "entry 0" in str(error.value).lower()

    def test_a_word_entry_with_non_numeric_timestamp_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({
                "transcript": {
                    "based_on": checksum(CORRECTED),
                    "words": [{"start": "zero", "end": 0.5, "text": " test"}]
                }
            }), encoding="utf-8")
        with pytest.raises(EditError) as error:
            load(tmp_path)
        assert "entry 0" in str(error.value).lower() and "start" in str(error.value).lower()


class TestChecksum:
    def test_the_same_words_give_the_same_checksum(self):
        assert checksum(DERIVED) == checksum(list(DERIVED))

    def test_different_words_give_different_checksums(self):
        assert checksum(DERIVED) != checksum(CORRECTED)

    def test_the_checksum_names_its_algorithm(self):
        assert checksum(DERIVED).startswith("sha256:")

    def test_checksum_is_independent_of_key_order(self):
        """Verifies that sort_keys=True works: same content, different key order."""
        # Create two word lists with same content but different key order
        words_a = [{"start": 0.0, "end": 0.5, "text": " very"}]
        words_b = [{"text": " very", "end": 0.5, "start": 0.0}]
        # They must hash identically because sort_keys=True normalizes the JSON
        assert checksum(words_a) == checksum(words_b)

    def test_checksum_normalizes_int_and_float_timestamps(self):
        """Verifies that 1 and 1.0 hash identically."""
        words_int = [{"start": 1, "end": 2, "text": " test"}]
        words_float = [{"start": 1.0, "end": 2.0, "text": " test"}]
        assert checksum(words_int) == checksum(words_float)

    def test_checksum_normalizes_mixed_int_float(self):
        """Verifies mixed int/float timestamps normalize correctly."""
        words_mixed = [{"start": 0, "end": 1.0, "text": " mixed"}]
        words_all_float = [{"start": 0.0, "end": 1.0, "text": " mixed"}]
        assert checksum(words_mixed) == checksum(words_all_float)


class TestEffectiveTitle:
    def test_without_an_override_the_harvested_title_is_used(self):
        assert effective_title(load_default(), "Speedy!") == "Speedy!"

    def test_an_override_wins(self):
        assert effective_title(Edit(title="Neu", status=CANDIDATE,
                                    transcript=None), "Speedy!") == "Neu"


def load_default():
    return Edit(title=None, status=CANDIDATE, transcript=None)


class TestEffectiveWords:
    def test_without_corrections_the_derived_words_are_used(self):
        words, conflict = effective_words(load_default(), DERIVED)
        assert words == DERIVED
        assert conflict is False

    def test_corrections_win_and_do_not_conflict_when_still_current(self):
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        words, conflict = effective_words(edit, DERIVED)
        assert words == CORRECTED
        assert conflict is False

    def test_a_changed_derived_transcript_is_a_conflict_and_corrections_still_win(self):
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        changed = DERIVED + [w(1.0, 1.4, " more")]
        words, conflict = effective_words(edit, changed)
        assert words == CORRECTED     # hand work is never dropped
        assert conflict is True

    def test_corrections_without_any_derived_transcript_are_not_a_conflict(self):
        # Nothing exists to contradict them.
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        words, conflict = effective_words(edit, None)
        assert words == CORRECTED
        assert conflict is False

    def test_no_corrections_and_no_derived_transcript_yields_nothing(self):
        words, conflict = effective_words(load_default(), None)
        assert words == []
        assert conflict is False


class TestWindowOverride:
    def test_absent_uses_the_clip_window(self):
        from yt_shorts.editorial import Edit, effective_window
        edit = Edit(title=None, status="candidate", transcript=None, window=None)
        assert effective_window(edit, 92.0, 104.0) == (92.0, 104.0)

    def test_present_window_wins(self):
        from yt_shorts.editorial import Edit, effective_window
        edit = Edit(title=None, status="candidate", transcript=None, window=(95.0, 108.0))
        assert effective_window(edit, 92.0, 104.0) == (95.0, 108.0)

    def test_window_round_trips_through_save_and_load(self, tmp_path):
        from yt_shorts.editorial import Edit, load, save
        save(tmp_path, Edit(title=None, status="kept", transcript=None, window=(95.0, 108.0)))
        assert load(tmp_path).window == (95.0, 108.0)


def test_upload_override_roundtrips(tmp_path):
    editorial.save(tmp_path, editorial.Edit(
        title=None, status=editorial.KEPT, transcript=None,
        upload={"description": "My short", "tags": ["gt7", "erf"], "category_id": "20"}))
    loaded = editorial.load(tmp_path)
    assert loaded.upload == {"description": "My short", "tags": ["gt7", "erf"], "category_id": "20"}


def test_upload_absent_is_omitted(tmp_path):
    editorial.save(tmp_path, editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None))
    payload = json.loads((tmp_path / editorial.EDIT_FILENAME).read_text())
    assert "upload" not in payload


def test_upload_bad_type_rejected(tmp_path):
    (tmp_path / editorial.EDIT_FILENAME).write_text('{"status": "kept", "upload": {"tags": "nope"}}')
    with pytest.raises(editorial.EditError):
        editorial.load(tmp_path)


def test_effective_upload_merges_clip_over_config():
    edit = editorial.Edit(title=None, status=editorial.KEPT, transcript=None,
                          upload={"description": "clip desc", "tags": ["a"]})
    config = {"upload": {"description": "tmpl", "tags": ["x"], "category_id": "20",
                         "made_for_kids": False, "mode": "api"}}
    eff = editorial.effective_upload(edit, config)
    assert eff["description"] == "clip desc"        # clip wins
    assert eff["tags"] == ["a"]                      # clip replaces
    assert eff["category_id"] == "20"                # from config
    assert "mode" not in eff                          # mode is not upload metadata


class TestNormaliseWordBoundaries:
    """faster-whisper marks a word START with a leading space, and
    captions._to_caption joins the tokens with "" to rely on that - which is
    what makes " C" + ".L" + ".R." render as C.L.R. rather than C .L .R.
    A human typing into the studio's text field types "Rei", not " Rei", so a
    hand-corrected word used to glue itself to its predecessor and render as
    IT'SREIRACING.
    """

    def test_a_hand_typed_word_gains_one_leading_space(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "Rei"}])
        assert out[0]["text"] == " Rei"

    def test_a_word_that_already_has_one_gains_none(self):
        """The property that makes the whole thing idempotent."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": " Rei"}])
        assert out[0]["text"] == " Rei"

    def test_normalising_twice_equals_normalising_once(self):
        words = [{"start": 0.0, "end": 1.0, "text": "Rei"},
                 {"start": 1.0, "end": 2.0, "text": " Racing"},
                 {"start": 2.0, "end": 3.0, "text": ".L"},
                 {"start": 3.0, "end": 4.0, "text": ""}]
        once = editorial.normalise_word_boundaries(words)
        assert editorial.normalise_word_boundaries(once) == once

    def test_a_double_space_is_unreachable(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "  Rei"}])
        assert out[0]["text"] == "  Rei"  # left alone, never made worse

    @pytest.mark.parametrize("text", [".L", ".R.", ".5", ".57", "-qualifying", "-up."])
    def test_the_measured_continuation_shapes_are_untouched(self, text):
        """Built from a census of this workspace's real transcripts: of 11518
        decoder tokens, 91 carry no leading space, and every one of them starts
        with '.' (71) or '-' (20). Those are continuations - "C.L.R.", "1.5",
        "pre-qualifying" - and splitting them is the regression this pins.
        """
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == text

    @pytest.mark.parametrize("text", ["Ähnlich", "Überholmanöver", "Öl"])
    def test_a_german_proper_noun_gains_its_boundary(self, text):
        """isalnum() is Unicode-aware, and it has to be: the corrections in
        this project are full of German names."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == " " + text

    def test_a_digit_led_word_gains_its_boundary(self):
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": "7"}])
        assert out[0]["text"] == " 7"

    @pytest.mark.parametrize("text", ["", " ", "   "])
    def test_empty_and_whitespace_only_are_left_alone(self, text):
        """An empty correction is the operator clearing a word;
        captions.group_words already skips it."""
        out = editorial.normalise_word_boundaries([{"start": 0.0, "end": 1.0, "text": text}])
        assert out[0]["text"] == text

    def test_timings_travel_through_untouched(self):
        out = editorial.normalise_word_boundaries([{"start": 1.25, "end": 2.5, "text": "Rei"}])
        assert (out[0]["start"], out[0]["end"]) == (1.25, 2.5)

    def test_the_input_is_not_mutated(self):
        words = [{"start": 0.0, "end": 1.0, "text": "Rei"}]
        editorial.normalise_word_boundaries(words)
        assert words[0]["text"] == "Rei"

    def test_an_unknown_key_survives(self):
        """The dicts reaching this come from a Pydantic model today, but the
        function must not quietly drop a field it does not know about."""
        out = editorial.normalise_word_boundaries(
            [{"start": 0.0, "end": 1.0, "text": "Rei", "probability": 0.9}])
        assert out[0]["probability"] == 0.9

    def test_an_empty_list_is_an_empty_list(self):
        assert editorial.normalise_word_boundaries([]) == []

    def test_the_reported_bug_renders_correctly_end_to_end(self):
        """The operator's own token sequence, straight out of their edit.json.
        Before the fix this rendered "and it'sReiRacing"."""
        from yt_shorts.captions import group_words
        words = [{"start": 1.18, "end": 2.78, "text": " and"},
                 {"start": 2.78, "end": 3.16, "text": " it's"},
                 {"start": 3.16, "end": 3.66, "text": "Rei"},
                 {"start": 3.70, "end": 4.02, "text": "Racing"}]
        assert [c.text for c in group_words(words)] == ["and it'sReiRacing"]
        fixed = editorial.normalise_word_boundaries(words)
        assert [c.text for c in group_words(fixed)] == ["and it's Rei Racing"]


class TestTrim:
    def test_absent_trim_loads_as_none(self, tmp_path):
        (tmp_path / "edit.json").write_text('{"status": "candidate"}')
        assert editorial.load(tmp_path).trim is None

    def test_a_trim_round_trips(self, tmp_path):
        editorial.save(tmp_path, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(3.0, 2.0)))
        assert editorial.load(tmp_path).trim == (3.0, 2.0)

    def test_a_zero_trim_is_still_written_and_read_back(self, tmp_path):
        # (0, 0) and None mean the same thing to every consumer, but an
        # operator who typed two zeros has made a decision; save must not
        # quietly drop it and make the field flicker back to "unset".
        editorial.save(tmp_path, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(0.0, 0.0)))
        assert editorial.load(tmp_path).trim == (0.0, 0.0)

    def test_a_non_pair_is_reported(self, tmp_path):
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [1.0]}')
        with pytest.raises(editorial.EditError) as error:
            editorial.load(tmp_path)
        assert "trim" in str(error.value)

    def test_a_negative_value_is_reported(self, tmp_path):
        # Negative would mean EXTENDING the short, and there is no material
        # for that - the master is exactly as long as it is.
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [-1.0, 0.0]}')
        with pytest.raises(editorial.EditError):
            editorial.load(tmp_path)

    def test_a_boolean_is_not_a_number(self, tmp_path):
        # bool is a subclass of int in Python; without an explicit check
        # `true` would load as 1.0 second.
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [true, 0.0]}')
        with pytest.raises(editorial.EditError):
            editorial.load(tmp_path)

    def test_effective_trim_defaults_to_zeros(self):
        edit = editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None)
        assert editorial.effective_trim(edit) == (0.0, 0.0)

    def test_effective_trim_returns_the_override(self):
        edit = editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None,
                              trim=(1.5, 0.5))
        assert editorial.effective_trim(edit) == (1.5, 0.5)


class TestValidateTrim:
    def test_none_is_always_valid(self):
        editorial.validate_trim(None, 10.0, "trim")

    def test_a_trim_leaving_enough_is_valid(self):
        editorial.validate_trim([3.0, 2.0], 20.0, "trim")

    def test_a_trim_leaving_less_than_the_floor_is_reported(self):
        # 20 - 9 - 9 = 2.0, below MIN_REMAINING_SECONDS.
        with pytest.raises(editorial.EditError) as error:
            editorial.validate_trim([9.0, 9.0], 20.0, "trim")
        assert "3.0" in str(error.value)

    def test_the_floor_itself_is_allowed(self):
        # 0.1 + 17.8 + MIN_REMAINING_SECONDS(3.0) is mathematically 20.9 -
        # exactly the floor - but float64 addition computes
        # 20.900000000000002, 3.552713678800501e-15 above the literal 20.9.
        # Without the epsilon in validate_trim this call raises EditError;
        # 8.5 + 8.5 + 3.0 (the previous operands) lands on 20.0 with NO
        # rounding error at all, so that version of this test stayed green
        # even with the epsilon deleted - verified by temporarily removing
        # it and re-running.
        editorial.validate_trim([0.1, 17.8], 20.9, "trim")

    def test_a_trim_longer_than_the_clip_is_reported(self):
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([30.0, 0.0], 20.0, "trim")

    # validate_trim is the function that will receive raw HTTP request
    # data (the studio's PATCH route), so it must not trust its caller's
    # shape - mirroring validate_upload_override. These three reproduce
    # exactly what a reviewer found before the shape check was added:
    # a bool silently becoming 1.0s, a short list raising a bare
    # IndexError, and non-numeric entries raising a bare ValueError.
    def test_a_bool_is_not_silently_a_number(self):
        # bool is a subclass of int in Python; without an explicit check
        # this used to pass validation with True read as 1.0 second - the
        # same trap load's own trim check has a comment about.
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([True, 0.0], 20.0, "trim")

    def test_a_short_list_raises_edit_error_not_index_error(self):
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([1.0], 20.0, "trim")

    def test_non_numeric_entries_raise_edit_error_not_value_error(self):
        with pytest.raises(editorial.EditError):
            editorial.validate_trim(["a", "b"], 20.0, "trim")

    def test_infinity_is_rejected_even_against_an_infinite_duration(self):
        # json.loads accepts `Infinity` as a stdlib extension of the JSON
        # grammar, so a hand-edited edit.json can carry one straight past
        # the old `v >= 0` shape check (float("inf") >= 0 is True). Against
        # a FINITE duration the duration comparison happens to catch it
        # anyway (inf + tail + floor > any finite duration) - which is why
        # this is not reachable through the PATCH route and must be tested
        # against `load`'s own duration=float("inf") call (see its
        # docstring: "no head+tail+MIN_REMAINING_SECONDS is ever greater
        # than infinity, so this call can only reject on shape"). Against
        # that same infinite duration, `inf > inf` is False, so the
        # duration check does NOT catch it either - only the shape check's
        # own math.isfinite guard does, which is the one this test pins.
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([float("inf"), 0.0], float("inf"), "trim")

    def test_load_refuses_a_hand_edited_infinite_trim(self, tmp_path):
        # The actually-reachable path: a hand-edited edit.json with
        # {"trim": [Infinity, 0.0]} loaded through `load`, which calls
        # validate_trim with duration=float("inf") and therefore relies on
        # the shape check alone.
        directory = tmp_path / "clip"
        directory.mkdir()
        (directory / editorial.EDIT_FILENAME).write_text(
            '{"title": null, "status": "candidate", "transcript": null, '
            '"trim": [Infinity, 0.0]}')
        with pytest.raises(editorial.EditError):
            editorial.load(directory)

    def test_negative_infinity_and_nan_are_already_rejected(self):
        # Claimed by the existing `>= 0` clause alone: NaN >= 0 is always
        # False, and -inf >= 0 is False. Checked here rather than assumed,
        # since the Infinity fix above touches this same condition.
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([float("-inf"), 0.0], 20.0, "trim")
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([float("nan"), 0.0], 20.0, "trim")


class TestTheWriteIsAtomic:
    def test_a_failed_save_leaves_the_previous_edit_json_complete(self, tmp_path, monkeypatch):
        """Writes through `atomicwrite`, so a reader can never find this file
    empty (see that module's docstring for the CI failure that measured
    the alternative). `os.replace` is the only step that can fail after
    the new bytes exist and before they are in place - failing anything
    earlier would pass under a truncating write too."""
        save(tmp_path, Edit(title="first", status=CANDIDATE, transcript=None))
        path = tmp_path / "edit.json"
        before = path.read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            save(tmp_path, Edit(title="second", status=CANDIDATE, transcript=None))

        assert path.read_bytes() == before
