# Track packs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope the glossary's shipped vocabulary to the circuit an event actually runs at, so every GT7 venue's names are available without any of them firing on the wrong track or blowing faster-whisper's hotword budget.

**Architecture:** A new pure module `tracks.py` holds one pack per venue (terms + replacements), validated at import through `glossary.parse_layer`. An event names its venue with a `track` key in its own `glossary.json`; `profile._load_glossary` inserts that pack as the second layer, where the Nordschleife-only built-in default used to sit. The built-in default becomes empty and its contents move into the `nurburgring-nordschleife` pack.

**Tech Stack:** Python 3 stdlib + FastAPI/pydantic (studio only), pytest, React + Mantine + TypeScript + Vitest, Playwright (inside pytest).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation; the venv is `.venv/`.
- Full gate before every commit that touches Python: `PYTHONPATH=src .venv/bin/pytest -q` and `python3 tools/lint.py` (must print `All checks passed!`, exit 0).
- Frontend gate: in `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint` (oxlint), `npm test`. `npm run build` regenerates the COMMITTED `src/yt_shorts/studio/static/`.
- `tracks.py`, `glossary.py`, `glossary_admin.py`, `profile.py` must NOT import FastAPI or google — only `yt_shorts/studio/` may import FastAPI. `tracks.py` is stdlib-only and does no file access.
- Layer order, least to most specific: built-in default (now empty) → track pack → workspace → channel → event.
- `merge_glossaries` emits terms MOST-SPECIFIC-FIRST so that faster-whisper's hotword truncation sacrifices shipped data before the operator's own. Do not change that.
- faster-whisper truncates the hotword prompt at 224 tokens. `glossary.HOTWORD_BUDGET_CHARS = 550` is the conservative character proxy; no shipped pack may approach it.
- A malformed or unknown value in any layer is reported as a problem string, never raised past the caller.
- Only an EVENT may set `track`. The same key at workspace or channel scope is a reported defect.
- The pack is referenced, never copied into an event's own layer.
- Every studio path segment goes through `pathnames.validate_segment` before any filesystem touch.
- Scrolling is a mandatory acceptance criterion: every pane owns its own scroll container and is verified at a short viewport.
- Spec: `docs/superpowers/specs/2026-07-25-track-packs-design.md`.

---

## File Structure

**Created**
- `src/yt_shorts/tracks.py` — the venue registry: `TrackPack`, `PACKS`, `get`, `as_layer`, `listing`.
- `tests/test_tracks.py` — registry integrity, budget, layer conversion.

**Modified**
- `src/yt_shorts/glossary.py` — `GlossaryLayer.track`, `parse_layer` validation, `DEFAULT_TERMS`/`DEFAULT_REPLACEMENTS` removed (moved into the Nordschleife pack), `DEFAULT_LAYER` becomes empty.
- `src/yt_shorts/profile.py` — `_load_glossary` inserts the pack layer and validates the `track` key's scope.
- `src/yt_shorts/glossary_admin.py` — `track` through `read`/`_own_shape`/`update`; `adopt_default` removed.
- `src/yt_shorts/studio/api.py` — `GET /api/tracks`; `GlossaryBody.track`; the adopt-default route removed.
- `src/yt_shorts/studio/web/src/api.ts` — `TrackListing`, `getTracks`, `track` on `GlossaryLayers`/`putGlossary`, `'track'` in `LayerSource`, `adoptDefaultGlossary` removed.
- `src/yt_shorts/studio/web/src/glossaryLayers.ts` — `sourceLabel` gains the fifth value (it lives in `momentsLexicon.ts`; see Task 6).
- `src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx` — the track selector; the adopt modal and button removed.
- `src/yt_shorts/studio/web/src/components/SettingsScreen.tsx` — unchanged mount, adopt button gone with the component's own change.
- `CLAUDE.md`, `README.md`, `tests/test_glossary_layers.py`, `tests/test_glossary_admin.py`, `tests/test_studio_glossary_api.py`, `tests/test_profile.py`, `tests/test_stream_transcribe.py`, `tests/test_studio_e2e.py`.

**Workspace data (not the repository)**
- `~/YT-Shorts-Data/channels/erfofficial/events/N24-2026/glossary.json` — gains `"track": "nurburgring-nordschleife"`. Task 8 does this and verifies the merged result is unchanged.

---

### Task 1: `tracks.py` — the registry and the 39 non-Nürburgring packs

**Files:**
- Create: `src/yt_shorts/tracks.py`
- Test: `tests/test_tracks.py` (create)

**Interfaces:**
- Consumes: `glossary.parse_layer(data) -> GlossaryLayer` (raises `ValueError`), `glossary.GlossaryLayer`, `glossary.HOTWORD_BUDGET_CHARS`, `glossary.hotwords`, `glossary.Glossary`.
- Produces:
  - `@dataclass(frozen=True) class TrackPack` with `track_id: str`, `name: str`, `terms: tuple[str, ...]`, `replacements: dict[str, str]`
  - `PACKS: dict[str, TrackPack]`
  - `get(track_id: str) -> TrackPack | None`
  - `as_layer(pack: TrackPack) -> GlossaryLayer`
  - `listing() -> list[dict]` — `[{"id": ..., "name": ...}, ...]`, sorted by name

Nothing outside this task's own tests imports it yet; the tree stays green.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracks.py`:

```python
import pytest

from yt_shorts import glossary, tracks


class TestRegistryIntegrity:
    def test_every_pack_is_keyed_by_its_own_id(self):
        for key, pack in tracks.PACKS.items():
            assert key == pack.track_id

    def test_every_pack_has_a_display_name(self):
        for pack in tracks.PACKS.values():
            assert pack.name.strip()

    def test_ids_are_safe_lowercase_slugs(self):
        """The id is written into a glossary.json and read back, so keep it to
        a shape that survives a hand edit and a URL without quoting."""
        import re
        for track_id in tracks.PACKS:
            assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", track_id), track_id

    def test_every_pack_converts_to_a_layer(self):
        """as_layer routes through glossary.parse_layer, so a pack that would
        be refused as a hand-written glossary.json fails here instead of at an
        operator's next run."""
        for pack in tracks.PACKS.values():
            layer = tracks.as_layer(pack)
            assert isinstance(layer, glossary.GlossaryLayer)
            assert len(layer.terms) == len(pack.terms)

    def test_no_pack_has_duplicate_terms_after_normalisation(self):
        for pack in tracks.PACKS.values():
            keys = [glossary.normalise_term(t) for t in pack.terms]
            assert len(keys) == len(set(keys)), pack.track_id

    def test_no_pack_is_empty(self):
        """A venue with no named corners still carries its own name - an empty
        pack would be a data gap wearing the costume of a feature."""
        for pack in tracks.PACKS.values():
            assert pack.terms, pack.track_id


class TestHotwordBudget:
    def test_no_pack_approaches_the_truncation_budget(self):
        """faster-whisper silently truncates the hotword prompt at 224 tokens.
        A shipped pack that filled the budget on its own would leave no room
        for the operator's own team and driver names - and the truncation
        drops the LAST terms, which after merge_glossaries' most-specific-first
        ordering are exactly theirs. This is the assertion that keeps a future
        oversized pack out of an operator's transcript."""
        for pack in tracks.PACKS.values():
            bias = glossary.hotwords(glossary.Glossary(terms=list(pack.terms),
                                                        replacements={}))
            assert bias is not None
            assert len(bias) < glossary.HOTWORD_BUDGET_CHARS, (
                f"{pack.track_id}: {len(bias)} chars, at or over the "
                f"{glossary.HOTWORD_BUDGET_CHARS}-character budget")

    def test_the_largest_pack_still_leaves_room_for_operator_terms(self):
        largest = max(tracks.PACKS.values(),
                      key=lambda p: len(glossary.hotwords(
                          glossary.Glossary(terms=list(p.terms), replacements={})) or ""))
        with_own = glossary.Glossary(
            terms=list(largest.terms) + ["Rei Racing", "Team Fullsend"],
            replacements={})
        assert not glossary.hotwords_at_risk(with_own), largest.track_id


class TestLookup:
    def test_get_returns_the_pack(self):
        assert tracks.get("spa-francorchamps").name == "Circuit de Spa-Francorchamps"

    def test_get_on_an_unknown_id_is_none(self):
        assert tracks.get("nope") is None

    def test_get_is_not_case_insensitive(self):
        """Ids are written by the studio's own selector and validated on the
        way in; accepting a stray case would hide a typo rather than report
        it."""
        assert tracks.get("Spa-Francorchamps") is None


class TestListing:
    def test_covers_every_pack(self):
        assert len(tracks.listing()) == len(tracks.PACKS)

    def test_carries_id_and_name_only(self):
        for row in tracks.listing():
            assert set(row) == {"id", "name"}

    def test_is_sorted_by_name(self):
        names = [row["name"] for row in tracks.listing()]
        assert names == sorted(names)


class TestCoverage:
    def test_the_venues_this_task_ships_are_all_present(self):
        expected = {
            "alsace", "autopolis", "barcelona-catalunya", "bb-raceway",
            "blue-moon-bay", "brands-hatch", "colorado-springs", "daytona",
            "deep-forest", "dragon-trail", "eiger-nordwand", "fishermans-ranch",
            "fuji", "gilles-villeneuve", "goodwood", "grand-valley",
            "high-speed-ring", "interlagos", "kyoto-driving-park",
            "laguna-seca", "lago-maggiore", "lake-louise", "le-mans", "monza",
            "mount-panorama", "northern-isle", "red-bull-ring", "road-atlanta",
            "sainte-croix", "sardegna", "special-stage-route-x",
            "spa-francorchamps", "suzuka", "tokyo-expressway",
            "trial-mountain", "tsukuba", "watkins-glen", "willow-springs",
            "yas-marina",
        }
        assert expected <= set(tracks.PACKS), expected - set(tracks.PACKS)


class TestNoHeavyImports:
    def test_module_imports_no_web_framework_or_google(self):
        """CLAUDE.md's rule for every pure module. Checked over the AST's
        import statements, not the source text, so the docstring stays free to
        NAME the constraint it upholds."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(tracks.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        for banned in ("fastapi", "google", "googleapiclient", "google_auth_oauthlib"):
            assert banned not in imported, f"{banned} must not be imported here"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_tracks.py -q
```
Expected: `ModuleNotFoundError: No module named 'yt_shorts.tracks'`.

