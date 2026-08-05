from yt_shorts import moment_scan
from yt_shorts import moments as moments_mod
from yt_shorts.providers import ModelError
from yt_shorts.lexicon import Lexicon


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


def words_over(seconds, *, step=1.0):
    return [w(t, t + step, f" x{int(t)}") for t in
            [i * step for i in range(int(seconds / step))]]


class TestGroupLines:
    def test_numbers_lines_from_zero_and_keeps_order(self):
        lines = moment_scan.group_lines(words_over(60), seconds=12.0)
        assert [line.index for line in lines] == list(range(len(lines)))
        assert lines[0].start == 0.0

    def test_a_line_spans_about_the_requested_seconds(self):
        lines = moment_scan.group_lines(words_over(60), seconds=12.0)
        assert all(line.end - line.start <= 13.0 for line in lines)
        assert len(lines) == 5

    def test_text_is_joined_with_no_separator(self):
        # Decoder tokens carry their own leading space; joining with " " would
        # render "C.L.R." as "C .L .R." - the same rule captions.py relies on.
        lines = moment_scan.group_lines([w(0, 1, " C"), w(1, 2, ".L"), w(2, 3, ".R.")],
                                        seconds=12.0)
        assert lines[0].text == " C.L.R."

    def test_no_words_is_no_lines(self):
        assert moment_scan.group_lines([], seconds=12.0) == []


class TestSplitWindows:
    def test_a_short_stream_is_one_window(self):
        lines = moment_scan.group_lines(words_over(300), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0)
        assert len(windows) == 1
        assert windows[0].index == 0

    def test_windows_overlap_so_a_seam_moment_is_not_lost(self):
        lines = moment_scan.group_lines(words_over(7200, step=2.0), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0,
                                            overlap_seconds=120.0)
        assert len(windows) >= 2
        first_last = windows[0].lines[-1].start
        second_first = windows[1].lines[0].start
        assert second_first < first_last, "windows must overlap, not abut"
        assert first_last - second_first >= 100.0

    def test_line_indices_stay_global_across_windows(self):
        # A moment is reported as a line NUMBER; if window 2 restarted at 0 the
        # lookup back to real time would silently target the wrong line.
        lines = moment_scan.group_lines(words_over(7200, step=2.0), seconds=12.0)
        windows = moment_scan.split_windows(lines, window_seconds=3600.0)
        assert windows[1].lines[0].index > windows[0].lines[0].index


class TestRenderWindow:
    def test_each_line_carries_its_number_and_a_clock_time(self):
        lines = moment_scan.group_lines([w(0, 2, " hello"), w(3661, 3663, " later")],
                                        seconds=12.0)
        text = moment_scan.render_window(moment_scan.Window(index=0, lines=lines))
        assert text.splitlines()[0].startswith("0\t0:00:00\t")
        assert "1:01:01" in text
        assert "hello" in text and "later" in text


def lines_by_index(count=20, seconds=12.0):
    return {i: moment_scan.Line(index=i, start=i * seconds, end=(i + 1) * seconds,
                                text=f" line {i}")
            for i in range(count)}


def raw(**over):
    base = {"start_line": 2, "end_line": 3, "category": "incident",
            "score": 7.5, "reason": "Car into the barrier at Pflanzgarten.",
            "hook_suggestion": "INTO THE BARRIER"}
    base.update(over)
    return base


