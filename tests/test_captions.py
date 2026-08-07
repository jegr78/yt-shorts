import pytest

from yt_shorts.captions import (
    Caption,
    DEFAULT_MAX_SECONDS,
    DEFAULT_MAX_WORDS,
    group_words,
    _merge_orphans,
    _forward_function_words,
    _is_function_word,
    _to_caption,
    FUNCTION_WORDS,
)


def w(start, end, text):
    """Shorthand for a word as transcribe.py emits it."""
    return {"start": start, "end": end, "text": text}


class TestGrouping:
    def test_empty_input_yields_nothing(self):
        assert group_words([]) == []

    def test_fewer_words_than_the_limit_form_one_caption(self):
        words = [w(0.0, 0.2, " it"), w(0.2, 0.5, " drives")]
        assert group_words(words, max_words=3) == [Caption(0.0, 0.5, "it drives")]

    def test_splits_at_the_word_limit(self):
        words = [w(i * 0.3, i * 0.3 + 0.3, f" w{i}") for i in range(6)]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["w0 w1 w2", "w3 w4 w5"]

    def test_splits_before_a_group_would_exceed_the_time_limit(self):
        """[one, two] would span 2.4s, past the 1.6s limit, so the group is
        closed BEFORE 'two' joins it. [two, three] spans exactly 1.6s and is
        therefore still allowed."""
        words = [w(0.0, 1.0, " one"), w(1.0, 2.4, " two"), w(2.4, 2.6, " three")]
        captions = group_words(words, max_words=99, max_seconds=1.6)
        assert [c.text for c in captions] == ["one", "two three"]

    def test_a_group_never_exceeds_the_time_limit(self):
        words = [w(i * 0.5, i * 0.5 + 0.5, f" w{i}") for i in range(20)]
        captions = group_words(words, max_words=99, max_seconds=1.6)
        assert all(c.end - c.start <= 1.6 for c in captions)

    def test_sentence_punctuation_closes_a_group_early(self):
        words = [w(0.0, 0.3, " no,"), w(0.3, 0.6, " what"), w(0.6, 0.9, " is")]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["no,", "what is"]

    def test_timestamps_span_first_to_last_word(self):
        words = [w(1.5, 1.8, " a"), w(1.8, 2.9, " b")]
        assert group_words(words, max_words=2)[0] == Caption(1.5, 2.9, "a b")

    def test_leading_whitespace_is_stripped(self):
        assert group_words([w(0.0, 0.2, "  hello  ")])[0].text == "hello"

    def test_blank_words_are_dropped(self):
        words = [w(0.0, 0.2, " a"), w(0.2, 0.3, "   "), w(0.3, 0.5, " b")]
        assert [c.text for c in group_words(words, max_words=9)] == ["a b"]

    def test_a_single_word_longer_than_the_time_limit_still_becomes_a_caption(self):
        assert group_words([w(0.0, 5.0, " loooong")], max_seconds=1.6) == [
            Caption(0.0, 5.0, "loooong")
        ]

    def test_captions_never_overlap_and_stay_in_order(self):
        # Built cumulatively (each word's start is the previous word's end)
        # so the timestamps are exactly contiguous. `i * 0.3` independent
        # float multiplications are not: word 5 ends at 1.8 (exact) while
        # word 6 starts at 1.7999999999999998, a ~2e-16 false overlap that
        # has nothing to do with the grouping logic under test.
        words = []
        t = 0.0
        for i in range(20):
            end = t + 0.3
            words.append(w(t, end, f" w{i}"))
            t = end
        captions = group_words(words, max_words=3, max_seconds=1.6)
        assert all(a.end <= b.start for a, b in zip(captions, captions[1:], strict=False))
        assert all(c.end >= c.start for c in captions)

    def test_a_real_gap_between_utterances_survives(self):
        """The second caption must start at its own first word's timestamp,
        not be dragged back to the previous caption's end. Doing the latter
        silently discards real silence and desyncs burned-in subtitles."""
        words = [
            w(0.0, 0.3, " one"), w(0.3, 0.6, " two"), w(0.6, 0.9, " three"),
            w(5.0, 5.3, " four"), w(5.3, 5.6, " five"),
        ]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["one two three", "four five"]
        assert captions[1].start == 5.0

    def test_every_word_survives_grouping(self):
        words = [w(i * 0.3, i * 0.3 + 0.3, f" w{i}") for i in range(20)]
        captions = group_words(words, max_words=3, max_seconds=1.6)
        assert " ".join(c.text for c in captions).split() == [f"w{i}" for i in range(20)]


