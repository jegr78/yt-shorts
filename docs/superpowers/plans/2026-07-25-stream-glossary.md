# Global additive glossary, applied to stream transcription — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the glossary four additive layers with a built-in Nordschleife default, and actually deliver it to stream transcription at both ends (decoder bias and post-decode correction), with a studio editor at all three writable scopes.

**Architecture:** Layer parsing moves out of `profile.py` into `glossary.py` as pure logic (`GlossaryLayer`/`parse_layer`/`load`), the same split `lexicon.normalise`/`lexicon.load` already has. `profile.merge_glossaries` folds four layers least-specific-first into the existing runtime `Glossary`; a falsy entry disables an inherited one. `subprocess_decoder` finally passes a glossary file as the `argv[3]` `_decode_worker` has always accepted, and `transcribe_stream` applies corrections at assembly while the chunk cache stays raw. `glossary_admin.py` + six routes + one `GlossaryEditor` mirror the moments lexicon feature exactly.

**Tech Stack:** Python 3 stdlib + FastAPI/pydantic (studio only), pytest, React + Mantine + TypeScript + Vitest, Playwright (inside pytest).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation; the venv is `.venv/`.
- Full gate before every commit that touches Python: `PYTHONPATH=src .venv/bin/pytest -q` and `python3 tools/lint.py` (must print `All checks passed!`, exit 0).
- Frontend gate: in `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint`, `npm test`. `npm run build` regenerates the COMMITTED `src/yt_shorts/studio/static/`.
- `glossary.py`, `glossary_admin.py`, `lexicon.py`, `profile.py`, `stream_transcribe.py`, `detect.py` must NOT import FastAPI or google — only `yt_shorts/studio/` may import FastAPI.
- Every studio path segment goes through `pathnames.validate_segment` (raises plain `ValueError`; there is no `SegmentError`) BEFORE any filesystem touch.
- A malformed layer file is reported as a problem string, never raised past the caller: `profile.load` collects all defects into one `ProfileError`; `glossary_admin.read` degrades that layer to empty and names it in `problems`.
- Logging and glossary failures are best-effort: they must never abort a render, a job or a run.
- The chunk cache key stays `(video_id, start, length)` — the glossary is NEVER fingerprinted into it.
- `MAX_ENTRY_LENGTH = 200` characters for every term, replacement key and replacement text.
- Layer order, least to most specific: `default` (code) → `workspace` → `channel` → `event`.
- Falsy disables: `false` for a term, `null` for a replacement. An empty-string replacement is REFUSED ("use null to disable").
- The suite must never write into the operator's real workspace and must pass identically whether `~/YT-Shorts-Data` exists or not.
- Scrolling is a mandatory acceptance criterion: every pane owns its own scroll container (`flex: 1 1 auto; minHeight: 0; overflowY: auto`) and is verified at a short viewport.
- Spec: `docs/superpowers/specs/2026-07-25-stream-glossary-design.md`.

---

## File Structure

**Created**
- `src/yt_shorts/glossary_admin.py` — the studio's pure read/update path onto the three writable glossary layers.
- `src/yt_shorts/studio/web/src/glossaryLayers.ts` — pure row/provenance/payload logic for the editor.
- `src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx` — the editor, mounted at three scopes.
- `tests/test_glossary_layers.py` — `parse_layer`/`load`/`merge_glossaries`/`_load_glossary`.
- `tests/test_glossary_admin.py` — the admin module.
- `tests/test_studio_glossary_api.py` — the six routes.
- `src/yt_shorts/studio/web/src/glossaryLayers.test.ts` — Vitest.

**Modified**
- `src/yt_shorts/glossary.py` — gains `GlossaryLayer`, `parse_layer`, `load`, `EMPTY_LAYER`, `DEFAULT_TERMS`, `DEFAULT_REPLACEMENTS`, `DEFAULT_LAYER`. `Glossary`/`hotwords`/`apply` unchanged.
- `src/yt_shorts/workspace.py` — `GLOSSARY_FILE`, `glossary_path`.
- `src/yt_shorts/profile.py` — `merge_glossaries`, four-layer `_load_glossary`, `_parse_glossary` deleted.
- `src/yt_shorts/stream_transcribe.py` — glossary reaches the worker; applied at assembly.
- `src/yt_shorts/detect.py` — passes `config["glossary"]` to the transcriber.
- `src/yt_shorts/studio/api.py` — `GlossaryBody` + six routes.
- `src/yt_shorts/studio/web/src/api.ts` — client + wire types.
- `SettingsScreen.tsx`, `ChannelScreen.tsx`, `App.tsx` — the three mounts.
- `.gitignore`, `CLAUDE.md`, `tests/test_profile.py`, `tests/test_stream_transcribe.py`, `tests/test_studio_e2e.py`.

---

### Task 1: `glossary.py` — layer type, parsing, and the built-in Nordschleife default

**Files:**
- Modify: `src/yt_shorts/glossary.py`
- Test: `tests/test_glossary_layers.py` (create)

**Interfaces:**
- Consumes: the existing `Glossary`, `EMPTY`, `SENTENCE_END`, `_normalized`, `hotwords`, `apply` in `glossary.py` — none of which change.
- Produces:
  - `MAX_ENTRY_LENGTH: int = 200`
  - `@dataclass class GlossaryLayer` with `terms: dict[str, tuple[str, bool]]` (normalised key → (spelling, enabled)) and `replacements: dict[str, tuple[str, str | None]]` (normalised key → (raw key, replacement or None when disabled))
  - `EMPTY_LAYER: GlossaryLayer`
  - `normalise_term(term: str) -> str`
  - `normalise_key(key: str) -> str`
  - `parse_layer(data: object) -> GlossaryLayer` — raises `ValueError`
  - `load(path) -> GlossaryLayer` — `EMPTY_LAYER` when absent, raises `ValueError`
  - `DEFAULT_TERMS: tuple[str, ...]`, `DEFAULT_REPLACEMENTS: dict[str, str]`, `DEFAULT_LAYER: GlossaryLayer`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_glossary_layers.py`:

```python
import json

import pytest

from yt_shorts import glossary as glossary_module
from yt_shorts.glossary import (
    DEFAULT_LAYER, DEFAULT_REPLACEMENTS, DEFAULT_TERMS, EMPTY_LAYER,
    MAX_ENTRY_LENGTH, GlossaryLayer, load, normalise_key, normalise_term,
    parse_layer)


class TestNormalisation:
    def test_term_key_is_stripped_and_lowercased(self):
        assert normalise_term("  Karussell ") == "karussell"

    def test_replacement_key_strips_punctuation_per_token(self):
        # Must agree with what apply() matches on: _normalized per token.
        assert normalise_key("Kleine, Carousel") == "kleine carousel"

    def test_replacement_key_collapses_whitespace(self):
        assert normalise_key("kleine   carousel") == "kleine carousel"


class TestParseTerms:
    def test_list_form_enables_every_term(self):
        layer = parse_layer({"terms": ["Karussell", "Galgenkopf"]})
        assert layer.terms == {"karussell": ("Karussell", True),
                               "galgenkopf": ("Galgenkopf", True)}

    def test_map_form_false_disables(self):
        layer = parse_layer({"terms": {"Karussell": True, "carousel": False}})
        assert layer.terms == {"karussell": ("Karussell", True),
                               "carousel": ("carousel", False)}

    def test_null_disables_a_term(self):
        layer = parse_layer({"terms": {"carousel": None}})
        assert layer.terms == {"carousel": ("carousel", False)}

    def test_missing_terms_key_is_empty(self):
        assert parse_layer({"replacements": {}}).terms == {}

    def test_terms_wrong_type_is_a_defect(self):
        with pytest.raises(ValueError, match="'terms' must be a list or an object"):
            parse_layer({"terms": "Karussell"})

    def test_non_string_term_is_a_defect(self):
        with pytest.raises(ValueError, match="each term must be a non-empty string"):
            parse_layer({"terms": ["fine", 42]})

    def test_blank_term_is_a_defect(self):
        with pytest.raises(ValueError, match="each term must be a non-empty string"):
            parse_layer({"terms": ["   "]})

    def test_duplicate_after_lowercasing_is_a_defect(self):
        with pytest.raises(ValueError, match="duplicate term 'karussell'"):
            parse_layer({"terms": ["Karussell", "karussell"]})

    def test_non_boolean_term_flag_is_a_defect(self):
        with pytest.raises(ValueError, match="must map to true or false"):
            parse_layer({"terms": {"Karussell": 1.5}})

    def test_over_long_term_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"terms": ["x" * (MAX_ENTRY_LENGTH + 1)]})

    def test_control_character_in_term_is_a_defect(self):
        with pytest.raises(ValueError, match="must not contain control characters"):
            parse_layer({"terms": ["kar\nussell"]})


class TestParseReplacements:
    def test_string_value_is_a_correction(self):
        layer = parse_layer({"replacements": {"Kessichen": "Kesselchen"}})
        assert layer.replacements == {"kessichen": ("Kessichen", "Kesselchen")}

    def test_null_disables_a_replacement(self):
        layer = parse_layer({"replacements": {"carousel": None}})
        assert layer.replacements == {"carousel": ("carousel", None)}

    def test_false_disables_a_replacement(self):
        layer = parse_layer({"replacements": {"carousel": False}})
        assert layer.replacements == {"carousel": ("carousel", None)}

    def test_replacements_wrong_type_is_a_defect(self):
        with pytest.raises(ValueError, match="'replacements' must be an object"):
            parse_layer({"replacements": ["kessichen"]})

    def test_non_string_value_is_a_defect(self):
        with pytest.raises(ValueError, match="must be a string, or null to disable"):
            parse_layer({"replacements": {"very very": 42}})

    def test_empty_value_is_refused_with_a_pointer_to_null(self):
        with pytest.raises(ValueError, match="use null to disable"):
            parse_layer({"replacements": {"very very": "  "}})

    def test_punctuation_only_key_is_a_defect(self):
        with pytest.raises(ValueError, match="is only punctuation"):
            parse_layer({"replacements": {"...": "Kesselchen"}})

    def test_duplicate_after_normalisation_is_a_defect(self):
        with pytest.raises(ValueError, match="duplicate replacement key"):
            parse_layer({"replacements": {"Kessichen": "Kesselchen",
                                          "kessichen,": "Kesselchen"}})

    def test_over_long_replacement_text_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"replacements": {"k": "x" * (MAX_ENTRY_LENGTH + 1)}})


class TestParseLayerShape:
    def test_not_an_object_is_a_defect(self):
        with pytest.raises(ValueError, match="glossary must be an object, found list"):
            parse_layer(["not", "an", "object"])

    def test_empty_object_is_the_empty_layer(self):
        assert parse_layer({}) == EMPTY_LAYER


class TestLoad:
    def test_missing_file_is_the_empty_layer(self, tmp_path):
        assert load(tmp_path / "nope.json") == EMPTY_LAYER

    def test_reads_a_file(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"terms": ["Karussell"]}), encoding="utf-8")
        assert load(path).terms == {"karussell": ("Karussell", True)}

    def test_invalid_json_says_so(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="is not valid JSON"):
            load(path)

    def test_a_defect_names_the_path(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"terms": [42]}), encoding="utf-8")
        with pytest.raises(ValueError, match=str(path)):
            load(path)


class TestBuiltInDefault:
    def test_default_layer_parses_the_shipped_constants(self):
        assert DEFAULT_LAYER == parse_layer({"terms": list(DEFAULT_TERMS),
                                             "replacements": dict(DEFAULT_REPLACEMENTS)})

    def test_every_shipped_term_is_enabled(self):
        assert all(enabled for _spelling, enabled in DEFAULT_LAYER.terms.values())

    def test_the_measured_mishearings_are_covered(self):
        # Each key below was observed in the real V9nVNEQNdR4 transcript; the
        # default exists to correct exactly these (see the design's table).
        for decoded, correct in [
            ("schwab schwanz", "Schwalbenschwanz"),
            ("shriver schwanz", "Schwalbenschwanz"),
            ("kleine carousel", "Kleines Karussell"),
            ("kleinica or sell", "Kleines Karussell"),
            ("carousel", "Karussell"),
            ("galgen cop", "Galgenkopf"),
            ("galbenkopf", "Galgenkopf"),
            ("geigenkop", "Galgenkopf"),
            ("kessichen", "Kesselchen"),
            ("boyacht", "Hohe Acht"),
        ]:
            assert DEFAULT_LAYER.replacements[decoded] == (decoded, correct)

    def test_the_longer_kleines_karussell_key_wins_over_carousel(self):
        """apply() prefers a two-token key at the same position - the
        behaviour that keeps 'Kleines Karussell' intact rather than letting
        'carousel' claim only the second word."""
        words = [{"start": 0.0, "end": 1.0, "text": " Kleine"},
                 {"start": 1.0, "end": 2.0, "text": " carousel"}]
        merged = glossary_module.Glossary(
            terms=[], replacements={raw: value
                                    for raw, value in DEFAULT_LAYER.replacements.values()
                                    if value is not None})
        out = glossary_module.apply(words, merged)
        assert "".join(w["text"] for w in out).strip() == "Kleines Karussell"

    def test_no_shipped_term_or_key_is_a_duplicate_of_another(self):
        keys = [normalise_term(t) for t in DEFAULT_TERMS]
        assert len(keys) == len(set(keys))
        rep_keys = [normalise_key(k) for k in DEFAULT_REPLACEMENTS]
        assert len(rep_keys) == len(set(rep_keys))


class TestEmptyLayerIsNotShared:
    def test_empty_layer_is_not_mutated_by_a_parse(self):
        parse_layer({"terms": ["Karussell"]})
        assert EMPTY_LAYER == GlossaryLayer(terms={}, replacements={})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_layers.py -q
```
Expected: collection error — `ImportError: cannot import name 'DEFAULT_LAYER' from 'yt_shorts.glossary'`.

- [ ] **Step 3: Implement in `src/yt_shorts/glossary.py`**

Append the following AFTER the existing `apply` function (leave `Glossary`, `EMPTY`, `hotwords`, `_normalized`, `_ends_sentence`, `_replacement_keys`, `_replace_run`, `apply` exactly as they are). Add `import json`, `from pathlib import Path` to the existing imports (`string`, `dataclass` are already there); `from __future__ import annotations` is already at the top.

```python
# A term, a replacement key and a replacement text are all JSON string
# values: not paths, and never used as one, so this cap is hygiene rather
# than a security boundary - the same reasoning lexicon.MAX_MARKER_LENGTH
# carries. Nothing legitimate needs a 200-character corner name, and a
# control character in one is a stray newline from a bad paste.
MAX_ENTRY_LENGTH = 200


def normalise_term(term: str) -> str:
    """A term's identity across layers: stripped and lower-cased.

    Only IDENTITY is normalised. The raw spelling is kept beside it in a
    GlossaryLayer because that spelling is what biases the decoder (see
    hotwords) and what the studio shows the operator."""
    return term.strip().lower()


def normalise_key(key: str) -> str:
    """A replacement key's identity across layers: each whitespace-separated
    token run through the same `_normalized` form `apply` matches with,
    rejoined by single spaces.

    Using `apply`'s own normalisation rather than a second, similar rule is
    the point: two layers writing "Kessichen" and "kessichen," must collide
    here exactly when they would have matched the same words there."""
    return " ".join(_normalized(token) for token in key.split() if _normalized(token))


@dataclass
class GlossaryLayer:
    """ONE glossary.json's worth of entries, keyed for merging.

    `terms` maps a term's normalised key to `(spelling, enabled)`; a layer
    that DISABLES an inherited term still records the spelling, so the studio
    can render the disabled row by name rather than by key. `replacements`
    maps a key's normalised form to `(raw key, replacement)`, with a
    replacement of None meaning "disabled at this layer".

    This is the layer format, not the runtime one: `profile.merge_glossaries`
    folds several of these into the `Glossary` that `hotwords` and `apply`
    consume, dropping every disabled entry on the way."""
    terms: dict[str, tuple[str, bool]]
    replacements: dict[str, tuple[str, str | None]]


# A layer contributing nothing. Never mutated - both fields are replaced
# wholesale by every function that produces a layer, never appended to.
EMPTY_LAYER = GlossaryLayer(terms={}, replacements={})


def _check_text(value: str, what: str) -> None:
    if len(value) > MAX_ENTRY_LENGTH:
        raise ValueError(
            f"{what} is {len(value)} characters, over the {MAX_ENTRY_LENGTH}-character "
            f"cap: {value[:40]!r}…")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise ValueError(f"{what} {value!r} must not contain control characters")