class TestValidateMoment:
    def test_a_good_moment_resolves_to_real_times(self):
        found = moment_scan.validate_moment(raw(), lines_by_index())
        assert found.start == 24.0 and found.end == 48.0
        assert found.category == "incident" and found.score == 7.5
        assert found.hook_suggestion == "INTO THE BARRIER"

    def test_a_line_number_outside_the_window_is_dropped(self):
        assert moment_scan.validate_moment(raw(start_line=99), lines_by_index()) is None
        assert moment_scan.validate_moment(raw(end_line=99), lines_by_index()) is None

    def test_an_inverted_range_is_dropped(self):
        assert moment_scan.validate_moment(raw(start_line=5, end_line=2),
                                           lines_by_index()) is None

    def test_a_bool_line_number_is_dropped_not_coerced_to_a_real_line(self):
        # bool is a subclass of int, so int(True) == 1 silently succeeds and
        # used to resolve to a real line - reproduced: raw(start_line=True,
        # end_line=True) against lines_by_index() used to validate to
        # Moment(start=12.0, end=24.0, ...), a spurious suggestion at the top
        # of the stream, not a harmless no-op.
        assert moment_scan.validate_moment(raw(start_line=True, end_line=True),
                                           lines_by_index()) is None
        assert moment_scan.validate_moment(raw(start_line=False, end_line=2),
                                           lines_by_index()) is None
        assert moment_scan.validate_moment(raw(start_line=2, end_line=False),
                                           lines_by_index()) is None

    def test_a_window_shorter_than_the_minimum_is_dropped(self):
        short = {0: moment_scan.Line(index=0, start=10.0, end=12.0, text=" hi")}
        assert moment_scan.validate_moment(raw(start_line=0, end_line=0), short) is None

    def test_a_window_longer_than_the_maximum_is_dropped(self):
        # 90 seconds is the ceiling: anything longer is not a Short.
        assert moment_scan.validate_moment(raw(start_line=0, end_line=15),
                                           lines_by_index()) is None

    def test_an_unknown_category_is_dropped(self):
        assert moment_scan.validate_moment(raw(category="vibes"),
                                           lines_by_index()) is None

    def test_a_score_outside_zero_to_ten_is_dropped(self):
        assert moment_scan.validate_moment(raw(score=99), lines_by_index()) is None
        assert moment_scan.validate_moment(raw(score=-1), lines_by_index()) is None

    def test_a_missing_field_is_dropped_not_raised(self):
        assert moment_scan.validate_moment({"start_line": 2}, lines_by_index()) is None

    def test_a_non_dict_is_dropped_not_raised(self):
        assert moment_scan.validate_moment("nonsense", lines_by_index()) is None

    # --- Finding 1: OverflowError from json.loads's `Infinity` / a huge int ---

    def test_a_huge_integer_score_is_dropped_not_raised(self):
        # json.loads has no upper bound on an integer literal, so a model can
        # return a score whose int->float conversion overflows: float() raises
        # OverflowError ("int too large to convert to float"), which is
        # neither ValueError nor TypeError.
        huge = 10 ** 400
        assert moment_scan.validate_moment(raw(score=huge), lines_by_index()) is None

    def test_an_infinite_start_line_is_dropped_not_raised(self):
        # json.loads accepts the bare token `Infinity` and returns float('inf');
        # int(float('inf')) raises OverflowError ("cannot convert float
        # infinity to integer"), reachable in production because
        # providers/anthropic_api.py parses the answer with plain json.loads.
        assert moment_scan.validate_moment(raw(start_line=float("inf")),
                                           lines_by_index()) is None

    # --- Finding 2: an explicit JSON null must not become the string "None" ---

    def test_a_null_reason_is_dropped_not_turned_into_the_string_none(self):
        found = moment_scan.validate_moment(raw(reason=None), lines_by_index())
        assert found is None

    def test_a_null_hook_suggestion_becomes_empty_not_the_string_none(self):
        found = moment_scan.validate_moment(raw(hook_suggestion=None), lines_by_index())
        assert found is not None
        assert found.hook_suggestion == ""

    # --- Reviewer-noted boundary: MIN_SECONDS/MAX_SECONDS are exactly 5.0/90.0 ---

    def test_a_window_of_exactly_min_seconds_survives(self):
        exact = {0: moment_scan.Line(index=0, start=10.0, end=15.0, text=" hi")}
        found = moment_scan.validate_moment(raw(start_line=0, end_line=0), exact)
        assert found is not None and found.end - found.start == moment_scan.MIN_SECONDS

    def test_a_window_just_under_min_seconds_is_dropped(self):
        short = {0: moment_scan.Line(index=0, start=10.0, end=14.999, text=" hi")}
        assert moment_scan.validate_moment(raw(start_line=0, end_line=0), short) is None

    def test_a_window_of_exactly_max_seconds_survives(self):
        exact = {0: moment_scan.Line(index=0, start=10.0, end=100.0, text=" hi")}
        found = moment_scan.validate_moment(raw(start_line=0, end_line=0), exact)
        assert found is not None and found.end - found.start == moment_scan.MAX_SECONDS

    def test_a_window_just_over_max_seconds_is_dropped(self):
        long = {0: moment_scan.Line(index=0, start=10.0, end=100.001, text=" hi")}
        assert moment_scan.validate_moment(raw(start_line=0, end_line=0), long) is None