- [ ] **Step 3: Create `src/yt_shorts/tracks.py`**

```python
"""One vocabulary pack per racing venue, selected by an event.

A glossary term biases the decoder (see glossary.hotwords) and that bias is
NOT free: faster-whisper truncates the hotword prompt at 224 tokens, so a
global list of every circuit's corner names would be silently cut to a
fraction of itself. Scoping the vocabulary to the circuit an event actually
runs at is what makes the whole set affordable - and it is also what stops a
track-specific correction from firing on the wrong track, which is why
`carousel -> Karussell` is safe here and was not safe as an always-on default
(Road America, Sears Point and Watkins Glen each have a Carousel).

Pure logic, stdlib only, no file access - the same constraints glossary.py
carries, and for the same reason: this is data plus lookups, and its only
dependency is the layer format it hands its data to.

A pack is REFERENCED by an event, never copied into it, so correcting a name
here corrects every event at that venue with no migration.

One pack per VENUE, not per layout: GT7's 121 layouts collapse to 41
locations, and corner names belong to the place. Monza and Monza No Chicane
share a pack. The Nürburgring is the one deliberate split - its GP circuit and
the Nordschleife have entirely different corners, and the combined set is 249
tokens, over the limit before an operator adds a single name of their own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .glossary import GlossaryLayer, parse_layer


@dataclass(frozen=True)
class TrackPack:
    """One venue's shipped vocabulary.

    `terms` bias the decoder before it errs; `replacements` correct what it
    already got wrong. A pack's replacements start EMPTY unless a mis-hearing
    has actually been OBSERVED in a transcript - inventing plausible ones for
    a venue nobody has transcribed yet would ship exactly the unmeasured rule
    this module exists to scope."""
    track_id: str
    name: str
    terms: tuple[str, ...]
    replacements: dict[str, str] = field(default_factory=dict)


def _pack(track_id: str, name: str, terms: list[str],
          replacements: dict[str, str] | None = None) -> TrackPack:
    return TrackPack(track_id=track_id, name=name, terms=tuple(terms),
                     replacements=dict(replacements or {}))


# Every pack below. Real circuits carry their published corner names; GT's own
# designs have no officially named corners, so theirs carry the venue and
# section names a commentator actually says instead ("Yamagiwa", "Seaside",
# "Nordwand") - which Whisper mangles just as reliably as a corner name would
# be mangled, and which come up far more often.
_ALL: list[TrackPack] = [
    # ---- Real circuits ----
    _pack("le-mans", "24 Heures du Mans Racing Circuit", [
        "Dunlop Curve", "Forest Esses", "Tertre Rouge", "Daytona Chicane",
        "Michelin Chicane", "Mulsanne Straight", "Hunaudières", "Mulsanne Corner",
        "Indianapolis", "Arnage", "Porsche Curves", "Maison Blanche",
        "Karting Esses", "Ford Chicanes", "Motul Turn",
    ]),
    _pack("monza", "Autodromo Nazionale Monza", [
        "Variante del Rettifilo", "Prima Variante", "Curva Grande",
        "Variante della Roggia", "Lesmo", "Variante Ascari", "Parabolica",
        "Curva Alboreto",
    ]),
    _pack("interlagos", "Autódromo de Interlagos", [
        "Senna S", "Curva do Sol", "Descida do Lago", "Ferradura", "Laranjinha",
        "Pinheirinho", "Bico de Pato", "Mergulho", "Junção", "Subida dos Boxes",
        "Arquibancadas",
    ]),
    _pack("autopolis", "Autopolis International Racing Course", [
        "Autopolis", "Nakayama Seimitsu Corner", "Astemo Corner", "Final Corner",
    ]),
    _pack("brands-hatch", "Brands Hatch", [
        "Paddock Hill Bend", "Druids", "Graham Hill Bend", "Surtees",
        "Hawthorn Bend", "Westfield Bend", "Sheene Curve", "Stirling's Bend",
        "Clark Curve", "Clearways", "McLaren",
    ]),
    _pack("gilles-villeneuve", "Circuit Gilles-Villeneuve", [
        "Senna S", "L'Épingle", "Casino Straight", "Wall of Champions",
    ]),
    _pack("barcelona-catalunya", "Circuit de Barcelona-Catalunya", [
        "Elf", "Renault", "Repsol", "Seat", "Würth", "Campsa", "La Caixa",
        "Banc Sabadell", "Europcar", "New Holland",
    ]),
    _pack("spa-francorchamps", "Circuit de Spa-Francorchamps", [
        "La Source", "Eau Rouge", "Raidillon", "Kemmel Straight", "Les Combes",
        "Malmedy", "Rivage", "Bruxelles", "Pouhon", "Fagnes", "Campus",
        "Stavelot", "Blanchimont", "Bus Stop",
    ]),
    _pack("daytona", "Daytona International Speedway", [
        "International Horseshoe", "Le Mans Chicane", "Tri-Oval", "Superstretch",
    ]),
    _pack("fuji", "Fuji International Speedway", [
        "TGR Corner", "Coca-Cola Corner", "100R", "Advan Corner", "Hairpin",
        "300R", "Dunlop Corner", "GR Supra Corner", "Panasonic Corner",
    ]),
    _pack("goodwood", "Goodwood Motor Circuit", [
        "Goodwood", "Madgwick", "Fordwater", "St Mary's", "Lavant", "Woodcote",
    ]),
    _pack("road-atlanta", "Michelin Raceway Road Atlanta", [
        "Road Atlanta", "The Esses",
    ]),
    _pack("mount-panorama", "Mount Panorama Motor Racing Circuit", [
        "Mount Panorama", "Hell Corner", "Griffins Bend", "The Cutting",
        "Quarry Corner", "Reid Park", "Sulman Park", "McPhillamy Park",
        "Brock's Skyline", "The Dipper", "Forrest's Elbow", "Conrod Straight",
        "The Chase", "Murray's Corner",
    ]),
    _pack("red-bull-ring", "Red Bull Ring", [
        "Red Bull Ring", "Niki Lauda Kurve", "Remus Kurve", "Rauch Kurve",
        "Graz Kurve", "Jochen Rindt Kurve",
    ]),
    _pack("suzuka", "Suzuka Circuit", [
        "Suzuka", "First Curve", "S Curves", "Degner", "Dunlop Curve",
        "Hairpin", "Spoon Curve", "130R", "Casio Triangle",
    ]),
    _pack("tsukuba", "Tsukuba Circuit", [
        "Tsukuba", "First Hairpin", "Dunlop Corner", "80R", "MC Corner",
        "Second Hairpin", "Final Corner",
    ]),
    _pack("watkins-glen", "Watkins Glen International", [
        "Watkins Glen", "The Esses", "The Chute", "Toe of the Boot",
        "Heel of the Boot", "Inner Loop", "The Boot",
    ]),
    _pack("laguna-seca", "WeatherTech Raceway Laguna Seca", [
        "Laguna Seca", "Andretti Hairpin", "The Corkscrew", "Rainey Curve",
    ]),
    _pack("willow-springs", "Willow Springs International Raceway", [
        "Willow Springs", "Big Willow", "Streets of Willow", "Horse Thief Mile",
        "Castrol Corner", "Rabbit's Ear", "The Omega", "Monroe Ridge",
        "Repass Pass", "The Sweeper",
    ]),
    _pack("yas-marina", "Yas Marina Circuit", [
        "Yas Marina", "North Hairpin", "Marsa Corner",
    ]),
    # ---- Polyphony's own designs: venue and section names ----
    _pack("alsace", "Alsace", ["Alsace", "Alsace Village"]),
    _pack("lago-maggiore", "Autodrome Lago Maggiore",
          ["Lago Maggiore", "Autodrome Lago Maggiore"]),
    _pack("blue-moon-bay", "Blue Moon Bay Speedway",
          ["Blue Moon Bay", "Blue Moon Bay Speedway"]),
    _pack("bb-raceway", "BB Raceway", ["BB Raceway"]),
    _pack("sainte-croix", "Circuit de Sainte-Croix",
          ["Sainte-Croix", "Circuit de Sainte-Croix"]),
    _pack("colorado-springs", "Colorado Springs", ["Colorado Springs"]),
    _pack("deep-forest", "Deep Forest Raceway",
          ["Deep Forest", "Deep Forest Raceway"]),
    _pack("dragon-trail", "Dragon Trail",
          ["Dragon Trail", "Dragon Trail Seaside", "Dragon Trail Gardens"]),
    _pack("eiger-nordwand", "Eiger Nordwand", ["Eiger Nordwand", "Eiger"]),
    _pack("fishermans-ranch", "Fishermans Ranch", ["Fishermans Ranch"]),
    _pack("grand-valley", "Grand Valley",
          ["Grand Valley", "Grand Valley Highway 1", "Grand Valley South"]),
    _pack("high-speed-ring", "High Speed Ring", ["High Speed Ring"]),
    _pack("kyoto-driving-park", "Kyoto Driving Park",
          ["Kyoto Driving Park", "Yamagiwa", "Miyabi"]),
    _pack("lake-louise", "Lake Louise", ["Lake Louise"]),
    _pack("northern-isle", "Northern Isle Speedway", ["Northern Isle Speedway"]),
    _pack("sardegna", "Sardegna",
          ["Sardegna", "Sardegna Windmills", "Sardegna Road Track"]),
    _pack("special-stage-route-x", "Special Stage Route X",
          ["Special Stage Route X", "Route X"]),
    _pack("tokyo-expressway", "Tokyo Expressway", ["Tokyo Expressway"]),
    _pack("trial-mountain", "Trial Mountain Circuit",
          ["Trial Mountain", "Trial Mountain Circuit"]),
]

PACKS: dict[str, TrackPack] = {pack.track_id: pack for pack in _ALL}

if len(PACKS) != len(_ALL):  # pragma: no cover - a data typo, caught at import
    raise ValueError("duplicate track id in the registry")


def get(track_id: str) -> TrackPack | None:
    """The pack for `track_id`, or None. Exact match only: the id comes from
    the studio's own selector or a hand-edited glossary.json, and quietly
    accepting a different case would hide a typo the caller should report."""
    return PACKS.get(track_id)


def as_layer(pack: TrackPack) -> GlossaryLayer:
    """A pack as a glossary layer, validated by glossary.parse_layer - the
    same function that validates a hand-written glossary.json, so a pack this
    module ships can never be one profile.load would refuse."""
    return parse_layer({"terms": list(pack.terms),
                        "replacements": dict(pack.replacements)})


def listing() -> list[dict]:
    """Every pack as `{"id", "name"}`, sorted by name - what the studio's
    track selector renders. Deliberately not the terms: the selector needs a
    name, and shipping ~40 packs' worth of vocabulary to the browser to fill a
    dropdown would be waste."""
    return sorted(({"id": pack.track_id, "name": pack.name} for pack in _ALL),
                  key=lambda row: row["name"])
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_tracks.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`. If `test_no_pack_approaches_the_truncation_budget` fails for a venue, that pack is too large — shorten it by dropping the least-said names, and say which in your report. Do NOT raise `HOTWORD_BUDGET_CHARS` to make a pack fit.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/tracks.py tests/test_tracks.py
git commit -m "feat(tracks): venue registry with 39 GT7 track packs"
```

---

### Task 2: `glossary.py` — the `track` key on a layer

**Files:**
- Modify: `src/yt_shorts/glossary.py`
- Test: `tests/test_glossary_layers.py`

**Interfaces:**
- Consumes: the existing `GlossaryLayer`, `parse_layer`, `load`, `EMPTY_LAYER`.
- Produces: `GlossaryLayer.track: str | None` (third field, defaulting to `None`), parsed by `parse_layer` from an optional `"track"` key.

The default still holds the Nordschleife after this task; nothing else changes. Tree stays green.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary_layers.py`:

