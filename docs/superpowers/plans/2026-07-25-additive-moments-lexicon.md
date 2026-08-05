# Additive Moments Lexicon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the moment-detection lexicon weighted and additive across four layers (built-in racing default → workspace → channel → event), editable in the studio at all three writable scopes.

**Architecture:** `lexicon.Lexicon.markers` becomes `dict[str, float]` (marker → weight) accepting the old flat list as weight 1.0; `moments._count_markers` becomes a weighted sum; `profile._load_lexicon` changes from wholesale replacement to a union where the most specific layer wins and weight 0 disables. A pure `lexicon_admin.py` reads/updates the three writable layers with provenance, thin studio routes expose it, and one React editor component is mounted at three scopes.

**Tech Stack:** Python 3 stdlib (`json`, `math`, `dataclasses`, `pathlib`), FastAPI (studio routes), React + Mantine + TypeScript + Vite (frontend), pytest + Playwright (backend/E2E), Vitest (frontend units).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Suite: `PYTHONPATH=src .venv/bin/pytest -q`. Linter: `python3 tools/lint.py` (must print `All checks passed!`). Frontend, in `src/yt_shorts/studio/web`: `npx tsc -b` (0 errors), `npm run lint` (oxlint, clean), `npm test` (Vitest, all pass), `npm run build` (regenerates the COMMITTED `src/yt_shorts/studio/static/`).
- **Weight rules, exact:** a weight is a finite number with `0 <= weight <= MAX_WEIGHT` where `MAX_WEIGHT = 10.0`. A negative weight is REFUSED (suppression is not a designed feature and would cancel the speech-rate signal); the upper bound stops a typo'd `300` from swamping every other signal.
- **Marker rules, exact:** a marker is a non-empty string, normalised to lower case (matching is already case-insensitive). A duplicate after normalisation is a defect.
- **Layer order, exact** (least → most specific, most specific weight wins):
  `lexicon.DEFAULT_MARKERS` → `<workspace>/moments.json` → `channels/<channel>/moments.json` → `channels/<channel>/events/<event>/moments.json`
- **Weight `0` disables:** a marker whose winning weight is `0` is DROPPED from the effective `Lexicon` so scoring never sees it, but stays visible in the raw per-layer data so the UI can render it struck through.
- **`glossary.json` is untouched.** Its wholesale replacement is deliberate and documented; only the lexicon becomes additive. The two files now behave differently on purpose — a documentation obligation in `CLAUDE.md`.
- **A malformed file is a reported defect, never a raise out of `profile.load`.** `lexicon.load` raises `ValueError`; `profile._load_lexicon` already catches it and returns a problem string so every profile defect is collected together.
- Pure modules stay pure: `lexicon.py`, `moments.py`, `lexicon_admin.py` must not import FastAPI or google (like `pathnames.py`, `upload_policy.py`, `brand_admin.py`).
- **Every studio path segment goes through `pathnames.validate_segment` before any filesystem touch** (it raises plain `ValueError` — there is no `SegmentError` class), and new routes are registered BEFORE the SPA fallback.
- **Scrolling is a mandatory acceptance criterion.** `index.css` sets `body { overflow: hidden }`, so every full-height pane owns its own scroll container (`flex: 1 1 auto; minHeight: 0; overflowY: auto` — `minHeight: 0` is the piece that is always forgotten and always breaks it) and must be verified at a short viewport (~900x600) with every control reachable.
- Pure frontend logic lives in non-component `.ts` modules with Vitest tests, so Vite's fast-refresh boundary stays component-only.
- The test suite must pass identically whether `~/YT-Shorts-Data` exists or not, and must never write into the operator's real workspace (`tests/conftest.py` patches `workspace.resolve`, `studio.api._resolve_workspace` and `studio.jobs._resolve_workspace`).
- Every `except` handler needs a short *why* comment on its own lines (the in-house lint guard flags a bare `pass`/`...` body without one).

---

### Task 1: Weighted `Lexicon` type, both file formats, the racing default

**Files:**
- Modify: `src/yt_shorts/lexicon.py`
- Modify: `tests/test_lexicon.py`

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces:
  - `lexicon.MAX_WEIGHT = 10.0`
  - `lexicon.DEFAULT_MARKERS: dict[str, float]`
  - `Lexicon.markers: dict[str, float]`, normalised in `__post_init__` so `Lexicon(markers=["crash"])` (the old call shape used throughout the suite) still works and means weight 1.0
  - `lexicon.load(path) -> Lexicon` accepting both file shapes
  - `lexicon.EMPTY = Lexicon(markers={})`