class TestMergeMoments:
    def test_overlapping_duplicates_collapse_to_the_higher_score(self):
        a = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                               score=6.0, reason="from window 0")
        b = moments_mod.Moment(start=105.0, end=135.0, category="incident",
                               score=8.0, reason="from window 1")
        merged = moment_scan.merge_moments([a, b])
        assert len(merged) == 1 and merged[0].score == 8.0

    def test_separate_moments_both_survive(self):
        a = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                               score=6.0, reason="a")
        b = moments_mod.Moment(start=900.0, end=930.0, category="highlight",
                               score=5.0, reason="b")
        assert len(moment_scan.merge_moments([a, b])) == 2

    def test_the_result_is_ordered_by_time(self):
        late = moments_mod.Moment(start=900.0, end=930.0, category="highlight",
                                  score=5.0, reason="late")
        early = moments_mod.Moment(start=100.0, end=130.0, category="incident",
                                   score=6.0, reason="early")
        assert [m.start for m in moment_scan.merge_moments([late, early])] == [100.0, 900.0]

    def test_a_chain_does_not_collapse_into_one_moment(self):
        # a and b overlap (b is dropped, lower score); b and c would also
        # overlap, but b is already gone by the time c is considered; a and c
        # do NOT overlap each other at all. Pairwise (not transitive) overlap
        # checking is deliberate - see merge_moments' own docstring for why
        # collapsing this chain into one moment would be wrong, not merely a
        # missed optimisation.
        a = moments_mod.Moment(start=0.0, end=10.0, category="incident",
                               score=5.0, reason="a")
        b = moments_mod.Moment(start=8.0, end=20.0, category="incident",
                               score=3.0, reason="b")
        c = moments_mod.Moment(start=18.0, end=30.0, category="highlight",
                               score=9.0, reason="c")
        merged = moment_scan.merge_moments([a, b, c])
        assert merged == [a, c]


import logging


