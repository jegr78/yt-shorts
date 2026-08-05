"""A channel's excitement markers - words that mark a moment worth clipping,
each with a WEIGHT saying how much it counts.

Separate from glossary.json on purpose: the glossary corrects proper nouns the
decoder mishears; the lexicon names the words that signal something happened
(crash, overtake, safety car). A missing or empty file means no lexicon signal,
never an error.

Weights exist because counting every marker equally does not survive contact
with real commentary. Measured on a 98-minute ERF qualifying transcript, the
ten incident markers this file used to ship scored THREE hits, while `pole`
occurred 19 times as ordinary chatter - and since a hit added exactly 1.0 and
the candidate threshold IS 1.0, marking `pole` would have made every mention of
it a candidate on its own. With weights, one `crash` (3.0) crosses the
threshold alone and `pole` (0.3) needs four mentions in the same window.

Two file shapes are accepted, so an existing hand-written list keeps working:

    {"markers": {"crash": 3.0, "pole": 0.3}}   # weighted
    {"markers": ["crash", "contact"]}           # every weight 1.0

DEFAULT_MARKERS below is the built-in racing lexicon. It is the LEAST specific
layer (see profile._load_lexicon): a workspace, channel or event adds to it, and
a weight of 0 at any of those layers disables an entry inherited from here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

MAX_WEIGHT = 10.0

# A marker is a JSON string value, so it is not a path and carries no
# traversal risk (it is re.escape'd before ever being matched, see
# moments._count_markers) - this cap is hygiene, not a security boundary:
# nothing legitimate needs a 1 MB "marker" or one containing control
# characters (a stray newline/tab from a bad paste, or worse).
MAX_MARKER_LENGTH = 80

# The shipped racing lexicon, in three bands. Values are deliberate, not
# decorative: an incident is worth several times an ambient session term, and
# the two lowest weights (`pole`, `big`) are words a commentator says constantly
# - included so a BURST of them still registers, weighted so a single mention
# does not.
DEFAULT_MARKERS: dict[str, float] = {
    # Incidents - unambiguous events.
    "crash": 3.0,
    "into the wall": 3.0,
    "safety car": 2.5,
    "red flag": 2.5,
    "spin": 2.5,
    "off the track": 2.0,
    "contact": 2.0,
    "puncture": 2.0,
    "damage": 2.0,
    "debris": 2.0,
    "yellow flag": 1.5,
    "incident": 1.5,
    # Session highlights - what makes a clean session worth clipping.
    "photo finish": 2.5,
    "purple": 2.0,
    "fastest lap": 2.0,
    "new record": 2.0,
    "overtake": 2.0,
    "side by side": 2.0,
    "personal best": 1.5,
    "provisional pole": 1.5,
    "fastest": 1.0,
    "flying lap": 1.0,
    "super pole": 1.0,
    "pole sitter": 0.5,
    "pole": 0.3,
    # Reactions - how commentators mark a moment.
    "oh my": 1.5,
    "oh no": 1.5,
    "unbelievable": 1.5,
    "incredible": 1.2,
    "what a": 1.2,
    "look at that": 1.2,
    "wow": 1.0,
    "here we go": 0.8,
    "huge": 0.8,
    "massive": 0.8,
    "brilliant": 0.5,
    "fantastic": 0.5,
    "come on": 0.5,
    "big": 0.3,
}


def _weight(value, marker: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"weight for {marker!r} must be a number, found {type(value).__name__}")
    weight = float(value)
    if not math.isfinite(weight):
        raise ValueError(f"weight for {marker!r} must be finite")
    if not 0.0 <= weight <= MAX_WEIGHT:
        raise ValueError(f"weight for {marker!r} must be between 0 and {MAX_WEIGHT}")
    return weight


def normalise(markers) -> dict[str, float]:
    """Both accepted shapes -> a lower-cased marker->weight dict.

    Lives here rather than only inside load() because Lexicon's own constructor
    runs it: `Lexicon(markers=["crash"])` is the call shape the suite and the
    D2b code have used since before weights existed, and it must keep meaning
    "these markers, weight 1.0 each"."""
    if isinstance(markers, dict):
        items = list(markers.items())
    elif isinstance(markers, (list, tuple)):
        items = [(m, 1.0) for m in markers]
    else:
        raise ValueError(
            f"'markers' must be a list or an object, found {type(markers).__name__}")
    result: dict[str, float] = {}
    for marker, value in items:
        if not isinstance(marker, str) or not marker.strip():
            raise ValueError(f"each marker must be a non-empty string, found {marker!r}")
        key = marker.strip().lower()
        if len(key) > MAX_MARKER_LENGTH:
            raise ValueError(
                f"marker is {len(key)} characters, over the {MAX_MARKER_LENGTH}-character "
                f"cap: {key[:40]!r}…")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in key):
            raise ValueError(f"marker {key!r} must not contain control characters")
        if key in result:
            raise ValueError(f"duplicate marker {key!r} (markers are case-insensitive)")
        result[key] = _weight(value, key)
    return result


@dataclass
class Lexicon:
    markers: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.markers = normalise(self.markers)


EMPTY = Lexicon(markers={})


def load(path) -> Lexicon:
    path = Path(path)
    if not path.exists():
        return EMPTY
    payload = json.loads(path.read_text(encoding="utf-8"))
    markers = payload.get("markers", {}) if isinstance(payload, dict) else None
    if markers is None:
        raise ValueError(f"moments file must be an object with 'markers': {path}")
    # No `: {path}` suffix on a Lexicon() defect - every caller
    # (profile._load_lexicon, lexicon_admin._load_markers_or_empty) already
    # prefixes the path itself; appending it here too doubled it, in a
    # ProfileError line and in the studio's problems Alert. glossary.load
    # mirrors this exactly, for the same reason - keep both in sync.
    return Lexicon(markers=markers)
