# Additive moments lexicon (weighted, four layers, UI-editable) — design

Date: 2026-07-25
Status: approved (design), ready for implementation plan

## Motivation

`moments.json` is the channel lexicon that marks excitement for moment
detection (`lexicon.py` + `moments.find_candidates`). Today it has three
problems:

1. **No UI.** It can only be hand-edited on disk. The operator went looking for
   it in the studio and found nothing — the trigger for this work.
2. **It replaces instead of accumulating.** `profile._load_lexicon` copies the
   glossary's rule: an event's own `moments.json` replaces the channel's
   WHOLESALE. There is no way to keep a shared list and add to it per channel or
   per event, which is exactly what the operator wants.
3. **The shipped list is nearly useless on real material, and every marker
   counts the same.** Measured against a real 98-minute ERF qualifying
   transcript (5234 words, produced during this session):
   - the test fixture's ten markers (`crash`, `safety car`, `spin`, …) score
     **3 hits in 98 minutes** — they are all incident words, and a clean
     qualifying session has no incidents;
   - the real commentary is about session structure instead: `pole` 19x,
     `super pole` 7x, `flying lap` 6x, `lap time`, `pole sitter`, `top seven`;
   - because a marker hit adds exactly 1.0 and the candidate threshold IS 1.0,
     any window containing one hit becomes a candidate on its own. `pole` is
     ambient vocabulary in a qualifying, so marking it would flag everything,
     while a genuine `crash` counts no more than a filler `big`.

   Candidate counts on that transcript: speech-rate alone 37, plus
   incidents+session 55, plus reaction vocabulary 61 (with `top_n = 20` chosen
   from them by loudness). So a broad list is affordable — the funnel stays
   healthy — but only if weights separate an event from ambient chatter.

## Decided requirements

- **Four layers, additive**, not override:
  1. a built-in racing default (code),
  2. a workspace-central list (new; edited in Settings),
  3. the channel's list,
  4. the event's list.
- **On collision the more specific layer wins**, and **weight `0` disables** an
  inherited marker — the escape hatch needs no separate mechanism.
- **Per-marker weights.** Format becomes `{"markers": {"crash": 3.0}}`; the old
  flat list `{"markers": ["crash"]}` stays valid and means weight 1.0.
- **A broad racing default including reaction vocabulary** (incidents + session
  highlights + reactions), shipped as a code constant so it is always present
  and improves with updates, PLUS an "adopt the default" button that copies it
  into the workspace list for operators who want to own and rework it.
- **`glossary.json` is untouched** — its wholesale replacement is deliberate and
  documented; only the lexicon becomes additive.

## Architecture

### Data format and the `Lexicon` type

`lexicon.Lexicon.markers` becomes `dict[str, float]` (marker → weight);
`EMPTY = Lexicon(markers={})`. `lexicon.load` accepts BOTH shapes:

- `{"markers": {"crash": 3.0, "pole": 0.3}}` — the new form;
- `{"markers": ["crash", "contact"]}` — the old form, every weight 1.0.

Validation (a defect is reported, never raised — see "Error handling"):
- a marker is a non-empty string; matching is case-insensitive, as today, so
  markers are normalised to lower case on load and a duplicate after
  normalisation is a defect;
- a weight is a finite number with `0 <= weight <= 10`. A negative weight is
  refused: "suppress this window" is not a designed feature and would let a
  lexicon cancel the speech-rate signal. The upper bound stops a typo'd `300`
  from swamping every other signal.

### Layering — `profile._load_lexicon`

Changes from "event replaces channel wholesale" to a union over four sources,
merged least- to most-specific so the most specific weight wins:

```
DEFAULT_MARKERS (lexicon.py)
  <workspace>/moments.json
    channels/<channel>/moments.json
      channels/<channel>/events/<event>/moments.json
```

A marker whose winning weight is `0` is **dropped from the effective lexicon**,
so scoring never sees it. It stays visible in the raw per-layer data, which is
what lets the UI show it struck through.

`config["lexicon"]` stays a `Lexicon` and everything downstream is unchanged.
`workspace.py` gains a resolver for `<workspace>/moments.json`, mirroring
`logs_dir`.

