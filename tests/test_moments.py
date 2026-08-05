
import pytest

from yt_shorts import moments
from yt_shorts.lexicon import Lexicon
from yt_shorts.moments import _count_markers


class TestOverlappingMarkers:
    """DEFAULT_MARKERS ships 'pole' (0.3), 'super pole' (1.0) and 'pole sitter'
    (0.5) together, and the old substring-per-marker sum double-counted: one
    "super pole" scored 1.3, one "pole sitter" scored 0.8. Longest-match-wins
    with span consumption is what makes the shipped weight the real score."""

    def test_super_pole_scores_only_the_longer_marker(self):
        words = [{"start": 1.0, "end": 1.4, "text": "super"},
                 {"start": 1.5, "end": 1.9, "text": "pole"}]
        assert _count_markers(words, {"pole": 0.3, "super pole": 1.0}) == pytest.approx(1.0)

    def test_pole_sitter_scores_only_the_longer_marker(self):
        words = [{"start": 1.0, "end": 1.4, "text": "pole"},
                 {"start": 1.5, "end": 2.0, "text": "sitter"}]
        assert _count_markers(words, {"pole": 0.3, "pole sitter": 0.5}) == pytest.approx(0.5)

    def test_bare_pole_still_scores_when_no_longer_phrase_matches(self):
        words = [{"start": 1.0, "end": 1.4, "text": "pole"}]
        markers = {"pole": 0.3, "super pole": 1.0, "pole sitter": 0.5}
        assert _count_markers(words, markers) == pytest.approx(0.3)

    def test_two_separate_mentions_of_the_same_phrase_still_score_twice(self):
        words = [{"start": 1.0, "end": 1.4, "text": "super"},
                 {"start": 1.5, "end": 1.9, "text": "pole"},
                 {"start": 2.0, "end": 2.4, "text": "super"},
                 {"start": 2.5, "end": 2.9, "text": "pole"}]
        assert _count_markers(words, {"pole": 0.3, "super pole": 1.0}) == pytest.approx(2.0)

    def test_a_zero_weight_long_marker_does_not_suppress_a_shorter_real_one(self):
        """A disabled marker (weight 0) must not claim a span and hide a marker
        underneath it - suppression is not a designed feature of this lexicon."""
        words = [{"start": 1.0, "end": 1.4, "text": "super"},
                 {"start": 1.5, "end": 1.9, "text": "pole"}]
        assert _count_markers(words, {"super pole": 0.0, "pole": 0.3}) == pytest.approx(0.3)

    def test_non_overlapping_distinct_markers_still_add_up(self):
        words = [{"start": 1.0, "end": 1.4, "text": "crash"},
                 {"start": 1.5, "end": 1.9, "text": "pole"}]
        assert _count_markers(words, {"crash": 3.0, "pole": 0.3}) == pytest.approx(3.3)

    def test_super_pole_sitter_scores_the_longer_phrase_not_the_higher_weight(self):
        """DEFAULT_MARKERS ships 'super pole' (1.0, 10 chars) and 'pole
        sitter' (0.5, 11 chars) both overlapping 'super pole sitter'. The
        LONGER match ('pole sitter') claims the span first and wins, even
        though 'super pole' carries the higher weight - weight is never the
        tie-break for an overlap, only length is (see _count_markers's own
        docstring and CLAUDE.md's moments-lexicon paragraph, which pins this
        exact example)."""
        words = [{"start": 1.0, "end": 1.4, "text": "super"},
                 {"start": 1.5, "end": 1.9, "text": "pole"},
                 {"start": 2.0, "end": 2.4, "text": "sitter"}]
        markers = {"pole": 0.3, "super pole": 1.0, "pole sitter": 0.5}
        assert _count_markers(words, markers) == pytest.approx(0.5)

    def test_a_same_length_overlap_resolves_by_weight_not_insertion_order(self):
        """Two equal-length markers that overlap must resolve deterministically
        regardless of which one a caller happens to list first (in practice:
        which LAYER introduced it) - by weight, never by dict/insertion order.
        "boo" and "oom" (both 3 chars) both literally appear in "boom" and
        overlap (spans (0,3) and (1,4))."""
        words = [{"start": 1.0, "end": 1.4, "text": "boom"}]
        forward = _count_markers(words, {"boo": 1.0, "oom": 5.0})
        backward = _count_markers(words, {"oom": 5.0, "boo": 1.0})
        assert forward == backward == pytest.approx(5.0)  # higher weight wins the tie

    def test_a_same_length_same_weight_overlap_resolves_alphabetically_not_insertion_order(self):
        """With length AND weight tied too, the final tie-break is the marker
        text itself, so the result stops depending on which layer happened to
        insert its marker into the merged dict first."""
        words = [{"start": 1.0, "end": 1.4, "text": "boom"}]
        forward = _count_markers(words, {"boo": 2.0, "oom": 2.0})
        backward = _count_markers(words, {"oom": 2.0, "boo": 2.0})
        assert forward == backward == pytest.approx(2.0)  # "boo" < "oom" alphabetically, wins the tie

    def test_an_empty_markers_dict_scores_zero(self):
        # The `if not markers` early return: no lexicon configured at all.
        words = [{"start": 1.0, "end": 1.4, "text": "crash"}]
        assert _count_markers(words, {}) == 0.0

    def test_a_markers_dict_of_only_disabled_entries_scores_zero(self):
        # The `if not active` early return: every marker present has weight 0,
        # i.e. every layer's marker was disabled rather than the dict being
        # empty - a disabled marker must not even reach the matching loop.
        words = [{"start": 1.0, "end": 1.4, "text": "crash"}]
        assert _count_markers(words, {"crash": 0.0, "pole": 0.0}) == 0.0