class TestBuildSystemPrompt:
    def test_names_every_category_and_the_density_target(self):
        prompt = moment_scan.build_system_prompt(Lexicon(markers={"crash": 3.0}))
        for category in moments_mod.CATEGORIES:
            assert category in prompt
        assert "3" in prompt and "6" in prompt      # 3-6 per hour
        assert "crash" in prompt                    # channel vocabulary is passed in

    def test_an_empty_lexicon_still_produces_a_prompt(self):
        assert moment_scan.build_system_prompt(Lexicon(markers={})).strip()

    def test_states_a_line_limit_consistent_with_max_seconds_over_line_seconds(self):
        # Pins the PROPERTY behind defect 1's fix, not the wording: a bake-off
        # against the real API showed claude-haiku-4-5 (the shipped default)
        # returning 26-40 line spans when the prompt stated only "5 to 90
        # seconds" - it never inferred that a line is LINE_SECONDS long. The
        # prompt must state a line-count ceiling, and that ceiling must be the
        # one MAX_SECONDS/LINE_SECONDS actually implies, not an arbitrary
        # number that could drift out of step with what validate_moment
        # enforces. A future reword may change the sentence; this test only
        # requires the derived number and the seconds figure to still appear.
        prompt = moment_scan.build_system_prompt(Lexicon(markers={}))
        expected_max_lines = int(moment_scan.MAX_SECONDS // moment_scan.LINE_SECONDS)
        assert expected_max_lines == moment_scan.MAX_LINE_SPAN
        assert str(expected_max_lines) in prompt
        assert str(int(moment_scan.MAX_SECONDS)) in prompt

    def test_states_the_score_scale_is_0_to_10(self):
        # Pins the PROPERTY behind defect 2's fix: all three bake-off models
        # defaulted to a 0-1 scale for `score` when the prompt left the scale
        # unstated, while moments.lexicon_moments (the other engine writing
        # the same Moment.score field) scores 0-10. The prompt must state that
        # scale numerically so a model-engine score and a lexicon-engine score
        # mean the same thing - not the exact sentence, which may be reworded.
        prompt = moment_scan.build_system_prompt(Lexicon(markers={}))
        assert "0-10" in prompt


class TestScan:
    def _words(self, seconds=1800):
        return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]

    def test_returns_validated_moments_from_the_model(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=2, end_line=3)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) == 1
        assert result.moments[0].category == "incident"
        assert result.missing_windows == []

    def test_a_failing_window_is_recorded_and_does_not_stop_the_run(self):
        def caller(system, user, schema):
            raise RuntimeError("network went away")
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert result.moments == []
        assert result.missing_windows == [0]

    def test_a_rejected_moment_does_not_fail_its_window(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=999), raw(start_line=2, end_line=3)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) == 1
        assert result.missing_windows == []

    def test_a_garbage_response_shape_is_a_failed_window(self):
        def caller(system, user, schema):
            return {"unexpected": True}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert result.missing_windows == [0]

    def test_no_more_than_the_cap_survives_per_window(self):
        def caller(system, user, schema):
            return {"moments": [raw(start_line=i, end_line=i + 1) for i in range(40)]}
        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(result.moments) <= moment_scan.MAX_PER_WINDOW

    def test_the_cause_of_a_failed_window_reaches_the_injected_logger(self, caplog):
        # A window that vanished without a logged cause is the silent-failure
        # bug this project has already paid for twice. A ModelError's message is
        # built from a type name by the provider and is safe to log in full.
        def caller(system, user, schema):
            raise ModelError("the Anthropic SDK raised APIConnectionError calling the model")
        logger = logging.getLogger("ytshorts.test.scan")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.scan"):
            moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                             logger=logger)
        assert "APIConnectionError" in caplog.text

    def test_an_unrecognised_exception_is_logged_by_TYPE_ONLY(self, caplog):
        # The whole point of ModelError is that the provider has PROMISED its
        # message carries no key. An exception from anywhere else has made no
        # such promise, and scan cannot check where its injected caller came
        # from - so it logs what it knows is safe: the type name.
        def caller(system, user, schema):
            raise RuntimeError("sk-ant-SECRET leaked in some library's message")
        logger = logging.getLogger("ytshorts.test.scan")
        with caplog.at_level(logging.WARNING, logger="ytshorts.test.scan"):
            moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                             logger=logger)
        assert "RuntimeError" in caplog.text
        assert "sk-ant-SECRET" not in caplog.text

    def test_progress_is_reported_per_window(self):
        seen = []
        def caller(system, user, schema):
            return {"moments": []}
        moment_scan.scan(self._words(7200), Lexicon(markers={}), caller=caller,
                         progress=lambda done, total: seen.append((done, total)))
        assert seen and seen[-1][0] == seen[-1][1]

    def test_an_empty_transcript_is_an_empty_result_not_a_crash(self):
        def caller(system, user, schema):
            raise AssertionError("must not be called for an empty transcript")
        result = moment_scan.scan([], Lexicon(markers={}), caller=caller)
        assert result.moments == [] and result.missing_windows == []


class _MemoryWindowCache:
    """The `get(index)`/`put(index, moments)` contract `scan` is handed, with
    nothing behind it but a dict - `moment_scan.py` stays filesystem-free, so
    the disk-backed one lives in detect.py and is tested there."""

    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.gets = []
        self.puts = []

    def get(self, index):
        self.gets.append(index)
        return self.stored.get(index)

    def put(self, index, moments):
        self.puts.append(index)
        self.stored[index] = list(moments)