```python
class TestTrackKey:
    def test_absent_track_is_none(self):
        assert parse_layer({"terms": ["Karussell"]}).track is None

    def test_a_track_is_parsed(self):
        assert parse_layer({"track": "spa-francorchamps"}).track == "spa-francorchamps"

    def test_a_track_is_stripped(self):
        assert parse_layer({"track": "  monza  "}).track == "monza"

    def test_an_empty_track_is_a_defect(self):
        with pytest.raises(ValueError, match="'track' must be a non-empty string"):
            parse_layer({"track": "   "})

    def test_a_non_string_track_is_a_defect(self):
        with pytest.raises(ValueError, match="'track' must be a non-empty string"):
            parse_layer({"track": 42})

    def test_a_null_track_is_the_same_as_absent(self):
        """The studio's selector sends null for "no track", and that must mean
        the same thing as never having chosen one."""
        assert parse_layer({"track": None}).track is None

    def test_an_over_long_track_is_a_defect(self):
        with pytest.raises(ValueError, match=f"over the {MAX_ENTRY_LENGTH}-character cap"):
            parse_layer({"track": "x" * (MAX_ENTRY_LENGTH + 1)})

    def test_the_empty_layer_has_no_track(self):
        assert EMPTY_LAYER.track is None

    def test_load_reads_a_track(self, tmp_path):
        path = tmp_path / "glossary.json"
        path.write_text(json.dumps({"track": "monza", "terms": ["Lesmo"]}),
                        encoding="utf-8")
        layer = load(path)
        assert layer.track == "monza"
        assert layer.terms == {"lesmo": ("Lesmo", True)}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_layers.py -q -k Track
```
Expected: FAIL — `AttributeError: 'GlossaryLayer' object has no attribute 'track'`.

- [ ] **Step 3: Implement**

In `src/yt_shorts/glossary.py`, add the field to `GlossaryLayer` (after `replacements`):

```python
    track: str | None = None
```

and extend its docstring with:

```
    `track` names a venue in tracks.PACKS whose vocabulary applies to this
    layer's event. It is a SELECTION, not content: the pack is referenced by
    profile._load_glossary and never copied in, so correcting a name in the
    registry corrects every event at that venue. Only an event may set it -
    that scope rule lives in profile._load_glossary, which is the only place
    that knows which layer it is reading.
```

Add the parser beside `_parse_terms`/`_parse_replacements`:

```python
def _parse_track(value: object) -> str | None:
    """An optional venue id. `null` and an absent key mean the same thing -
    "no track" - because that is what the studio's selector sends when the
    operator clears it, and a save that meant something different from an
    unset file would be a trap."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"'track' must be a non-empty string naming a venue, found {value!r}")
    track = value.strip()
    _check_text(track, "track")
    return track
```

and wire it into `parse_layer`'s return:

```python
    return GlossaryLayer(terms=_parse_terms(data.get("terms", [])),
                         replacements=_parse_replacements(data.get("replacements", {})),
                         track=_parse_track(data.get("track")))
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_layers.py tests/test_glossary.py tests/test_glossary_admin.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/glossary.py tests/test_glossary_layers.py
git commit -m "feat(glossary): a layer may name its venue with a track key"
```