### Scoring — `moments._count_markers`

Today it returns the number of marker occurrences in a window. It becomes the
**weighted sum** `Σ weight × occurrences`, and `find_candidates`'s signature
does not change. Effect at the default threshold of 1.0: one `crash` (3.0)
crosses it alone; `pole` (0.3) needs four mentions inside the same 6-second
window. That is precisely the separation that is missing today.

**This changes detection output.** Clips already written stay as they are, but
re-running detection over the same stream yields a different candidate set than
the 20 currently in `N24-2026`. Intended, and called out here because it
matters before curating those 20.

### The built-in default

A `DEFAULT_MARKERS: dict[str, float]` constant in `lexicon.py`, in three bands
(the implementation plan carries the full list; this is its shape and the
reasoning):

- **Incidents, 2.0–3.0** — unambiguous events: `crash` 3.0, `into the wall` 3.0,
  `safety car` 2.5, `red flag` 2.5, `spin` 2.5, `off the track` 2.0,
  `contact` 2.0, `puncture` 2.0, `damage` 2.0, `debris` 2.0, `yellow flag` 1.5,
  `incident` 1.5.
- **Session highlights, 0.3–2.5** — what makes a clean session worth clipping:
  `photo finish` 2.5, `purple` 2.0, `fastest lap` 2.0, `new record` 2.0,
  `overtake` 2.0, `side by side` 2.0, `personal best` 1.5,
  `provisional pole` 1.5, `fastest` 1.0, `flying lap` 1.0, `super pole` 1.0,
  `pole sitter` 0.5, `pole` 0.3 (ambient — measured 19x in 98 minutes).
- **Reactions, 0.3–1.5** — how commentators mark a moment: `oh my` 1.5,
  `oh no` 1.5, `unbelievable` 1.5, `incredible` 1.2, `what a` 1.2,
  `look at that` 1.2, `wow` 1.0, `here we go` 0.8, `huge` 0.8, `massive` 0.8,
  `brilliant` 0.5, `fantastic` 0.5, `come on` 0.5, `big` 0.3 (ambient).

The default is English because the commentary is; a channel adds its own
language by adding markers at its own layer.

### Backend — admin module and routes

`lexicon_admin.py`, pure (no FastAPI, like `brand_admin`/`event_brand_admin`/
`pathnames`), reads and validate-updates the three editable layers and computes
the merge with provenance:

- `read(scope) -> {"own": {marker: weight}, "effective": {marker: {"weight":
  float, "source": "default"|"workspace"|"channel"|"event"}}}`
- `update(scope, markers)` — replaces that layer's own entries after validating
  exactly as `lexicon.load` does, so a list this accepts is one `profile.load`
  accepts (the same "accepted ⇒ loadable" invariant `brand_admin` keeps).
- `adopt_default(workspace)` — writes `DEFAULT_MARKERS` into
  `<workspace>/moments.json` as own entries. The default layer still applies
  underneath afterwards; identical values, own entries win, so the operation is
  idempotent and harmless to repeat.

Routes, thin over that module, mapping its `*AdminError.kind` to 400/404/409 the
way the other admin routes do:

- `GET`/`PUT /api/moments` — the workspace-central list
- `GET`/`PUT /api/channels/{channel}/moments`
- `GET`/`PUT /api/channels/{channel}/events/{event}/moments`
- `POST /api/moments/adopt-default`

Every `{channel}`/`{event}` segment goes through `pathnames.validate_segment`
before any filesystem touch, like every other write op, and the routes are
registered before the SPA fallback.

### Frontend

One editor component used at three scopes (the `BrandEditor` /
`EventBrandEditor` pattern): Settings for the central list, the channel screen
for the channel list, a drawer in the event editor for the event list. Each row
is a marker plus a weight; inherited rows render greyed with their source layer
named, editing one creates an own entry at this scope, and weight `0` renders
struck through as "disabled". Settings also carries the "Adopt the default"
button with a confirmation, since it writes ~40 entries.

Pure logic (parsing a weight input, the merge/provenance shaping for display,
sorting by band or weight) lives in its own non-component `.ts` module with
Vitest tests, so Vite's fast-refresh boundary stays component-only.

