"""Find candidate moments in a stream's transcript (see the stage D2b design).

The engine here scores emphasis over a transcript using a channel's own
excitement lexicon: `lexicon_moments` is the fallback used when no model
caller is configured (see detect.py), and `activity_curve` drives the
stream-overview strip. The original speech-rate/loudness engine
(Candidate, find_candidates, rank_moments, measure_loudness_ffmpeg,
LoudnessMoment) was replaced by the model-based scan in moment_scan.py plus
this module's own lexicon fallback (Task 6) - it is gone, not merely
superseded, because it was measured USELESS rather than just weaker: on this
workspace's own qualifying transcript, 43 of 79 candidates it found had no
marker hit at all, e.g. "Set up seems to be working. All right. That's
good." scored as a detected moment on speech rate alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .lexicon import Lexicon


def _text(words) -> str:
    return " ".join(w["text"].strip().lower() for w in words)


def _count_markers(bin_words, markers) -> float:
    """The window's WEIGHTED marker score: sum of weight x occurrences.

    Weighted rather than counted because an unweighted list does not survive
    real commentary - see lexicon.py's module docstring for the measurement.
    `markers` is a marker->weight mapping (lexicon.Lexicon.markers); a weight of
    0 is dropped before matching, which is how a layer disables an inherited
    marker - and, crucially, a disabled marker must never claim a span and
    thereby suppress another marker's match; suppression is not a feature of
    this lexicon (the same reason `lexicon._weight` refuses a negative weight).

    Markers are matched LONGEST FIRST, and every match claims its character
    span in the window's text; a shorter marker is skipped wherever its match
    would overlap a span a longer marker already claimed. Without this, a
    phrase and its own substring - e.g. "super pole" (1.0) and "pole" (0.3),
    or "pole sitter" (0.5) and "pole" - would both score for the very same
    words, and the shipped weight would not be the number the operator
    actually sees for that mention.

    The longest match wins - weight is NOT the tie-break, and never
    substitutes for it: a phrase always beats its own substring, full stop.
    Two markers of the SAME length that overlap (e.g. one added at a channel
    layer, the other inherited from the default) are still resolved
    deterministically - by weight, then alphabetically - rather than by
    dict/insertion order, which would silently depend on which layer
    happened to introduce the marker first. This means, with the shipped
    defaults, "super pole sitter" scores `pole sitter` (0.5) rather than
    `super pole` (1.0): `pole sitter` (11 chars) is longer than `super pole`
    (10 chars) and claims the span first - an acknowledged, deliberate
    consequence of always preferring the longest match (see
    TestOverlappingMarkers.test_super_pole_sitter_scores_the_longer_phrase_not_the_higher_weight
    in tests/test_moments.py)."""
    if not markers or not bin_words:
        return 0.0
    active = [(marker, weight) for marker, weight in markers.items() if weight > 0]
    if not active:
        return 0.0
    joined = _text(bin_words)
    consumed: list[tuple[int, int]] = []
    total = 0.0
    for marker, weight in sorted(active, key=lambda item: (-len(item[0]), -item[1], item[0])):
        for match in re.finditer(re.escape(marker), joined):
            start, end = match.span()
            if any(start < c_end and end > c_start for c_start, c_end in consumed):
                continue
            consumed.append((start, end))
            total += weight
    return total


CATEGORIES = ("start_finish", "incident", "highlight", "race_control", "reaction")

# Which category a marker belongs to, and what a hit in it is worth. The order
# above is the operator's own ranking: start/finish first, then incidents,
# sporting highlights, race control, commentator reaction.
CATEGORY_WEIGHTS = {
    "start_finish": 4.0,
    "incident": 3.0,
    "highlight": 2.0,
    "race_control": 2.0,
    "reaction": 1.0,
}

MARKER_CATEGORY = {
    "green green green": "start_finish", "chequered flag": "start_finish",
    "checkered flag": "start_finish", "and they are away": "start_finish",
    "crash": "incident", "into the wall": "incident", "spin": "incident",
    "contact": "incident", "puncture": "incident", "damage": "incident",
    "debris": "incident", "off the track": "incident", "into the barrier": "incident",
    "safety car": "race_control", "red flag": "race_control",
    "yellow flag": "race_control", "full course yellow": "race_control",
    "incident": "race_control", "penalty": "race_control",
    "photo finish": "highlight", "purple": "highlight", "fastest lap": "highlight",
    "new record": "highlight", "overtake": "highlight", "side by side": "highlight",
    "personal best": "highlight", "provisional pole": "highlight",
    "fastest": "highlight", "flying lap": "highlight", "super pole": "highlight",
    "pole sitter": "highlight", "pole": "highlight",
    "oh my": "reaction", "oh no": "reaction", "unbelievable": "reaction",
    "incredible": "reaction", "what a": "reaction",
}
DEFAULT_CATEGORY = "reaction"


@dataclass
class Moment:
    """One candidate, from either engine. Both write this same shape."""
    start: float
    end: float
    category: str
    score: float
    reason: str
    hook_suggestion: str = ""


def activity_curve(words, lexicon: Lexicon, *, step: float = 60.0) -> list[float]:
    """Words plus marker weight per `step`, normalised to 0..1.

    Computed LOCALLY and deliberately not by the model: the model returns
    discrete moments, while the overview strip needs a continuous signal. This
    costs nothing, and - the point - it exists with no key and no network, so
    the stream view is useful before detection has ever run. It is labelled
    "activity" rather than "importance" because that is honestly what it is.
    """
    if not words:
        return []
    end = max(word["end"] for word in words)
    buckets = int(end // step) + 1
    raw = [0.0] * buckets
    for word in words:
        raw[min(int(word["start"] // step), buckets - 1)] += 1.0
    for index in range(buckets):
        lo, hi = index * step, (index + 1) * step
        window = [w for w in words if lo <= w["start"] < hi]
        raw[index] += _count_markers(window, lexicon.markers) * 10.0
    peak = max(raw) or 1.0
    return [round(value / peak, 4) for value in raw]


def _category_for(marker: str) -> str:
    return MARKER_CATEGORY.get(marker, DEFAULT_CATEGORY)


def lexicon_moments(words, lexicon: Lexicon, *, threshold: float = 1.0,
                    min_gap: float = 20.0) -> list[Moment]:
    """The FALLBACK engine: marker hits only, speech rate as an amplifier.

    Used by detect.py whenever no model caller is configured (no API key, or
    the SDK is unreachable) - see detect._caller_from_config. It replaces the
    old speech-rate/loudness engine (find_candidates/rank_moments, deleted
    with this rewrite) rather than sitting beside it under a different name.

    Speech rate can no longer produce a candidate on its own. Measured on this
    workspace's own qualifying, 43 of 79 candidates had no marker hit at all -
    the pre-race block where "Set up seems to be working. All right. That's
    good." was a detected moment. Rate now scales a real hit and nothing else.

    The window comes from the matched span, not a fixed pre/post-roll. The old
    fixed 12 seconds were also MISALIGNED: emphasis at tick t was caused by
    speech in [t, t+6) while the clip was [t-8, t+4), so eight silent seconds
    were included and the last two seconds of the triggering speech were cut.
    """
    if not words or not lexicon.markers:
        return []
    counts = [len(w) for w in _bins(words)] or [1]
    positive = sorted(count for count in counts if count > 0)
    baseline = max(positive[len(positive) // 2] if positive else 1, 1)

    found: list[Moment] = []
    for bin_words in _bins(words):
        if not bin_words:
            continue
        best_marker, best_weight = _best_marker(bin_words, lexicon.markers)
        if best_weight <= 0:
            continue
        category = _category_for(best_marker)
        rate = max(0.0, len(bin_words) / baseline - 1.0)
        score = min(10.0, CATEGORY_WEIGHTS[category] * best_weight * (1.0 + rate))
        if score < threshold:
            continue
        start = bin_words[0]["start"]
        end = bin_words[-1]["end"]
        if found and start - found[-1].start < min_gap:
            if score > found[-1].score:
                found[-1] = Moment(start=start, end=end, category=category,
                                   score=round(score, 2),
                                   reason=f"lexicon: {best_marker}")
            continue
        found.append(Moment(start=start, end=end, category=category,
                            score=round(score, 2),
                            reason=f"lexicon: {best_marker}"))
    return found


def _bins(words, *, window: float = 12.0):
    """Contiguous ~`window`-second groups, in transcript order."""
    current: list[dict] = []
    for word in words:
        if current and word["end"] - current[0]["start"] > window:
            yield current
            current = []
        current.append(word)
    if current:
        yield current


def _best_marker(bin_words, markers) -> tuple[str, float]:
    """The highest-weighted marker present in this bin, or ("", 0.0)."""
    joined = _text(bin_words)
    best, weight = "", 0.0
    for marker, value in markers.items():
        if value > 0 and marker in joined and value > weight:
            best, weight = marker, value
    return best, weight