def _parse_terms(value: object) -> dict[str, tuple[str, bool]]:
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = [(term, True) for term in value]
    else:
        raise ValueError(
            f"'terms' must be a list or an object, found {type(value).__name__}")

    result: dict[str, tuple[str, bool]] = {}
    for term, flag in items:
        if not isinstance(term, str) or not term.strip():
            raise ValueError(f"each term must be a non-empty string, found {term!r}")
        spelling = term.strip()
        _check_text(spelling, "term")
        key = normalise_term(spelling)
        if key in result:
            raise ValueError(f"duplicate term {key!r} (terms are case-insensitive)")
        if flag is None or flag is False:
            enabled = False
        elif flag is True:
            enabled = True
        else:
            raise ValueError(
                f"term {key!r} must map to true or false, found {type(flag).__name__}")
        result[key] = (spelling, enabled)
    return result


def _parse_replacements(value: object) -> dict[str, tuple[str, str | None]]:
    if not isinstance(value, dict):
        raise ValueError(
            f"'replacements' must be an object, found {type(value).__name__}")

    result: dict[str, tuple[str, str | None]] = {}
    for raw, replacement in value.items():
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"each replacement key must be a non-empty string, found {raw!r}")
        raw_key = raw.strip()
        _check_text(raw_key, "replacement key")
        key = normalise_key(raw_key)
        if not key:
            raise ValueError(f"replacement key {raw_key!r} is only punctuation")
        if key in result:
            raise ValueError(
                f"duplicate replacement key {key!r} (keys are matched case- and "
                f"punctuation-insensitively)")
        if replacement is None or replacement is False:
            result[key] = (raw_key, None)
            continue
        if not isinstance(replacement, str):
            raise ValueError(
                f"replacement for {key!r} must be a string, or null to disable it, "
                f"found {type(replacement).__name__}")
        text = replacement.strip()
        if not text:
            # Refused rather than accepted as "delete these words": an empty
            # replacement makes _replace_run drop the matched run entirely,
            # which is indistinguishable from a typo and would silently eat
            # transcript text. Disabling is what null is for.
            raise ValueError(
                f"replacement for {key!r} must not be empty - use null to disable it")
        _check_text(text, "replacement text")
        result[key] = (raw_key, text)
    return result


def parse_layer(data: object) -> GlossaryLayer:
    """Validates one glossary.json's already-parsed JSON as a layer.

    Raises ValueError on the first defect. Lives here rather than in
    profile.py so glossary_admin validates a payload EXACTLY the way
    profile.load validates a file - the "what this accepts, profile.load
    accepts" invariant every admin module in this project keeps."""
    if not isinstance(data, dict):
        raise ValueError(f"glossary must be an object, found {type(data).__name__}")
    return GlossaryLayer(terms=_parse_terms(data.get("terms", [])),
                         replacements=_parse_replacements(data.get("replacements", {})))


def load(path) -> GlossaryLayer:
    """Reads one layer from disk. An absent file is EMPTY_LAYER, not an error -
    every layer being absent still leaves the built-in default."""
    path = Path(path)
    if not path.exists():
        return EMPTY_LAYER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"is not valid JSON\n{error}") from error
    try:
        return parse_layer(data)
    except ValueError as error:
        raise ValueError(f"{error}: {path}") from error


# The Nordschleife, as the decoder bias half of the built-in default. These
# are the section and corner names a Nürburgring broadcast says out loud;
# biasing them costs nothing when they are not said and is the only chance
# Whisper has of spelling them right when they are.
DEFAULT_TERMS: tuple[str, ...] = (
    "Nordschleife",
    "Hatzenbach",
    "Hocheichen",
    "Quiddelbacher Höhe",
    "Flugplatz",
    "Schwedenkreuz",
    "Aremberg",
    "Fuchsröhre",
    "Adenauer Forst",
    "Metzgesfeld",
    "Kallenhard",
    "Wehrseifen",
    "Ex-Mühle",
    "Bergwerk",
    "Kesselchen",
    "Klostertal",
    "Steilstrecke",
    "Karussell",
    "Hohe Acht",
    "Wippermann",
    "Eschbach",
    "Brünnchen",
    "Pflanzgarten",
    "Schwalbenschwanz",
    "Galgenkopf",
    "Döttinger Höhe",
    "Antoniusbuche",
    "Tiergarten",
    "Hohenrain",
    "Kleines Karussell",
    "Stefan-Bellof-S",
    "Mutkurve",
)

# The correction half. EVERY key here was OBSERVED in the real 98-minute ERF
# qualifying transcript (streams/V9nVNEQNdR4/transcript.json) - nothing is
# invented, which is why a decoded form as odd as "kleinica or sell" is on
# the list. `apply` sorts keys by token count, so the two-token
# "kleine carousel" is tried before the one-token "carousel" at the same
# position regardless of the order written here.
DEFAULT_REPLACEMENTS: dict[str, str] = {
    "schwab schwanz": "Schwalbenschwanz",
    "shriver schwanz": "Schwalbenschwanz",
    "kleine carousel": "Kleines Karussell",
    "kleinica or sell": "Kleines Karussell",
    "carousel": "Karussell",
    "galgen cop": "Galgenkopf",
    "galbenkopf": "Galgenkopf",
    "geigenkop": "Galgenkopf",
    "kessichen": "Kesselchen",
    "boyacht": "Hohe Acht",
}

# Parsed at import deliberately: a typo in the two constants above (a
# duplicate key, a control character) fails the very first import rather
# than at some operator's next detection run.
DEFAULT_LAYER = parse_layer({"terms": list(DEFAULT_TERMS),
                             "replacements": dict(DEFAULT_REPLACEMENTS)})
```

Also extend the module docstring: after the existing paragraphs, add

```
Four layers feed one glossary. This module owns the LAYER format
(GlossaryLayer, parse_layer, load) and the built-in default; profile.py owns
the merge that turns several layers into the one Glossary above (see
profile.merge_glossaries). Least to most specific: DEFAULT_LAYER here, the
workspace's glossary.json, the channel's, the event's - the most specific
layer wins per entry, and a falsy entry (`false` for a term, `null` for a
replacement) DISABLES one inherited from a less specific layer.
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_layers.py tests/test_glossary.py -q
```
Expected: all pass (`test_glossary.py` covers the untouched `hotwords`/`apply` and must stay green).

- [ ] **Step 5: Run the linter**

```bash
python3 tools/lint.py
```
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/glossary.py tests/test_glossary_layers.py
git commit -m "feat(glossary): layer format, validation and the built-in Nordschleife default"
```

---

### Task 2: Four additive layers in `profile.py`, plus the workspace resolver

**Files:**
- Modify: `src/yt_shorts/workspace.py`
- Modify: `src/yt_shorts/profile.py` (delete `_parse_glossary`; rewrite `_load_glossary`; add `merge_glossaries`)
- Modify: `tests/test_profile.py:544-658` (the whole `TestGlossary` class)
- Modify: `.gitignore`
- Test: `tests/test_glossary_layers.py` (append)

**Interfaces:**
- Consumes: `glossary.DEFAULT_LAYER`, `glossary.EMPTY_LAYER`, `glossary.GlossaryLayer`, `glossary.load` from Task 1.
- Produces:
  - `workspace.GLOSSARY_FILE = "glossary.json"`, `workspace.glossary_path(root) -> Path` (creates nothing)
  - `profile.merge_glossaries(layers: Iterable[GlossaryLayer]) -> Glossary`
  - `profile._load_glossary(event_dir, channel_dir, workspace_root) -> tuple[Glossary, list[str]]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary_layers.py`:

```python
class TestWorkspaceGlossaryPath:
    def test_path_is_the_workspace_root_file(self, tmp_path):
        from yt_shorts import workspace
        assert workspace.glossary_path(tmp_path) == tmp_path / "glossary.json"

    def test_nothing_is_created(self, tmp_path):
        from yt_shorts import workspace
        workspace.glossary_path(tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestMergeGlossaries:
    def _layer(self, terms=None, replacements=None):
        return parse_layer({"terms": terms or [], "replacements": replacements or {}})

    def test_all_four_layers_combine(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Default"]),
            self._layer(terms=["Workspace"]),
            self._layer(terms=["Channel"]),
            self._layer(terms=["Event"]),
        ])
        assert merged.terms == ["Default", "Workspace", "Channel", "Event"]

    def test_a_more_specific_layer_wins_a_terms_spelling(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["karussell"]),
            self._layer(terms=["Karussell"]),
        ])
        assert merged.terms == ["Karussell"]

    def test_a_more_specific_layer_wins_a_replacement(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(replacements={"carousel": "Karussell"}),
            self._layer(replacements={"carousel": "Carousel corner"}),
        ])
        assert merged.replacements == {"carousel": "Carousel corner"}

    def test_a_disabled_term_is_dropped_from_the_merge(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Karussell"]),
            self._layer(terms={"Karussell": False}),
        ])
        assert merged.terms == []

    def test_a_disabled_replacement_is_dropped_from_the_merge(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(replacements={"carousel": "Karussell"}),
            self._layer(replacements={"carousel": None}),
        ])
        assert merged.replacements == {}

    def test_a_disabled_entry_stays_in_its_raw_layer(self):
        """What lets the editor strike it through instead of losing it."""
        layer = self._layer(terms={"Karussell": False})
        assert layer.terms == {"karussell": ("Karussell", False)}

    def test_a_more_specific_layer_can_re_enable_a_disabled_term(self):
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([
            self._layer(terms=["Karussell"]),
            self._layer(terms={"Karussell": False}),
            self._layer(terms=["Karussell"]),
        ])
        assert merged.terms == ["Karussell"]

    def test_no_layers_is_the_empty_glossary(self):
        from yt_shorts.glossary import EMPTY
        from yt_shorts.profile import merge_glossaries
        assert merge_glossaries([]) == EMPTY

    def test_the_default_alone_is_usable(self):
        from yt_shorts.glossary import hotwords
        from yt_shorts.profile import merge_glossaries
        merged = merge_glossaries([DEFAULT_LAYER])
        assert "Karussell" in merged.terms
        assert merged.replacements["kessichen"] == "Kesselchen"
        assert hotwords(merged) is not None
```

Now replace the ENTIRE `class TestGlossary` in `tests/test_profile.py` (lines 544-658) with:

```python
class TestGlossary:
    """config["glossary"] is always a yt_shorts.glossary.Glossary, merged from
    FOUR additive layers (built-in default, workspace, channel, event) -
    most specific wins per entry, and a falsy entry disables one inherited
    from a less specific layer. This replaced the original wholesale rule
    (an event's glossary.json used to replace the channel's outright), which
    could not survive a global corner-name list: ERF is both the channel
    that needs the corners and the channel with a glossary.json of its own.
    A malformed layer is collected into the same ProfileError as every other
    profile defect, not raised on its own."""

    def test_no_files_yields_the_built_in_default(self, monkeypatch, tmp_path):
        from yt_shorts.glossary import DEFAULT_LAYER
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "plain", events=["event"])

        p = profile.load("plain/event")

        assert p.config["glossary"] == profile.merge_glossaries([DEFAULT_LAYER])
        assert "Karussell" in p.config["glossary"].terms

    def test_channel_glossary_ADDS_to_the_default(self, monkeypatch, tmp_path):
        """The regression this feature exists to prevent: a channel's own
        glossary.json must not replace the corner names."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Rei Racing"],
            "replacements": {"very very": "Rei Racing"},
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Rei Racing" in p.config["glossary"].terms
        assert "Karussell" in p.config["glossary"].terms
        assert p.config["glossary"].replacements["very very"] == "Rei Racing"
        assert p.config["glossary"].replacements["kessichen"] == "Kesselchen"

    def test_workspace_layer_applies_to_every_channel(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (tmp_path / "glossary.json").write_text(json.dumps({
            "terms": ["Workspace Term"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Workspace Term" in p.config["glossary"].terms

    def test_event_layer_adds_to_the_channel_layer(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Channel Term"],
        }), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "terms": ["Event Term"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Channel Term" in p.config["glossary"].terms
        assert "Event Term" in p.config["glossary"].terms

    def test_event_layer_can_disable_an_inherited_replacement(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(json.dumps({
            "replacements": {"carousel": None},
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "carousel" not in p.config["glossary"].replacements
        # A sibling default entry is untouched - one disable is not a purge.
        assert p.config["glossary"].replacements["kessichen"] == "Kesselchen"

    def test_glossary_not_an_object_is_collected_with_other_defects(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps(["not", "an", "object"]),
                                                    encoding="utf-8")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "glossary must be an object" in message
        assert "colors.accent" in message

    def test_terms_not_a_list_of_strings_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["fine", 42],
        }), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="each term must be a non-empty string"):
            profile.load("broken/event")

    def test_replacement_value_of_the_wrong_type_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "replacements": {"very very": 42},
        }), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="must be a string, or null to disable"):
            profile.load("broken/event")

    def test_invalid_json_glossary_is_collected_not_raised_on_its_own(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "broken", events=["event"])
        (channel_dir / "glossary.json").write_text("{not json", encoding="utf-8")
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        del brand["colors"]["accent"]
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")

        with pytest.raises(profile.ProfileError) as excinfo:
            profile.load("broken/event")

        message = str(excinfo.value)
        assert "glossary.json" in message
        assert "not valid JSON" in message
        assert "colors.accent" in message

    def test_one_broken_layer_does_not_take_out_the_others(self, monkeypatch, tmp_path):
        """A defect is reported, and the LOADABLE layers are still merged -
        the same degradation _load_lexicon already makes."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text("{not json", encoding="utf-8")

        glossary, problems = profile._load_glossary(
            channel_dir / "events" / "event", channel_dir, tmp_path)

        assert len(problems) == 1
        assert "not valid JSON" in problems[0]
        assert "Karussell" in glossary.terms

    def test_missing_terms_or_replacements_default_to_empty(self, monkeypatch, tmp_path):
        """Neither key is mandatory - a glossary.json naming only one of
        them must still load, with the other contributing nothing."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(json.dumps({
            "terms": ["Rei Racing"],
        }), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Rei Racing" in p.config["glossary"].terms
```

Note the change of `CHANNELS_DIR` from `tmp_path` to `tmp_path / "channels"` throughout this class: the workspace root is derived as `CHANNELS_DIR.parent`, so pointing `CHANNELS_DIR` at `tmp_path` itself would make the workspace root the pytest tmp BASE directory, shared between tests. `_build_channel_dir(tmp_path / "channels", …)` creates the parent as needed.

Then delete the now-unused import at `tests/test_profile.py:20`:
```python
from yt_shorts.glossary import EMPTY as GLOSSARY_EMPTY
```
Keep line 21 (`from yt_shorts.glossary import Glossary`) only if some other test in the file still uses `Glossary`; check with `grep -n "Glossary" tests/test_profile.py` and delete whichever import is unused — an unused import is a `tools/lint.py` failure (F401).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_layers.py tests/test_profile.py -q -k "Glossary or Merge or glossary"
```
Expected: FAIL — `AttributeError: module 'yt_shorts.workspace' has no attribute 'glossary_path'` and `module 'yt_shorts.profile' has no attribute 'merge_glossaries'`.

- [ ] **Step 3: Add the workspace resolver**

In `src/yt_shorts/workspace.py`, beside `MOMENTS_FILE`/`moments_path`:

```python
GLOSSARY_FILE = "glossary.json"


def glossary_path(root) -> Path:
    """The workspace-central glossary layer (see yt_shorts.glossary).

    Deliberately NOT created on demand, like moments_path and unlike
    logs_dir: an absent file is the normal state and simply means this layer
    contributes nothing on top of the built-in default."""
    return Path(root) / GLOSSARY_FILE
```

- [ ] **Step 4: Rewrite the glossary loading in `profile.py`**

Delete `_parse_glossary` (lines 422-445) entirely, and replace `_load_glossary` (lines 448-482) with:

```python
def merge_glossaries(layers) -> Glossary:
    """Fold GlossaryLayers, least specific first, into one runtime Glossary.

    The more specific layer wins an entry, and a DISABLED winner (a term
    mapped to false, a replacement mapped to null) drops the entry entirely,
    so neither the decoder bias nor the corrections ever see it - the same
    contract merge_lexicons has for a winning weight of 0. The dropped entry
    still exists in its own raw layer, which is what lets the studio render
    it struck through (see glossary_admin.read)."""
    terms: dict[str, tuple[str, bool]] = {}
    replacements: dict[str, tuple[str, str | None]] = {}
    for layer in layers:
        terms.update(layer.terms)
        replacements.update(layer.replacements)
    return Glossary(
        terms=[spelling for spelling, enabled in terms.values() if enabled],
        replacements={raw: text for raw, text in replacements.values() if text is not None},
    )