class TestMultiTokenWords:
    """A single word dict may itself contain internal whitespace (Whisper
    can emit " Y Z W" as one entry). Grouping must key everything off word
    dicts, never off `len(text.split())` of already-joined text - the two
    diverge the moment a dict's own text contains a space, and re-deriving
    membership from the wrong count duplicates or drops words.
    """

    def test_a_multi_token_word_does_not_duplicate_or_lose_tokens(self):
        words = [
            w(0.0, 0.2, " X"), w(0.2, 0.8, " Y Z W"),
            w(0.8, 1.0, " blue"), w(1.0, 1.2, " to"),
            w(1.2, 1.4, " green"), w(1.4, 1.6, " sky"),
        ]
        captions = group_words(words, max_words=2, max_seconds=99.0)
        # Every input token appears exactly once, in order, across all
        # captions - regardless of how a multi-token dict got grouped.
        assert " ".join(c.text for c in captions).split() == [
            "X", "Y", "Z", "W", "blue", "to", "green", "sky",
        ]

    def test_a_multi_token_word_does_not_defeat_function_word_forwarding(self):
        words = [
            w(0.0, 0.2, " He"), w(0.2, 1.0, " ran to the store"),
            w(1.0, 1.2, " at"), w(1.2, 1.5, " noon"),
        ]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        # No caption but the last may end on a function word ("at" must
        # not be stranded at the end of a non-final caption).
        assert not any(
            c.text.split()[-1].rstrip(".!?,;:").lower() in FUNCTION_WORDS
            for c in captions[:-1]
        )
        assert " ".join(c.text for c in captions).split() == [
            "He", "ran", "to", "the", "store", "at", "noon",
        ]

    def test_a_multi_token_word_alone_in_a_group_is_recognised_as_an_orphan(self):
        """A caption made of exactly one word dict is an orphan - eligible
        for merging - regardless of how many whitespace-separated tokens
        that single dict's own text happens to contain. The merged-in word
        dict must contribute its own single start/end, and the untouched
        neighbour on the far side must keep its own timestamps exactly."""
        words = [
            w(0.0, 0.2, " a"), w(0.2, 0.4, " b"),
            w(0.4, 0.7, " Y Z W,"),
            w(0.7, 0.9, " c"), w(0.9, 1.1, " d"),
        ]
        captions = group_words(words, max_words=2, max_seconds=99.0)
        assert captions == [
            Caption(0.0, 0.7, "a b Y Z W,"),
            Caption(0.7, 1.1, "c d"),
        ]


class TestWhisperSpacing:
    """_to_caption must join word dicts using each dict's own text, not
    strip-and-rejoin-with-space: Whisper marks the START of a new word
    with a leading space, but a continuation token (part of the same word)
    carries none. Reproduces the real case measured on
    channels/erf/events/community-clips-back-catalogue/transcripts/speedy.json
    - "friend", " C", ".L", ".R." decode as four separate word dicts, the
    last two with no leading space, and used to render as "friend C .L
    .R." instead of the intended "friend C.L.R."
    """

    def test_a_continuation_token_is_not_split_into_its_own_word(self):
        words = [
            w(9.44, 9.8, " friend"), w(9.8, 10.12, " C"),
            w(10.12, 10.24, ".L"), w(10.24, 10.44, ".R."),
        ]
        assert _to_caption(words).text == "friend C.L.R."


