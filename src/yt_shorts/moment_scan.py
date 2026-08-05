"""Turn a stream transcript into moment candidates, via a language model.

Pure: the model call is an injected callable, exactly as `detect_moments`
already injects `transcriber`. Nothing here opens a socket, so the whole module
tests with no network, no key and no cost.

Two shapes decide the design:

**Hour-long windows with a two-minute overlap.** An 8-hour transcript fits in
one request on every candidate model, so this is not a context limit - it is
calibration and blast radius. Scored window by window, hour 6 is judged like
hour 1; a failed window costs one hour rather than the run; and progress is
reportable. The overlap keeps a moment sitting on a seam from being lost.

**Line NUMBERS, never timestamps.** Models are unreliable at arithmetic over
clock times and would return plausible-looking times that are twenty seconds
off - the exact failure this rewrite exists to remove, in a new costume. A line
number either exists in the window or does not, so a hallucinated boundary is
structurally impossible and validation is a dictionary lookup.

**The line limit has to be spelled out in lines, not just seconds - measured,
not assumed.** A bake-off run against the real API over a 98-minute qualifying
transcript caught this: `claude-haiku-4-5` (the DEFAULT model at the time - see
below) returned spans of 26 to 40 lines even though the prompt already said
"between 5 and 90 seconds" - it never worked out on its own that a line is
`LINE_SECONDS` (12.0) seconds long, so 90 seconds is roughly
`MAX_SECONDS / LINE_SECONDS` (about 7.5) lines. `validate_moment` correctly
rejected every one of those spans for exceeding `MAX_SECONDS`, so five model
answers produced zero moments with zero windows recorded as failed - a run that
looked entirely clean and quietly found nothing. `claude-sonnet-5` and
`claude-opus-5` inferred the conversion unprompted and passed validation; Haiku
did not, and Haiku was the shipped default at the time this was measured, so
this was a silent failure of the then-default configuration, not an edge case.
`_INSTRUCTION` now states the line ceiling explicitly, computed from
`MAX_SECONDS` and `LINE_SECONDS` rather than hardcoded, so the stated figure
cannot drift out of step with the constraint that is actually enforced.

The default has since changed: a later bake-off (see
`providers/anthropic_api.py`'s own account beside `DEFAULT_MODEL`) moved it
from Haiku to `claude-opus-5`, which was one of the two models that already
inferred the conversion correctly here. Do not read
`providers.anthropic_api.DEFAULT_MODEL` as still naming Haiku - check it
directly rather than trusting this paragraph's history.

**The score scale has to be stated too, for the same reason: two engines write
the same field.** The same bake-off measured all three models defaulting to a
0-1 scale for `score` when the prompt left the scale unstated - every answer
came back between 0.6 and 0.95. `moments.lexicon_moments`, the other engine
that fills the same `Moment.score` field, scores 0-10
(`CATEGORY_WEIGHTS[category] * best_weight * (1.0 + rate)`, capped at 10 - see
`moments.py`). A model-engine analysis and a lexicon-engine analysis writing the
same field on two different scales are not comparable, and anything that later
thresholds or displays a score means something different depending on which
engine produced it. `_INSTRUCTION` now states the 0-10 scale explicitly, in
editorial terms rather than as bare numbers - `validate_moment`'s own
`0.0 <= score <= 10.0` check is unchanged and remains the actual guarantee; the
prompt is only the request, not the gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .providers import ModelError
from .lexicon import Lexicon
from .moments import CATEGORIES, Moment

_logger = logging.getLogger("ytshorts.moment_scan")

TARGET_PER_HOUR = "3-6"

LINE_SECONDS = 12.0
WINDOW_SECONDS = 3600.0
OVERLAP_SECONDS = 120.0
MIN_SECONDS = 5.0
MAX_SECONDS = 90.0
MAX_PER_WINDOW = 12

# The line-count ceiling the prompt states, derived rather than hardcoded so it
# cannot drift out of step with MAX_SECONDS/LINE_SECONDS the way the plain-
# seconds wording did (see the module docstring's bake-off account). Floor
# division: 7 whole lines of up to LINE_SECONDS each is 84s, safely under
# MAX_SECONDS - 8 lines could reach 96s, over it. There is no equivalent
# MIN_LINE_SPAN: MIN_SECONDS (5.0) is under one line's own length, so "a single
# line can be enough" is the honest floor rather than a computed one that would
# round to zero.
MAX_LINE_SPAN = int(MAX_SECONDS // LINE_SECONDS)

_INSTRUCTION = """\
You are selecting moments from a live sim-racing broadcast transcript that are \
worth cutting into a vertical short.