def _load_glossary(event_dir: Path, channel_dir: Path,
                   workspace_root: Path) -> tuple[Glossary, list[str]]:
    """Loads this profile's Glossary by MERGING four layers.

    Least to most specific: the built-in default (glossary.DEFAULT_LAYER, the
    Nordschleife corner set), the workspace-central glossary.json, the
    channel's, then the event's. The most specific layer wins per entry and a
    falsy entry disables one inherited from a less specific layer - see
    merge_glossaries.

    This REPLACED an earlier wholesale rule, under which an event's own
    glossary.json replaced the channel's outright. That rule's argument was
    that merging two term lists has no obviously correct result - an event
    list could mean "add to the channel's" or "these and only these". The
    ambiguity is now resolved rather than avoided: *add* is the rule, and
    "only these" has an explicit spelling (disable what you do not want).
    Restoring the override would silently drop the corner names for the one
    channel that both needs them and has a glossary.json of its own. Do not.

    A malformed file at any layer is reported as a problem string, not
    raised, so the caller collects it with every other profile defect -
    mirroring _load_lexicon, which sits right below this. Every layer being
    absent is not an error: the built-in default still applies.
    """
    problems: list[str] = []
    layers = [glossary_module.DEFAULT_LAYER]
    for path in (workspace.glossary_path(workspace_root),
                 channel_dir / "glossary.json",
                 event_dir / "glossary.json"):
        if not path.exists():
            continue
        try:
            layers.append(glossary_module.load(path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"{path}: {error}")
    return merge_glossaries(layers), problems
```

Fix the imports at `profile.py:48-49`: delete `from .glossary import EMPTY as GLOSSARY_EMPTY` (now unused — F401), keep `from .glossary import Glossary`, and add

```python
from . import glossary as glossary_module
```

beside the other `from . import …` imports. Then update the call site at `profile.py:621`:

```python
    glossary, glossary_problems = _load_glossary(
        event_dir, channel_dir, CHANNELS_DIR.parent)
```

and the module docstring: the line stating the glossary resolution order

```
  glossary   ->  event/          ->  channel/           -> EMPTY (see below)
```

becomes

```
  glossary   ->  MERGED: built-in default + workspace + channel + event
```

and the paragraph beginning "The glossary is the one exception to that deep merge" is replaced with:

```
The glossary and the lexicon are the two exceptions to that deep merge: both
are merged ENTRY BY ENTRY across four additive layers (built-in default,
workspace, channel, event), most specific winning, with a falsy value
disabling an inherited entry - see _load_glossary and _load_lexicon.
```

- [ ] **Step 5: Guard the repo-fallback workspace file**

Append to `.gitignore`, after the `/moments.json` block:

```
# Same reasoning as /moments.json above: the workspace-layer glossary
# (workspace.glossary_path) lands at <workspace root>/glossary.json, and
# under the repo-fallback workspace that root IS the repository root.
# ROOTED so channels/*/glossary.json and the suite's own tracked
# tests/fixtures/channels/erf/glossary.json stay tracked.
/glossary.json
```

- [ ] **Step 6: Run the full suite and the linter**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`. Other suites now see the default glossary through `profile.load` — if any test asserts an exact `config["glossary"]`, update it to assert membership as `TestGlossary` above does, and name it in your report.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/profile.py src/yt_shorts/workspace.py tests/test_profile.py tests/test_glossary_layers.py .gitignore
git commit -m "feat(glossary): four additive layers, replacing the wholesale event override"
```

---

### Task 3: Deliver the glossary to stream transcription (decode bias + assembly correction)

**Files:**
- Modify: `src/yt_shorts/stream_transcribe.py:143-155` (`subprocess_decoder`), `:237-301` (`transcribe_stream`)
- Modify: `src/yt_shorts/detect.py:27-51`
- Modify: `tests/test_stream_transcribe.py:26-38` (`fake_decoder`)
- Test: `tests/test_stream_transcribe.py`, `tests/test_detect.py`

**Interfaces:**
- Consumes: `glossary.Glossary`, `glossary.EMPTY`, `glossary.apply` (unchanged, pre-existing).
- Produces:
  - `subprocess_decoder(audio_path, start, length, *, glossary=EMPTY, ffmpeg="ffmpeg", model_name="small", runner=run_with_timeout)`
  - `transcribe_stream(video_id, workspace_dir, *, glossary=EMPTY, downloader=ytdlp_downloader, decoder=subprocess_decoder, chunk_seconds=600)` — calls `decoder(path, start, length, glossary=glossary)`
  - `detect_moments` unchanged in signature; it now calls `transcriber(video_id, workspace_dir, glossary=config.get("glossary", GLOSSARY_EMPTY))`

- [ ] **Step 1: Update the decoder stub, then write the failing tests**

In `tests/test_stream_transcribe.py`, change `fake_decoder`'s inner signature to record the glossary it was handed (this is deliberate, not incidental: a stub that swallowed the keyword would let the whole feature regress unnoticed):

```python
def fake_decoder(fail_on=()):
    # Returns one chunk-relative word per chunk, tagged by the chunk's start,
    # so the assembly's absolute times are checkable. Raises for chunks in
    # fail_on. Records the glossary it was handed per call - transcribe_stream
    # is required to pass one, and a stub that ignored it would hide a
    # regression in exactly the plumbing that was missing for months.
    calls = []
    glossaries = []

    def decode(audio_path, start, length, glossary=None):
        calls.append(start)
        glossaries.append(glossary)
        if start in fail_on:
            raise RuntimeError(f"decode hung at {start}")
        return [{"start": 0.0, "end": 1.0, "text": f" s{int(start)}"}]

    decode.calls = calls
    decode.glossaries = glossaries
    return decode
```

Then append these test classes to `tests/test_stream_transcribe.py`:

```python
class TestGlossaryReachesTheWorker:
    """The gap this closes: _decode_worker.main has always accepted argv[3] as
    a glossary JSON path, and subprocess_decoder never passed one - so every
    stream chunk ever decoded in this project decoded with an EMPTY glossary."""

    def _runner(self):
        seen = {}

        def run(args, *, timeout, env=None):
            seen["args"] = list(args)
            seen["env"] = env
            return json.dumps([{"start": 0.0, "end": 1.0, "text": " word"}])

        run.seen = seen
        return run

    def test_a_non_empty_glossary_is_written_and_passed_as_argv3(self, tmp_path, monkeypatch):
        from yt_shorts.glossary import Glossary

        written = {}

        def fake_run(args, **kwargs):
            # The ffmpeg extraction: create the wav the decoder expects, and
            # capture the glossary file while the TemporaryDirectory lives.
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)

        runner = self._runner()

        def capturing(args, *, timeout, env=None):
            written["payload"] = json.loads(Path(args[5]).read_text(encoding="utf-8"))
            return runner(args, timeout=timeout, env=env)

        out = subprocess_decoder(
            tmp_path / "audio.webm", 0.0, 600.0,
            glossary=Glossary(terms=["Karussell"], replacements={"carousel": "Karussell"}),
            runner=capturing)

        assert out == [{"start": 0.0, "end": 1.0, "text": " word"}]
        assert len(runner.seen["args"]) == 6
        assert written["payload"] == {"terms": ["Karussell"],
                                     "replacements": {"carousel": "Karussell"}}

    def test_an_empty_glossary_passes_no_fourth_argument(self, tmp_path, monkeypatch):
        def fake_run(args, **kwargs):
            Path(args[-1]).write_bytes(b"wav")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(stream_transcribe_module.subprocess, "run", fake_run)
        runner = self._runner()

        subprocess_decoder(tmp_path / "audio.webm", 0.0, 600.0, runner=runner)

        # Pins that nothing shifted for a caller that passes no glossary:
        # python -m yt_shorts._decode_worker <wav> <model>, five elements.
        assert len(runner.seen["args"]) == 5


class TestGlossaryAtAssembly:
    """Corrections are applied to the ASSEMBLED word list, and the per-chunk
    cache stays RAW - so a glossary change takes effect on the next assembly
    with no re-decode, and a correction may span a chunk boundary."""

    def test_the_transcript_is_corrected_but_the_chunk_cache_is_not(self, tmp_path):
        from yt_shorts.glossary import Glossary

        def decoder(audio_path, start, length, glossary=None):
            return [{"start": 0.0, "end": 1.0, "text": " carousel"}]

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(300), decoder=decoder,
            glossary=Glossary(terms=[], replacements={"carousel": "Karussell"}))

        assert [w["text"].strip() for w in result.words] == ["Karussell"]
        transcript = json.loads(
            (tmp_path / "streams" / VIDEO / "transcript.json").read_text(encoding="utf-8"))
        assert transcript["words"][0]["text"].strip() == "Karussell"
        cached = json.loads(
            (tmp_path / "streams" / VIDEO / "chunks" / "000.json").read_text(encoding="utf-8"))
        assert cached["words"][0]["text"].strip() == "carousel"

    def test_a_changed_glossary_recorrects_a_fully_cached_run(self, tmp_path):
        from yt_shorts.glossary import Glossary

        first = fake_decoder()
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(300),
                          decoder=first, chunk_seconds=600)
        assert first.calls == [0.0]

        def exploding(audio_path, start, length, glossary=None):
            raise AssertionError("must not decode again - the chunk cache is still valid")

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(300), decoder=exploding,
            chunk_seconds=600,
            glossary=Glossary(terms=[], replacements={"s0": "CORRECTED"}))

        assert [w["text"].strip() for w in result.words] == ["CORRECTED"]

    def test_a_correction_spans_a_chunk_boundary(self, tmp_path):
        from yt_shorts.glossary import Glossary

        def decoder(audio_path, start, length, glossary=None):
            return [{"start": 0.0, "end": 1.0, "text": " kleine" if start == 0.0 else " carousel"}]

        result = transcribe_stream(
            VIDEO, tmp_path, downloader=fake_downloader(1200), decoder=decoder,
            chunk_seconds=600,
            glossary=Glossary(terms=[], replacements={"kleine carousel": "Kleines Karussell"}))

        assert "".join(w["text"] for w in result.words).strip() == "Kleines Karussell"

    def test_the_glossary_reaches_every_decoded_chunk(self, tmp_path):
        from yt_shorts.glossary import Glossary

        glossary = Glossary(terms=["Karussell"], replacements={})
        decoder = fake_decoder()
        transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(1200),
                          decoder=decoder, chunk_seconds=600, glossary=glossary)
        assert decoder.glossaries == [glossary, glossary]

    def test_no_glossary_leaves_the_words_untouched(self, tmp_path):
        result = transcribe_stream(VIDEO, tmp_path, downloader=fake_downloader(300),
                                    decoder=fake_decoder())
        assert [w["text"].strip() for w in result.words] == ["s0"]