---

### Task 3: The switch — the Nürburgring packs, an empty default, and the pack layer

This is the one task that must land atomically: emptying the built-in default and inserting the pack layer are two halves of one behaviour change, and separating them would leave the tree red and `erfofficial/N24-2026` silently uncorrected in between.

**Files:**
- Modify: `src/yt_shorts/glossary.py` (remove `DEFAULT_TERMS`/`DEFAULT_REPLACEMENTS`, empty `DEFAULT_LAYER`)
- Modify: `src/yt_shorts/tracks.py` (add `nurburgring-nordschleife` and `nurburgring-gp`)
- Modify: `src/yt_shorts/profile.py` (`_load_glossary`)
- Test: `tests/test_tracks.py`, `tests/test_glossary_layers.py`, `tests/test_profile.py`, `tests/test_stream_transcribe.py`

**Interfaces:**
- Consumes: `tracks.get`, `tracks.as_layer` from Task 1; `GlossaryLayer.track` from Task 2.
- Produces: `profile._load_glossary(event_dir, channel_dir, workspace_root) -> tuple[Glossary, list[str]]` — signature unchanged, behaviour extended.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracks.py`:

```python
class TestNurburgring:
    def test_both_nurburgring_packs_exist(self):
        assert tracks.get("nurburgring-nordschleife") is not None
        assert tracks.get("nurburgring-gp") is not None

    def test_the_nordschleife_pack_carries_the_measured_replacements(self):
        """These ten keys were OBSERVED in the real V9nVNEQNdR4 transcript and
        used to live in glossary's built-in default. They move here, which is
        what stops `carousel` firing on a circuit that has its own Carousel."""
        pack = tracks.get("nurburgring-nordschleife")
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
            assert pack.replacements[decoded] == correct

    def test_the_two_nurburgring_packs_do_not_share_corners(self):
        nords = {t.lower() for t in tracks.get("nurburgring-nordschleife").terms}
        gp = {t.lower() for t in tracks.get("nurburgring-gp").terms}
        assert not (nords & gp)

    def test_combining_them_would_exceed_the_budget(self):
        """The measurement that made them two packs rather than one: 249
        tokens together, over faster-whisper's 224-token truncation point
        before the operator adds a single name of their own. If a future
        change merges them, this fails."""
        both = (list(tracks.get("nurburgring-nordschleife").terms)
                + list(tracks.get("nurburgring-gp").terms))
        bias = glossary.hotwords(glossary.Glossary(terms=both, replacements={}))
        assert len(bias) > glossary.HOTWORD_BUDGET_CHARS
```

Replace `TestBuiltInDefault` in `tests/test_glossary_layers.py` with:

```python
class TestBuiltInDefaultIsEmpty:
    """The built-in default used to carry the Nordschleife. It does not any
    more: there is no proper noun that is correct on every circuit, so an
    always-on layer has nothing to hold, and a track-specific rule in it fired
    on the wrong track (`carousel` -> `Karussell` on any circuit with its own
    Carousel). The Nordschleife lives in tracks.PACKS now."""

    def test_the_default_layer_is_empty(self):
        assert DEFAULT_LAYER == EMPTY_LAYER

    def test_the_old_default_constants_are_gone(self):
        import yt_shorts.glossary as g
        assert not hasattr(g, "DEFAULT_TERMS")
        assert not hasattr(g, "DEFAULT_REPLACEMENTS")