- [ ] **Step 1: Write the failing tests**

Replace the contents of `tests/test_lexicon.py` with (keep any existing test that still applies — read the file first and fold it in):

```python
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
    assert lexicon.load(path).markers == {"crash": 3.0}


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
])
def test_a_malformed_marker_set_is_refused(tmp_path, payload):
    with pytest.raises(ValueError):
        lexicon.load(_write(tmp_path, payload))


def test_a_payload_without_markers_is_empty(tmp_path):
    assert lexicon.load(_write(tmp_path, {})).markers == {}


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon.py -q`
Expected: FAIL — `AttributeError: module 'yt_shorts.lexicon' has no attribute 'MAX_WEIGHT'` (and the dict-form tests fail because `markers` is still a list).

- [ ] **Step 3: Rewrite `src/yt_shorts/lexicon.py`**

```python
"""A channel's excitement markers - words that mark a moment worth clipping,
each with a WEIGHT saying how much it counts.

Separate from glossary.json on purpose: the glossary corrects proper nouns the
decoder mishears; the lexicon names the words that signal something happened
(crash, overtake, safety car). A missing or empty file means no lexicon signal,
never an error - speech-rate still detects moments on its own.

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
    try:
        return Lexicon(markers=markers)
    except ValueError as error:
        raise ValueError(f"{error}: {path}") from error
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon.py -q`
Expected: PASS.

Then the whole suite, to see exactly what the type change broke (Task 2 fixes scoring; anything else surfacing here belongs to Task 3):
Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: `tests/test_moments.py` may still pass (a dict iterates its keys, so the unweighted `_count_markers` keeps working by accident) — that is exactly why Task 2 exists. Record any other failure in your report.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/lexicon.py tests/test_lexicon.py
git commit -m "feat(lexicon): weighted markers, both file shapes, the racing default"
```

---

### Task 2: Weighted scoring

**Files:**
- Modify: `src/yt_shorts/moments.py`
- Modify: `tests/test_moments.py`

**Interfaces:**
- Consumes: `Lexicon.markers: dict[str, float]` from Task 1.
- Produces: `moments._count_markers(bin_words, markers: dict[str, float]) -> float` — the weighted sum. `find_candidates`'s signature is UNCHANGED.

**Why this task is separate:** a dict iterates its keys, so the current
`_count_markers` keeps compiling and returns an unweighted count after Task 1 —
the weights would be silently ignored. This task is what makes them real.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_moments.py` (read the file first; keep its existing style and its existing `Lexicon(markers=[...])` call sites working — they now mean weight 1.0, so their assertions should still hold):

```python
class TestWeightedMarkers:
    def test_a_marker_contributes_its_weight_not_a_count_of_one(self):
        words = [{"start": 1.0, "end": 1.4, "text": "crash"}]
        assert _count_markers(words, {"crash": 3.0}) == pytest.approx(3.0)

    def test_repeated_hits_multiply_by_the_weight(self):
        words = [{"start": 1.0, "end": 1.2, "text": "crash"},
                 {"start": 1.3, "end": 1.5, "text": "crash"}]
        assert _count_markers(words, {"crash": 2.5}) == pytest.approx(5.0)

    def test_a_zero_weight_contributes_nothing(self):
        words = [{"start": 1.0, "end": 1.4, "text": "pole"}]
        assert _count_markers(words, {"pole": 0.0}) == pytest.approx(0.0)

    def test_weights_of_different_markers_add_up(self):
        words = [{"start": 1.0, "end": 1.2, "text": "crash"},
                 {"start": 1.3, "end": 1.5, "text": "pole"}]
        assert _count_markers(words, {"crash": 3.0, "pole": 0.3}) == pytest.approx(3.3)

    def test_no_markers_scores_zero(self):
        words = [{"start": 1.0, "end": 1.4, "text": "crash"}]
        assert _count_markers(words, {}) == pytest.approx(0.0)

    def test_a_high_weight_marker_alone_becomes_a_candidate(self):
        """One crash crosses the default threshold on its own."""
        words = [{"start": t, "end": t + 0.4, "text": "and"} for t in range(0, 60, 2)]
        words.append({"start": 30.0, "end": 30.4, "text": "crash"})
        words.sort(key=lambda w: w["start"])
        found = find_candidates(words, Lexicon(markers={"crash": 3.0}), threshold=1.0)
        assert any(abs(c.peak - 30.0) <= 6.0 for c in found), [c.peak for c in found]

    def test_a_low_weight_marker_alone_does_not(self):
        """'pole' is ambient in a qualifying: one mention must not be a moment.
        Steady chatter keeps the rate signal at ~0, so 0.3 is the whole score."""
        words = [{"start": t, "end": t + 0.4, "text": "and"} for t in range(0, 60, 2)]
        words.append({"start": 30.0, "end": 30.4, "text": "pole"})
        words.sort(key=lambda w: w["start"])
        found = find_candidates(words, Lexicon(markers={"pole": 0.3}), threshold=1.0)
        assert not any(abs(c.peak - 30.0) <= 1.0 for c in found), [c.peak for c in found]
```