class TestOrphanMerging:
    """A group of a single word dict ("orphan") is folded into a neighbour
    where possible, so it never has to hold the screen alone. These test
    the merge sub-routine directly with hand-built groups of word dicts:
    constructing a case where BOTH neighbours are genuinely eligible is not
    reachable through group_words's own splitting rules, because a
    mid-list orphan can only be produced by a time- or punctuation-forced
    split, and both of those make merging back across that exact boundary
    provably impossible (the same inequality that forced the split forbids
    undoing it). Direct construction is the only way to exercise that
    branch.

    Word texts here carry a leading space on every word, mirroring
    Whisper's own word-boundary marker (see TestWhisperSpacing) - not just
    for realism: since _to_caption now joins word dicts with "" and only
    strips the caption's own outer edges (fix for finding F2), a bare word
    text with no leading space at all would run into its neighbour with no
    space between them.
    """

    def test_orphan_merges_into_the_shorter_neighbour(self):
        groups = [
            [w(0.0, 0.3, " aa"), w(0.3, 0.6, " bb"), w(0.6, 0.9, " cc")],
            [w(1.0, 1.2, " dd")],          # merging in: span 1.2 - 0.0 = 1.2
            [w(1.25, 1.35, " ee")],        # merging in: span 1.35 - 1.0 = 0.35
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa bb cc", "dd ee"]
        assert captions[1] == Caption(1.0, 1.35, "dd ee")

    def test_orphan_merge_tie_prefers_the_preceding_neighbour(self):
        # Timestamps chosen to be exactly representable in binary floating
        # point (quarter-second steps), so the tie is exact and not an
        # artefact of float rounding.
        groups = [
            [w(0.0, 0.1, " aa"), w(0.1, 0.25, " xx")],   # merging in: span 1.0 - 0.0 = 1.0
            [w(0.75, 1.0, " bb")],
            [w(1.0, 1.4, " cc"), w(1.4, 1.75, " yy")],   # merging in: span 1.75 - 0.75 = 1.0
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa xx bb", "cc yy"]
        assert captions[0] == Caption(0.0, 1.0, "aa xx bb")

    def test_orphan_merges_into_previous_when_only_previous_is_eligible(self):
        groups = [
            [w(0.0, 0.3, " aa"), w(0.3, 0.6, " bb"), w(0.6, 0.9, " cc")],
            [w(1.0, 1.1, " dd")],
            [w(1.15, 5.0, " ee"), w(5.0, 10.0, " ff")],   # would need 9.0s to absorb - refused
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=2.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa bb cc dd", "ee ff"]
        assert captions[0] == Caption(0.0, 1.1, "aa bb cc dd")

    def test_orphan_merges_into_following_when_only_following_is_eligible(self):
        groups = [
            [w(0.0, 10.0, " aa")],        # would need 10.2s to absorb - refused
            [w(10.1, 10.2, " ee")],
            [w(10.25, 10.4, " ff")],
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=2.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa", "ee ff"]
        assert captions[1] == Caption(10.1, 10.4, "ee ff")

    def test_orphan_with_no_eligible_neighbour_stays_alone(self):
        groups = [
            [w(0.0, 10.0, "aa")],
            [w(15.0, 15.1, "bb")],
            [w(20.0, 20.1, "cc")],
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=2.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa", "bb", "cc"]

    def test_a_merge_exceeding_max_seconds_is_refused(self):
        groups = [
            [w(0.0, 0.5, " aa"), w(0.5, 1.0, " bb")],
            [w(1.0, 1.7, " cc")],   # merging in would span 1.7s, over the 1.6s limit
        ]
        result = _merge_orphans(groups, max_words=4, max_seconds=1.6)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["aa bb", "cc"]

    def test_merge_never_produces_more_than_max_words_plus_one(self):
        words = [w(i * 0.2, i * 0.2 + 0.2, f" w{i}") for i in range(40)]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert all(len(c.text.split()) <= 4 for c in captions)

    def test_orphan_merge_never_crosses_a_punctuation_boundary(self):
        """The punctuation rule intentionally splits here, so the merge
        rule must not silently undo it by pulling "no," back together
        with what follows."""
        words = [w(0.0, 0.3, " no,"), w(0.3, 0.6, " what"), w(0.6, 0.9, " is")]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["no,", "what is"]


class TestFunctionWordMatching:
    """_is_function_word is the matcher the forwarding rule uses to decide
    whether a trailing word should be moved. It must ignore case and
    trailing punctuation."""

    def test_matches_a_known_function_word(self):
        assert _is_function_word("the")

    def test_matches_regardless_of_case(self):
        assert _is_function_word("The")
        assert _is_function_word("THE")

    def test_matches_with_trailing_punctuation_stripped(self):
        assert _is_function_word("the,")
        assert _is_function_word("The,")

    def test_does_not_match_a_content_word(self):
        assert not _is_function_word("penalty")
        assert not _is_function_word("Ray")


class TestFunctionWordForwarding:
    """A caption should not end on a function word, since it reads as
    pointing forward to the word that follows. _forward_function_words
    moves up to two trailing function-word dicts into the following
    group - but only when doing so does not recreate an orphan, does not
    push the following group past its limits, and does not cross a
    boundary the punctuation rule deliberately created.

    Constructed directly (lists of word dicts) rather than through
    group_words, mirroring the orphan-merging tests: this gives full
    control over word-level timestamps needed to verify the exact
    start/end split, and lets each refusal condition be exercised in
    isolation.

    Word texts carry a leading space on every word, mirroring Whisper's
    own word-boundary marker - see TestWhisperSpacing and the note on
    TestOrphanMerging.
    """

    def test_a_single_trailing_function_word_is_moved(self):
        groups = [
            [w(0.0, 0.3, " pick"), w(0.3, 0.6, " up"), w(0.6, 1.0, " a")],
            [w(1.0, 1.5, " penalty"), w(1.5, 2.0, " as")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["pick up", "a penalty as"]
        assert captions[0] == Caption(0.0, 0.6, "pick up")
        assert captions[1] == Caption(0.6, 2.0, "a penalty as")

    def test_two_trailing_function_words_are_both_moved(self):
        groups = [
            [w(0.0, 0.3, " we"), w(0.3, 0.7, " stay"), w(0.7, 1.0, " with"), w(1.0, 1.4, " the")],
            [w(1.4, 1.9, " car"), w(1.9, 2.4, " park")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["we stay", "with the car park"]
        assert captions[0] == Caption(0.0, 0.7, "we stay")
        assert captions[1] == Caption(0.7, 2.4, "with the car park")

    def test_three_trailing_function_words_only_two_are_moved(self):
        groups = [
            [w(0.0, 0.2, " cat"), w(0.2, 0.4, " sat"), w(0.4, 0.6, " on"),
             w(0.6, 0.8, " to"), w(0.8, 1.0, " the")],
            [w(1.0, 1.3, " dog"), w(1.3, 1.6, " run")],
        ]
        result = _forward_function_words(groups, max_words=9, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        # "on" stays behind - it is a function word too, but the run is
        # capped at two so a caption cannot be hollowed out entirely.
        assert [c.text for c in captions] == ["cat sat on", "to the dog run"]

    def test_move_refused_when_source_would_drop_below_two_words(self):
        groups = [
            [w(0.0, 0.3, " is"), w(0.3, 0.6, " the")],
            [w(0.6, 1.0, " fine")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["is the", "fine"]

    def test_move_refused_when_destination_would_exceed_max_seconds(self):
        groups = [
            [w(0.0, 0.3, " pick"), w(0.3, 0.6, " up"), w(0.6, 1.0, " a")],
            [w(1.0, 3.5, " penalty")],
        ]
        result = _forward_function_words(groups, max_words=9, max_seconds=2.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["pick up a", "penalty"]

    def test_move_refused_when_destination_would_exceed_max_words_plus_one(self):
        groups = [
            [w(0.0, 0.3, " pick"), w(0.3, 0.6, " up"), w(0.6, 1.0, " a")],
            [w(1.0, 1.3, " penalty"), w(1.3, 1.6, " as"), w(1.6, 1.9, " that")],
        ]
        # max_words=2 -> destination may hold at most 3 words; it already has 3.
        result = _forward_function_words(groups, max_words=2, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["pick up a", "penalty as that"]

    def test_final_caption_keeps_a_trailing_function_word(self):
        groups = [
            [w(0.0, 0.5, " double"), w(0.5, 1.0, " overtake")],
            [w(1.0, 1.5, " go"), w(1.5, 1.7, " for"), w(1.7, 2.0, " the")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        # There is nothing after the last caption to move "the" into.
        assert [c.text for c in captions] == ["double overtake", "go for the"]

    def test_move_refused_across_a_sentence_punctuation_boundary(self):
        groups = [
            [w(0.0, 0.5, " wait"), w(0.5, 1.0, " and,")],
            [w(1.0, 1.5, " what"), w(1.5, 2.0, " next")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        # "and," closed its caption on purpose (the punctuation rule); the
        # forwarding rule must not undo that even though "and" is a
        # function word.
        assert [c.text for c in captions] == ["wait and,", "what next"]

    def test_case_and_punctuation_insensitivity_through_the_pipeline(self):
        groups = [
            [w(0.0, 0.3, " pick"), w(0.3, 0.6, " up"), w(0.6, 1.0, " The")],
            [w(1.0, 1.5, " penalty"), w(1.5, 2.0, " as")],
        ]
        result = _forward_function_words(groups, max_words=4, max_seconds=99.0)
        captions = [_to_caption(g) for g in result]
        assert [c.text for c in captions] == ["pick up", "The penalty as"]


class TestFunctionWordForwardingThroughGroupWords:
    """End-to-end checks that group_words wires the forwarding pass in
    correctly, plus the scale/invariant properties re-verified with real
    function words present so the new pass is actually exercised."""

    def test_a_caption_ending_on_a_lone_function_word_is_fixed_end_to_end(self):
        words = [
            w(0.0, 0.2, " to"), w(0.2, 0.5, " pick"), w(0.5, 0.7, " up"),
            w(0.7, 0.85, " a"), w(0.85, 1.3, " penalty"), w(1.3, 1.6, " as"),
        ]
        captions = group_words(words, max_words=4, max_seconds=99.0)
        # "to pick up" / "a penalty as" - the lone "a" no longer ends the
        # first caption. Every caption but the last is checked, since only
        # the last is allowed to keep a trailing function word.
        assert not any(c.text.split()[-1].rstrip(".!?,;:").lower() in FUNCTION_WORDS
                        for c in captions[:-1])

    def test_every_word_survives_in_order_with_function_words_present(self):
        vocabulary = ["the", "cat", "sat", "on", "the", "mat", "and", "then",
                      "it", "ran", "away", "quickly", "to", "find", "a",
                      "warm", "spot", "in", "the", "sun"]
        words = []
        t = 0.0
        for i in range(120):
            token = vocabulary[i % len(vocabulary)]
            end = t + 0.25
            words.append(w(t, end, f" {token}"))
            t = end
        captions = group_words(words, max_words=4, max_seconds=3.0)
        expected = [tok.strip() for tok in (f" {vocabulary[i % len(vocabulary)]}"
                                             for i in range(120))]
        assert " ".join(c.text for c in captions).split() == [e.strip() for e in expected]
        assert all(c.end - c.start <= 3.0 for c in captions)
        assert all(a.end <= b.start for a, b in zip(captions, captions[1:], strict=False))
        assert all(c.end >= c.start for c in captions)


class TestGuards:
    def test_max_words_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            group_words([w(0.0, 0.2, " a")], max_words=0)

    def test_max_seconds_of_zero_or_less_is_rejected(self):
        with pytest.raises(ValueError):
            group_words([w(0.0, 0.2, " a")], max_seconds=0.0)


class TestDocumentedDefaults:
    """Finding F4: the grouping defaults were previously two unpinned
    copies of the same numbers - once as group_words' own parameter
    defaults, once as literals in bin/yt-shorts - and neither was asserted
    anywhere, so either (or both) could drift silently. bin/yt-shorts now
    falls back to these same named constants instead of repeating the
    literals, making this the one place that can still catch a drift.

    4 words / 3.0 seconds: measured on the real speedy transcript - see
    the constants' own comment in captions.py and the wiki's Subtitles
    page, which makes the same measured quality claim.
    """

    def test_the_documented_defaults_are_pinned(self):
        assert DEFAULT_MAX_WORDS == 4
        assert DEFAULT_MAX_SECONDS == 3.0

    def test_calling_group_words_with_no_arguments_uses_the_documented_defaults(self):
        words = [w(i * 0.5, i * 0.5 + 0.5, f" w{i}") for i in range(10)]
        assert group_words(words) == group_words(
            words, max_words=DEFAULT_MAX_WORDS, max_seconds=DEFAULT_MAX_SECONDS)