```

Append to `tests/test_profile.py`'s `TestGlossary`:

```python
    def test_an_event_track_inserts_the_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "spa-francorchamps"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Eau Rouge" in p.config["glossary"].terms
        assert "Raidillon" in p.config["glossary"].terms

    def test_no_track_means_no_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])

        p = profile.load("chan/event")

        assert p.config["glossary"].terms == []
        assert p.config["glossary"].replacements == {}

    def test_the_operator_layers_win_over_the_pack(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"terms": {"Eau Rouge": False}}), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "spa-francorchamps"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert "Eau Rouge" not in p.config["glossary"].terms
        assert "Pouhon" in p.config["glossary"].terms

    def test_operator_terms_precede_the_packs(self, monkeypatch, tmp_path):
        """The truncation-survival property: faster-whisper drops the LAST
        hotwords, so the operator's own must come first."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"terms": ["Rei Racing"]}), encoding="utf-8")
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        p = profile.load("chan/event")

        assert p.config["glossary"].terms[0] == "Rei Racing"

    def test_an_unknown_track_is_a_reported_defect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "events" / "event" / "glossary.json").write_text(
            json.dumps({"track": "nope"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="unknown track 'nope'"):
            profile.load("chan/event")

    def test_a_track_at_channel_scope_is_a_reported_defect(self, monkeypatch, tmp_path):
        """Writing it at the wrong level must be an error, not a silent
        no-op - otherwise an operator finds out three hours into a transcript
        that their corner names never applied."""
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (channel_dir / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="only an event selects a track"):
            profile.load("chan/event")

    def test_a_track_at_workspace_scope_is_a_reported_defect(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        (tmp_path / "glossary.json").write_text(
            json.dumps({"track": "monza"}), encoding="utf-8")

        with pytest.raises(profile.ProfileError, match="only an event selects a track"):
            profile.load("chan/event")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_tracks.py tests/test_profile.py tests/test_glossary_layers.py -q
```
Expected: FAIL — the Nürburgring packs do not exist, `DEFAULT_LAYER` is not empty, and `profile.load` does not know the word `track`.

- [ ] **Step 3: Move the Nordschleife into two packs**

In `src/yt_shorts/tracks.py`, add these two entries at the TOP of `_ALL`'s real-circuit section, copying the term list and the replacement map verbatim out of `glossary.DEFAULT_TERMS`/`DEFAULT_REPLACEMENTS`:

```python
    _pack("nurburgring-nordschleife", "Nürburgring Nordschleife", [
        "Nordschleife", "Hatzenbach", "Hocheichen", "Quiddelbacher Höhe",
        "Flugplatz", "Schwedenkreuz", "Aremberg", "Fuchsröhre",
        "Adenauer Forst", "Metzgesfeld", "Kallenhard", "Wehrseifen",
        "Ex-Mühle", "Bergwerk", "Kesselchen", "Klostertal", "Steilstrecke",
        "Karussell", "Hohe Acht", "Wippermann", "Eschbach", "Brünnchen",
        "Pflanzgarten", "Schwalbenschwanz", "Galgenkopf", "Döttinger Höhe",
        "Antoniusbuche", "Tiergarten", "Hohenrain", "Kleines Karussell",
        "Stefan-Bellof-S", "Mutkurve",
    ], {
        # Every key OBSERVED in the real V9nVNEQNdR4 transcript. `carousel` is
        # the reason this belongs to a venue rather than to an always-on
        # default: Road America, Sears Point and Watkins Glen each have a
        # Carousel of their own, and this rule must not reach them.
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
    }),
    _pack("nurburgring-gp", "Nürburgring Grand-Prix-Strecke", [
        "Mercedes-Arena", "Yokohama-S", "Ford-Kurve", "Goodyear-Kehre",
        "Michael-Schumacher-S", "Kumho-Kurve", "Warsteiner-Kurve", "RTL-Kurve",
        "Advan-Bogen", "Veedol-Schikane", "Coca-Cola-Kurve", "Valvoline-Kurve",
    ]),
```

Then in `src/yt_shorts/glossary.py`, DELETE `DEFAULT_TERMS`, `DEFAULT_REPLACEMENTS` and their comments entirely, and replace the `DEFAULT_LAYER` assignment with:

```python
# The layer that applies before any other, for every channel and every clip.
# It is EMPTY, and that is the design rather than an omission: there is no
# proper noun that is correct on every circuit, so an always-on layer has
# nothing it can safely hold. What used to live here - the Nürburgring
# Nordschleife's section names and the ten mis-hearings measured on a real
# transcript - is now tracks.PACKS["nurburgring-nordschleife"], selected by
# the events that actually race there. `carousel -> Karussell` is the reason:
# as an always-on rule it rewrote the Carousel at Road America, Sears Point
# and Watkins Glen too.
#
# Do NOT repopulate this with "generally useful" racing vocabulary without
# measuring it first. Every term here costs hotword-prompt budget on every
# window of every clip and every chunk, for every channel (see
# HOTWORD_BUDGET_CHARS), and that cost is what the track packs exist to spend
# deliberately.
DEFAULT_LAYER = EMPTY_LAYER
```

Update `glossary.py`'s module docstring: the paragraph naming "DEFAULT_LAYER here" as the least specific layer becomes:

```
Four layers feed one glossary, and a fifth sits between them: the built-in
default (now empty, see DEFAULT_LAYER), the track pack an event selects (see
tracks.py), the workspace's glossary.json, the channel's, the event's - most
specific winning per entry, with a falsy entry disabling one inherited from a
less specific layer.
```

- [ ] **Step 4: Insert the pack layer in `profile.py`**

Replace `_load_glossary`'s body (keeping the existing docstring and extending it as shown):

```python
def _load_glossary(event_dir: Path, channel_dir: Path,
                   workspace_root: Path) -> tuple[Glossary, list[str]]:
    """Loads this profile's Glossary by MERGING five layers.

    Least to most specific: the built-in default (now EMPTY - see
    glossary.DEFAULT_LAYER), the track pack the EVENT selects, the
    workspace-central glossary.json, the channel's, then the event's. The most
    specific layer wins per entry and a falsy entry disables one inherited
    from a less specific layer - see merge_glossaries.

    The pack is REFERENCED, not copied: an event names a venue and gets
    whatever tracks.py currently says about it, so correcting a corner name
    corrects every event at that venue with no migration.

    Only an EVENT may select a track. The same key at workspace or channel
    scope is reported as a defect rather than ignored - an operator who writes
    it at the wrong level must find out from the error, not from three hours
    of transcript with no corner names in it.

    This REPLACED an earlier wholesale rule, under which an event's own
    glossary.json replaced the channel's outright. The ambiguity that rule
    avoided is now resolved rather than dodged: *add* is the rule, and "only
    these" has an explicit spelling. Restoring the override would silently
    drop the corner names for the one channel that both needs them and has a
    glossary.json of its own. Do not.

    A malformed file at any layer is reported as a problem string, not raised,
    so the caller collects it with every other profile defect - mirroring
    _load_lexicon, which sits right below this. Every layer being absent is
    not an error: the profile simply has no proper nouns to correct.
    """
    problems: list[str] = []
    paths = [(workspace.glossary_path(workspace_root), "workspace"),
             (channel_dir / "glossary.json", "channel"),
             (event_dir / "glossary.json", "event")]
    loaded: list[tuple[glossary_module.GlossaryLayer, str]] = []
    for path, scope in paths:
        if not path.exists():
            continue
        try:
            loaded.append((glossary_module.load(path), scope))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            problems.append(f"{path}: {error}")

    track = None
    for layer, scope in loaded:
        if layer.track is None:
            continue
        if scope != "event":
            problems.append(
                f"{scope} glossary.json: only an event selects a track, "
                f"found 'track': {layer.track!r}")
            continue
        track = layer.track

    layers = [glossary_module.DEFAULT_LAYER]
    if track is not None:
        pack = tracks.get(track)
        if pack is None:
            problems.append(
                f"{event_dir / 'glossary.json'}: unknown track {track!r} - "
                f"valid ids: {', '.join(sorted(tracks.PACKS))}")
        else:
            layers.append(tracks.as_layer(pack))
    layers.extend(layer for layer, _scope in loaded)
    return merge_glossaries(layers), problems
```

Add the import beside the other `from . import` lines in `profile.py`:

```python
from . import tracks
```

- [ ] **Step 5: Fix the tests this breaks elsewhere**

`tests/test_stream_transcribe.py`'s `TestSubprocessDecoderRealChain` builds its glossary from `merge_glossaries([DEFAULT_LAYER])`, which is now empty — and the test's whole point is that a NON-empty glossary's replacements never reach the worker. Change it to use a pack:

```python
        from yt_shorts import tracks
        from yt_shorts.profile import merge_glossaries
        ...
        subprocess_decoder(tmp_path / "audio.webm", 0.0, 600.0,
                           glossary=merge_glossaries(
                               [tracks.as_layer(tracks.get("nurburgring-nordschleife"))]),
                           runner=capturing_runner)
```

and update its docstring's reference from "the real shipped default glossary" to "the real shipped Nordschleife pack — 32 terms AND 10 replacements".

Then run the FULL suite and fix whatever else asserted on the old default. Name every test you touched in your report.

- [ ] **Step 6: Run every gate**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/glossary.py src/yt_shorts/tracks.py src/yt_shorts/profile.py tests/
git commit -m "feat(tracks): the Nordschleife becomes a pack; the built-in default is empty"
```

---

### Task 4: `glossary_admin` — carry the track, drop adopt-default

**Files:**
- Modify: `src/yt_shorts/glossary_admin.py`
- Test: `tests/test_glossary_admin.py`

**Interfaces:**
- Consumes: `GlossaryLayer.track`, `tracks.PACKS`, `tracks.as_layer`.
- Produces:
  - `read(...)` gains `"track"` (the event scope's own selection, else None) and a `"track"` source in `effective`
  - `update(root, terms, replacements, *, track=None, channel=None, event=None) -> None`
  - `adopt_default` REMOVED

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_glossary_admin.py`:

```python
class TestTrack:
    def test_read_reports_no_track_by_default(self, root):
        assert glossary_admin.read(root, channel="erf", event="race")["track"] is None

    def test_update_writes_the_track(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        written = json.loads(
            (root / "channels" / "erf" / "events" / "race" / "glossary.json")
            .read_text(encoding="utf-8"))
        assert written["track"] == "monza"

    def test_read_reports_the_track_back(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        assert glossary_admin.read(root, channel="erf", event="race")["track"] == "monza"

    def test_the_pack_appears_as_an_inherited_layer(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        state = glossary_admin.read(root, channel="erf", event="race")
        assert state["effective"]["terms"]["lesmo"] == {
            "term": "Lesmo", "enabled": True, "source": "track"}

    def test_a_saved_row_edit_PRESERVES_the_track(self, root):
        """The data-loss risk this feature carries: the editor overwrites the
        whole own layer on every save, so a track dropped anywhere in
        read -> row -> payload -> write disappears on the next unrelated
        edit."""
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        state = glossary_admin.read(root, channel="erf", event="race")

        glossary_admin.update(root, {"Rei Racing": True}, {},
                              track=state["track"], channel="erf", event="race")

        assert glossary_admin.read(root, channel="erf", event="race")["track"] == "monza"

    def test_clearing_the_track_removes_the_key(self, root):
        glossary_admin.update(root, {}, {}, track="monza", channel="erf", event="race")
        glossary_admin.update(root, {}, {}, track=None, channel="erf", event="race")
        written = json.loads(
            (root / "channels" / "erf" / "events" / "race" / "glossary.json")
            .read_text(encoding="utf-8"))
        assert "track" not in written
        assert glossary_admin.read(root, channel="erf", event="race")["track"] is None

    def test_an_unknown_track_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="nope", channel="erf", event="race")
        assert excinfo.value.kind == "bad_glossary"

    def test_a_track_at_channel_scope_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="monza", channel="erf")
        assert excinfo.value.kind == "bad_glossary"

    def test_a_track_at_workspace_scope_is_refused(self, root):
        with pytest.raises(glossary_admin.GlossaryAdminError) as excinfo:
            glossary_admin.update(root, {}, {}, track="monza")
        assert excinfo.value.kind == "bad_glossary"


class TestAdoptDefaultIsGone:
    def test_the_function_no_longer_exists(self):
        """With an empty built-in default it would adopt nothing. Per-row
        Override and Disable already let an operator own any pack entry."""
        assert not hasattr(glossary_admin, "adopt_default")
```

Delete the whole `TestAdoptDefault` class from the same file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_admin.py -q
```
Expected: FAIL — `update() got an unexpected keyword argument 'track'`.

- [ ] **Step 3: Implement**

In `src/yt_shorts/glossary_admin.py`:

Add the import: `from . import tracks`.

Extend `_own_shape` so a track survives the rebuild:

```python
def _own_shape(layer) -> dict:
    """A layer as the file/wire shape a PUT sends back: terms keyed by their
    raw spelling, replacements keyed by their raw key, plus the track when one
    is selected. Rebuilt from the parsed layer rather than echoed from disk,
    so a GET always returns the canonical form regardless of which accepted
    shape the file on disk happens to use.

    The `track` key is omitted entirely when there is none, rather than
    written as null - an absent key and a null mean the same thing to
    parse_layer, and not writing it keeps a hand-edited file clean."""
    shape = {
        "terms": {spelling: enabled for spelling, enabled in layer.terms.values()},
        "replacements": {raw: text for raw, text in layer.replacements.values()},
    }
    if layer.track is not None:
        shape["track"] = layer.track
    return shape
```

Extend `_layers` to insert the pack after the default:

```python
def _layers(root, channel: str | None, event: str | None) -> tuple[list, list[str]]:
    """Every layer that applies at this scope, least specific first, as
    (source, layer) pairs, plus a problem string per layer that failed to
    load. Stops at the requested scope.

    The event's own layer is read FIRST when one applies, because its `track`
    decides which pack sits between the default and the workspace layer - the
    pack is more specific than the built-in default and less specific than
    anything the operator wrote."""
    problems: list[str] = []
    workspace_path = workspace.glossary_path(root)
    workspace_layer = _load_layer_or_empty(workspace_path, problems)
    layers = [("default", glossary_module.DEFAULT_LAYER)]

    event_layer = None
    if channel is not None and event is not None:
        event_path = Path(root) / "channels" / channel / "events" / event / "glossary.json"
        event_layer = _load_layer_or_empty(event_path, problems)
        pack = tracks.get(event_layer.track) if event_layer.track else None
        if pack is not None:
            layers.append(("track", tracks.as_layer(pack)))
        elif event_layer.track:
            problems.append(f"{event_path}: unknown track {event_layer.track!r}")

    layers.append(("workspace", workspace_layer))
    if channel is None:
        return layers, problems
    channel_path = Path(root) / "channels" / channel / "glossary.json"
    layers.append(("channel", _load_layer_or_empty(channel_path, problems)))
    if event is None:
        return layers, problems
    layers.append(("event", event_layer))
    return layers, problems
```

Add `"track"` to `SOURCES`: `SOURCES = ("default", "track", "workspace", "channel", "event")`.

Extend `read` to report the selection — after `own = _own_shape(layers[-1][1])`, add `track` to the returned dict:

```python
    # CORRECTED after the Task 4 review. This originally read
    # `"track": own.get("track")`, which ECHOES a track a hand-edited
    # workspace or channel glossary.json carries - with no problem reported,
    # while profile._load_glossary reports exactly that file as a defect and
    # ignores the selection. It also wedged saving: the editor sends `track`
    # back with every save, and update refuses a track at channel scope, so
    # no channel-scope glossary edit could be saved at all. Report the EVENT
    # scope's selection only, strip the key from `own` at any other scope
    # (own is what the client sends back), and append a problem naming the
    # offending file, worded as profile._load_glossary words it.
    return {"scope": scope, "own": own,
            "track": own.get("track") if scope == "event" else None,
            "effective": {"terms": effective_terms, "replacements": effective_reps},
            "problems": problems}
```

Extend `update`:

```python
def update(root, terms, replacements, *, track: str | None = None,
           channel: str | None = None, event: str | None = None) -> None:
    """Overwrites the scope's own layer with exactly `terms`, `replacements`
    and `track` - never a merge. Empty dicts and a null track still write the
    file (without a `track` key) so "I cleared this layer" is an explicit,
    re-editable state.

    A caller that means to keep the current track must PASS it back: this
    overwrites, so omitting it clears it. The studio's editor reads it from
    `read`'s `track` and sends it with every save for exactly that reason.

    Validated through glossary.parse_layer - the same function profile.load
    validates a file with - plus two rules parse_layer cannot know because it
    sees one file without knowing which layer it is: the track must name a
    real pack, and only an event may select one.

    The payload validation deliberately runs before the scope is resolved.
    That is safe ONLY because parse_layer is pure and touches no filesystem;
    if this function ever grows a read-before-write, the ordering must be
    flipped so an unvalidated segment cannot reach disk."""
    # CORRECTED after the Task 4 review. This guard originally read
    # `if event is None:` - which is NOT the same question. _resolve
    # documents that an `event` passed without a `channel` is validated and
    # then silently ignored, so the scope falls back to WORKSPACE while
    # `event is not None` still holds: update(root, {}, {}, track="monza",
    # event="race") wrote a track into <workspace>/glossary.json, and
    # profile.load then raised for every event of every channel in that
    # workspace. Gate on the RESOLVED scope. _resolve is safe to call first:
    # it validates segments and checks existence, and writes nothing.
    payload = {"terms": terms, "replacements": replacements}
    if track is not None:
        payload["track"] = track
    try:
        layer = glossary_module.parse_layer(payload)
    except ValueError as error:
        raise GlossaryAdminError(str(error), kind="bad_glossary") from error
    scope, target = _resolve(root, channel, event)
    if layer.track is not None:
        if scope != "event":
            raise GlossaryAdminError(
                "only an event selects a track", kind="bad_glossary")
        if tracks.get(layer.track) is None:
            raise GlossaryAdminError(
                f"unknown track {layer.track!r}", kind="bad_glossary")
    target.write_text(json.dumps(_own_shape(layer), indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
```

Delete `adopt_default` entirely, and remove the paragraph about it from the module docstring.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_glossary_admin.py tests/test_lexicon_admin.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/glossary_admin.py tests/test_glossary_admin.py
git commit -m "feat(studio): glossary_admin carries an event's track; adopt-default removed"
```

---

### Task 5: The studio routes

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_glossary_api.py`

**Interfaces:**
- Consumes: `tracks.listing()`, `glossary_admin.read/update` from Task 4.
- Produces: `GET /api/tracks` → `{"tracks": [{"id", "name"}, ...]}`; `GlossaryBody` gains `track: str | None = None`; `POST /api/glossary/adopt-default` REMOVED.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_glossary_api.py`:

```python
class TestTracksRoute:
    def test_lists_every_pack(self, client):
        from yt_shorts import tracks
        body = client.get("/api/tracks").json()
        assert len(body["tracks"]) == len(tracks.PACKS)

    def test_carries_id_and_name_only(self, client):
        for row in client.get("/api/tracks").json()["tracks"]:
            assert set(row) == {"id", "name"}

    def test_is_not_shadowed_by_the_spa_fallback(self, client):
        response = client.get("/api/tracks")
        assert response.headers["content-type"].startswith("application/json")


class TestTrackOnTheGlossaryRoutes:
    def test_put_writes_the_track_at_event_scope(self, client):
        body = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"}).json()
        assert body["track"] == "monza"
        assert body["effective"]["terms"]["lesmo"]["source"] == "track"

    def test_omitting_the_track_clears_it(self, client):
        client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"})
        body = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}}).json()
        assert body["track"] is None

    def test_an_unknown_track_is_400(self, client):
        response = client.put("/api/channels/erf/events/race/glossary", json={
            "terms": {}, "replacements": {}, "track": "nope"})
        assert response.status_code == 400

    def test_a_track_at_channel_scope_is_400(self, client):
        response = client.put("/api/channels/erf/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"})
        assert response.status_code == 400

    def test_a_track_at_workspace_scope_is_400(self, client):
        response = client.put("/api/glossary", json={
            "terms": {}, "replacements": {}, "track": "monza"})
        assert response.status_code == 400


class TestAdoptDefaultRouteIsGone:
    def test_the_route_404s(self, client):
        assert client.post("/api/glossary/adopt-default").status_code == 404
```

Delete the `TestAdoptDefault` class from the same file.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_glossary_api.py -q
```
Expected: FAIL — 404 on `/api/tracks`, and the track is ignored on PUT.

- [ ] **Step 3: Implement**

In `src/yt_shorts/studio/api.py`, add `from .. import tracks` beside the other imports.

Extend `GlossaryBody`:

```python
    track: str | None = None
```

and add to its docstring:

```
    `track` names the venue whose shipped vocabulary applies (see tracks.py).
    Only an event may set it; the route refuses it at any other scope. It is
    OPTIONAL here but NOT sticky: a PUT overwrites the whole layer, so a
    client that means to keep the current track must send it back with every
    save. The editor reads it from the GET and does exactly that.
```

Add the route beside the other glossary routes:

```python
    @app.get("/api/tracks")
    def get_tracks() -> dict:
        """Every venue in the registry, id and name only - what the event
        editor's track selector renders. A read of shipped code, so it needs
        neither the workspace nor a profile."""
        return {"tracks": tracks.listing()}
```

Pass the track through all three PUT handlers, e.g. for the event scope:

```python
    @app.put(EV + "/glossary")
    def put_event_glossary(channel: str, event: str, body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, track=body.track,
            channel=channel, event=event))
        return _glossary_or_http(
            lambda: glossary_admin.read(root, channel=channel, event=event))
```

Do the same for the workspace and channel PUTs — they pass `track=body.track` too, so `glossary_admin` refuses it at those scopes with a mapped 400 rather than the route silently dropping it.

Delete the `post_adopt_default_glossary` route.

Update the module's route-list docstring: add `GET /api/tracks`, remove the adopt-default line.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_glossary_api.py tests/test_studio_api.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_glossary_api.py
git commit -m "feat(studio): GET /api/tracks and a track on the glossary PUT"
```

---

### Task 6: Frontend client and the fifth source

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/momentsLexicon.ts` (`sourceLabel` only)
- Modify: `src/yt_shorts/studio/web/src/glossaryLayers.test.ts`
- Modify: `src/yt_shorts/studio/web/src/momentsLexicon.test.ts`

**Interfaces:**
- Produces: `LayerSource` gains `'track'`; `TrackRow` type `{id: string; name: string}`; `getTracks(): Promise<TrackRow[]>`; `GlossaryLayers.track: string | null`; `putGlossary(scope, terms, replacements, track)`; `adoptDefaultGlossary` REMOVED.

- [ ] **Step 1: Write the failing tests**

Append to `src/yt_shorts/studio/web/src/momentsLexicon.test.ts`:

```ts
describe('sourceLabel', () => {
  it('labels the track layer', () => {
    expect(sourceLabel('track')).toBe('track')
  })

  it('still labels the four original layers', () => {
    expect(sourceLabel('default')).toBe('built-in')
    expect(sourceLabel('workspace')).toBe('workspace')
    expect(sourceLabel('channel')).toBe('channel')
    expect(sourceLabel('event')).toBe('event')
  })
})
```

Append to `src/yt_shorts/studio/web/src/glossaryLayers.test.ts`:

```ts
describe('track rows', () => {
  it('marks a pack row inherited, never own', () => {
    const rows = toTermRows(
      layers({
        own: { terms: {}, replacements: {} },
        effective: {
          terms: { lesmo: { term: 'Lesmo', enabled: true, source: 'track' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ source: 'track', own: false })
  })

  it('lets an own row override a pack row', () => {
    const rows = toTermRows(
      layers({
        own: { terms: { Lesmo: false }, replacements: {} },
        effective: {
          terms: { lesmo: { term: 'Lesmo', enabled: false, source: 'event' } },
          replacements: {},
        },
      }),
    )
    expect(rows[0]).toMatchObject({ own: true, enabled: false })
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd src/yt_shorts/studio/web && npm test -- sourceLabel && cd -
```
Expected: FAIL — `sourceLabel('track')` is a type error and returns undefined.

- [ ] **Step 3: Implement**

In `momentsLexicon.ts`, extend `sourceLabel`'s switch with the new case and its note:

```ts
    case 'track':
      // The venue pack an event selected (see tracks.py). Labelled by layer
      // rather than by venue name: the row already sits in that event's
      // editor, so which track is not in question - only where the row came
      // from.
      return 'track'
```

In `api.ts`:

```ts
export type LayerSource = 'default' | 'track' | 'workspace' | 'channel' | 'event'

/** One venue in the shipped registry (see tracks.py), as GET /api/tracks
 * returns it. Id and name only - the selector needs a label, and shipping
 * every pack's vocabulary to fill a dropdown would be waste. */
export interface TrackRow {
  id: string
  name: string
}

/** GET /api/tracks - every venue, sorted by name. A read of shipped code, so
 * it takes no scope and never 404s. */
export function getTracks(): Promise<TrackRow[]> {
  return fetch('/api/tracks').then(asJson<{ tracks: TrackRow[] }>).then((body) => body.tracks)
}
```

Add `track: string | null` to `GlossaryLayers`, documented:

```ts
  /** The venue this EVENT selected, or null. Only ever non-null at event
   * scope. A PUT overwrites the whole layer, so a caller that means to keep
   * it must send it back — see putGlossary. */
  track: string | null
```

Extend `putGlossary`:

```ts
export function putGlossary(
  scope: { channel?: string; event?: string },
  terms: Record<string, boolean>,
  replacements: Record<string, string | null>,
  track: string | null = null,
): Promise<GlossaryLayers> {
  return fetch(glossaryBase(scope), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ terms, replacements, track }),
  }).then(asJson<GlossaryLayers>)
}
```

and extend its docstring with: `Omitting `track` CLEARS the event's venue selection — the server overwrites the whole layer. Always pass back what the last GET reported unless the operator changed it.`

Delete `adoptDefaultGlossary` and its docstring.

- [ ] **Step 4: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0 (it will flag the editor's now-removed `adoptDefaultGlossary` import — Task 7 fixes that; if you cannot get a clean `tsc` without touching the component, make the minimal deletion of the adopt button here and say so in your report), oxlint clean, all Vitest pass.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/momentsLexicon.ts src/yt_shorts/studio/web/src/glossaryLayers.test.ts src/yt_shorts/studio/web/src/momentsLexicon.test.ts
git commit -m "feat(studio-web): track listing client and the track source layer"
```

---

### Task 7: The track selector in the editor

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx`

**Interfaces:**
- Consumes: `getTracks`, `TrackRow`, `GlossaryLayers.track`, `putGlossary(..., track)` from Task 6.

- [ ] **Step 1: Add the selector**

In `GlossaryEditor.tsx`:

Import `Select` from `@mantine/core`, and `getTracks`, `type TrackRow` from `../api`. Remove the `adoptDefaultGlossary` import.

Add state beside the existing row state:

```tsx
  const [tracks, setTracks] = useState<TrackRow[]>([])
  const [track, setTrack] = useState<string | null>(null)
  const [savedTrack, setSavedTrack] = useState<string | null>(null)
```

`applyState` sets both from the response:

```tsx
    setTrack(layers.track)
    setSavedTrack(layers.track)
```

Load the registry once, alongside the layer load — a separate effect, because it does not depend on the scope:

```tsx
  useEffect(() => {
    // Only an event picks a venue, so only an event scope needs the list.
    if (!channel || !event) return
    let cancelled = false
    getTracks()
      .then((rows) => {
        if (!cancelled) setTracks(rows)
      })
      .catch(() => {
        // A failed registry load leaves the selector empty rather than
        // breaking the editor: the rows below still load and save, and the
        // operator can still edit everything except the venue. Surfacing it
        // as a second error banner would bury the one that matters.
        if (!cancelled) setTracks([])
      })
    return () => {
      cancelled = true
    }
  }, [channel, event])
```

Include the track in `dirty`:

```tsx
    (JSON.stringify(termRows) !== JSON.stringify(savedTermRows) ||
      JSON.stringify(repRows) !== JSON.stringify(savedRepRows) ||
      track !== savedTrack)
```

Send it on save — this is the line that keeps the selection alive across an unrelated edit:

```tsx
      const layers = await putGlossary(scope, payload.terms, payload.replacements, track)
```

Render the selector above the intro text, outside the scrolling region (`flex: '0 0 auto'`), only at event scope:

```tsx
      {channel && event ? (
        <Stack gap={4} style={{ flex: '0 0 auto', marginBottom: 8 }}>
          <Select
            label="Circuit"
            placeholder="No circuit selected"
            data={tracks.map((row) => ({ value: row.id, label: row.name }))}
            value={track}
            onChange={setTrack}
            clearable
            searchable
            nothingFoundMessage="No circuit of that name"
          />
          <Text size="xs" c="dimmed">
            The circuit's own corner names apply to this event, and only to it - which
            is why a correction like "carousel" is safe here and would not be as a
            global rule. Rows it contributes appear below marked "track"; override or
            disable any of them like any other inherited row.
          </Text>
        </Stack>
      ) : null}
```

Delete the "Adopt the built-in default" button, the `adoptOpen`/`adopting`/`adoptError` state, `handleAdopt`, the whole adopt `Modal`, and the now-unused `isWorkspaceScope`. The Save button moves to fill the footer row.

Update the component's module docstring: the paragraph naming the adopt behaviour is replaced with one naming the selector, and stating that the pack is referenced rather than copied so an operator who wants to change a pack entry overrides that row instead of editing shipped data.

- [ ] **Step 2: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass. Do NOT run `npm run build` — Task 8 owns the committed bundle.

- [ ] **Step 3: Verify scrolling at a short viewport**

The selector adds a fixed header row to a pane that already has an intro, two lists and a footer. Confirm the structure still holds: exactly ONE `flex: 1 1 auto; minHeight: 0; overflowY: auto` region, every sibling `flex: 0 0 auto`. State in your report whether you verified this structurally or against a running studio, and how.

- [ ] **Step 4: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/GlossaryEditor.tsx
git commit -m "feat(studio-web): pick an event's circuit in the glossary editor"
```

---

### Task 8: Migration, docs, E2E, bundle, full verification

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `tests/test_studio_e2e.py`
- Modify: `src/yt_shorts/studio/static/**` (rebuilt)
- Workspace: `~/YT-Shorts-Data/channels/erfofficial/events/N24-2026/glossary.json`

- [ ] **Step 1: Migrate the operator's live event**

`erfofficial/N24-2026` currently relies on the built-in default for every corner correction. Set its venue, then PROVE the merged result is unchanged:

```bash
PYTHONPATH=src YT_SHORTS_DATA="$HOME/YT-Shorts-Data" .venv/bin/python - <<'PY'
import json, pathlib
from yt_shorts.profile import load
p = pathlib.Path.home() / "YT-Shorts-Data/channels/erfofficial/events/N24-2026/glossary.json"
data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
data["track"] = "nurburgring-nordschleife"
p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
g = load("erfofficial/N24-2026").config["glossary"]
print("terms:", len(g.terms), "replacements:", len(g.replacements))
for name in ["Karussell", "Schwalbenschwanz", "Galgenkopf", "Kesselchen", "Hohe Acht"]:
    assert name in g.terms, name
assert g.replacements["carousel"] == "Karussell"
assert g.replacements["mootkowe"] == "Mutkurve"   # the workspace layer still applies
print("migration OK")
PY
```
Expected: `migration OK`, with a term and replacement count matching what the event had before this branch. Paste the real output.

- [ ] **Step 2: Update `CLAUDE.md`**

In the glossary section, replace the paragraph that describes the built-in default and the accepted `carousel` false positive with:

```markdown
**Shipped vocabulary is scoped to a circuit, not global.** `tracks.py` holds
one pack per venue (its corner names as `terms`, its measured mis-hearings as
`replacements`), and an event names its venue with a `track` key in its own
`glossary.json`. The layer order is: built-in default (EMPTY) -> the event's
track pack -> workspace -> channel -> event.

Two reasons, both measured, and both of which a future change would undo by
putting vocabulary back into the always-on default:

- **A track-specific rule fires on the wrong track otherwise.** `carousel ->
  Karussell` is the most frequent correction the Nordschleife pack carries,
  and Road America, Sears Point and Watkins Glen each have a Carousel of their
  own. As an always-on default this rewrote all of them.
- **The hotword prompt has a hard budget.** faster-whisper truncates it at 224
  tokens. The Nordschleife's 32 names alone are 164; adding Spa and Monza
  reaches 239. A global list of every venue would be silently cut to a
  fraction of itself, and because `merge_glossaries` emits terms
  most-specific-first, what survives is the operator's own names rather than
  the shipped ones - which is the right way round, but only helps while the
  shipped half stays small.

The Nürburgring is deliberately TWO packs. Its GP circuit and the Nordschleife
share no corners, and together they are 249 tokens - over the limit before an
operator adds a single name of their own.

A pack is REFERENCED by an event, never copied into it: correcting a name in
`tracks.py` corrects every event at that venue with no migration. Only an
EVENT may select a track; the same key at workspace or channel scope is a
reported defect rather than a silent no-op, because an operator who writes it
at the wrong level would otherwise find out three hours into a transcript.

`tracks.py` is pure and stdlib-only, like `glossary.py` - it is data plus
lookups, and its only dependency is the layer format it hands its data to.
Every pack is validated at import through `glossary.parse_layer`, so shipped
data that `profile.load` would refuse fails the first import instead.
```

- [ ] **Step 3: Update `README.md`**

Find the glossary passages (search for `glossary.json`) and add, in README's voice, that an event may name its circuit with `"track": "<id>"`, that the circuit's corner names then apply to that event only, and that the full list of ids is in the studio's circuit selector or `src/yt_shorts/tracks.py`. Keep it to a short paragraph — README documents the workflow, not the registry.

- [ ] **Step 4: Extend the E2E**

In `tests/test_studio_e2e.py`'s `TestGlossaryEditor`, replace the adopt-default step (step 4, which no longer exists) with a track-selection step:

```python
        # 4. Event scope: pick a circuit, save, and assert BOTH that the file
        #    records it and that the pack's rows arrive as inherited - then
        #    save an unrelated row and assert the circuit SURVIVES, which is
        #    the round-trip this feature can most easily lose.
        page.goto(f"{live_server}/erf/{event_dir.name}")
        page.get_by_role("button", name="Event glossary").click()
        drawer = page.get_by_role("dialog")
        drawer.wait_for()
        drawer.get_by_label("Circuit").click()
        page.get_by_role("option", name="Autodromo Nazionale Monza").click()
        drawer.get_by_role("button", name="Save").click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")

        written = json.loads((event_dir / "glossary.json").read_text(encoding="utf-8"))
        assert written["track"] == "monza"
        drawer.get_by_text("Lesmo", exact=True).wait_for()

        drawer.get_by_label("New term").fill("Boxengasse")
        drawer.get_by_role("button", name="Add term").click()
        drawer.get_by_role("button", name="Save").click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")
        after = json.loads((event_dir / "glossary.json").read_text(encoding="utf-8"))
        assert after["track"] == "monza", "an unrelated save cleared the circuit"
        assert after["terms"]["Boxengasse"] is True
```

If a selector does not match the rendered DOM, fix the SELECTOR — never weaken an assertion. Report every selector you changed and why.

- [ ] **Step 5: Build and run every gate**

```bash
cd src/yt_shorts/studio/web && npm run lint && npm run build && npm test && npx tsc -b && cd -
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: oxlint clean, build exit 0, Vitest all pass, tsc exit 0, pytest all pass, `All checks passed!`. Paste the REAL output of each.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(tracks): document circuit packs; e2e; rebuild static"
```

- [ ] **Step 7: Operator smoke (not the implementer's job)**

Restart `bin/yt-shorts studio`, open `erfofficial/N24-2026` → Event glossary, confirm the circuit reads "Nürburgring Nordschleife" and the corner rows show as `track`. Then confirm a different event with no circuit selected shows no corner rows at all.

---

## Self-Review

**Spec coverage.** Registry in code with one pack per venue → Task 1 (+ Nürburgring in Task 3). `track` key on the event's own `glossary.json` → Task 2 (parsing) and Task 4 (admin round trip). Reference-not-copy → Task 3's `_load_glossary`, which looks the pack up per load. Empty built-in default and the Nordschleife's move → Task 3. Layer order → Task 3 and Task 4's `_layers`. Only-an-event scope rule → Task 3 (profile) and Task 4 (admin), enforced in both because each is reachable without the other. Unknown-id defect → Tasks 3, 4, 5. `GET /api/tracks` → Task 5. Selector, fifth source label, adopt removal → Tasks 6 and 7. Data scope, real circuits vs GT originals → Task 1's registry. Migration of `erfofficial/N24-2026` → Task 8 Step 1, with an assertion rather than a hope. Budget assertion → Task 1's `TestHotwordBudget`. The `track` round-trip data-loss risk the spec calls out → Task 4's `test_a_saved_row_edit_PRESERVES_the_track` and Task 8's E2E step.

The spec's "replacements start empty unless observed" rule is implemented by the data itself: only `nurburgring-nordschleife` ships replacements, because it is the only venue with a transcript behind it.

**Type consistency.** `TrackPack(track_id, name, terms, replacements)` is used identically in Tasks 1, 3, 4 and 5. `tracks.get -> TrackPack | None`, `tracks.as_layer -> GlossaryLayer`, `tracks.listing -> list[dict]` match every call site. `GlossaryLayer.track: str | None` (Task 2) is what `_own_shape`, `_layers`, `update` and `_load_glossary` all read. The wire field is `track` in `GlossaryBody`, in `read`'s returned dict, and in `GlossaryLayers` — one name throughout. `LayerSource` gains `'track'` in `api.ts` and `sourceLabel` handles it, matching `glossary_admin.SOURCES`.

**Placeholder scan.** No TBD/TODO. Every code step carries the code, every test step the test, every command its expected output. The one judgement call left to an implementer — trimming a pack that fails the budget assertion — is bounded by an explicit instruction not to raise the budget instead.