Add `import pytest` and the `_count_markers` / `find_candidates` / `Lexicon` imports the file needs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q -k Weighted`
Expected: FAIL — `_count_markers` returns `1` (a count) where `3.0` is expected.

- [ ] **Step 3: Make `_count_markers` weighted**

In `src/yt_shorts/moments.py`, replace:

```python
def _count_markers(bin_words, markers) -> int:
    if not markers or not bin_words:
        return 0
    joined = _text(bin_words)
    return sum(len(re.findall(re.escape(m.lower()), joined)) for m in markers)
```

with:

```python
def _count_markers(bin_words, markers) -> float:
    """The window's WEIGHTED marker score: sum of weight x occurrences.

    Weighted rather than counted because an unweighted list does not survive
    real commentary - see lexicon.py's module docstring for the measurement.
    `markers` is a marker->weight mapping (lexicon.Lexicon.markers); a weight of
    0 contributes nothing, which is how a layer disables an inherited marker."""
    if not markers or not bin_words:
        return 0.0
    joined = _text(bin_words)
    return sum(weight * len(re.findall(re.escape(marker), joined))
               for marker, weight in markers.items())
```

Note the `.lower()` on the marker is gone: `lexicon.normalise` already
lower-cases every marker, and `_text` already lower-cases the window, so
lowering again here would be dead work. Confirm `_text` does lower-case (read
it); if it does not, keep lowering the joined text, not the marker.

Update `find_candidates`'s `lex_score = _count_markers(bin_, lexicon.markers)`
only if the attribute name changed — it did not, so no change is expected there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_moments.py -q`
Expected: PASS, including the pre-existing tests (their `Lexicon(markers=[...])`
lists now mean weight 1.0, which reproduces the old behaviour exactly).

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS. If `tests/test_detect.py` fails, its expectations encode the
unweighted model — update them to the weighted one and say so in your report;
do NOT weaken the weighted scoring to keep an old assertion green.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/moments.py tests/test_moments.py tests/test_detect.py
git commit -m "feat(moments): score markers by weight instead of counting them"
```

---

### Task 3: Four-layer additive merge

**Files:**
- Modify: `src/yt_shorts/profile.py`
- Modify: `src/yt_shorts/workspace.py`
- Modify: `tests/test_profile.py`
- Create: `tests/test_lexicon_layers.py`

**Interfaces:**
- Consumes: `lexicon.DEFAULT_MARKERS`, `lexicon.load`, `lexicon.normalise` (Task 1).
- Produces:
  - `workspace.MOMENTS_FILE = "moments.json"` and `workspace.moments_path(root) -> Path` (`<root>/moments.json`, NOT created on demand — absence is normal)
  - `profile.merge_lexicons(layers: list[dict[str, float]]) -> dict[str, float]` — pure, least→most specific, most specific wins, `0` dropped
  - `profile._load_lexicon(event_dir, channel_dir, workspace_root)` returning the merged `Lexicon` plus problems

**Design note:** `_load_lexicon`'s current signature is
`(event_dir, channel_dir)`. It gains the workspace root. `profile.load` resolves
that root itself — read how `profile.py` already reaches the workspace
(`CHANNELS_DIR = workspace.resolve().channels_dir` at import) and derive the
root from `CHANNELS_DIR.parent` rather than calling `resolve()` again, so the
suite's `CHANNELS_DIR` patching keeps controlling it and no test can reach the
operator's real workspace.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lexicon_layers.py`:

```python
"""The lexicon's four additive layers: default -> workspace -> channel -> event."""

import json

from yt_shorts import lexicon
from yt_shorts.profile import merge_lexicons


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
```

Add an `erf_profile_factory` fixture to this file (do NOT put it in the shared
`tests/conftest.py` — it is specific to these tests). It must:
- copy `tests/fixtures/channels/erf` into a `tmp_path` channels dir so writing
  `moments.json` never touches the committed fixture,
