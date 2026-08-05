# Track packs: per-circuit glossary vocabulary, selected per event — design

Date: 2026-07-25
Status: approved (design), ready for implementation plan

## Motivation

The glossary feature shipped a built-in default containing the Nürburgring
Nordschleife's section names and the ten mis-hearings measured on a real ERF
qualifying transcript. That default is **always on, for every channel and every
clip**. Two problems follow, and both are measured rather than hypothetical.

**1. A track-specific rule fires on the wrong track.** `carousel → Karussell`
is the most frequent correction in the shipped set (5 occurrences in one
session), and Road America, Sears Point and Watkins Glen all have a corner
called the Carousel. The glossary feature's own spec recorded this as an
accepted false positive because there was no mechanism to scope it. There is
now an obvious one: the track.

**2. A global list of every track's corner names is physically impossible.**
faster-whisper truncates the hotword prompt once it reaches 224 tokens, cutting
it to 223 (`faster_whisper/transcribe.py`, around line 1545). Measured with the
cached `faster-whisper-small` tokenizer:

| terms | tokens |
|---|---|
| Nordschleife alone (32) | 164 |
| + ERF's two team names | 172 |
| Nordschleife + Spa (13) + Monza (7) | **239 → truncated** |

Three circuits exceed the limit. Gran Turismo 7 lists 41 locations. A global
list would be silently cut to roughly a fifth of itself — and the final review
of the glossary feature already found and fixed the ordering half of exactly
this failure, where truncation discarded the operator's own terms first.

Scoping the vocabulary to the track an event actually runs at solves both. One
pack per event fits comfortably: the largest (Nordschleife, 164 tokens) still
leaves ~50 tokens for the operator's own team names; Spa costs 50, Mount
Panorama 47, Suzuka 36, Laguna Seca 19.

## Decided requirements

- **A registry of track packs in code**, one per venue, each carrying that
  venue's `terms` (decoder bias) and `replacements` (corrections).
- **An event selects exactly one track**, by id, in its own `glossary.json`.
  No channel-level default and no multi-track events — deliberately the
  simplest thing that works (see "Rejected alternatives").
- **The pack is a reference, not a copy.** Improving a name in the registry
  improves every event at that track, with no per-event migration.
- **The built-in default becomes empty**, and the Nordschleife entries move
  into the `nurburgring-nordschleife` pack. This is what delivers requirement 1: on a
  Watkins Glen event the `carousel` rule is simply not active.
- **Packs cover all 41 GT7 locations.** Venues with no officially named
  corners carry their own track and section names instead (see "Data scope").

## Architecture

### The registry — `tracks.py`

A new module, pure and stdlib-only, with the same constraints `glossary.py`
carries: no FastAPI, no google, no file access. It exposes:

```python
@dataclass(frozen=True)
class TrackPack:
    track_id: str          # "nurburgring-nordschleife", "spa-francorchamps", …
    name: str              # "Nürburgring Nordschleife", "Circuit de Spa-Francorchamps"
    terms: tuple[str, ...]
    replacements: dict[str, str]

PACKS: dict[str, TrackPack]              # track_id -> pack
def get(track_id: str) -> TrackPack | None
def as_layer(pack: TrackPack) -> GlossaryLayer   # via glossary.parse_layer
def listing() -> list[dict]              # id + name, for the studio's selector
```

`as_layer` routes through `glossary.parse_layer`, so a pack is validated by
exactly the rules a hand-written `glossary.json` is validated by — the same
"what this accepts, `profile.load` accepts" invariant every other layer keeps.
Building `PACKS` at import time means a duplicate key or a control character in
the shipped data fails the first import rather than an operator's next run,
mirroring how `DEFAULT_LAYER` is built today.

**One pack per VENUE, not per layout.** GT7's 121 layouts collapse to 41
locations, and corner names belong to the venue: Monza and Monza No Chicane
share one pack, as do Nürburgring 24h, Endurance and Nordschleife. The registry
keys on the venue.

**With one measured exception: the Nürburgring is two packs.** The GP circuit's
corners (Mercedes-Arena, Yokohama-S, Michael-Schumacher-S, Veedol-Schikane …)
have nothing to do with the Nordschleife's, and the combined set does not fit:

| | terms | tokens |
|---|---|---|
| `nurburgring-nordschleife` | 32 | 164 |
| `nurburgring-gp` | 12 | 88 |
| both together | 44 | **249 — over the 224 limit before a single operator term** |

So they are separate packs, which is also why the registry ends up with 41
entries rather than 40. A 24h or Endurance broadcast selects
`nurburgring-nordschleife`, where the commentary actually lives; a GP-circuit
event selects `nurburgring-gp`. An event that genuinely needs a handful from
the other set adds them to its own layer.

### Selecting a track — the `track` key

An event declares its track in its own `glossary.json`:

```jsonc
{
  "track": "nurburgring-nordschleife",
  "terms": {"Rei Racing": true},
  "replacements": {"mootkowe": "Mutkurve"}
}
```