**Scrolling is a mandatory acceptance criterion** — the list can be long, so
every pane owns its own scroll container and is verified at a short viewport,
per the standing rule after repeated regressions.

### Error handling

Unchanged in kind: a malformed `moments.json` at any layer is collected as a
profile defect string (`_load_lexicon` already returns problems rather than
raising), so `profile.load` reports every defect together. A missing file at
every layer is not an error — detection simply runs on the default plus the
speech-rate signal. The admin module refuses an invalid update with a typed
error the route maps to 400, so a bad edit never reaches disk.

## Testing

- **`lexicon.load`:** the dict form; the old list form (weights 1.0); a
  duplicate after lower-casing; a non-string marker; a non-numeric, negative,
  non-finite or out-of-range weight; a missing file → `EMPTY`.
- **Merge:** all four layers combine; a more specific layer wins a collision;
  weight `0` drops a marker from the effective set while remaining in the raw
  layer; absent layers are skipped; the default alone is enough.
- **Scoring:** `_count_markers` returns the weighted sum; a single high-weight
  marker crosses the threshold alone while a low-weight one needs several hits;
  a marker with weight 0 contributes nothing. Existing scoring tests in
  `tests/test_moments.py` / `tests/test_detect.py` are updated to the weighted
  model — deliberately, not worked around.
- **`lexicon_admin`:** an accepted update is one `profile.load` accepts;
  provenance is correct per layer; `adopt_default` is idempotent; a bad payload
  is refused.
- **Studio API:** `GET` returns own + effective with provenance at each of the
  three scopes; `PUT` writes only its own layer; the segment guard refuses a bad
  `{channel}`/`{event}`; `POST …/adopt-default` writes the workspace list.
- **Vitest:** the pure weight-parsing and provenance-shaping helpers.
- **Playwright E2E:** edit the central list in Settings and assert
  `<workspace>/moments.json` on disk; add a channel-level marker and assert the
  event editor shows it as inherited; set a weight to 0 and assert it renders
  disabled; adopt the default and assert the file gains the entries.
- **The fixture:** `tests/fixtures/channels/erf/moments.json` is updated to the
  new format. It does not affect the pinned overlay hashes (the lexicon plays no
  part in rendering), but it does affect detection tests — the plan states which.
- Full pytest suite green, `npm test` green, `python3 tools/lint.py` green,
  `npm run build` committed (`static/`).

## Out of scope (explicitly)

- **The glossary gap this investigation surfaced.** The real transcript contains
  mis-transcribed Nürburgring corner names — `a-geigenkop`, `schwanz-and-galbenkop`,
  `at-kessichen` (Galgenkopf, Schwalbenschwanz, Kesselchen). Those belong in
  `glossary.json`, which corrects proper nouns Whisper does not know, and would
  raise transcription quality across the board. A separate, worthwhile follow-up;
  the lexicon marks excitement, the glossary fixes names, and the two stay apart.
- Re-tuning `threshold`, `min_gap`, `top_n`, `preroll`/`postroll` or the loudness
  ranking — this feature changes what a marker is worth, not the detector's other
  knobs.
- Automatic marker suggestion from a transcript (mining frequent terms).
- Any change to `glossary.json`'s wholesale layering.
- Per-language default lists.

## Notable risks / decisions carried forward

- **The scoring change alters detection output**, so a re-run over an
  already-detected stream produces a different candidate set. Existing written
  clips are untouched. Flagged before curating the 20 candidates now in
  `N24-2026`.
- **Weights are the whole point** — measured evidence shows an unweighted broad
  list would make every `pole` mention a candidate. If the weighted model is
  ever simplified back to counting, this failure returns.
- **The default lives in code**, so it is invisible on disk until adopted. The
  UI must therefore show inherited rows clearly, or an operator will not
  understand where a marker came from; the "adopt" button is the escape hatch
  for anyone who prefers owning the list outright.
- **Additive layering diverges from the glossary's wholesale rule** on purpose.
  The two files sit next to each other and now behave differently, which is a
  documentation obligation in `CLAUDE.md`, not just a code change.