```

Add `import subprocess` to `tests/test_stream_transcribe.py`'s imports if it is not already there.

And append to `tests/test_detect.py`:

```python
class TestGlossaryIsPassedToTheTranscriber:
    def test_config_glossary_reaches_the_transcriber(self, tmp_path):
        from yt_shorts.glossary import Glossary

        seen = {}
        words = words_at([10, 40], ["ok", "crash"])
        inner = fake_transcriber(words)

        def transcriber(video_id, workspace_dir, **kwargs):
            seen.update(kwargs)
            return inner(video_id, workspace_dir)

        cfg = config(["crash"])
        cfg["glossary"] = Glossary(terms=["Karussell"], replacements={})
        detect_moments("vid", tmp_path / "ws", tmp_path / "ev", cfg,
                       stream_title="ERF", transcriber=transcriber,
                       measure_loudness=lambda p, s, e: -5.0)

        assert seen["glossary"] == cfg["glossary"]

    def test_a_config_without_a_glossary_still_works(self, tmp_path):
        """cmd_detect always has one from profile.load, but detect_moments is
        called with hand-built configs in tests and must not KeyError."""
        from yt_shorts.glossary import EMPTY

        seen = {}
        words = words_at([10, 40], ["ok", "crash"])
        inner = fake_transcriber(words)

        def transcriber(video_id, workspace_dir, **kwargs):
            seen.update(kwargs)
            return inner(video_id, workspace_dir)

        detect_moments("vid", tmp_path / "ws", tmp_path / "ev", config(["crash"]),
                       stream_title="ERF", transcriber=transcriber,
                       measure_loudness=lambda p, s, e: -5.0)

        assert seen["glossary"] == EMPTY
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py tests/test_detect.py -q
```
Expected: FAIL — `TypeError: subprocess_decoder() got an unexpected keyword argument 'glossary'` and `KeyError: 'glossary'` / assertion failures on the un-corrected words.

- [ ] **Step 3: Implement the decoder half**

In `src/yt_shorts/stream_transcribe.py`, add to the imports:

```python
from . import glossary as _glossary
from .glossary import Glossary
```

and replace `subprocess_decoder`:

```python
def subprocess_decoder(audio_path, start, length, *, glossary: Glossary = _glossary.EMPTY,
                       ffmpeg="ffmpeg", model_name="small",
                       runner=run_with_timeout) -> list[dict]:
    """Extracts [start, start+length] to a wav and decodes it in a killable worker.

    `glossary`'s terms are handed to the worker as the hotword bias for this
    chunk, via a JSON file whose path becomes the worker's argv[3]. That
    parameter has existed in _decode_worker.main since D2a and was never
    passed - which is why every stream chunk in this project decoded with no
    bias at all. The file lives in the same TemporaryDirectory as the wav, so
    it is cleaned up with it and never lands in the workspace.

    An EMPTY glossary appends nothing, keeping the argv byte-identical to what
    it was before this parameter existed: "no hotwords" and "hotwords=''" are
    not the same request (see glossary.hotwords), so an empty glossary must
    not reach the worker as an empty one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "chunk.wav"
        extract = [ffmpeg, "-v", "error", "-y", "-ss", str(start), "-t", str(length),
                   "-i", str(audio_path), "-ac", "1", "-ar", "16000", str(wav)]
        subprocess.run(extract, capture_output=True, text=True, timeout=900, check=True)
        args = [sys.executable, "-m", "yt_shorts._decode_worker", str(wav), model_name]
        if glossary.terms or glossary.replacements:
            glossary_path = Path(tmp) / "glossary.json"
            glossary_path.write_text(
                json.dumps({"terms": list(glossary.terms),
                            "replacements": dict(glossary.replacements)}),
                encoding="utf-8")
            args.append(str(glossary_path))
        _logger.info("decoding chunk at %.0fs (%.0fs) with model %s, %d hotword(s)",
                     start, length, model_name, len(glossary.terms))
        out = runner(args, timeout=CHUNK_TIMEOUT_SECONDS, env=_worker_env())
    return json.loads(out)
```

- [ ] **Step 4: Implement the assembly half**

Replace `transcribe_stream`'s signature and add the correction. The signature becomes:

```python
def transcribe_stream(video_id, workspace_dir, *, glossary: Glossary = _glossary.EMPTY,
                      downloader=ytdlp_downloader,
                      decoder=subprocess_decoder,
                      chunk_seconds: int = 600) -> StreamTranscript:
```

and its docstring gains, after the existing paragraph:

```
    ``glossary`` (see yt_shorts.glossary) is applied at BOTH ends: its terms
    bias each chunk's decode, and its replacements correct the ASSEMBLED word
    list just before StreamTranscript and transcript.json are built. The
    per-chunk cache files keep the RAW decode output, deliberately:

    - a glossary change then takes effect on the next assembly with NO
      re-decode, which for an hours-long stream is the difference between
      seconds and half an hour;
    - a correction may span a chunk boundary, because two adjacent chunks'
      words are adjacent in the assembled list - applying per chunk before
      caching would lose exactly those matches.

    The consequence, and it is real: chunks decoded BEFORE a glossary change
    keep the old decoder bias. The correction half still fixes their text,
    but a term the decoder could have heard correctly with a hotword stays a
    guess. Recovering the bias means deleting streams/<video_id>/chunks/ and
    running again. The chunk cache key stays (video_id, start, length) -
    fingerprinting the glossary into it would invalidate hours of decode on
    every edit in the studio's glossary editor, the wrong trade for a
    best-effort bias.
```

Change the decoder call inside the loop:

```python
            chunk_words = offset_words(
                decoder(audio.path, start, length, glossary=glossary), start)
```

and insert the correction immediately after the `for` loop ends, BEFORE the `decoded = …` summary block, so the logged word count matches what transcript.json actually holds:

```python
    # Corrections run on the assembled list, not per chunk - see the
    # docstring above on why the cache stays raw.
    words = _glossary.apply(words, glossary)

    decoded = len(windows) - len(missing)
```

- [ ] **Step 5: Pass the profile's glossary from `detect.py`**

In `src/yt_shorts/detect.py`, add to the imports:

```python
from .glossary import EMPTY as GLOSSARY_EMPTY
```

and change the transcription call:

```python
    transcript = transcriber(video_id, workspace_dir,
                             glossary=config.get("glossary", GLOSSARY_EMPTY))
```

Extend `detect_moments`'s docstring with:

```
    ``config["glossary"]`` (four merged layers, see profile._load_glossary) is
    handed to the transcriber, which is the only path by which a stream's
    proper nouns are ever corrected - `.get` with EMPTY rather than `[...]`
    because detect_moments is also called with hand-built configs.
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_stream_transcribe.py tests/test_detect.py -q
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/stream_transcribe.py src/yt_shorts/detect.py tests/test_stream_transcribe.py tests/test_detect.py
git commit -m "fix(stream): pass the glossary to the decode worker and apply it at assembly"
```

---

### Task 4: `glossary_admin.py` — the studio's pure write path

**Files:**
- Create: `src/yt_shorts/glossary_admin.py`
- Test: `tests/test_glossary_admin.py` (create)

**Interfaces:**
- Consumes: `glossary.DEFAULT_LAYER`, `glossary.EMPTY_LAYER`, `glossary.load`, `glossary.parse_layer`, `workspace.glossary_path`, `pathnames.validate_segment`.
- Produces:
  - `SOURCES = ("default", "workspace", "channel", "event")`
  - `GlossaryAdminError(Exception)` with `.kind ∈ {"bad_name", "not_found", "bad_glossary"}`
  - `read(root, *, channel=None, event=None) -> dict` with keys `scope`, `own`, `effective`, `problems`:
    - `own = {"terms": {spelling: bool}, "replacements": {raw_key: str | None}}`
    - `effective = {"terms": {key: {"term": str, "enabled": bool, "source": str}}, "replacements": {key: {"key": str, "value": str | None, "source": str}}}`
  - `update(root, terms, replacements, *, channel=None, event=None) -> None`
  - `adopt_default(root) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_glossary_admin.py`:

```python
import json

import pytest

from yt_shorts import glossary as glossary_module
from yt_shorts import glossary_admin


@pytest.fixture
def root(tmp_path):
    (tmp_path / "channels" / "erf" / "events" / "race").mkdir(parents=True)
    return tmp_path


class TestScopeResolution:
    def test_workspace_scope(self, root):
        assert glossary_admin.read(root)["scope"] == "workspace"

    def test_channel_scope(self, root):
        assert glossary_admin.read(root, channel="erf")["scope"] == "channel"

    def test_event_scope(self, root):
        assert glossary_admin.read(root, channel="erf", event="race")["scope"] == "event"

    def test_unsafe_channel_segment_is_bad_name(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="../etc")
        assert excinfo.value.kind == "bad_name"

    def test_unsafe_event_segment_is_bad_name(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="erf", event="..")
        assert excinfo.value.kind == "bad_name"

    def test_segment_is_validated_before_existence(self, root):
        """bad_name must win over not_found, so a traversal attempt never
        reaches a filesystem check - the same order event_brand_admin keeps."""
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="../nope", event="..")
        assert excinfo.value.kind == "bad_name"

    def test_unknown_channel_is_not_found(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="nope")
        assert excinfo.value.kind == "not_found"

    def test_unknown_event_is_not_found(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.read(root, channel="erf", event="nope")
        assert excinfo.value.kind == "not_found"


class TestRead:
    def test_the_default_is_visible_with_its_source(self, root):
        state = glossary_admin.read(root)
        assert state["effective"]["terms"]["karussell"] == {
            "term": "Karussell", "enabled": True, "source": "default"}
        assert state["effective"]["replacements"]["kessichen"] == {
            "key": "kessichen", "value": "Kesselchen", "source": "default"}

    def test_own_is_empty_when_no_file_exists(self, root):
        state = glossary_admin.read(root)
        assert state["own"] == {"terms": {}, "replacements": {}}
        assert state["problems"] == []

    def test_a_channel_entry_is_own_at_channel_scope_and_absent_at_workspace(self, root):
        glossary_admin.update(root, {"Rei Racing": True}, {}, channel="erf")

        channel_state = glossary_admin.read(root, channel="erf")
        assert channel_state["own"]["terms"] == {"Rei Racing": True}
        assert channel_state["effective"]["terms"]["rei racing"]["source"] == "channel"

        workspace_state = glossary_admin.read(root)
        assert "rei racing" not in workspace_state["effective"]["terms"]

    def test_a_workspace_entry_is_inherited_at_channel_scope(self, root):
        glossary_admin.update(root, {"Workspace Term": True}, {})

        state = glossary_admin.read(root, channel="erf")
        assert state["effective"]["terms"]["workspace term"]["source"] == "workspace"
        assert "Workspace Term" not in state["own"]["terms"]

    def test_a_disabled_entry_is_KEPT_in_effective_with_its_source(self, root):
        """Deliberately NOT merge_glossaries, which drops a disabled entry so
        scoring never sees it - an editor must show that a layer disabled
        something, struck through, rather than make it vanish."""
        glossary_admin.update(root, {"Karussell": False}, {"carousel": None})

        state = glossary_admin.read(root, channel="erf")
        assert state["effective"]["terms"]["karussell"] == {
            "term": "Karussell", "enabled": False, "source": "workspace"}
        assert state["effective"]["replacements"]["carousel"] == {
            "key": "carousel", "value": None, "source": "workspace"}

    def test_the_most_specific_layer_wins(self, root):
        glossary_admin.update(root, {}, {"carousel": "Workspace"})
        glossary_admin.update(root, {}, {"carousel": "Channel"}, channel="erf")
        glossary_admin.update(root, {}, {"carousel": "Event"}, channel="erf", event="race")

        state = glossary_admin.read(root, channel="erf", event="race")
        assert state["effective"]["replacements"]["carousel"] == {
            "key": "carousel", "value": "Event", "source": "event"}

    def test_a_scope_never_sees_a_more_specific_layer(self, root):
        glossary_admin.update(root, {"Event Term": True}, {}, channel="erf", event="race")
        assert "event term" not in glossary_admin.read(root, channel="erf")["effective"]["terms"]

    def test_a_malformed_layer_is_a_problem_not_an_exception(self, root):
        (root / "glossary.json").write_text("{not json", encoding="utf-8")

        state = glossary_admin.read(root, channel="erf")

        assert len(state["problems"]) == 1
        assert "not valid JSON" in state["problems"][0]
        # The other layers still load - one bad file must not 500 the route.
        assert state["effective"]["terms"]["karussell"]["source"] == "default"

    def test_a_malformed_own_layer_still_reads(self, root):
        (root / "channels" / "erf" / "glossary.json").write_text("{nope", encoding="utf-8")
        state = glossary_admin.read(root, channel="erf")
        assert state["own"] == {"terms": {}, "replacements": {}}
        assert len(state["problems"]) == 1


class TestUpdate:
    def test_writes_the_layer_and_nothing_else(self, root):
        glossary_admin.update(root, {"Rei Racing": True}, {"very very": "Rei Racing"},
                              channel="erf")

        written = json.loads(
            (root / "channels" / "erf" / "glossary.json").read_text(encoding="utf-8"))
        assert written == {"terms": {"Rei Racing": True},
                           "replacements": {"very very": "Rei Racing"}}
        assert not (root / "glossary.json").exists()

    def test_an_empty_update_still_writes_the_file(self, root):
        """"I cleared this layer" is an explicit, re-editable state, not a
        deletion - same contract lexicon_admin.update keeps."""
        glossary_admin.update(root, {}, {})
        assert json.loads((root / "glossary.json").read_text(encoding="utf-8")) == {
            "terms": {}, "replacements": {}}

    def test_an_accepted_update_is_one_profile_load_accepts(self, root):
        """The invariant every admin module keeps: what update() writes,
        glossary.load reads back without a defect - and glossary.load is
        exactly what profile._load_glossary calls per layer."""
        glossary_admin.update(root, {"Rei Racing": True}, {"very very": "Rei Racing"},
                              channel="erf")
        layer = glossary_module.load(root / "channels" / "erf" / "glossary.json")
        assert layer.terms["rei racing"] == ("Rei Racing", True)
        assert layer.replacements["very very"] == ("very very", "Rei Racing")

    def test_a_bad_payload_never_reaches_disk(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {"Karussell": 1.5}, {})
        assert excinfo.value.kind == "bad_glossary"
        assert not (root / "glossary.json").exists()

    def test_an_empty_replacement_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {"carousel": ""})
        assert excinfo.value.kind == "bad_glossary"


class TestAdoptDefault:
    def test_copies_the_default_into_the_workspace_layer(self, root):
        glossary_admin.adopt_default(root)

        state = glossary_admin.read(root)
        assert state["own"]["terms"]["Karussell"] is True
        assert state["own"]["replacements"]["kessichen"] == "Kesselchen"
        assert state["effective"]["terms"]["karussell"]["source"] == "workspace"

    def test_is_ADDITIVE_and_preserves_own_entries(self, root):
        """The lexicon's final review found the overwrite version of this
        DELETING custom entries while the studio's own dialog promised it
        changed nothing. Same promise here, same requirement."""
        glossary_admin.update(root, {"Rei Racing": True, "Karussell": False},
                              {"very very": "Rei Racing", "carousel": None})

        glossary_admin.adopt_default(root)

        state = glossary_admin.read(root)
        assert state["own"]["terms"]["Rei Racing"] is True
        assert state["own"]["terms"]["Karussell"] is False        # the disable survives
        assert state["own"]["replacements"]["very very"] == "Rei Racing"
        assert state["own"]["replacements"]["carousel"] is None   # so does this one
        assert state["own"]["replacements"]["kessichen"] == "Kesselchen"

    def test_is_idempotent(self, root):
        glossary_admin.adopt_default(root)
        first = (root / "glossary.json").read_text(encoding="utf-8")
        glossary_admin.adopt_default(root)
        assert (root / "glossary.json").read_text(encoding="utf-8") == first

    def test_does_not_change_what_is_effective(self, root):
        before = glossary_admin.read(root)["effective"]
        glossary_admin.adopt_default(root)
        after = glossary_admin.read(root)["effective"]
        # Only the source layer changes; every term and value is identical.
        assert {k: (v["term"], v["enabled"]) for k, v in after["terms"].items()} == \
               {k: (v["term"], v["enabled"]) for k, v in before["terms"].items()}
        assert {k: v["value"] for k, v in after["replacements"].items()} == \
               {k: v["value"] for k, v in before["replacements"].items()}

    def test_a_broken_workspace_layer_does_not_block_adopting(self, root):
        (root / "glossary.json").write_text("{not json", encoding="utf-8")
        glossary_admin.adopt_default(root)
        assert glossary_admin.read(root)["own"]["terms"]["Karussell"] is True


class TestNoFastAPI:
    def test_module_imports_no_fastapi(self):
        """CLAUDE.md's rule for every pure admin module. Asserting the source
        rather than sys.modules: FastAPI is almost certainly already imported
        by another test in the same session, so absence from sys.modules would
        prove nothing."""
        from pathlib import Path
        text = Path(glossary_admin.__file__).read_text(encoding="utf-8")
        assert "fastapi" not in text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_admin.py -q
```
Expected: `ModuleNotFoundError: No module named 'yt_shorts.glossary_admin'`.

- [ ] **Step 3: Implement `src/yt_shorts/glossary_admin.py`**

```python
"""Read and update the three writable glossary layers - workspace, channel,
event - the studio's write path onto yt_shorts.glossary. Pure, no FastAPI.

Mirrors lexicon_admin.py's shape deliberately, down to the names: a typed
error carrying a `kind`, segment validation before any filesystem touch, the
"what this accepts, profile.load accepts" invariant (see update's use of
glossary.parse_layer), and an `effective` map that KEEPS disabled entries
where the scoring merge drops them.

The built-in default (glossary.DEFAULT_LAYER) is never written here except by
adopt_default, which copies it into the workspace layer so an operator can
start editing the corner list without retyping 32 terms.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import glossary as glossary_module
from . import pathnames, workspace

SOURCES = ("default", "workspace", "channel", "event")