The track is a glossary decision — it selects vocabulary — so it lives in the
glossary file and saves through the glossary route. `GlossaryLayer` gains
`track: str | None`, parsed and validated by `parse_layer`.

**Only an event may set it.** A `track` key in the workspace or the channel
file is reported as a defect ("only an event selects a track"), never silently
ignored — an operator who writes it at the wrong level must find out from the
error, not from a missing correction three hours into a transcript. An unknown
track id is likewise a reported defect naming the valid ids.

### Layering

```
(empty built-in default)
  track pack, selected by the event
    <workspace>/glossary.json
      channels/<channel>/glossary.json
        channels/<channel>/events/<event>/glossary.json
```

Shipped data first, the operator's own layers above it, most specific winning
per entry — unchanged in kind from today, with the pack inserted where the
Nordschleife default used to sit. `merge_glossaries` needs no change beyond
receiving one more layer; in particular its most-specific-first term ordering
(which exists so hotword truncation sacrifices shipped data before the
operator's own) already puts the pack behind the operator's entries.

`profile._load_glossary` gains the step: parse the event layer, read its
`track`, look the pack up, and insert it as the second layer. A missing pack
for a declared id is a problem string, not a raise — one more instance of the
degrade-and-report contract every layer already follows.

### What happens to the built-in default

`glossary.DEFAULT_TERMS` and `DEFAULT_REPLACEMENTS` move wholesale into the
`nurburgring-nordschleife` pack, and `DEFAULT_LAYER` becomes `EMPTY_LAYER`. There is no
proper noun that is correct on every circuit, so an always-on default has
nothing left to hold. A track-independent default (the game's own vocabulary —
"Gran Turismo", "Balance of Performance") is deliberately NOT added: no
measurement supports it yet, and this design exists because unmeasured
always-on vocabulary is expensive.

Two consequences the implementation must handle:

- **`erfofficial/N24-2026` gets `"track": "nurburgring-nordschleife"`** written into its
  `glossary.json`, so the operator's live event behaves exactly as it does
  today. Without it, that event silently loses every corner correction.
- **"Adopt the built-in default" is removed** — the Settings button, the
  `POST /api/glossary/adopt-default` route, `glossary_admin.adopt_default` and
  its tests. With an empty default it would adopt nothing, and a button that
  does nothing is worse than an absent one. Per-row Override and Disable
  already let an operator take ownership of any pack entry. If bulk-owning a
  whole pack turns out to be wanted, that is a later, separate addition with
  its own evidence.

### Studio

`GET /api/tracks` returns the registry listing (id and name), so the editor can
populate a selector without hardcoding the venue names in the frontend.

The event glossary drawer gains a **track selector** above the two lists: every
venue in the registry plus an explicit "no track". Choosing one writes `track` into the
event's own layer through the existing PUT — so `GlossaryBody` gains an
optional `track` field, and `glossary_admin`'s `own` shape and `update` must
carry it through rather than dropping it on the next save. That round-trip is
the single most likely place for this feature to lose data, and it needs a test
that asserts a save of an unrelated row preserves the track.

Pack entries render as inherited rows with source **`track`** — a fifth value
in the source union, alongside `default`, `workspace`, `channel`, `event`, and
labelled with the venue's name so the operator sees *which* track a row came
from. They are overridable and disableable exactly like any other inherited
row.

### Error handling

Unchanged in kind. A `track` at the wrong layer, an unknown id, and a malformed
pack are all collected as problem strings by `profile.load` and surfaced by
`glossary_admin.read`'s `problems`; nothing raises past the caller, and a
defect in one layer never prevents the others from loading. An event with no
`track` is not an error — it simply gets no pack, which is the correct
behaviour for a venue nobody has written a pack for yet.

## Data scope

The official list counts **41 locations across 121 layouts**. This sentence
originally also claimed the Nürburgring's GP and Nordschleife variants were
"one venue sharing one set of names" while the registry split them in two -
a contradiction the final review caught, resolved here against the source
(read December 2025).

The official list names **40 distinct places**; it reaches 41 by counting
Sardegna twice (Road Track and Windmills are separate entries) while treating
the whole Nürburgring complex as one. The registry does the opposite: the
Nürburgring is **two** packs, because the GP circuit and the Nordschleife
share no corners and together exceed the hotword budget (see "The registry"),
and Sardegna is **one**, because its two areas are one venue's vocabulary.
Both land on **41 packs**, by different arithmetic, and every place on the
official list has one.
Roughly half are real circuits with well-documented corner names; the rest are
Polyphony's own designs, which have **no officially named corners**.

**Real circuits (~21)** — 24 Heures du Mans, Monza, Interlagos, Autopolis,
Brands Hatch, Circuit Gilles-Villeneuve, Barcelona-Catalunya, Spa-Francorchamps,
Daytona, Fuji, Goodwood, Road Atlanta, Mount Panorama, Nürburgring, Red Bull
Ring, Suzuka, Tsukuba, Watkins Glen, Laguna Seca, Willow Springs, Yas Marina.
These get their real corner names (Eau Rouge, Parabolica, Forrest's Elbow, The
Corkscrew, 130R, …).

**GT originals (~20)** — Alsace, Autodrome Lago Maggiore, Blue Moon Bay, BB
Raceway, Circuit de Sainte-Croix, Colorado Springs, Deep Forest, Dragon Trail,
Eiger Nordwand, Fishermans Ranch, Grand Valley, High Speed Ring, Kyoto Driving
Park, Lake Louise, Northern Isle, Sardegna, Special Stage Route X, Tokyo
Expressway, Trial Mountain. Their packs carry the **venue and section names**
instead: "Yamagiwa", "Miyabi", "Seaside", "Gardens", "Windmills", "Nordwand",
"Sainte-Croix". This is not a consolation prize — a commentator says these
constantly and Whisper mangles every one of them.

The replacement half of each pack starts **empty except where a mis-hearing has
actually been observed**. The Nordschleife's ten replacements exist because
they were measured on a real transcript; inventing plausible mis-hearings for
40 other venues would ship exactly the kind of unmeasured rule this design
exists to scope. Operators add them per venue as they meet them, and the
registry grows from evidence.

Track data is sourced from the official GT7 track list
(`https://www.gran-turismo.com/gb/gt7/tracklist/`, 41 locations / 121 layouts
as of December 2025) for the venue set, and from each circuit's own published
material for corner names.

## Testing

- **`tracks.py`:** every pack parses through `glossary.parse_layer` at import;
  no duplicate ids; no pack's terms exceed a documented token budget (asserted
  against `glossary.HOTWORD_BUDGET_CHARS`, so a future oversized pack fails the
  suite rather than an operator's transcript); `get` on an unknown id returns
  None; `listing` covers every pack.
- **Layering:** a pack is inserted for a declared track; the operator's layers
  win over it; a pack entry can be disabled at workspace, channel or event
  scope; an event with no track gets no pack; an unknown id is a problem
  string, not a raise; a `track` at workspace or channel scope is a problem
  string.
- **Ordering:** with a pack and an own term, the own term still precedes the
  pack's in `Glossary.terms` — the truncation-survival property.
- **Round trip:** `glossary_admin.read` reports the track; `update` preserves
  it when saving unrelated rows; a save that changes only a term does not clear
  the track.
- **Studio API:** `GET /api/tracks` lists the registry; PUT with a `track`
  writes it; PUT with an unknown id is a 400; the event routes surface pack
  rows with source `track`.
- **Migration:** `erfofficial/N24-2026` with `track: nurburgring-nordschleife` produces
  exactly the glossary it has today — asserted by comparing the merged result
  against the pre-change `DEFAULT_LAYER` contents.
- **Frontend:** Vitest for the selector's pure logic and the fifth source
  label; the Playwright E2E picks a track at event scope and asserts the file
  on disk plus the pack rows appearing as inherited.
- Existing tests that assert the Nordschleife lives in the built-in default
  (`test_glossary_layers.py`, `test_stream_transcribe.py`'s real-chain test,
  `test_profile.py`, the E2E's adopt scenario) move to the pack or are removed
  with the adopt feature. The plan names each.
- Full pytest, `npm test`, `python3 tools/lint.py`, `npm run build` committed.

## Rejected alternatives

- **A channel-level track default with event override.** Offered and declined:
  ERF races one venue, but the saving is one line per event, and inheritance
  adds a resolution rule to explain and test for no measured benefit.
- **Multiple packs per event.** The hotword budget caps it at two or three
  before silent truncation, so the feature would carry a limit that is
  invisible until it bites. An event that genuinely spans venues can add the
  second venue's handful of names to its own layer.
- **Copying a pack into the event layer instead of referencing it.** Copies go
  stale: a corrected corner name would reach only events created afterwards.
- **Shipping the packs as a JSON data file rather than code.** Code keeps the
  import-time validation and matches how `DEFAULT_MARKERS` and `DEFAULT_TERMS`
  already ship. A data file becomes worth it only if non-developers maintain
  the registry.

## Notable risks / decisions carried forward

- **The migration is silent if missed.** An event that does not declare a track
  loses every correction it used to get from the built-in default, with no
  error — the profile is still valid. The one existing event must be migrated
  in the same change, and the plan must treat that as a deliverable rather than
  a follow-up.
- **`track` round-tripping through the PUT is the data-loss risk.** The editor
  overwrites the whole own layer on every save; a `track` dropped anywhere in
  read → row → payload → write disappears on the next unrelated edit. This is
  the same class of failure that bit the glossary feature twice (the client
  normalisation divergence, the worker payload), and it gets an explicit test.
- **A fifth source value touches the frontend union in several places.** The
  `LayerSource` type, `sourceLabel`, and the row badges all enumerate four
  values today; TypeScript will catch the type sites but not a missed label.
- **Corner-name accuracy is research, not code.** 40 venues of names collected
  from public sources will contain mistakes. Every pack should be reviewable in
  one place, and a wrong name costs a wrong bias, not a crash — but the plan
  should not pretend this part is mechanical.