Categories, in the channel's own order of importance:
1. start_finish  - the race start and the chequered flag. Rare and always worth it.
2. incident      - crash, contact, spin, going off, puncture, hitting a barrier.
3. highlight     - overtakes, wheel-to-wheel battles, fastest laps, purple sectors, pole.
4. race_control  - safety car, full course yellow, red flag, penalties, a lead change.
5. reaction      - the commentators audibly losing it, whatever the cause.

Rules:
- Return {target} moments per hour of transcript. If the hour was uneventful, \
return fewer. Never pad the list to reach a number.
- Identify a moment by the LINE NUMBERS it spans, never by a timestamp.
- A moment must run between {min_seconds:.0f} and {max_seconds:.0f} seconds. Each \
line is about {line_seconds:.0f} seconds, so that is at most about {max_lines} \
lines - a single line can be enough for a short one. Include the build-up, and do \
not cut before the payoff.
- The transcript comes from speech recognition and contains errors. Read through \
them: "Super Bowl" in a qualifying session is "Super Pole".
- `score` is 0-10, the same scale this channel's own automatic detector uses: 0 is \
barely worth a mention, 10 is the best moment in the whole stream. Use the full \
range - most moments should land well short of 10, not cluster near the top.
- `reason` is one English sentence saying what happened.
- `hook_suggestion` is a short English on-screen title, at most six words.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    # No "minimum"/"maximum" here: Anthropic's structured-output
                    # JSON Schema dialect does not support numeric constraints
                    # (minimum/maximum/multipleOf are silently stripped, per the
                    # claude-api skill's JSON Schema Limitations) - declaring them
                    # would look like a real constraint without being one. The
                    # 0-10 scale is stated in _INSTRUCTION instead, and
                    # validate_moment's `0.0 <= score <= 10.0` check is the actual
                    # gate regardless of what the schema can express.
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                    "hook_suggestion": {"type": "string"},
                },
                "required": ["start_line", "end_line", "category", "score", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


@dataclass
class Line:
    index: int
    start: float
    end: float
    text: str


@dataclass
class Window:
    index: int
    lines: list[Line] = field(default_factory=list)


def group_lines(words, *, seconds: float = LINE_SECONDS) -> list[Line]:
    """Groups words into ~`seconds`-long numbered lines, in transcript order.

    Text is joined with "" and not " ": a decoder token carries its own leading
    space, which is what makes " C" + ".L" + ".R." render as "C.L.R." rather
    than "C .L .R.". captions.py relies on the same property.
    """
    lines: list[Line] = []
    current: list[dict] = []
    for word in words:
        if current and word["end"] - current[0]["start"] > seconds:
            lines.append(_line(len(lines), current))
            current = []
        current.append(word)
    if current:
        lines.append(_line(len(lines), current))
    return lines


def _line(index: int, words: list[dict]) -> Line:
    return Line(index=index, start=words[0]["start"], end=words[-1]["end"],
                text="".join(word["text"] for word in words))


def split_windows(lines, *, window_seconds: float = WINDOW_SECONDS,
                  overlap_seconds: float = OVERLAP_SECONDS) -> list[Window]:
    """Slices lines into overlapping windows, keeping GLOBAL line indices.

    The indices must not restart per window: a moment comes back as a line
    number, and a per-window numbering would resolve to the wrong line without
    ever looking wrong.
    """
    if not lines:
        return []
    windows: list[Window] = []
    start_time = lines[0].start
    end_time = lines[-1].end
    step = max(window_seconds - overlap_seconds, 1.0)
    cursor = start_time
    while cursor < end_time:
        chunk = [line for line in lines
                 if cursor <= line.start < cursor + window_seconds]
        if chunk:
            windows.append(Window(index=len(windows), lines=chunk))
        cursor += step
    return windows or [Window(index=0, lines=list(lines))]


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600}:{total // 60 % 60:02d}:{total % 60:02d}"


def render_window(window: Window) -> str:
    """One line per transcript line: `<index>\\t<clock>\\t<text>`."""
    return "\n".join(f"{line.index}\t{_clock(line.start)}\t{line.text.strip()}"
                     for line in window.lines)


def validate_moment(raw, lines_by_index) -> Moment | None:
    """A validated Moment, or None. NEVER raises.

    Nothing the model returns is trusted: a hallucinated line number simply
    is not a key in `lines_by_index`, which is what makes a fabricated window
    structurally impossible rather than merely implausible. A rejected moment
    is dropped and logged by the caller while the window still counts - the
    same stance "one failed clip must never abort a run" takes, one layer down.
    """
    if not isinstance(raw, dict):
        return None
    # bool is a subclass of int in Python, so int(True) == 1 silently
    # succeeds - a model answering with a JSON `true`/`false` for a line
    # number would otherwise resolve to a REAL line (line 1, in the reported
    # case) and produce a spurious Moment, not merely a harmless no-op: a
    # bogus suggestion at the top of the stream. Reject explicitly, matching
    # this function's own "nothing the model returns is trusted" contract.
    if isinstance(raw.get("start_line"), bool) or isinstance(raw.get("end_line"), bool):
        return None
    try:
        start_line = int(raw["start_line"])
        end_line = int(raw["end_line"])
        # str() on a JSON-decoded value (str/int/float/bool/None/list/dict -
        # the only shapes json.loads can produce) never itself raises, so no
        # further guard is needed here beyond the except tuple below; an odd
        # value just fails the CATEGORIES membership check that follows.
        category = str(raw["category"])
        score = float(raw["score"])
    except (KeyError, TypeError, ValueError, OverflowError):
        # OverflowError, not just ValueError/TypeError: Python's json.loads
        # accepts the bare token `Infinity` (it's valid per the json module,
        # though not per the JSON spec) and hands back float('inf'); int()
        # then raises OverflowError converting it ("cannot convert float
        # infinity to integer"), and float() raises the same converting a
        # too-large int literal ("int too large to convert to float"). Both
        # are ordinary-looking JSON a model can return, and neither is a
        # ValueError or TypeError - miss this and one such answer would crash
        # the whole window instead of costing just this one moment.
        return None
    if category not in CATEGORIES or not 0.0 <= score <= 10.0:
        return None
    if start_line > end_line:
        return None
    first = lines_by_index.get(start_line)
    last = lines_by_index.get(end_line)
    if first is None or last is None:
        return None
    if not MIN_SECONDS <= last.end - first.start <= MAX_SECONDS:
        return None
    reason = raw.get("reason")
    if not isinstance(reason, str):
        # `reason` is required text, not a value to coerce: str(None) is the
        # four-character string "None", indistinguishable from a real reason
        # once stored - the same class of bug as IT'SREIRACING (see
        # CLAUDE.md's account of editorial.normalise_word_boundaries). A
        # non-string reason means the model's answer is malformed for a
        # required field, so the whole moment is dropped rather than kept
        # with fabricated or empty text standing in for a real explanation.
        return None
    hook_suggestion = raw.get("hook_suggestion", "")
    if not isinstance(hook_suggestion, str):
        # hook_suggestion is optional and becomes on-screen text on the
        # rendered short, so a null/malformed value degrades to "no
        # suggestion" (empty string) rather than dropping an otherwise-good
        # moment over one bad optional field - but it must NEVER become the
        # literal word "None" via a blanket str(), which is exactly what
        # publishing null text used to do.
        hook_suggestion = ""
    return Moment(start=first.start, end=last.end, category=category,
                  score=round(score, 2), reason=reason,
                  hook_suggestion=hook_suggestion)


def merge_moments(found) -> list[Moment]:
    """Collapses overlapping duplicates from the window seams, ordered by time.

    Two consecutive windows share OVERLAP_SECONDS of transcript, so a moment
    sitting on that seam is reported once by each window's scan. The higher
    score wins; ties keep the first seen.

    Overlap is checked PAIRWISE against the moments already kept, so a chain
    does not collapse transitively - and that is intended, not an oversight.
    Given a=[0,10] score 5, b=[8,20] score 3, c=[18,30] score 9: a and b
    overlap (b is dropped, a survives), and c and b would also overlap - but
    b is already gone, and a and c do NOT overlap each other at all, so c
    survives as its own moment. The result is {a, c}, not one moment spanning
    [0,30]. That is correct: a and c are two genuinely distinct moments 18
    seconds apart, not the same incident reported twice across a seam, which
    is the only thing this function exists to deduplicate. Transitively
    merging the chain (as a union-find approach would) collapses a busy
    stretch of racing into one moment and silently deletes a real one the
    operator should have been able to see - this project's operator has
    rejected features that discard finds like that (see
    TestMergeMoments.test_a_chain_does_not_collapse_into_one_moment, which
    pins exactly this a/b/c case).
    """
    merged: list[Moment] = []
    for moment in sorted(found, key=lambda m: (m.start, -m.score)):
        clash = next((kept for kept in merged
                      if moment.start < kept.end and kept.start < moment.end), None)
        if clash is None:
            merged.append(moment)
        elif moment.score > clash.score:
            merged[merged.index(clash)] = moment
    return sorted(merged, key=lambda m: m.start)


@dataclass
class ScanResult:
    """What a scan produced, plus what it could not - and those are TWO
    different things, in two separate fields, because conflating them lies
    to the operator in a way they cannot detect.

    `missing_windows` means ATTEMPTED AND FAILED: the model was asked about
    that hour and the answer never arrived or made no sense. The caller
    writes it straight into the analysis file so an operator can see which
    hours have no coverage.

    `unscanned_windows` means NOBODY LOOKED: the operator's stop landed
    before the window was ever attempted. Recording those in
    `missing_windows` would tell the operator an hour of their stream was
    examined and found nothing, when in truth no request was ever made for
    it - and the second reading is the one that would send them looking for
    a defect that does not exist. `stopped` says which of the two situations
    the result as a whole is in; it is the flag the caller keys on to raise
    rather than write a partial analysis that looks complete.
    """
    moments: list[Moment] = field(default_factory=list)
    missing_windows: list[int] = field(default_factory=list)
    unscanned_windows: list[int] = field(default_factory=list)
    stopped: bool = False


def build_system_prompt(lexicon: Lexicon) -> str:
    """The scoring instructions, plus this channel's own excitement vocabulary.

    The lexicon is appended rather than baked into `_INSTRUCTION` because the
    same five categories and density target apply to every channel - only the
    vocabulary is channel-specific, exactly as `moments.lexicon_moments`
    already treats it as an addition to a fixed scoring rule rather than a
    replacement for one.
    """
    prompt = _INSTRUCTION.format(target=TARGET_PER_HOUR, min_seconds=MIN_SECONDS,
                                 max_seconds=MAX_SECONDS, line_seconds=LINE_SECONDS,
                                 max_lines=MAX_LINE_SPAN)
    markers = sorted(marker for marker, weight in lexicon.markers.items() if weight > 0)
    if markers:
        prompt += ("\nWords this channel treats as significant: "
                   + ", ".join(markers) + ".\n")
    return prompt


def scan(words, lexicon: Lexicon, *, caller, logger=None, progress=None,
         should_stop=None, window_cache=None) -> ScanResult:
    """Scores every window and collects what survives validation.

    A window that fails - a raised exception from `caller`, or a response
    shape that is not `{"moments": [...]}`  - is dropped, its index recorded
    in `missing_windows`, and its CAUSE logged through the injected logger:
    the studio passes the running job's own logger, so an operator reading
    that job's log sees WHY an hour is missing rather than finding a quiet
    hole in the results. This is the same "one failed clip must never abort a
    run" guarantee CLAUDE.md documents for rendering, one layer up: here it
    costs one hour of the stream, never the whole scan.

    A rejected MOMENT (bad line number, bad category, ...) does not fail its
    window - only `validate_moment` returning None for that one candidate is
    dropped, exactly as intended by its own "never raises" contract.

    Two additive, injected seams make a detection stoppable at no cost. Both
    default to None, so every caller and every test written before they
    existed exercises exactly the path it always did:

    ``should_stop`` is a `Callable[[], bool]` checked at the TOP of the
    window loop, OUTSIDE the try below. That placement is the whole point,
    and it is measured rather than argued: the handler below catches
    `Exception` per window, so a stop signalled as an exception from inside
    the injected `caller` is swallowed into `missing_windows` and the scan
    carries straight on to the next hour (see
    `TestStoppingAScan.test_a_stop_signalled_from_inside_the_caller_would_be_swallowed`,
    which pins that swallow). A plain predicate cannot be swallowed. Windows
    after the stop are recorded in `unscanned_windows`, never
    `missing_windows` - see `ScanResult`.

    ``window_cache`` is an object with `get(index)` and `put(index,
    moments)`; a hit skips the model call entirely. This module stays
    FILESYSTEM-FREE - the disk-backed implementation is `detect.py`'s, at
    `streams/<video_id>/windows/`, exactly symmetric with the chunk cache at
    `streams/<video_id>/chunks/` that already makes transcription free to
    stop and resume. `put` stores what the window CONTRIBUTED (after
    MAX_PER_WINDOW, before the cross-window merge), so a hit reproduces the
    same scan rather than a wider one, and a FAILED window is never cached -
    caching a failure would make it permanent, and no retry could ever fill
    the hole it left.
    """
    log = logger if logger is not None else _logger
    lines = group_lines(words)
    windows = split_windows(lines)
    result = ScanResult()
    if not windows:
        return result
    system = build_system_prompt(lexicon)

    for position, window in enumerate(windows):
        # OUTSIDE the try, and that placement is the whole point: the handler
        # below catches Exception per window and records it in
        # missing_windows, so a stop signalled as an exception from inside the
        # injected `caller` would be swallowed and the scan would carry on -
        # measured, not assumed. A plain predicate cannot be swallowed.
        if should_stop is not None and should_stop():
            result.stopped = True
            result.unscanned_windows = [w.index for w in windows[position:]]
            log.info("stopped after %d of %d window(s); %d not scanned",
                     position, len(windows), len(result.unscanned_windows))
            break

        by_index = {line.index: line for line in window.lines}
        cached = None if window_cache is None else window_cache.get(window.index)
        if cached is not None:
            result.moments.extend(cached)
            if progress is not None:
                progress(window.index + 1, len(windows))
            continue
        try:
            answer = caller(system, render_window(window), SCHEMA)
            candidates = answer["moments"]
            if not isinstance(candidates, list):
                raise TypeError(f"'moments' is {type(candidates).__name__}, not a list")
        except Exception as error:
            # Any failure here (a network error inside `caller`, a malformed
            # answer shape, a missing 'moments' key) costs exactly this one
            # window, never the run - the same stance "one failed clip must
            # never abort a run" takes in render.py, one layer up.
            #
            # `scan`'s own signature takes an UNCONSTRAINED `caller` callable -
            # it is not typed as a provider's `make_caller` output, so the
            # safety of logging `error` in full cannot be assumed here the way
            # an earlier version of this comment claimed. A ModelError is
            # `providers`' own type, built from an exception's TYPE NAME
            # only and therefore safe to log in full by construction; anything
            # else arrives from a caller this function cannot inspect and may
            # be quoting the very request that carries the API key. Widening
            # this back to `str(error)` for a nicer message is exactly how the
            # key would get into a log.
            detail = error if isinstance(error, ModelError) else type(error).__name__
            log.warning("window %d failed: %s: %s", window.index,
                        type(error).__name__, detail)
            result.missing_windows.append(window.index)
            if progress is not None:
                progress(window.index + 1, len(windows))
            continue

        kept = []
        for candidate in candidates:
            moment = validate_moment(candidate, by_index)
            if moment is None:
                log.info("window %d: dropped an invalid moment", window.index)
                continue
            kept.append(moment)
        kept.sort(key=lambda m: -m.score)
        contributed = kept[:MAX_PER_WINDOW]
        result.moments.extend(contributed)
        if window_cache is not None:
            # Inside the loop, per window, and never batched at the end: a
            # cache written only on a clean finish would be worthless to
            # exactly the run that needs it - the one that was stopped.
            window_cache.put(window.index, contributed)
        if progress is not None:
            progress(window.index + 1, len(windows))

    result.moments = merge_moments(result.moments)
    log.info("scanned %d of %d window(s): %d moment(s), %d failed, %d not scanned",
             len(windows) - len(result.unscanned_windows), len(windows),
             len(result.moments), len(result.missing_windows),
             len(result.unscanned_windows))
    return result