- point `profile.CHANNELS_DIR` at that copy via `monkeypatch`,
- write the given `workspace` / `channel` / `event` payloads to
  `<root>/moments.json`, `<channels>/erf/moments.json` and
  `<channels>/erf/events/<event>/moments.json` respectively,
- call `profile.load("erf/community-clips-back-catalogue")` and return
  `profile.config`, or the collected problem strings when
  `expect_problems=True` (catch `profile.ProfileError` and return its problems).

Read `tests/test_profile.py` first to copy the established way it builds a
throwaway channels tree — reuse that helper rather than inventing a second one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon_layers.py -q`
Expected: FAIL — `ImportError: cannot import name 'merge_lexicons' from 'yt_shorts.profile'`.

- [ ] **Step 3: Implement the merge and rewire `_load_lexicon`**

In `src/yt_shorts/workspace.py`, beside the other constants:

```python
MOMENTS_FILE = "moments.json"


def moments_path(root) -> Path:
    """The workspace-central moments lexicon (see yt_shorts.lexicon).

    Deliberately NOT created on demand, unlike logs_dir: an absent file is the
    normal state and simply means this layer contributes nothing."""
    return Path(root) / MOMENTS_FILE
```

In `src/yt_shorts/profile.py`:

```python
def merge_lexicons(layers) -> dict[str, float]:
    """Merge marker->weight layers, least specific first.

    The more specific layer wins a collision, and a winning weight of 0 DROPS
    the marker so scoring never sees it - that is how a channel or event
    disables something inherited from a broader layer (see the stage-D2b
    lexicon design). The dropped entry still exists in the raw layer, which is
    what lets the studio render it struck through."""
    merged: dict[str, float] = {}
    for layer in layers:
        merged.update(layer)
    return {marker: weight for marker, weight in merged.items() if weight > 0}
```

Then rewrite `_load_lexicon`:

```python
def _load_lexicon(event_dir: Path, channel_dir: Path,
                  workspace_root: Path) -> tuple[Lexicon, list[str]]:
    """Loads this profile's excitement Lexicon by MERGING four layers.

    Least to most specific: the built-in racing default (lexicon.DEFAULT_MARKERS),
    the workspace-central moments.json, the channel's, then the event's. The most
    specific weight wins and a weight of 0 disables an inherited marker - see
    merge_lexicons.

    This deliberately DIVERGES from _load_glossary, which sits next to it and
    replaces wholesale. A glossary is a set of corrections that must be applied
    exactly as written for one event; a lexicon is a shared vocabulary an
    operator wants to extend per channel and per event without restating it.

    A malformed file at any layer is reported as a problem string, not raised,
    so the caller collects it with every other profile defect - mirroring
    _load_glossary. Every layer being absent is not an error: the built-in
    default still applies, and detection also has the speech-rate signal.
    """
    problems: list[str] = []
    layers: list[dict[str, float]] = [dict(LEXICON_DEFAULT_MARKERS)]
    for path in (workspace.moments_path(workspace_root),
                 channel_dir / "moments.json",
                 event_dir / "moments.json"):
        if not path.exists():
            continue
        try:
            layers.append(_lexicon_load(path).markers)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"{path}: {error}")
    return Lexicon(markers=merge_lexicons(layers)), problems
```

Import `DEFAULT_MARKERS as LEXICON_DEFAULT_MARKERS` beside the existing
`lexicon` imports. Update `load`'s call site to pass the workspace root, derived
from `CHANNELS_DIR.parent` (so the suite's `CHANNELS_DIR` patch controls it):

```python
    lexicon_value, lexicon_problems = _load_lexicon(
        event_dir, channel_dir, CHANNELS_DIR.parent)