class GlossaryAdminError(Exception):
    """kind: "bad_name" | "not_found" | "bad_glossary".
    Maps to HTTP: bad_name/bad_glossary -> 400, not_found -> 404."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_segment(value: str, what: str) -> None:
    try:
        pathnames.validate_segment(value, what=what)
    except ValueError as error:
        raise GlossaryAdminError(str(error), kind="bad_name") from error


def _resolve(root, channel: str | None, event: str | None) -> tuple[str, Path]:
    """The scope name and its own-layer glossary.json path. Every segment given
    is validated (bad_name) BEFORE either segment's existence is checked
    (not_found), and existence is checked before anything else touches the
    filesystem - mirroring lexicon_admin._resolve."""
    if channel is not None:
        _validate_segment(channel, "channel name")
    if event is not None:
        _validate_segment(event, "event name")
    if channel is None:
        return "workspace", workspace.glossary_path(root)
    channel_dir = Path(root) / "channels" / channel
    if not channel_dir.is_dir():
        raise GlossaryAdminError(f"unknown channel: {channel!r}", kind="not_found")
    if event is None:
        return "channel", channel_dir / "glossary.json"
    event_dir = channel_dir / "events" / event
    if not event_dir.is_dir():
        raise GlossaryAdminError(f"unknown event: {event!r}", kind="not_found")
    return "event", event_dir / "glossary.json"


def _load_layer_or_empty(path: Path, problems: list[str] | None = None):
    """glossary.load, degraded: a malformed layer becomes EMPTY_LAYER instead
    of raising, mirroring profile._load_glossary's handling of the identical
    failure mode - a bad glossary.json at one layer must not break every layer
    above it, nor 500 a route that just wrote a DIFFERENT layer successfully.

    When `problems` is given the error is appended to it. adopt_default has no
    problems list to report into: its own workspace layer is about to be
    rewritten anyway, so a broken file there degrades to "nothing to preserve"
    rather than blocking the very operation that would fix it."""
    try:
        return glossary_module.load(path)
    except (OSError, ValueError) as error:
        # Deliberately swallowed here, not re-raised: see the docstring above.
        if problems is not None:
            problems.append(f"{path}: {error}")
        return glossary_module.EMPTY_LAYER


def _layers(root, channel: str | None, event: str | None) -> tuple[list, list[str]]:
    """Every layer that applies at this scope, least specific first, as
    (source, layer) pairs, plus a problem string per layer that failed to
    load. Stops at the requested scope: a channel-scoped call yields
    default/workspace/channel only, never an event layer the caller did not
    ask about."""
    problems: list[str] = []
    layers = [("default", glossary_module.DEFAULT_LAYER)]
    workspace_path = workspace.glossary_path(root)
    layers.append(("workspace", _load_layer_or_empty(workspace_path, problems)))
    if channel is None:
        return layers, problems
    channel_path = Path(root) / "channels" / channel / "glossary.json"
    layers.append(("channel", _load_layer_or_empty(channel_path, problems)))
    if event is None:
        return layers, problems
    event_path = Path(root) / "channels" / channel / "events" / event / "glossary.json"
    layers.append(("event", _load_layer_or_empty(event_path, problems)))
    return layers, problems


def _own_shape(layer) -> dict:
    """A layer as the file/wire shape a PUT sends back: terms keyed by their
    raw spelling, replacements keyed by their raw key. Rebuilt from the parsed
    layer rather than echoed from disk, so a GET always returns the canonical
    form regardless of which accepted shape the file on disk happens to use
    (a plain list of terms, for instance)."""
    return {
        "terms": {spelling: enabled for spelling, enabled in layer.terms.values()},
        "replacements": {raw: text for raw, text in layer.replacements.values()},
    }


def read(root, *, channel: str | None = None, event: str | None = None) -> dict:
    """{"scope", "own", "effective", "problems"} for the given scope.

    `effective` is NOT profile.merge_glossaries: that function deliberately
    DROPS a disabled entry so neither the decoder bias nor the corrections see
    it, which is correct for transcription and wrong for an editor - an
    operator needs to see that a channel or event disabled something it
    inherited, struck through rather than absent. So this walks `_layers`
    itself, least specific first, letting each later layer's entry (value AND
    source) overwrite the earlier one, disabled entries included. Do not
    "simplify" this into a merge_glossaries call; that would silently make
    disabled entries invisible.

    `problems` lists any layer that failed to load, by path and error - the
    caller returns it as-is so the editor can warn while staying usable,
    rather than the whole scope 500ing over one bad file."""
    scope, _target = _resolve(root, channel, event)
    layers, problems = _layers(root, channel, event)
    own = _own_shape(layers[-1][1])  # the requested scope's own layer is last
    effective_terms: dict[str, dict] = {}
    effective_reps: dict[str, dict] = {}
    for source, layer in layers:
        for key, (spelling, enabled) in layer.terms.items():
            effective_terms[key] = {"term": spelling, "enabled": enabled, "source": source}
        for key, (raw, text) in layer.replacements.items():
            effective_reps[key] = {"key": raw, "value": text, "source": source}
    return {"scope": scope, "own": own,
            "effective": {"terms": effective_terms, "replacements": effective_reps},
            "problems": problems}


def update(root, terms, replacements, *,
           channel: str | None = None, event: str | None = None) -> None:
    """Overwrites the scope's own layer with exactly `terms` and
    `replacements` - never a merge. Empty dicts still write
    `{"terms": {}, "replacements": {}}` (rather than deleting the file) so
    "I cleared this layer" is an explicit, re-editable state.

    Validated through glossary.parse_layer - the same function profile.load
    validates a file with - so a payload this accepts is one profile.load
    accepts, and a bad payload never reaches disk."""
    try:
        layer = glossary_module.parse_layer({"terms": terms, "replacements": replacements})
    except ValueError as error:
        raise GlossaryAdminError(str(error), kind="bad_glossary") from error
    _scope, target = _resolve(root, channel, event)
    target.write_text(json.dumps(_own_shape(layer), indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def adopt_default(root) -> None:
    """Copies glossary.DEFAULT_LAYER into the workspace layer's own entries,
    ADDITIVELY: existing own entries are PRESERVED and WIN over the default
    for any entry both define, exactly like a more specific layer already
    wins over a less specific one everywhere else in this module.

    This must NOT be an `update(root, DEFAULT_TERMS, DEFAULT_REPLACEMENTS)`:
    `update` OVERWRITES the whole layer, so that call would DELETE every
    custom entry the operator had saved here and re-enable anything they had
    disabled - both change what a transcript comes out as, directly
    contradicting the studio's own confirmation dialog ("This does not change
    what a transcript currently comes out as - the built-in default already
    underlies every scope. It only makes it editable."). The lexicon's final
    review caught exactly this bug in the equivalent function; building the
    merged layer here instead keeps the promise true.

    Idempotent: after a first adopt, `own` already carries every default entry
    with its final value, so a second adopt's `{**default, **own}` reproduces
    it byte for byte."""
    own = _load_layer_or_empty(workspace.glossary_path(root))
    merged_terms = {**glossary_module.DEFAULT_LAYER.terms, **own.terms}
    merged_reps = {**glossary_module.DEFAULT_LAYER.replacements, **own.replacements}
    update(root,
           {spelling: enabled for spelling, enabled in merged_terms.values()},
           {raw: text for raw, text in merged_reps.values()})
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_admin.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/glossary_admin.py tests/test_glossary_admin.py
git commit -m "feat(studio): glossary_admin - read, update and additively adopt the default"
```

---

### Task 5: The six studio routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (route list docstring, `GlossaryBody`, six routes)
- Test: `tests/test_studio_glossary_api.py` (create)

**Interfaces:**
- Consumes: `glossary_admin.read/update/adopt_default`, `GlossaryAdminError.kind` from Task 4.
- Produces:
  - `GET`/`PUT /api/glossary`
  - `GET`/`PUT /api/channels/{channel}/glossary`
  - `GET`/`PUT /api/channels/{channel}/events/{event}/glossary`
  - `POST /api/glossary/adopt-default`
  - Body: `{"terms": {str: bool}, "replacements": {str: str | null}}`

- [ ] **Step 1: Write the failing tests**

Read `tests/test_studio_moments_api.py` first — it establishes the client/workspace-isolation fixtures this file mirrors. Create `tests/test_studio_glossary_api.py`:

```python
"""The six glossary routes (GET/PUT at three scopes + adopt-default), a thin
layer over glossary_admin - so these tests assert the HTTP contract (status
codes, the segment guard, which layer a PUT touches), not the merge logic
tests/test_glossary_admin.py already covers."""

import json

import pytest
from fastapi.testclient import TestClient

from yt_shorts import profile as profile_module
from yt_shorts.studio.api import create_app
from yt_shorts.workspace import Workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    channels = tmp_path / "channels"
    (channels / "erf" / "events" / "race").mkdir(parents=True)
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    workspace = Workspace(root=tmp_path, channels_dir=channels, origin="test")
    import yt_shorts.studio.api as api
    import yt_shorts.studio.jobs as jobs
    monkeypatch.setattr(api, "_resolve_workspace", lambda: workspace)
    monkeypatch.setattr(jobs, "_resolve_workspace", lambda: workspace)
    app = create_app()
    with TestClient(app) as test_client:
        test_client.root = tmp_path
        yield test_client


class TestGet:
    def test_workspace_scope_returns_the_default(self, client):
        body = client.get("/api/glossary").json()
        assert body["scope"] == "workspace"
        assert body["effective"]["terms"]["karussell"]["source"] == "default"
        assert body["own"] == {"terms": {}, "replacements": {}}
        assert body["problems"] == []

    def test_channel_scope(self, client):
        body = client.get("/api/channels/erf/glossary").json()
        assert body["scope"] == "channel"

    def test_event_scope(self, client):
        body = client.get("/api/channels/erf/events/race/glossary").json()
        assert body["scope"] == "event"

    def test_unknown_channel_is_404(self, client):
        assert client.get("/api/channels/nope/glossary").status_code == 404

    def test_unknown_event_is_404(self, client):
        assert client.get("/api/channels/erf/events/nope/glossary").status_code == 404

    def test_a_traversal_segment_never_reaches_disk(self, client):
        # Starlette normalises "..", so send an encoded segment: the guard,
        # not the router, is what must refuse it.
        response = client.get("/api/channels/%2E%2E/glossary")
        assert response.status_code in (400, 404)


class TestPut:
    def test_writes_only_the_addressed_layer(self, client):
        response = client.put("/api/channels/erf/glossary", json={
            "terms": {"Rei Racing": True},
            "replacements": {"very very": "Rei Racing"},
        })

        assert response.status_code == 200
        assert response.json()["own"]["terms"] == {"Rei Racing": True}
        written = json.loads(
            (client.root / "channels" / "erf" / "glossary.json").read_text(encoding="utf-8"))
        assert written["terms"] == {"Rei Racing": True}
        assert not (client.root / "glossary.json").exists()

    def test_returns_the_freshly_read_state(self, client):
        body = client.put("/api/glossary", json={
            "terms": {"Workspace Term": True}, "replacements": {}}).json()
        assert body["effective"]["terms"]["workspace term"]["source"] == "workspace"

    def test_null_disables_a_replacement(self, client):
        body = client.put("/api/glossary", json={
            "terms": {}, "replacements": {"carousel": None}}).json()
        assert body["effective"]["replacements"]["carousel"]["value"] is None

    def test_an_invalid_payload_is_400(self, client):
        response = client.put("/api/glossary", json={
            "terms": {}, "replacements": {"carousel": ""}})
        assert response.status_code == 400
        assert not (client.root / "glossary.json").exists()

    def test_an_unknown_event_is_404(self, client):
        response = client.put("/api/channels/erf/events/nope/glossary",
                              json={"terms": {}, "replacements": {}})
        assert response.status_code == 404

    def test_a_malformed_other_layer_does_not_500_a_good_write(self, client):
        (client.root / "glossary.json").write_text("{not json", encoding="utf-8")
        response = client.put("/api/channels/erf/glossary",
                              json={"terms": {"Ok": True}, "replacements": {}})
        assert response.status_code == 200
        assert len(response.json()["problems"]) == 1


class TestAdoptDefault:
    def test_writes_the_workspace_layer(self, client):
        body = client.post("/api/glossary/adopt-default").json()
        assert body["own"]["terms"]["Karussell"] is True
        assert (client.root / "glossary.json").exists()

    def test_preserves_an_existing_own_entry(self, client):
        client.put("/api/glossary", json={
            "terms": {"Rei Racing": True}, "replacements": {}})
        body = client.post("/api/glossary/adopt-default").json()
        assert body["own"]["terms"]["Rei Racing"] is True
        assert body["own"]["terms"]["Karussell"] is True


class TestRoutesPrecedeTheSpaFallback:
    def test_a_glossary_route_is_not_shadowed_by_index_html(self, client):
        response = client.get("/api/glossary")
        assert response.headers["content-type"].startswith("application/json")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_glossary_api.py -q
```
Expected: FAIL — 404 on every glossary route.

- [ ] **Step 3: Implement the routes**

In `src/yt_shorts/studio/api.py`, add to the imports beside `lexicon_admin`:

```python
from .. import glossary_admin
```

Add the body model next to `MarkersBody`:

```python
class GlossaryBody(BaseModel):
    """Body for PUT …/glossary: the scope's own layer, in full - see
    glossary_admin.update's own docstring on why this always overwrites
    rather than merges. `terms` maps a term's spelling to whether it is
    enabled (false disables one inherited from a less specific layer);
    `replacements` maps a key to its replacement text, or null to disable an
    inherited one."""
    terms: dict[str, bool]
    replacements: dict[str, str | None]
```

Add the routes immediately after the moments block (so they land before the SPA fallback, like every other `/api` route):

```python
    # ---- Glossary: workspace, channel and event layers ----
    # Segment validation (bad_name) and existence checks (not_found) both
    # happen INSIDE glossary_admin, before any filesystem touch (see its own
    # _resolve) - these routes only map the resulting kind to a status, the
    # same way the moments routes above do. No second, possibly divergent
    # guard is added here.

    def _glossary_or_http(call):
        """Run a glossary_admin call, mapping its typed error to a status the
        way every other admin route in this file does."""
        try:
            return call()
        except glossary_admin.GlossaryAdminError as error:
            status = {"bad_name": 400, "not_found": 404, "bad_glossary": 400}
            raise HTTPException(status_code=status.get(error.kind, 400),
                                detail=str(error)) from error

    @app.get("/api/glossary")
    def get_workspace_glossary() -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(lambda: glossary_admin.read(root))

    @app.put("/api/glossary")
    def put_workspace_glossary(body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(root, body.terms, body.replacements))
        return _glossary_or_http(lambda: glossary_admin.read(root))

    @app.get(CH + "/glossary")
    def get_channel_glossary(channel: str) -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(lambda: glossary_admin.read(root, channel=channel))

    @app.put(CH + "/glossary")
    def put_channel_glossary(channel: str, body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, channel=channel))
        return _glossary_or_http(lambda: glossary_admin.read(root, channel=channel))

    @app.get(EV + "/glossary")
    def get_event_glossary(channel: str, event: str) -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(
            lambda: glossary_admin.read(root, channel=channel, event=event))

    @app.put(EV + "/glossary")
    def put_event_glossary(channel: str, event: str, body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, channel=channel, event=event))
        return _glossary_or_http(
            lambda: glossary_admin.read(root, channel=channel, event=event))

    @app.post("/api/glossary/adopt-default")
    def post_adopt_default_glossary() -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.adopt_default(root))
        return _glossary_or_http(lambda: glossary_admin.read(root))
```

Extend the module's route-list docstring (beside the `…/moments` lines):

```
  GET   /api/glossary                                       the workspace's glossary layer
  PUT   /api/glossary                                       overwrite the workspace layer
  GET   /api/channels/{channel}/glossary                    the channel's layer, workspace inherited
  PUT   /api/channels/{channel}/glossary                    overwrite the channel layer
  GET   …/events/{event}/glossary     the event's layer, channel inherited
  PUT   …/events/{event}/glossary     overwrite the event layer
  POST  /api/glossary/adopt-default                         copy the built-in default into the workspace layer
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_glossary_api.py tests/test_studio_api.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_glossary_api.py
git commit -m "feat(studio): glossary routes at all three scopes plus adopt-default"
```

---

### Task 6: Frontend client and pure layer logic

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Create: `src/yt_shorts/studio/web/src/glossaryLayers.ts`
- Test: `src/yt_shorts/studio/web/src/glossaryLayers.test.ts` (create)

**Interfaces:**
- Consumes: the wire shape from Task 5; `channelBase`/`eventBase`/`asJson` already in `api.ts`; `sourceLabel` from `./momentsLexicon` (reused, not duplicated — the source union is identical).
- Produces (from `api.ts`): `LayerSource`, `EffectiveTerm`, `EffectiveReplacement`, `GlossaryLayers`, `getGlossary(scope)`, `putGlossary(scope, terms, replacements)`, `adoptDefaultGlossary()`.
- Produces (from `glossaryLayers.ts`): `TermRow`, `ReplacementRow`, `toTermRows`, `toReplacementRows`, `overrideRow`, `disableRow`, `removeOwnRow`, `addOwnTermRow`, `addOwnReplacementRow`, `setReplacementText`, `rowsToOwn`, `pendingRemovals`, `normaliseTerm`, `normaliseKey`.

- [ ] **Step 1: Write the failing Vitest tests**

Create `src/yt_shorts/studio/web/src/glossaryLayers.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import type { GlossaryLayers } from './api'
import {
  addOwnReplacementRow,
  addOwnTermRow,
  disableRow,
  normaliseKey,
  normaliseTerm,
  overrideRow,
  pendingRemovals,
  removeOwnRow,
  rowsToOwn,
  setReplacementText,
  toReplacementRows,
  toTermRows,
} from './glossaryLayers'

function layers(overrides: Partial<GlossaryLayers> = {}): GlossaryLayers {
  return {
    scope: 'channel',
    own: { terms: {}, replacements: {} },
    effective: { terms: {}, replacements: {} },
    problems: [],
    ...overrides,
  }
}

describe('normalisation', () => {
  it('lower-cases and trims a term', () => {
    expect(normaliseTerm('  Karussell ')).toBe('karussell')
  })

  it('strips punctuation per token in a key, matching the backend', () => {
    expect(normaliseKey('Kleine, Carousel')).toBe('kleine carousel')
  })

  it('collapses runs of whitespace in a key', () => {
    expect(normaliseKey('kleine   carousel')).toBe('kleine carousel')
  })
})

describe('toTermRows', () => {
  it('marks a row own when the scope has its own entry', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Karussell: true }, replacements: {} },
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: true, source: 'channel' } },
          replacements: {},
        },
      }),
    )
    expect(rows).toEqual([
      { key: 'karussell', term: 'Karussell', enabled: true, source: 'channel', own: true },
    ])
  })

  it('marks an inherited row not own', () => {
    const rows = toTermRows(
      layers({
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: true, source: 'default' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0].own).toBe(false)
  })

  it('sorts own first, then disabled last, then alphabetically', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Bravo: true }, replacements: {} },
        effective: {
          terms: {
            alpha: { term: 'Alpha', enabled: true, source: 'default' },
            bravo: { term: 'Bravo', enabled: true, source: 'channel' },
            charlie: { term: 'Charlie', enabled: false, source: 'default' },
          },
          replacements: {},
        },
      }),
    )
    expect(rows.map((r) => r.key)).toEqual(['bravo', 'alpha', 'charlie'])
  })

  it('keeps a disabled row visible with the layer that disabled it', () => {
    const rows = toTermRows(
      layers({
        effective: {
          terms: { karussell: { term: 'Karussell', enabled: false, source: 'workspace' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ enabled: false, source: 'workspace', own: false })
  })
})

describe('toReplacementRows', () => {
  it('carries the from/to pair and ownership', () => {
    const rows = toReplacementRows(
      layers({
        own: { terms: {}, replacements: { kessichen: 'Kesselchen' } },
        effective: {
          terms: {},
          replacements: {
            kessichen: { key: 'kessichen', value: 'Kesselchen', source: 'channel' },
          },
        },
      }),
    )
    expect(rows).toEqual([
      {
        key: 'kessichen',
        from: 'kessichen',
        to: 'Kesselchen',
        enabled: true,
        source: 'channel',
        own: true,
      },
    ])
  })

  it('renders a disabled replacement with an empty target', () => {
    const rows = toReplacementRows(
      layers({
        effective: {
          terms: {},
          replacements: { carousel: { key: 'carousel', value: null, source: 'event' } },
        },
      }),
    )
    expect(rows[0]).toMatchObject({ to: '', enabled: false })
  })
})

describe('row actions', () => {
  const inherited = [
    { key: 'karussell', term: 'Karussell', enabled: true, source: 'default' as const, own: false },
  ]

  it('override copies an inherited row into the own layer at its current value', () => {
    expect(overrideRow(inherited, 'karussell')[0]).toMatchObject({ own: true, enabled: true })
  })

  it('disable creates an own entry pinned to disabled', () => {
    expect(disableRow(inherited, 'karussell')[0]).toMatchObject({ own: true, enabled: false })
  })

  it('override is a no-op for an unknown key', () => {
    expect(overrideRow(inherited, 'nope')).toEqual(inherited)
  })

  it('remove drops the row entirely', () => {
    const own = [{ ...inherited[0], own: true }]
    expect(removeOwnRow(own, 'karussell')).toEqual([])
  })

  it('setReplacementText edits an own row without re-sorting', () => {
    const rows = [
      { key: 'b', from: 'b', to: 'B', enabled: true, source: 'channel' as const, own: true },
      { key: 'a', from: 'a', to: 'A', enabled: true, source: 'channel' as const, own: true },
    ]
    const next = setReplacementText(rows, 'a', 'AA')
    expect(next.map((r) => r.key)).toEqual(['b', 'a'])
    expect(next[1].to).toBe('AA')
  })

  it('setReplacementText ignores an inherited row', () => {
    expect(setReplacementText(inherited.map((r) => ({
      key: r.key, from: r.key, to: 'x', enabled: true, source: r.source, own: false,
    })), 'karussell', 'y')[0].to).toBe('x')
  })
})

describe('addOwnTermRow', () => {
  it('adds a new own row', () => {
    const rows = addOwnTermRow([], ' Neu ')
    expect(rows).not.toBeNull()
    expect(rows![0]).toMatchObject({ key: 'neu', term: 'Neu', own: true, enabled: true })
  })

  it('promotes an already-inherited term instead of adding a second row', () => {
    const inheritedRows = [
      { key: 'karussell', term: 'Karussell', enabled: false, source: 'default' as const, own: false },
    ]
    const rows = addOwnTermRow(inheritedRows, 'KARUSSELL')
    expect(rows).toHaveLength(1)
    expect(rows![0]).toMatchObject({ own: true, enabled: true })
  })

  it('rejects a blank term', () => {
    expect(addOwnTermRow([], '   ')).toBeNull()
  })

  it('rejects a duplicate of an existing own row', () => {
    const own = [
      { key: 'neu', term: 'Neu', enabled: true, source: 'channel' as const, own: true },
    ]
    expect(addOwnTermRow(own, 'neu')).toBeNull()
  })
})

describe('addOwnReplacementRow', () => {
  it('adds a from/to pair', () => {
    const rows = addOwnReplacementRow([], ' Kessichen ', ' Kesselchen ')
    expect(rows![0]).toMatchObject({
      key: 'kessichen', from: 'Kessichen', to: 'Kesselchen', own: true, enabled: true,
    })
  })

  it('rejects a blank from or to', () => {
    expect(addOwnReplacementRow([], '', 'x')).toBeNull()
    expect(addOwnReplacementRow([], 'x', '  ')).toBeNull()
  })

  it('rejects a key that is only punctuation', () => {
    expect(addOwnReplacementRow([], '...', 'x')).toBeNull()
  })

  it('rejects a duplicate of an existing own key', () => {
    const own = [
      { key: 'kessichen', from: 'kessichen', to: 'Kesselchen', enabled: true,
        source: 'channel' as const, own: true },
    ]
    expect(addOwnReplacementRow(own, 'Kessichen,', 'Other')).toBeNull()
  })
})

describe('rowsToOwn', () => {
  it('sends ONLY own rows - an inherited row must never be promoted silently', () => {
    const termRows = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
      { key: 'b', term: 'B', enabled: true, source: 'default' as const, own: false },
    ]
    const repRows = [
      { key: 'c', from: 'c', to: 'C', enabled: true, source: 'channel' as const, own: true },
      { key: 'd', from: 'd', to: 'D', enabled: true, source: 'default' as const, own: false },
    ]
    expect(rowsToOwn(termRows, repRows)).toEqual({
      terms: { A: true },
      replacements: { c: 'C' },
    })
  })

  it('keeps a disabled own row as an explicit false/null', () => {
    const termRows = [
      { key: 'a', term: 'A', enabled: false, source: 'channel' as const, own: true },
    ]
    const repRows = [
      { key: 'c', from: 'c', to: '', enabled: false, source: 'channel' as const, own: true },
    ]
    expect(rowsToOwn(termRows, repRows)).toEqual({
      terms: { A: false },
      replacements: { c: null },
    })
  })

  it('survives a __proto__ entry', () => {
    // Assigning into a `{}` literal at this key reassigns the prototype
    // instead of creating an own property, so the entry would silently vanish
    // from JSON.stringify - the exact bug momentsLexicon.rowsToMarkers
    // documents. Object.fromEntries has no such special case.
    const termRows = [
      { key: '__proto__', term: '__proto__', enabled: true, source: 'channel' as const, own: true },
    ]
    const payload = rowsToOwn(termRows, [])
    expect(Object.prototype.hasOwnProperty.call(payload.terms, '__proto__')).toBe(true)
    expect(JSON.parse(JSON.stringify(payload)).terms.__proto__).toBe(true)
  })
})

describe('pendingRemovals', () => {
  it('names own rows that vanished since the last save', () => {
    const saved = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
      { key: 'b', term: 'B', enabled: true, source: 'default' as const, own: false },
    ]
    expect(pendingRemovals([], saved)).toEqual(['a'])
  })

  it('ignores an edited-but-present row', () => {
    const saved = [
      { key: 'a', term: 'A', enabled: true, source: 'channel' as const, own: true },
    ]
    const now = [{ ...saved[0], enabled: false }]
    expect(pendingRemovals(now, saved)).toEqual([])
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd src/yt_shorts/studio/web && npm test -- glossaryLayers && cd -
```
Expected: FAIL — `Failed to resolve import "./glossaryLayers"`.

- [ ] **Step 3: Add the client to `api.ts`**

Append after the moments-lexicon section:

```ts
/** The four layers a glossary entry can come from (see
 * profile._load_glossary): the built-in Nordschleife default in code, the
 * workspace file, the channel's, the event's - least to most specific. */
export type LayerSource = 'default' | 'workspace' | 'channel' | 'event'

/** One entry of a glossary's effective `terms` map: the spelling to bias the
 * decoder with, whether it is enabled, and which layer set it. `enabled:
 * false` means "disabled at that layer" - glossary_admin.read deliberately
 * KEEPS a disabled entry rather than dropping it the way the transcription
 * merge does, so the editor can show it struck through. */
export interface EffectiveTerm {
  term: string
  enabled: boolean
  source: LayerSource
}

/** One entry of a glossary's effective `replacements` map: the key as its
 * author wrote it, the replacement text, and the layer that set it. A `value`
 * of null means disabled at that layer (see EffectiveTerm). */
export interface EffectiveReplacement {
  key: string
  value: string | null
  source: LayerSource
}

/** The shape every glossary route returns (GET/PUT at all three scopes, and
 * POST /api/glossary/adopt-default) - see glossary_admin.read. `own` is
 * exactly this scope's own glossary.json in the shape a PUT sends back;
 * `effective` is every entry visible at this scope, least specific layer
 * overwritten by a more specific one, disabled entries included. `problems`
 * lists any layer that failed to load and was therefore treated as empty
 * rather than raised. */
export interface GlossaryLayers {
  scope: 'workspace' | 'channel' | 'event'
  own: { terms: Record<string, boolean>; replacements: Record<string, string | null> }
  effective: {
    terms: Record<string, EffectiveTerm>
    replacements: Record<string, EffectiveReplacement>
  }
  problems: string[]
}

/** Builds the glossary base URL for a scope, mirroring momentsBase and the
 * three route pairs in api.py exactly. An `event` without a `channel` is a
 * caller bug (there is no such backend route); it falls back to the
 * workspace base rather than building a malformed URL. */
function glossaryBase(scope: { channel?: string; event?: string }): string {
  if (scope.channel && scope.event) return `${eventBase(scope.channel, scope.event)}/glossary`
  if (scope.channel) return `${channelBase(scope.channel)}/glossary`
  return '/api/glossary'
}

/** GET the glossary layers for `scope` - see GlossaryLayers. */
export function getGlossary(scope: { channel?: string; event?: string }): Promise<GlossaryLayers> {
  return fetch(glossaryBase(scope)).then(asJson<GlossaryLayers>)
}

/** PUT the glossary for `scope` - OVERWRITES that scope's own layer with
 * exactly `terms` and `replacements` (see glossary_admin.update: never a
 * merge, so a caller must always send the FULL own layer it wants, e.g. via
 * rowsToOwn in glossaryLayers.ts). Returns the freshly-read state. */
export function putGlossary(
  scope: { channel?: string; event?: string },
  terms: Record<string, boolean>,
  replacements: Record<string, string | null>,
): Promise<GlossaryLayers> {
  return fetch(glossaryBase(scope), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ terms, replacements }),
  }).then(asJson<GlossaryLayers>)
}

/** POST /api/glossary/adopt-default - copies the built-in Nordschleife
 * default into the WORKSPACE layer's own entries, ADDITIVELY (existing own
 * entries win, see glossary_admin.adopt_default), so an operator can start
 * editing the corner list. Idempotent. */
export function adoptDefaultGlossary(): Promise<GlossaryLayers> {
  return fetch('/api/glossary/adopt-default', { method: 'POST' }).then(asJson<GlossaryLayers>)
}
```

- [ ] **Step 4: Implement `glossaryLayers.ts`**

Create `src/yt_shorts/studio/web/src/glossaryLayers.ts`:

```ts
/**
 * Pure helpers for the glossary editor (GlossaryEditor.tsx wires these up).
 * No React import - unit-tested directly with Vitest, the same pattern
 * momentsLexicon.ts/words.ts/format.ts follow.
 *
 * See api.ts's GlossaryLayers for the wire shape, and glossary_admin.read's
 * own docstring (src/yt_shorts/glossary_admin.py) for why `effective`
 * deliberately KEEPS disabled entries rather than dropping them the way the
 * transcription merge does: this editor must show a disabled entry struck
 * through, with the layer that disabled it, not make it disappear.
 *
 * Terms and replacements are two lists with the same ownership rules, so the
 * row actions here are generic over anything carrying `{ key, own }` - one
 * implementation, not two that can drift apart.
 */

import type { GlossaryLayers, LayerSource } from './api'

/** One row of the Terms list: the decoder-bias spelling, its enabled state,
 * whether it belongs to THIS scope's own layer (editable in place, vs.
 * inherited and read-only until Override/Disable creates an own entry), and
 * the layer the effective value came from. */
export type TermRow = {
  key: string
  term: string
  enabled: boolean
  source: LayerSource
  own: boolean
}

/** One row of the Corrections list: what the decoder heard (`from`), what it
 * should say (`to`, empty when the entry is disabled), plus the same
 * ownership/source fields TermRow carries. */
export type ReplacementRow = {
  key: string
  from: string
  to: string
  enabled: boolean
  source: LayerSource
  own: boolean
}

type Row = { key: string; own: boolean; enabled: boolean }

/** Mirrors the backend's glossary.normalise_term (`term.strip().lower()`) -
 * used before every own-entry comparison and before storing a freshly-typed
 * term, so a row added here already carries exactly the key a save writes. */
export function normaliseTerm(input: string): string {
  return input.trim().toLowerCase()
}

/** Mirrors the backend's glossary.normalise_key: each whitespace-separated
 * token lower-cased with punctuation stripped, rejoined by single spaces.
 * This must agree with the backend, because that is the identity two layers
 * collide on - a key that normalises differently here would let the editor
 * show two rows the backend considers one. */
export function normaliseKey(input: string): string {
  return input
    .split(/\s+/)
    .map((token) => token.toLowerCase().replace(/[^\p{L}\p{N}]/gu, ''))
    .filter((token) => token !== '')
    .join(' ')
}

/** Own first (what this scope can edit belongs at the top), then enabled
 * before disabled, then alphabetically by key - stable and predictable.
 * Shared by the toRows builders and every row action that can move a row
 * between the own and inherited groups, so a row always lands where a fresh
 * load would put it. */
function sortRows<T extends Row>(rows: T[]): T[] {
  return [...rows].sort((a, b) => {
    if (a.own !== b.own) return a.own ? -1 : 1
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1
    return a.key.localeCompare(b.key)
  })
}

/** Every effective term as a row. */
export function toTermRows(layers: GlossaryLayers): TermRow[] {
  const ownKeys = new Set(
    Object.keys(layers.own.terms).map((spelling) => normaliseTerm(spelling)),
  )
  return sortRows(
    Object.entries(layers.effective.terms).map(([key, entry]) => ({
      key,
      term: entry.term,
      enabled: entry.enabled,
      source: entry.source,
      own: ownKeys.has(key),
    })),
  )
}

/** Every effective replacement as a row. A disabled entry has no target text
 * to show (the merge keeps only the winning value, which is null), so `to`
 * is empty - the row still names what it matches, which is the half that
 * identifies it. */
export function toReplacementRows(layers: GlossaryLayers): ReplacementRow[] {
  const ownKeys = new Set(Object.keys(layers.own.replacements).map((raw) => normaliseKey(raw)))
  return sortRows(
    Object.entries(layers.effective.replacements).map(([key, entry]) => ({
      key,
      from: entry.key,
      to: entry.value ?? '',
      enabled: entry.value !== null,
      source: entry.source,
      own: ownKeys.has(key),
    })),
  )
}

/** Copies an inherited row into this scope's own layer at its current value -
 * the "Override" row action. A no-op if `key` is not found. */
export function overrideRow<T extends Row>(rows: T[], key: string): T[] {
  return sortRows(rows.map((row) => (row.key === key ? { ...row, own: true } : row)))
}

/** Writes an own entry marked disabled for `key` - the "Disable" row action.
 * Works on an inherited row exactly like overrideRow, just pinned to
 * disabled instead of carrying the inherited state forward. */
export function disableRow<T extends Row>(rows: T[], key: string): T[] {
  return sortRows(
    rows.map((row) => (row.key === key ? { ...row, own: true, enabled: false } : row)),
  )
}

/** Drops `key`'s row entirely - the "Remove" action on an own row. It does
 * NOT flip the row back to inherited and leave it displayed: what would then
 * show (an inherited value, the built-in default, or nothing) cannot be known
 * reliably once the row may have been edited in this session, and a stale
 * value under an invented "inherited" label would be wrong more often than
 * helpful. The true picture arrives with the next Save's response; until
 * then the component surfaces the pending state via pendingRemovals. */
export function removeOwnRow<T extends Row>(rows: T[], key: string): T[] {
  return rows.filter((row) => row.key !== key)
}

/** Edits an own replacement's target text in place WITHOUT re-sorting -
 * re-sorting on every keystroke would shuffle the row out from under the
 * operator's cursor. A no-op for an inherited row. */
export function setReplacementText(
  rows: ReplacementRow[],
  key: string,
  to: string,
): ReplacementRow[] {
  return rows.map((row) => (row.own && row.key === key ? { ...row, to } : row))
}

/** Adds a brand-new own term, or - if the typed term already matches an
 * INHERITED row - promotes and enables that row rather than creating a second
 * row for the same key. Returns null (reject, nothing changed) for a blank
 * term or one that already matches an existing OWN row: that is a real
 * duplicate, and silently overwriting it would surprise the operator. */
export function addOwnTermRow(rows: TermRow[], termInput: string): TermRow[] | null {
  const term = termInput.trim()
  const key = normaliseTerm(term)
  if (key === '') return null
  if (rows.some((row) => row.own && row.key === key)) return null
  const index = rows.findIndex((row) => row.key === key)
  if (index !== -1) {
    return sortRows(
      rows.map((row, i) => (i === index ? { ...row, own: true, enabled: true } : row)),
    )
  }
  return sortRows([...rows, { key, term, enabled: true, source: 'default', own: true }])
}

/** Adds a brand-new own correction, or promotes an inherited one at the typed
 * target text. Returns null for a blank `from`/`to`, a `from` that normalises
 * to nothing (punctuation only - the backend refuses it too), or a duplicate
 * of an existing own row. */
export function addOwnReplacementRow(
  rows: ReplacementRow[],
  fromInput: string,
  toInput: string,
): ReplacementRow[] | null {
  const from = fromInput.trim()
  const to = toInput.trim()
  const key = normaliseKey(from)
  if (key === '' || to === '') return null
  if (rows.some((row) => row.own && row.key === key)) return null
  const index = rows.findIndex((row) => row.key === key)
  if (index !== -1) {
    return sortRows(
      rows.map((row, i) => (i === index ? { ...row, own: true, enabled: true, to } : row)),
    )
  }
  return sortRows([...rows, { key, from, to, enabled: true, source: 'default', own: true }])
}

/** The PUT payload for the current scope: ONLY the own rows. This is the most
 * important correctness property here - a PUT overwrites exactly one layer
 * (see glossary_admin.update), so an inherited row leaking into this payload
 * would be silently PROMOTED into the current layer and stop tracking its
 * real source. A disabled own row is kept as an explicit `false`/`null`: it
 * is a deliberate disable written at this layer, not an absence.
 *
 * Built via Object.fromEntries rather than assigning into a `{}` literal: an
 * operator is free to type `__proto__` as a term (the backend has no reason
 * to refuse a plain string), and `payload[key] = value` for that key does NOT
 * create an own property - it reassigns the object's PROTOTYPE, so the entry
 * silently vanishes from JSON.stringify and Save would drop it with no error. */
export function rowsToOwn(
  termRows: TermRow[],
  replacementRows: ReplacementRow[],
): { terms: Record<string, boolean>; replacements: Record<string, string | null> } {
  return {
    terms: Object.fromEntries(
      termRows.filter((row) => row.own).map((row) => [row.term, row.enabled] as const),
    ),
    replacements: Object.fromEntries(
      replacementRows
        .filter((row) => row.own)
        .map((row) => [row.from, row.enabled ? row.to : null] as const),
    ),
  }
}

/** The keys dropped from `rows` since `savedRows` (the last load/Save) by
 * removeOwnRow - rows that were own as of the last save and are no longer
 * present at all. Pure so the "Remove is pending until Save" caption never
 * has to inline this comparison. Compares PRESENCE only: a row that is still
 * there, even if edited, is not a pending removal. */
export function pendingRemovals<T extends Row>(rows: T[], savedRows: T[]): string[] {
  const current = new Set(rows.map((row) => row.key))
  return savedRows.filter((row) => row.own && !current.has(row.key)).map((row) => row.key)
}
```

Note on `normaliseKey` — **corrected during execution; the original note here was wrong.** It claimed that "keep letters and numbers" (`[^\p{L}\p{N}]`) was equivalent to the backend's `string.punctuation` stripping for every realistic input and, unlike a hand-copied list, could not drift. Both halves were false: `\p{L}\p{N}` also strips every NON-ASCII punctuation character, which Python keeps, so `"O’Brien"` normalised to `obrien` in the client and `o’brien` on the server — and a smart apostrophe is what macOS, iOS and Word produce by default. The consequence was not cosmetic: a mismatched key makes `toReplacementRows` mark a genuinely own entry `own: false`, and since `rowsToOwn` sends own rows only while `update` overwrites the whole layer, the next Save of any unrelated row would silently delete that entry from disk. `normaliseKey` therefore spells out Python's exact ASCII `string.punctuation` set, and the Vitest cases pin agreement against outputs captured from the real backend, including the three characters that disagreed.

- [ ] **Step 5: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/glossaryLayers.ts src/yt_shorts/studio/web/src/glossaryLayers.test.ts
git commit -m "feat(studio-web): glossary API client and pure layer logic"
```

---

### Task 7: `GlossaryEditor` and its three mounts

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx`
- Modify: `src/yt_shorts/studio/web/src/components/ChannelScreen.tsx`
- Modify: `src/yt_shorts/studio/web/src/App.tsx`

**Interfaces:**
- Consumes: everything Task 6 produced, plus `sourceLabel` from `../momentsLexicon` (the `LayerSource` union is identical — reused rather than duplicated).
- Produces: `GlossaryEditor({ channel?: string; event?: string })`.

- [ ] **Step 1: Write the component**

Create `src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx`:

```tsx
import { useEffect, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { ApiError, adoptDefaultGlossary, getGlossary, putGlossary } from '../api'
import {
  addOwnReplacementRow,
  addOwnTermRow,
  disableRow,
  pendingRemovals,
  removeOwnRow,
  rowsToOwn,
  setReplacementText,
  toReplacementRows,
  toTermRows,
  overrideRow,
  type ReplacementRow,
  type TermRow,
} from '../glossaryLayers'
import { sourceLabel } from '../momentsLexicon'

/**
 * The glossary editor, mounted at all three writable scopes exactly the way
 * MomentsEditor is - workspace (a Card in SettingsScreen), channel (a
 * Tabs.Tab in ChannelScreen) and event (a Drawer in App.tsx) - and built to
 * the same load/dirty/save shape: load once, edit local row arrays, Save PUTs
 * the whole own layer and replaces state from the response.
 *
 * What differs from the moments editor: TWO lists, not one.
 *
 * - Terms bias the decoder BEFORE it errs (glossary.hotwords). A term is
 *   either on or off; there is nothing to type but the spelling, and the
 *   spelling matters because it is literally what the decoder is biased
 *   toward.
 * - Corrections fix what it already got wrong, AFTER it errs
 *   (glossary.apply). A correction is a pair: what was heard, what it should
 *   say.
 *
 * Ownership is per ENTRY in both lists, not per section: an inherited row is
 * read-only until Override or Disable creates an own entry at this scope. A
 * disabled row stays visible, struck through, with a badge naming the layer
 * that disabled it - never dropped, the way the transcription merge drops it.
 * Every row mutation goes through glossaryLayers.ts's pure helpers, so the
 * sort order and the own/disabled invariants stay unit-tested in one place.
 *
 * SCROLLING: the built-in default alone is 32 terms and 10 corrections, and
 * this mounts inside three different scroll contexts (NavScreen's page scroll
 * for Settings/Channel, a Drawer's ScrollArea for the event case) - see
 * CLAUDE.md's standing scrolling requirement. Rather than depend on whichever
 * host gives it room, this owns a fixed-height flex column: the intro and add
 * controls are `flex: 0 0 auto` at the top, the two lists share the one
 * `flex: 1 1 auto; minHeight: 0; overflowY: auto` region, and Save/Adopt stay
 * pinned at the bottom.
 */
export function GlossaryEditor({ channel, event }: { channel?: string; event?: string }) {
  const scope = { channel, event }
  // Only a workspace-scope instance offers "Adopt the built-in default" -
  // adoptDefaultGlossary always writes the WORKSPACE layer regardless of
  // which scope called it, so offering it elsewhere would adopt into a layer
  // this editor is not looking at.
  const isWorkspaceScope = !channel && !event

  const [termRows, setTermRows] = useState<TermRow[] | null>(null)
  const [savedTermRows, setSavedTermRows] = useState<TermRow[] | null>(null)
  const [repRows, setRepRows] = useState<ReplacementRow[] | null>(null)
  const [savedRepRows, setSavedRepRows] = useState<ReplacementRow[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [problems, setProblems] = useState<string[]>([])

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const [newTerm, setNewTerm] = useState('')
  const [termError, setTermError] = useState<string | null>(null)
  const [newFrom, setNewFrom] = useState('')
  const [newTo, setNewTo] = useState('')
  const [repError, setRepError] = useState<string | null>(null)

  const [adoptOpen, setAdoptOpen] = useState(false)
  const [adopting, setAdopting] = useState(false)
  const [adoptError, setAdoptError] = useState<string | null>(null)

  function applyState(layers: Parameters<typeof toTermRows>[0]) {
    const terms = toTermRows(layers)
    const reps = toReplacementRows(layers)
    setTermRows(terms)
    setSavedTermRows(terms)
    setRepRows(reps)
    setSavedRepRows(reps)
    setProblems(layers.problems)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getGlossary(scope)
      .then((layers) => {
        if (cancelled) return
        applyState(layers)
        setLoadError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setLoadError(err instanceof ApiError ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channel, event])

  const dirty =
    termRows !== null &&
    savedTermRows !== null &&
    repRows !== null &&
    savedRepRows !== null &&
    (JSON.stringify(termRows) !== JSON.stringify(savedTermRows) ||
      JSON.stringify(repRows) !== JSON.stringify(savedRepRows))

  const removedCount =
    termRows && savedTermRows && repRows && savedRepRows
      ? pendingRemovals(termRows, savedTermRows).length +
        pendingRemovals(repRows, savedRepRows).length
      : 0

  function handleAddTerm() {
    if (!termRows) return
    setTermError(null)
    const next = addOwnTermRow(termRows, newTerm)
    if (next === null) {
      setTermError(
        newTerm.trim() === ''
          ? 'Enter a term.'
          : 'This term is already one of your own entries - edit it in the list below instead.',
      )
      return
    }
    setTermRows(next)
    setNewTerm('')
  }

  function handleAddReplacement() {
    if (!repRows) return
    setRepError(null)
    const next = addOwnReplacementRow(repRows, newFrom, newTo)
    if (next === null) {
      if (newTo.trim() === '') setRepError('Enter what it should say.')
      else if (newFrom.trim() === '') setRepError('Enter what the decoder heard.')
      else
        setRepError(
          'Enter a heard phrase with at least one letter or number that is not already one of your own entries.',
        )
      return
    }
    setRepRows(next)
    setNewFrom('')
    setNewTo('')
  }

  function noteRemoval() {
    // The row vanishes immediately (see removeOwnRow on why it cannot show a
    // placeholder), so this toast is a second, non-exclusive cue - the
    // caption near Save is the one that survives after it fades.
    notifications.show({ message: 'Entry removed - Save to apply.', color: 'yellow' })
  }

  async function handleSave() {
    if (!termRows || !repRows || !dirty || saving) return
    setSaving(true)
    setSaveError(null)
    try {
      const payload = rowsToOwn(termRows, repRows)
      const layers = await putGlossary(scope, payload.terms, payload.replacements)
      applyState(layers)
      notifications.show({ message: 'Saved.', color: 'green' })
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  async function handleAdopt() {
    setAdopting(true)
    setAdoptError(null)
    try {
      applyState(await adoptDefaultGlossary())
      setAdoptOpen(false)
      notifications.show({ message: 'Adopted the built-in default as your own.', color: 'green' })
    } catch (err) {
      setAdoptError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setAdopting(false)
    }
  }

  if (loading) {
    return (
      <Center py="xl">
        <Stack align="center" gap="xs">
          <Loader color="steel" />
          <Text size="xs" c="dimmed">
            Loading glossary…
          </Text>
        </Stack>
      </Center>
    )
  }

  if (loadError || !termRows || !repRows) {
    return (
      <Alert color="red" title="Could not load the glossary">
        {loadError} - check that the studio server is still running, then reload this page.
      </Alert>
    )
  }

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', height: '70vh', minHeight: 460 }}>
      {problems.length > 0 ? (
        <Alert
          color="yellow"
          variant="light"
          title="Some layers could not be read"
          style={{ flex: '0 0 auto', marginBottom: 8 }}
        >
          <Stack gap={2}>
            <Text size="xs">
              The file(s) below could not be read, so they contribute nothing right now - neither
              to the rows shown here nor to transcription - until the file is fixed by hand, or
              Save at that layer overwrites it with a clean one.
            </Text>
            {problems.map((problem) => (
              <Text key={problem} size="xs" ff="monospace">
                {problem}
              </Text>
            ))}
          </Stack>
        </Alert>
      ) : null}

      <Stack gap="xs" style={{ flex: '0 0 auto' }}>
        <Text size="xs" c="dimmed">
          Proper nouns a transcript keeps getting wrong. <b>Terms</b> bias the decoder before it
          errs; <b>corrections</b> fix what it already got wrong. Every row below is what applies
          here; only the highlighted rows belong to this scope's own layer and are editable
          directly. Disabling an entry never deletes it from the layer that set it.
        </Text>
      </Stack>

      <Box style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', marginTop: 12 }}>
        <Stack gap="lg">
          <Stack gap="xs">
            <Text fw={600} size="sm" tt="uppercase" c="dimmed">
              Terms ({termRows.length})
            </Text>
            <Group align="flex-end" gap="xs" wrap="wrap">
              <TextInput
                label="New term"
                placeholder="e.g. Schwalbenschwanz"
                value={newTerm}
                onChange={(e) => setNewTerm(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddTerm()
                }}
                style={{ flex: '1 1 240px' }}
              />
              <Button variant="light" color="steel" onClick={handleAddTerm}>
                Add term
              </Button>
            </Group>
            {termError ? (
              <Text size="xs" c="red">
                {termError}
              </Text>
            ) : null}
            <Stack gap={4}>
              {termRows.map((row) => (
                <EntryRow
                  key={row.key}
                  label={row.term}
                  detail={null}
                  enabled={row.enabled}
                  own={row.own}
                  source={row.source}
                  onOverride={() => setTermRows((prev) => (prev ? overrideRow(prev, row.key) : prev))}
                  onDisable={() => setTermRows((prev) => (prev ? disableRow(prev, row.key) : prev))}
                  onRemove={() => {
                    setTermRows((prev) => (prev ? removeOwnRow(prev, row.key) : prev))
                    noteRemoval()
                  }}
                />
              ))}
            </Stack>
          </Stack>

          <Stack gap="xs">
            <Text fw={600} size="sm" tt="uppercase" c="dimmed">
              Corrections ({repRows.length})
            </Text>
            <Group align="flex-end" gap="xs" wrap="wrap">
              <TextInput
                label="Heard as"
                placeholder="e.g. kessichen"
                value={newFrom}
                onChange={(e) => setNewFrom(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddReplacement()
                }}
                style={{ flex: '1 1 180px' }}
              />
              <TextInput
                label="Should say"
                placeholder="e.g. Kesselchen"
                value={newTo}
                onChange={(e) => setNewTo(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleAddReplacement()
                }}
                style={{ flex: '1 1 180px' }}
              />
              <Button variant="light" color="steel" onClick={handleAddReplacement}>
                Add correction
              </Button>
            </Group>
            {repError ? (
              <Text size="xs" c="red">
                {repError}
              </Text>
            ) : null}
            <Stack gap={4}>
              {repRows.map((row) => (
                <EntryRow
                  key={row.key}
                  label={row.from}
                  detail={
                    row.own && row.enabled ? (
                      <TextInput
                        size="xs"
                        w={200}
                        value={row.to}
                        onChange={(e) =>
                          setRepRows((prev) =>
                            prev ? setReplacementText(prev, row.key, e.currentTarget.value) : prev,
                          )
                        }
                      />
                    ) : (
                      <Text size="sm" c="dimmed" ff="monospace" truncate w={200}>
                        {row.enabled ? row.to : '—'}
                      </Text>
                    )
                  }
                  enabled={row.enabled}
                  own={row.own}
                  source={row.source}
                  onOverride={() => setRepRows((prev) => (prev ? overrideRow(prev, row.key) : prev))}
                  onDisable={() => setRepRows((prev) => (prev ? disableRow(prev, row.key) : prev))}
                  onRemove={() => {
                    setRepRows((prev) => (prev ? removeOwnRow(prev, row.key) : prev))
                    noteRemoval()
                  }}
                />
              ))}
            </Stack>
          </Stack>
        </Stack>
      </Box>

      {saveError ? (
        <Alert color="red" title="Could not save the glossary" mt="sm">
          {saveError}
        </Alert>
      ) : null}

      {removedCount > 0 ? (
        <Text size="xs" c="yellow" mt="sm" style={{ flex: '0 0 auto' }}>
          {removedCount} removed - Save to apply; each removed entry falls back to its inherited or
          built-in value.
        </Text>
      ) : null}

      <Group justify="space-between" mt="sm" style={{ flex: '0 0 auto' }}>
        {isWorkspaceScope ? (
          <Button variant="default" onClick={() => setAdoptOpen(true)}>
            Adopt the built-in default
          </Button>
        ) : (
          <span />
        )}
        <Button color="steel" onClick={handleSave} loading={saving} disabled={!dirty}>
          Save
        </Button>
      </Group>

      <Modal
        opened={adoptOpen}
        onClose={() => {
          if (!adopting) setAdoptOpen(false)
        }}
        title="Adopt the built-in default"
        closeOnEscape={!adopting}
        closeOnClickOutside={!adopting}
        centered
      >
        <Stack gap="md">
          <Text size="sm">
            Copies the built-in default's 32 Nordschleife terms and 10 corrections into this
            workspace's own layer. They become your own entries here - editable and removable like
            any other own row - and stop tracking future updates to the built-in list. Entries you
            already own are kept as they are and win over the default.
          </Text>
          <Text size="sm" c="dimmed">
            This does not change what a transcript currently comes out as - the built-in default
            already underlies every scope. It only makes it editable.
          </Text>
          {dirty ? (
            <Alert color="yellow" variant="light" title="Unsaved changes in this editor">
              You have unsaved changes here - adopting the default will discard them. Adopt
              replaces the rows shown with the server's response; any override, addition or
              removal you have not Saved will be lost.
            </Alert>
          ) : null}
          {adoptError ? (
            <Alert color="red" title="Could not adopt the default">
              {adoptError}
            </Alert>
          ) : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setAdoptOpen(false)} disabled={adopting}>
              Cancel
            </Button>
            <Button color="steel" onClick={handleAdopt} loading={adopting}>
              Adopt as my own
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Box>
  )
}

/** One row of either list: its name (struck through with a badge when
 * disabled), an optional detail slot (the correction's target text, editable
 * for an own row), and the row's actions - Override/Disable for an inherited
 * row, Remove for an own one. Kept local/unexported; the shared logic these
 * call into all lives in glossaryLayers.ts. */
function EntryRow({
  label,
  detail,
  enabled,
  own,
  source,
  onOverride,
  onDisable,
  onRemove,
}: {
  label: string
  detail: React.ReactNode
  enabled: boolean
  own: boolean
  source: 'default' | 'workspace' | 'channel' | 'event'
  onOverride: () => void
  onDisable: () => void
  onRemove: () => void
}) {
  return (
    <Group
      justify="space-between"
      wrap="nowrap"
      gap="xs"
      p={6}
      style={{
        borderRadius: 'var(--mantine-radius-sm)',
        background: own ? 'var(--mantine-color-dark-7)' : 'transparent',
      }}
    >
      <Group gap={8} wrap="nowrap" style={{ minWidth: 0, flex: '1 1 auto' }}>
        <Text
          size="sm"
          ff="monospace"
          truncate
          td={!enabled ? 'line-through' : undefined}
          c={!enabled ? 'dimmed' : undefined}
        >
          {label}
        </Text>
        {!enabled ? (
          <Badge size="xs" color="dark.3" variant="light">
            {own ? 'disabled' : `disabled (${sourceLabel(source)})`}
          </Badge>
        ) : !own ? (
          <Badge size="xs" color="dark.3" variant="light">
            {sourceLabel(source)}
          </Badge>
        ) : null}
      </Group>

      <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
        {detail}
        {own ? (
          <Tooltip label="Drop this scope's own entry - the inherited value or the built-in default takes its place after Save">
            <Button size="xs" variant="subtle" color="red" onClick={onRemove}>
              Remove
            </Button>
          </Tooltip>
        ) : (
          <>
            <Button size="xs" variant="default" onClick={onOverride}>
              Override
            </Button>
            <Button size="xs" variant="subtle" color="red" onClick={onDisable}>
              Disable
            </Button>
          </>
        )}
      </Group>
    </Group>
  )
}
```

- [ ] **Step 2: Mount it in Settings**

In `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx`, add the import beside `MomentsEditor`:

```tsx
import { GlossaryEditor } from './GlossaryEditor'
```

and a second Card immediately after the existing "Moments lexicon" Card (around line 138):

```tsx
          <Card padding="md">
            <Stack gap="sm">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Glossary
              </Text>
              <GlossaryEditor />
            </Stack>
          </Card>
```

- [ ] **Step 3: Mount it as a channel tab**

In `src/yt_shorts/studio/web/src/components/ChannelScreen.tsx`, add the import, a tab beside `moments` (line 92) and its panel beside the moments panel (line 224):

```tsx
import { GlossaryEditor } from './GlossaryEditor'
```
```tsx
          <Tabs.Tab value="glossary">Glossary</Tabs.Tab>
```
```tsx
        <Tabs.Panel value="glossary">
          <GlossaryEditor channel={channel} />
        </Tabs.Panel>
```

Also extend that file's module docstring listing the tabs, so it names Glossary alongside Moments.

- [ ] **Step 4: Mount it as an event drawer**

In `src/yt_shorts/studio/web/src/App.tsx`: the import,

```tsx
import { GlossaryEditor } from './components/GlossaryEditor'
```

the state beside `momentsOpen` (line 62),

```tsx
  const [glossaryOpen, setGlossaryOpen] = useState(false)
```

a header button beside "Event moments" (line 368),

```tsx
            <Button variant="default" size="xs" onClick={() => setGlossaryOpen(true)}>
              Event glossary
            </Button>
```

and a Drawer after the moments Drawer (line 413):

```tsx
      {/* Same idiom as the two Drawers above: one event's own glossary layer,
          not a navigation destination. GlossaryEditor owns its own bounded,
          independently-scrolling lists internally (see its own docstring)
          rather than relying on this Drawer's scrollAreaComponent - the
          built-in default's 32 terms and 10 corrections are long enough that
          the header and Save button must stay reachable without scrolling the
          whole drawer body. */}
      <Drawer
        opened={glossaryOpen}
        onClose={() => setGlossaryOpen(false)}
        position="right"
        size="xl"
        title="Event glossary"
        scrollAreaComponent={ScrollArea.Autosize}
      >
        <GlossaryEditor channel={channel} event={event} />
      </Drawer>
```

- [ ] **Step 5: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx src/yt_shorts/studio/web/src/components/SettingsScreen.tsx src/yt_shorts/studio/web/src/components/ChannelScreen.tsx src/yt_shorts/studio/web/src/App.tsx
git commit -m "feat(studio-web): glossary editor at workspace, channel and event scope"
```

---

### Task 8: Docs, E2E, rebuilt static, full verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `tests/test_studio_e2e.py`
- Modify: `src/yt_shorts/studio/static/**` (rebuilt)

- [ ] **Step 1: Correct and extend `CLAUDE.md`**

Two edits, and BOTH must land — the second is the one a future reader would otherwise use to re-introduce the override.

(a) In the weighted-lexicon section, the sentence currently reading

> This deliberately diverges from `glossary.json`, which sits right beside it in a channel and an event folder - but only at those two layers, with neither a workspace file nor a built-in default - and still replaces wholesale rather than merging: a glossary is a set of exact corrections for one event (a decoder's mishearing of a driver's name), while a lexicon is a shared vocabulary meant to be extended at every layer down to the event, not overwritten by one.

becomes

> The glossary now layers **identically** - same four layers, same
> most-specific-wins rule, same disable-by-falsy escape hatch (see the
> glossary section below). The two files sat beside each other behaving
> differently for exactly three commits; that divergence is gone, and the
> reasoning that justified it ("a glossary is a set of exact corrections for
> one event") did not survive a corner-name list that is a fact about a
> racetrack rather than about one event.

(b) Add a new Architecture section after the lexicon one, in the file's voice
(constraints and their reasons):

```markdown
**The glossary is additive across the same four layers, and it finally
reaches stream transcription.** `glossary.json` corrects proper nouns Whisper
does not know, at both ends: `terms` bias the decoder before it errs
(`hotwords`), `replacements` correct its output after (`apply`). Layer
parsing lives in `glossary.py` (`GlossaryLayer`, `parse_layer`, `load`) so
`glossary_admin` validates a payload exactly the way `profile.load` validates
a file; `profile.merge_glossaries` folds the layers - built-in default
(`glossary.DEFAULT_LAYER`, the Nordschleife corner set, in CODE), the
workspace's `glossary.json`, the channel's, the event's - most specific
winning per entry, with `false` for a term and `null` for a replacement
DISABLING one inherited from a less specific layer. An empty-string
replacement is refused (it would make `apply` delete the matched words):
`null` is how you disable.

**This replaced a wholesale rule, and the replacement is not optional.** An
event's `glossary.json` used to replace the channel's outright, argued for on
the grounds that merging two term lists has no obviously correct result. The
ambiguity is now resolved rather than avoided - *add* is the rule, and "only
these" has an explicit spelling. Restoring the override would silently drop
the corner names for ERF, the one channel that both needs them and has a
`glossary.json` of its own.

**Two things about the stream path are easy to get wrong.** First: for months
`_decode_worker.main` accepted `argv[3]` as a glossary path and
`subprocess_decoder` never passed one, so every stream chunk in this project
decoded with an EMPTY glossary while ERF's `glossary.json` sat on disk doing
nothing. `subprocess_decoder` now writes the glossary into the same
`TemporaryDirectory` as the wav and appends that path - but ONLY for a
non-empty glossary, because "no hotwords" and "hotwords=''" are different
requests to faster-whisper (see `glossary.hotwords`).

Second: **corrections are applied at ASSEMBLY, and the per-chunk cache stays
RAW.** `transcribe_stream` runs `glossary.apply` on the assembled word list
just before writing `transcript.json`. That means a glossary change takes
effect on the next assembly with no re-decode (seconds instead of half an
hour for an 8-hour stream), and a correction may span a chunk boundary -
applying per chunk before caching would lose exactly those matches. For
streams this escapes the cache wart `transcribe.py`'s docstring documents for
clips; the clip path keeps it. The consequence is real and must stay
documented: chunks decoded BEFORE a glossary change keep the old decoder
bias, and recovering it means deleting `streams/<video_id>/chunks/`. The
chunk cache key stays `(video_id, start, length)` - fingerprinting the
glossary into it would invalidate hours of decode on every keystroke in the
editor.

`glossary_admin.py` is the studio's write path onto the three writable
layers - pure, no FastAPI, like every other admin module - and its routes are
a thin mapping of its `kind` to 400/404. Like `lexicon_admin.read`, its
`effective` deliberately KEEPS a disabled entry (struck through in the
editor) where `profile.merge_glossaries` DROPS it before transcription:
correct for transcribing, wrong for an editor that must show a disabled
inherited entry rather than make it vanish. `adopt_default` is ADDITIVE
(`{**default, **own}`) for the same reason `lexicon_admin.adopt_default` is,
after its review found the overwrite version deleting custom entries while
the dialog promised it changed nothing.

**One shipped default rule has a known false positive.** `carousel` →
`Karussell` is global, and Road America, Sears Point and Watkins Glen all
have a Carousel: an English-language broadcast of one would be rewritten to
German. It ships anyway because it is the most frequent mis-hearing measured
on the real ERF transcript (5 occurrences) and because any layer can disable
it - that escape hatch is why the disable mechanism exists at all, not
symmetry for its own sake. If a second circuit ever appears, the right shape
is a registry of adoptable named packs, not a second always-on default.
```

- [ ] **Step 2: Add the E2E**

In `tests/test_studio_e2e.py`, following `TestMomentsEditor`'s fixtures and
selector style (read it first — it establishes the `_resolve_workspace`
monkeypatch these scopes need), add:

```python
class TestGlossaryEditor:
    """The glossary editor mounted at all three scopes - workspace (a Card in
    Settings), channel (a Tabs.Tab) and event (a Drawer) - proving the
    additive-layers contract end to end: an entry added at a less specific
    scope shows up INHERITED at a more specific one, a save writes ONLY the
    scope's own layer, and disabling an inherited entry is itself an own
    entry at the disabling scope.

    One flowing test, not four: each step depends on state the earlier steps
    wrote (the channel step needs a real workspace entry to inherit; adopting
    the default has to run last because it rewrites the workspace layer), and
    splitting them would mean re-deriving that state with no browser -
    which tests/test_studio_glossary_api.py already covers over plain HTTP.
    """

    def test_workspace_channel_event_layers_and_adopt_default(
            self, studio_profile, event_dir, live_server, page, monkeypatch):
        import json

        import yt_shorts.studio.api as api
        from yt_shorts.workspace import Workspace

        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA"))

        workspace_path = root / "glossary.json"
        channel_path = profile_module.CHANNELS_DIR / "erf" / "glossary.json"

        # 1. Workspace scope: add a correction in Settings and assert the file.
        page.goto(f"{live_server}/settings")
        page.get_by_label("Heard as").last.fill("kessichen")
        page.get_by_label("Should say").last.fill("Kesselchen")
        page.get_by_role("button", name="Add correction").last.click()
        page.get_by_role("button", name="Save").last.click()
        page.wait_for_function(
            "() => document.body.innerText.includes('Saved.')")
        written = json.loads(workspace_path.read_text(encoding="utf-8"))
        assert written["replacements"]["kessichen"] == "Kesselchen"

        # 2. Channel scope: the workspace entry shows as inherited; add a
        #    channel term and assert ONLY the channel file changed.
        before = workspace_path.read_text(encoding="utf-8")
        page.goto(f"{live_server}/erf")
        page.get_by_role("tab", name="Glossary").click()
        page.wait_for_function(
            "() => document.body.innerText.includes('kessichen')")
        assert page.get_by_text("workspace", exact=True).count() >= 1
        page.get_by_label("New term").fill("Rei Racing")
        page.get_by_role("button", name="Add term").click()
        page.get_by_role("button", name="Save").click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")
        assert json.loads(channel_path.read_text(encoding="utf-8"))["terms"] == {
            "Rei Racing": True}
        assert workspace_path.read_text(encoding="utf-8") == before

        # 3. Event scope: disable an inherited correction and assert the event
        #    file records the disable as null.
        page.goto(f"{live_server}/erf/{event_dir.name}")
        page.get_by_role("button", name="Event glossary").click()
        page.wait_for_function("() => document.body.innerText.includes('kessichen')")
        row = page.get_by_text("kessichen", exact=True).first
        row.scroll_into_view_if_needed()
        page.get_by_role("button", name="Disable").first.click()
        page.get_by_role("button", name="Save").click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")
        event_written = json.loads(
            (event_dir / "glossary.json").read_text(encoding="utf-8"))
        assert None in event_written["replacements"].values()
        assert page.get_by_text("disabled", exact=False).count() >= 1

        # 4. Adopt the default in Settings and assert the workspace file gains
        #    the corner entries while keeping the entry added in step 1.
        page.goto(f"{live_server}/settings")
        page.get_by_role("button", name="Adopt the built-in default").last.click()
        page.get_by_role("button", name="Adopt as my own").click()
        page.wait_for_function(
            "() => document.body.innerText.includes('Adopted the built-in default')")
        adopted = json.loads(workspace_path.read_text(encoding="utf-8"))
        assert adopted["terms"]["Karussell"] is True
        assert adopted["replacements"]["kessichen"] == "Kesselchen"
        assert adopted["replacements"]["carousel"] == "Karussell"

    def test_the_editor_scrolls_at_a_short_viewport(
            self, studio_profile, event_dir, live_server, page, monkeypatch):
        """Standing acceptance criterion (CLAUDE.md): every pane must scroll
        to all its elements. The default alone is 42 rows, so Save must stay
        reachable at a laptop-short viewport without the page scrolling."""
        import yt_shorts.studio.api as api
        from yt_shorts.workspace import Workspace

        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA"))

        page.set_viewport_size({"width": 1280, "height": 620})
        page.goto(f"{live_server}/erf")
        page.get_by_role("tab", name="Glossary").click()
        page.wait_for_function("() => document.body.innerText.includes('Corrections')")

        save = page.get_by_role("button", name="Save")
        save.scroll_into_view_if_needed()
        assert save.is_visible()
        # The last correction row is reachable through the list's OWN scroll
        # container, not the page's.
        last = page.get_by_text("boyacht", exact=True).first
        last.scroll_into_view_if_needed()
        assert last.is_visible()
```

If a selector above does not match the rendered DOM, fix the SELECTOR (or the
component's labels) — do not weaken an assertion. Report any selector you had
to change and why.

- [ ] **Step 3: Build and run every gate**

```bash
cd src/yt_shorts/studio/web && npm run lint && npm run build && npm test && cd -
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: oxlint clean, build exit 0, Vitest all pass, pytest all pass, `All checks passed!`. Paste the REAL output of each.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(glossary): document the additive glossary and the stream plumbing; e2e; rebuild static"
```

- [ ] **Step 5: Operator smoke (not the implementer's job)**

Restart `bin/yt-shorts studio` (uvicorn runs without `--reload`, so backend
routes need a restart). Then, in order:

1. Settings → Glossary → "Adopt the built-in default", confirm.
2. Re-run detection on stream `V9nVNEQNdR4` in `erfofficial/N24-2026`. The 9
   cached chunks are reused, so this is seconds. Check
   `~/YT-Shorts-Data/streams/V9nVNEQNdR4/transcript.json` for `Karussell`,
   `Schwalbenschwanz`, `Galgenkopf`, `Kesselchen` and `Hohe Acht`, and confirm
   `chunks/000.json` still holds the RAW `carousel`.
3. Optional, ~30 minutes: delete `streams/V9nVNEQNdR4/chunks/` and run again
   for the hotword-bias half — also the only run that can fill chunk 6, lost
   to the 600 s per-chunk timeout. Comparing the two transcripts is the honest
   measurement of what the bias is worth.

---

## Self-Review

**Spec coverage.** Four additive layers → Tasks 1–2. Both `terms` file shapes and falsy-disables → Task 1. Built-in Nordschleife default → Task 1 (`DEFAULT_TERMS`/`DEFAULT_REPLACEMENTS`/`DEFAULT_LAYER`), adopted additively in Task 4. Parsing moved into `glossary.py` with the "accepts ⇒ loadable" invariant → Tasks 1, 4. `merge_glossaries` + `_load_glossary` + `workspace.glossary_path` + the retired wholesale rule → Task 2. Decoder plumbing (`argv[3]`), assembly correction, raw chunk cache, `detect_moments` → Task 3. `glossary_admin` with `read`/`update`/`adopt_default` and the disabled-entries-kept divergence → Task 4. Six routes with `kind`→status mapping and segment validation → Task 5. Frontend client + pure module + Vitest → Task 6. `GlossaryEditor` at three scopes with scrolling → Task 7. Docs (both the new section AND the correction of the divergence paragraph), E2E, fixture check, rebuilt `static/` → Task 8. Reprocessing `V9nVNEQNdR4` → Task 8 Step 5. Error-handling contract → Tasks 2, 4, 5. Out-of-scope items are not implemented anywhere.

The spec's fixture note ("`tests/fixtures/channels/erf/glossary.json` stays as it is") needs no task: Task 2's `test_channel_glossary_ADDS_to_the_default` proves additivity against exactly that file's shape, and Task 8's full-suite run covers the pinned overlay hashes.

**Type consistency.** `GlossaryLayer.terms: dict[str, tuple[str, bool]]` and `.replacements: dict[str, tuple[str, str | None]]` are used identically in Tasks 1, 2 and 4. `glossary_admin.read`'s wire shape (`own.terms: {spelling: bool}`, `own.replacements: {raw: str | null}`, `effective.terms[key] = {term, enabled, source}`, `effective.replacements[key] = {key, value, source}`) matches `GlossaryBody` in Task 5 and `GlossaryLayers` in Task 6 field for field. `update(root, terms, replacements, *, channel, event)` is called with that argument order in Tasks 4, 5 and the tests. `normalise_term`/`normalise_key` (Python) pair with `normaliseTerm`/`normaliseKey` (TS). `subprocess_decoder`/`transcribe_stream` take `glossary` as a keyword-only argument everywhere it appears.

**Placeholder scan.** No TBD/TODO; every code step carries the actual code, every test step the actual test, every command its expected output.