class TestStoppingAScan:
    """`should_stop` and `window_cache` - the two additive seams that make
    stopping a detection cost nothing. Both default to None, so every test
    above exercises the unchanged path."""

    def _words(self, seconds=7200):
        return [{"start": t, "end": t + 0.8, "text": " word"} for t in range(seconds)]

    def test_both_new_parameters_default_to_none(self):
        import inspect
        parameters = inspect.signature(moment_scan.scan).parameters
        assert parameters["should_stop"].default is None
        assert parameters["window_cache"].default is None

    def test_detection_stops_between_windows(self):
        # The stop is checked at the TOP of the window loop, so the window in
        # flight finishes and nothing after it is ever asked for.
        asked = []

        def caller(system, user, schema):
            asked.append(len(asked))
            return {"moments": [raw(start_line=2, end_line=3)]}

        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                                  should_stop=lambda: len(asked) >= 1)
        assert len(asked) == 1, "a second window was scored after the stop"
        assert result.stopped is True
        assert len(result.moments) == 1          # what was scored is kept

    def test_unscanned_windows_are_not_reported_as_missing(self):
        # missing_windows means "attempted and failed". A window the operator's
        # stop prevented from being attempted must not appear there - that would
        # tell the operator an hour was looked at and found nothing, when nobody
        # looked at it at all.
        def caller(system, user, schema):
            return {"moments": []}

        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                                  should_stop=lambda: True)
        assert result.missing_windows == []
        assert result.unscanned_windows == [0, 1, 2]
        assert result.stopped is True

    def test_a_failed_window_is_still_missing_not_unscanned(self):
        # The other side of the same distinction: an attempt that failed keeps
        # being reported as a failure even when a stop follows it.
        attempts = []

        def caller(system, user, schema):
            attempts.append(len(attempts))
            raise RuntimeError("the network went away")

        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                                  should_stop=lambda: len(attempts) >= 1)
        assert result.missing_windows == [0]
        assert result.unscanned_windows == [1, 2]

    def test_a_stop_signalled_from_inside_the_caller_would_be_swallowed(self):
        # Measured, not assumed - this is WHY should_stop is a predicate checked
        # outside the try rather than an exception raised from the caller. The
        # per-window handler catches Exception, so a Stopped raised in there is
        # recorded as a failed window and the scan carries on to the next one.
        from yt_shorts.cancel import Stopped

        attempts = []

        def caller(system, user, schema):
            attempts.append(len(attempts))
            raise Stopped("stop requested")

        result = moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller)
        assert len(attempts) == 3, "the scan stopped, so the swallow no longer happens"
        assert result.missing_windows == [0, 1, 2]
        assert result.stopped is False

    def test_a_scored_window_is_cached_and_not_paid_for_twice(self):
        # The symmetry with the chunk cache, and the reason stopping a detection
        # is free: the second run must not ask the model for window 0 again.
        cache = _MemoryWindowCache()
        first_calls = []

        def caller(system, user, schema):
            first_calls.append(len(first_calls))
            return {"moments": [raw(start_line=2, end_line=3)]}

        moment_scan.scan(self._words(), Lexicon(markers={}), caller=caller,
                         window_cache=cache, should_stop=lambda: len(first_calls) >= 1)
        assert cache.puts == [0]

        def refusing_caller(system, user, schema):
            raise AssertionError("a cached window must never be paid for again")

        result = moment_scan.scan(self._words(1800), Lexicon(markers={}),
                                  caller=refusing_caller, window_cache=cache)
        # One window of transcript, already cached: the model is never called
        # and the cached moment comes back intact.
        assert len(result.moments) == 1
        assert result.moments[0].category == "incident"
        assert result.missing_windows == []

    def test_a_failed_window_is_not_cached(self):
        # Caching a failure would make the failure permanent - a retry could
        # never fill the hole it left.
        cache = _MemoryWindowCache()

        def caller(system, user, schema):
            raise RuntimeError("the network went away")

        moment_scan.scan(self._words(3600), Lexicon(markers={}), caller=caller,
                         window_cache=cache)
        assert cache.puts == []

    def test_the_cap_is_applied_before_a_window_is_cached(self):
        # What is stored is what the window CONTRIBUTED, so a cache hit
        # reproduces the same scan exactly rather than a wider one.
        cache = _MemoryWindowCache()

        def caller(system, user, schema):
            return {"moments": [raw(start_line=i, end_line=i + 1) for i in range(40)]}

        moment_scan.scan(self._words(3600), Lexicon(markers={}), caller=caller,
                         window_cache=cache)
        assert len(cache.stored[0]) <= moment_scan.MAX_PER_WINDOW