```

Read the surrounding code to match how the existing `except` clause in
`_load_lexicon` was written and keep the same exception set.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon_layers.py -q`
Expected: PASS.

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS. `tests/test_profile.py` has a test asserting the OLD wholesale
behaviour — update it to the additive model (that is this task's whole point)
and name it in your report.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/profile.py src/yt_shorts/workspace.py tests/test_lexicon_layers.py tests/test_profile.py
git commit -m "feat(profile): merge the lexicon across four additive layers"
```

---

### Task 4: `lexicon_admin.py` — read with provenance, update, adopt the default

**Files:**
- Create: `src/yt_shorts/lexicon_admin.py`
- Create: `tests/test_lexicon_admin.py`

**Interfaces:**
- Consumes: `lexicon.DEFAULT_MARKERS`, `lexicon.load`, `lexicon.normalise`, `profile.merge_lexicons`, `workspace.moments_path`, `pathnames.validate_segment`.
- Produces:
  - `class LexiconAdminError(Exception)` with `.kind` in `{"bad_name", "not_found", "bad_markers"}` (mirroring `BrandAdminError`)
  - `SOURCES = ("default", "workspace", "channel", "event")`
  - `read(root, *, channel=None, event=None) -> dict` →
    `{"scope": "workspace"|"channel"|"event", "own": {marker: weight}, "effective": {marker: {"weight": float, "source": str}}}`
    where `effective` includes weight-0 entries with the source that set them, so the UI can render "disabled" (this is the ONE place a 0 survives — `merge_lexicons` drops it for scoring)
  - `update(root, markers, *, channel=None, event=None) -> None`
  - `adopt_default(root) -> None`

**Design notes:**
- `read`/`update` derive the target file from the scope: no channel → the
  workspace file; channel only → `channels/<channel>/moments.json`; channel and
  event → `channels/<channel>/events/<event>/moments.json`.
- Every `channel`/`event` value goes through `pathnames.validate_segment` FIRST
  (`bad_name`), then existence is checked (`not_found`), then anything touches
  the filesystem.
- `update` validates via `lexicon.normalise` and re-raises its `ValueError` as
  `LexiconAdminError(kind="bad_markers")`, so an accepted payload is one
  `profile.load` accepts.
- `update` writes only that layer's own entries; it never merges. An empty dict
  writes `{"markers": {}}` rather than deleting the file, so "I cleared this
  layer" is explicit and re-editable.
- `adopt_default` writes `DEFAULT_MARKERS` as the workspace layer's own entries.
  Idempotent: the default layer still applies underneath with identical values.
- Pure: no FastAPI import.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lexicon_admin.py`:

```python
"""Reading, updating and adopting the lexicon layers (the studio's write path)."""

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon_admin.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.lexicon_admin'`.

- [ ] **Step 3: Write `src/yt_shorts/lexicon_admin.py`**

Read `src/yt_shorts/brand_admin.py` FIRST and mirror its shape: the typed error
with a `kind`, segment validation before any filesystem touch, the "what this
accepts, `profile.load` accepts" invariant, and its docstring voice. Then
implement the interfaces listed above. It must import nothing heavy — `json`,
`pathlib`, and this project's `lexicon`, `pathnames`, `profile.merge_lexicons`
and `workspace.moments_path` only.

The provenance walk is the substance:

```python
def _layers(root: Path, channel: str | None, event: str | None):
    """Every layer that applies at this scope, least specific first, as
    (source, path-or-None, markers) triples. `default` has no path."""
```

`read` builds `effective` by walking those triples in order and recording the
LAST source that set each marker — including weight 0, which
`profile.merge_lexicons` deliberately drops for scoring but the editor must
still display.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_lexicon_admin.py -q`
Expected: PASS.

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/lexicon_admin.py tests/test_lexicon_admin.py
git commit -m "feat(lexicon): pure admin module for the three writable layers"
```

---

### Task 5: Studio routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Create: `tests/test_studio_moments_api.py`

**Interfaces:**
- Consumes: `lexicon_admin.read/update/adopt_default`, `LexiconAdminError.kind`.
- Produces four routes, each returning `lexicon_admin.read`'s dict on success:
  - `GET`/`PUT /api/moments`
  - `GET`/`PUT /api/channels/{channel}/moments`
  - `GET`/`PUT /api/channels/{channel}/events/{event}/moments`
  - `POST /api/moments/adopt-default`
  and a request body `class MarkersBody(BaseModel): markers: dict[str, float]`.

**Design notes:**
- `kind` maps to status the way the other admin routes do:
  `{"bad_name": 400, "not_found": 404, "bad_markers": 400}`.
- The workspace root comes from the module's existing resolver
  (`_resolve_workspace().root`) — the same one `tests/conftest.py` patches.
- Register these BEFORE the SPA fallback. `POST /api/moments/adopt-default` must
  be registered before `GET`/`PUT /api/moments` is irrelevant (different
  methods/paths), but its literal path must not be shadowed by a `{channel}`
  pattern — verify by testing it.
- A `PUT` returns the freshly-read state so the client needs no second request.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_studio_moments_api.py`, using the `client` fixture pattern
from `tests/test_studio_logs_api.py` (read it first) — it must patch
`workspace.resolve`, `api._resolve_workspace` AND `jobs._resolve_workspace`,
because each binds its own name via from-import. Cover:

```python
def test_get_workspace_moments_reports_the_default(client): ...
def test_put_workspace_moments_writes_and_returns_the_new_state(client, root): ...
def test_get_channel_moments_shows_the_workspace_as_inherited(client, root): ...
def test_put_channel_moments_writes_only_the_channel_layer(client, root): ...
def test_get_event_moments_shows_the_channel_as_inherited(client, root): ...
def test_put_event_moments_writes_only_the_event_layer(client, root): ...
def test_a_disabled_marker_is_reported_with_weight_zero(client, root): ...
def test_adopt_default_writes_the_workspace_layer(client, root): ...
def test_adopt_default_is_idempotent(client, root): ...
def test_a_bad_weight_is_400_and_nothing_is_written(client, root): ...
def test_an_unknown_channel_is_404(client): ...
def test_an_unsafe_channel_segment_is_400(client): ...   # e.g. ".hidden"
def test_an_unsafe_event_segment_is_400(client): ...
```

Assert on the JSON body's `own`/`effective`/`scope` keys and, for writes, on the
file on disk — not merely on the status code.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_moments_api.py -q`
Expected: FAIL — the SPA fallback answers 404 for the missing `/api/moments`.

- [ ] **Step 3: Implement the routes**

Add them in `src/yt_shorts/studio/api.py` beside the other channel/event admin
routes (NOT after the SPA fallback), following the file's established shape:

```python
    class MarkersBody(BaseModel):
        markers: dict[str, float]

    def _moments_or_http(call):
        """Run a lexicon_admin call, mapping its typed error to a status the
        way every other admin route in this file does."""
        try:
            return call()
        except lexicon_admin.LexiconAdminError as error:
            status = {"bad_name": 400, "not_found": 404, "bad_markers": 400}
            raise HTTPException(status_code=status.get(error.kind, 400),
                                detail=str(error)) from error

    @app.get("/api/moments")
    def get_workspace_moments() -> dict:
        root = _resolve_workspace().root
        return _moments_or_http(lambda: lexicon_admin.read(root))

    @app.put("/api/moments")
    def put_workspace_moments(body: MarkersBody) -> dict:
        root = _resolve_workspace().root
        _moments_or_http(lambda: lexicon_admin.update(root, body.markers))
        return _moments_or_http(lambda: lexicon_admin.read(root))

    @app.post("/api/moments/adopt-default")
    def post_adopt_default_moments() -> dict:
        root = _resolve_workspace().root
        _moments_or_http(lambda: lexicon_admin.adopt_default(root))
        return _moments_or_http(lambda: lexicon_admin.read(root))
```

and the channel/event pairs following the same pattern, passing
`channel=channel` and `channel=channel, event=event`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_moments_api.py -q`
Expected: PASS.

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: PASS, and after the run neither `~/YT-Shorts-Data/moments.json` nor
`<repo>/moments.json` exists (the isolation fixture must be doing its job — check
and report).

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/studio/api.py tests/test_studio_moments_api.py
git commit -m "feat(studio): read/write the moments lexicon at all three scopes"
```

---

### Task 6: Frontend API client and pure helpers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Create: `src/yt_shorts/studio/web/src/momentsLexicon.ts`
- Create: `src/yt_shorts/studio/web/src/momentsLexicon.test.ts`

**Interfaces:**
- Consumes: Task 5's routes.
- Produces, in `api.ts`:
  - `export interface MarkerSource { weight: number; source: 'default' | 'workspace' | 'channel' | 'event' }`
  - `export interface MomentsLexicon { scope: 'workspace' | 'channel' | 'event'; own: Record<string, number>; effective: Record<string, MarkerSource> }`
  - `getMoments(scope: { channel?: string; event?: string }): Promise<MomentsLexicon>`
  - `putMoments(scope: { channel?: string; event?: string }, markers: Record<string, number>): Promise<MomentsLexicon>`
  - `adoptDefaultMoments(): Promise<MomentsLexicon>`
- Produces, in `momentsLexicon.ts` (pure, no React):
  - `export type MarkerRow = { marker: string; weight: number; source: MarkerSource['source']; own: boolean; disabled: boolean }`
  - `toRows(lex: MomentsLexicon): MarkerRow[]` — every effective marker as a row, `own` true when the marker is in `own`, `disabled` true when `weight === 0`, sorted own-first then by descending weight then marker (so what the operator edits is at the top)
  - `parseWeight(input: string): number | null` — accepts `"2"`, `"2.5"`, `"0"`, a comma decimal (`"2,5"`); returns `null` for anything not a finite number in `0..10`
  - `MAX_WEIGHT = 10`
  - `rowsToMarkers(rows: MarkerRow[]): Record<string, number>` — only the `own` rows, so a PUT never promotes inherited entries into this layer
  - `sourceLabel(source: MarkerSource['source']): string` — `'default' → 'built-in'`, etc.

- [ ] **Step 1: Write the failing tests**

Create `momentsLexicon.test.ts` covering: `toRows` marks own/inherited/disabled and sorts own-first; a `0` weight yields `disabled: true`; `parseWeight` accepts integers, decimals and a comma decimal, rejects `""`, `"abc"`, `"-1"`, `"11"`, `"Infinity"`, `"NaN"`; `rowsToMarkers` drops inherited rows and keeps own ones including weight 0; `sourceLabel` covers all four sources.

- [ ] **Step 2: Run the tests to verify they fail**

Run (in `src/yt_shorts/studio/web`): `npm test`
Expected: FAIL — `Cannot find module './momentsLexicon'`.

- [ ] **Step 3: Implement**

Write `momentsLexicon.ts` per the interfaces above, and add the three fetchers
to `api.ts` matching its existing fetch/error idiom exactly (read it first;
build the scope path as `/api/moments`, `/api/channels/<c>/moments`,
`/api/channels/<c>/events/<e>/moments`, encoding each segment).

- [ ] **Step 4: Run the gates**

Run (in `src/yt_shorts/studio/web`): `npm test` → all pass; `npx tsc -b` → 0; `npm run lint` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/momentsLexicon.ts src/yt_shorts/studio/web/src/momentsLexicon.test.ts src/yt_shorts/studio/web/src/api.ts
git commit -m "feat(studio-web): moments lexicon API client and pure row helpers"
```

---

### Task 7: The lexicon editor, mounted at three scopes

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/MomentsEditor.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx` (a Card for the central list)
- Modify: `src/yt_shorts/studio/web/src/components/ChannelScreen.tsx` (a `Tabs.Tab value="moments"` beside the existing `events`/`brand`/`channel` tabs)
- Modify: `src/yt_shorts/studio/web/src/App.tsx` (a Drawer for the event scope, mirroring the existing `EventBrandEditor` drawer)

**Interfaces:**
- Consumes: Task 6's `getMoments`/`putMoments`/`adoptDefaultMoments` and all of `momentsLexicon.ts`.
- Produces: `export function MomentsEditor({ channel, event }: { channel?: string; event?: string })` — one component whose scope is implied by which props are given (no props → workspace).

**Design notes for the implementer:**
- Read `BrandEditor.tsx` and `EventBrandEditor.tsx` in full FIRST: the
  load/error/dirty/save shape, the Mantine idioms and the "inherit vs override"
  presentation are all established there. Mirror them.
- A row shows the marker, a weight `NumberInput` (step 0.1, min 0, max
  `MAX_WEIGHT`), the source when inherited, and an action: inherited rows get
  "Override" (copies the row into own at its current weight) and "Disable"
  (own entry at weight 0); own rows get "Remove" (drops the own entry, so the
  inherited value or the default comes back) and edit-in-place.
- Disabled rows (weight 0) render struck through with a "disabled" badge.
- A "New marker" text input + weight adds an own row; reject a duplicate of an
  existing own marker and a string `parseWeight` rejects.
- Save PUTs `rowsToMarkers(rows)` and replaces state from the response.
- Settings additionally shows **"Adopt the built-in default"** behind a
  confirmation modal (it writes ~40 entries), stating that the entries become
  the operator's own and stop tracking future updates to the built-in list.
- **SCROLLING (mandatory):** the marker list is long. The list pane owns its own
  scroll container (`flex: 1 1 auto; minHeight: 0; overflowY: auto`), the
  header/actions stay outside it, and you must verify at ~900x600 that every
  control is reachable and the page body never scrolls sideways. Read how
  `SettingsScreen.tsx`/`NavScreen.tsx` nest their scroll containers and match it.
- Keep ALL pure logic in `momentsLexicon.ts`; if you need a new helper, add it
  there WITH a Vitest test.

- [ ] **Step 1: Implement `MomentsEditor` and mount it at all three scopes**

- [ ] **Step 2: Run the gates**

Run (in `src/yt_shorts/studio/web`): `npx tsc -b` → 0; `npm run lint` → clean; `npm test` → all pass.

- [ ] **Step 3: Verify scrolling at a short viewport**

Resize to ~900x600 and confirm the marker list scrolls independently, the
header and the "Add"/"Save"/"Adopt" controls stay reachable, and nothing
overflows horizontally. State in your report exactly what you did and observed.

- [ ] **Step 4: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/MomentsEditor.tsx src/yt_shorts/studio/web/src/components/SettingsScreen.tsx src/yt_shorts/studio/web/src/components/ChannelScreen.tsx src/yt_shorts/studio/web/src/App.tsx
git commit -m "feat(studio-web): moments lexicon editor at workspace, channel and event scope"
```

---

### Task 8: Fixture, docs, E2E, build, full verification

**Files:**
- Modify: `tests/fixtures/channels/erf/moments.json`
- Modify: `CLAUDE.md`
- Modify: `tests/test_studio_e2e.py`
- Modify: `src/yt_shorts/studio/static/**` (rebuilt)

- [ ] **Step 1: Convert the fixture to the weighted form**

Rewrite `tests/fixtures/channels/erf/moments.json` in the new shape, keeping its
current markers but giving them sensible weights, e.g.:

```json
{
  "markers": {
    "crash": 3.0,
    "contact": 2.0,
    "safety car": 2.5,
    "overtake": 2.0,
    "spin": 2.5,
    "incident": 1.5,
    "oh my": 1.5,
    "into the wall": 3.0,
    "puncture": 2.0,
    "off the track": 2.0
  }
}
```

This does NOT affect the pinned overlay hashes in
`tests/test_event_layer_no_regression.py` (the lexicon plays no part in
rendering — confirm by running that file). It DOES affect detection tests; fix
any that break and name them in your report.

- [ ] **Step 2: Add the E2E**

In `tests/test_studio_e2e.py`, following the existing fixtures and selector
style, add a `TestMomentsEditor` covering:
1. open Settings, add a marker with a weight, save, assert `<workspace>/moments.json` on disk contains it;
2. open the channel's Moments tab, assert the workspace marker shows as inherited, add a channel marker, save, assert only the channel file changed;
3. open the event drawer, disable an inherited marker (weight 0), save, assert the event file records `0` and the row renders as disabled;
4. click "Adopt the built-in default" in Settings, confirm the modal, and assert the workspace file gained the default entries.

- [ ] **Step 3: Update `CLAUDE.md`**

In the file's voice (constraints and their reasons), extend the lexicon
paragraph in the Architecture section to state:
- the lexicon is **weighted** (`marker → weight`, `0 <= w <= 10`), both file
  shapes are accepted (a flat list means 1.0), and WHY weights exist (the
  measured 98-minute qualifying: incident-only markers scored 3 hits while
  `pole` occurred 19x, and an unweighted hit of 1.0 equals the candidate
  threshold, so ambient vocabulary would flag everything);
- the **four additive layers** and their order, that the most specific weight
  wins, and that weight `0` disables an inherited marker;
- that this **deliberately diverges from `glossary.json`**, which sits beside it
  and still replaces wholesale — a glossary is a set of exact corrections for
  one event, a lexicon is a shared vocabulary meant to be extended;
- that `lexicon_admin.py` is pure (no FastAPI) like the other admin modules, the
  routes are a thin mapping of its `kind` to 400/404, and the built-in default
  lives in code (`lexicon.DEFAULT_MARKERS`) so it is invisible on disk until
  adopted — which is why the editor must show inherited rows with their source;
- that changing a weight changes detection output, so re-running detection over
  an already-detected stream yields a different candidate set.

- [ ] **Step 4: Build and run every gate**

```bash
cd src/yt_shorts/studio/web && npm run lint && npm run build && npm test && cd -
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: oxlint clean, build exit 0, Vitest all pass, pytest all pass, `All checks passed!`. Paste the REAL output of each.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/fixtures/channels/erf/moments.json tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(lexicon): document the weighted additive lexicon; e2e; rebuild static"
```

- [ ] **Step 6: Operator smoke (not the implementer's job)**

Restart `bin/yt-shorts studio` (uvicorn runs without `--reload`, so backend
routes need a restart), open Settings → Moments, adopt the default, then re-run
detection on stream `V9nVNEQNdR4` in `erfofficial/N24-2026` and compare the new
candidates against the 20 currently there.