class TestActivityCurve:
    def test_one_value_per_step_across_the_stream(self):
        words = [{"start": t, "end": t + 0.5, "text": " x"} for t in range(0, 300)]
        curve = moments.activity_curve(words, Lexicon(markers={}), step=60.0)
        assert len(curve) == 5

    def test_a_dense_minute_scores_above_a_sparse_one(self):
        dense = [{"start": 10 + i * 0.1, "end": 10 + i * 0.1 + 0.05, "text": " x"}
                 for i in range(200)]
        sparse = [{"start": 70 + i * 5.0, "end": 70 + i * 5.0 + 0.05, "text": " x"}
                  for i in range(5)]
        curve = moments.activity_curve(dense + sparse, Lexicon(markers={}), step=60.0)
        assert curve[0] > curve[1]

    def test_no_words_is_an_empty_curve(self):
        assert moments.activity_curve([], Lexicon(markers={}), step=60.0) == []


class TestLexiconMoments:
    def test_speech_rate_alone_never_produces_a_moment(self):
        # 54% of the candidates this replaces were speech-rate only - the
        # pre-race block where "Set up seems to be working" was a moment.
        fast = [{"start": i * 0.2, "end": i * 0.2 + 0.1, "text": " word"}
                for i in range(400)]
        assert moments.lexicon_moments(fast, Lexicon(markers={})) == []

    def test_a_marker_hit_produces_a_moment_with_its_category(self):
        words = [{"start": 10.0, "end": 10.4, "text": " crash"}]
        found = moments.lexicon_moments(words, Lexicon(markers={"crash": 3.0}))
        assert len(found) == 1
        assert found[0].category in moments.CATEGORIES

    def test_the_window_is_derived_from_the_hit_not_fixed_at_twelve_seconds(self):
        words = [{"start": 10.0, "end": 10.4, "text": " crash"},
                 {"start": 10.4, "end": 30.0, "text": " and the car is out"}]
        found = moments.lexicon_moments(words, Lexicon(markers={"crash": 3.0}))
        assert found[0].end - found[0].start != 12.0
        assert found[0].start <= 10.0 <= found[0].end

    def test_the_loudness_engine_is_gone(self):
        # Measured useless, not merely down-weighted. Its return would mean the
        # finding was lost - see the Task 3 measurement note.
        assert not hasattr(moments, "rank_moments")
        assert not hasattr(moments, "measure_loudness_ffmpeg")
        assert not hasattr(moments, "find_candidates")
