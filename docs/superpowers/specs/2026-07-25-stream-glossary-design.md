# Global additive glossary, applied to stream transcription — design

Date: 2026-07-25
Status: approved (design), ready for implementation plan

## Motivation

The moments lexicon marks excitement; the glossary fixes names. Feature 2 built
the lexicon. This is the glossary, and it starts from a measurement rather than
an intuition: the real 98-minute ERF qualifying transcript
(`~/YT-Shorts-Data/streams/V9nVNEQNdR4/transcript.json`, 5234 words) gets the
Nürburgring's corner names wrong in a way that damages both halves of the
pipeline — a hook quoting "Schwab Schwanz" is unusable, and a lexicon marker can
never match a corner Whisper never spelled.

What the transcript actually contains:

| decoded | occurrences | correct |
|---|---|---|
| `Schwab Schwanz`, `Shriver Schwanz` | 2 | Schwalbenschwanz |
| `carousel` | 5 | Karussell |
| `Kleine carousel`, `Kleinica or sell` | 2 | Kleines Karussell |
| `galgen cop`, `Galbenkopf`, `Geigenkop` | 3 | Galgenkopf |
| `Kessichen` | 1 | Kesselchen |
| `boyacht` | 1 | Hohe Acht |

Already correct and needing nothing: Brünnchen (3×), Döttinger (2×),
Schwedenkreuz, Bergwerk.

Three separate problems stand between that measurement and a fix:

1. **The stream path never passes a glossary at all.** `_decode_worker.py`
   accepts `argv[3]` as a glossary JSON path, but `subprocess_decoder` invokes
   it with only `(wav, model_name)`. Every stream chunk ever decoded in this
   project was decoded with `Glossary(terms=[], replacements={})`, and nothing
   corrects the assembled words afterwards either. ERF's existing
   `channels/erf/glossary.json` has therefore never affected a stream
   transcript — only clip transcription (`transcribe.transcribe`) honours it.
2. **The glossary has no global layer.** `profile._load_glossary` resolves
   exactly two layers, channel and event, with no workspace file and no
   built-in default.
3. **It replaces wholesale.** An event's `glossary.json` replaces the
   channel's entirely. Corner names are not event-specific — they are facts
   about a racetrack — so under the current rule any channel or event that
   wants one correction of its own would silently drop every corner name.

Problem 3 is why the operator's requirement ("die Kurvennamen sind global für
alle wichtig, daher bitte allgemein ablegen") forces additive layering rather
than merely permitting it: ERF is the one channel that needs the corners AND
the one channel that already has a `glossary.json` of its own.

## Decided requirements

- **Four layers, additive** — the same shape the lexicon just gained:
  1. a built-in default in code (`glossary.DEFAULT_TERMS` /
     `DEFAULT_REPLACEMENTS`),
  2. `<workspace>/glossary.json` (new; edited in Settings),
  3. `channels/<channel>/glossary.json`,
  4. `channels/<channel>/events/<event>/glossary.json`.
- **The more specific layer wins per entry**, and a falsy value **disables** an
  inherited entry — the lexicon's weight-0 escape hatch, transposed.
- **Both file shapes accepted for `terms`**: the existing list
  (`["Karussell"]`, all enabled) and a map (`{"Karussell": false}`) that can
  disable.
- **The built-in default carries the Nordschleife corner set**, both as decoder
  bias (`terms`) and as corrections (`replacements`) for the mis-hearings
  measured above.
- **The glossary reaches stream transcription at BOTH ends** — hotword bias in
  the killable decode worker, and `glossary.apply` at assembly.
- **A UI editor at all three writable scopes**, mirroring `MomentsEditor`:
  Settings, a channel tab, an event drawer.

## Architecture

### Layer format and where parsing lives

Today a glossary file is parsed by `profile._parse_glossary`. The admin module
needs the identical validation (the "what this accepts, `profile.load` accepts"
invariant every admin module keeps), so parsing moves into `glossary.py` as
pure logic and `profile._parse_glossary` delegates to it — exactly the
`lexicon.normalise` / `lexicon.load` split.

`glossary.py` gains:

- `parse_layer(data) -> GlossaryLayer` — validates one file's already-parsed
  JSON, raising `ValueError` on a defect;
- `load(path) -> GlossaryLayer` — reads a file, `EMPTY_LAYER` when absent;
- `GlossaryLayer`, a dataclass of two maps:
  - `terms: dict[str, str | None]` — normalised key → the spelling to bias
    with, or `None` when this layer disables the term;
  - `replacements: dict[str, tuple[str, str] | None]` — normalised key →
    `(raw key, replacement)`, or `None` when this layer disables the rule.

Normalisation is what makes a merge possible: a term's key is its lower-cased
form, a replacement's key is its tokens run through the same `_normalized`
function `apply` already matches with, so `"Kessichen"` and `"kessichen"` are
one entry across layers instead of two. The **raw** key and spelling are kept
because they are what the operator sees and what biases the decoder; only
identity is normalised.

Accepted per section:

```jsonc
{
  "terms": ["Karussell", "Galgenkopf"],          // list form: all enabled
  "terms": {"Karussell": true, "carousel": false}, // map form: false disables
  "replacements": {
    "kessichen": "Kesselchen",                   // a correction
    "carousel": null                             // disables an inherited rule
  }
}
```

Validation defects (each reported, never raised past the caller — see "Error
handling"): a section of the wrong type; a non-string term; an empty term; a
duplicate term or replacement key after normalisation **within one layer**; a
replacement value that is neither a string nor `null`/`false`; a key or value
longer than 200 characters or containing a control character (the same cap the
lexicon's markers got).

`Glossary` itself — the runtime type `hotwords` and `apply` consume — does not
change. It stays `terms: list[str]` + `replacements: dict[str, str]`, and it is
what a merge produces.

### Merging — `profile.merge_glossaries`

A sibling of `merge_lexicons`, same contract: layers least specific first, the
last layer to mention an entry wins, a falsy winner drops the entry.

```
DEFAULT_TERMS / DEFAULT_REPLACEMENTS (glossary.py)
  <workspace>/glossary.json
    channels/<channel>/glossary.json
      channels/<channel>/events/<event>/glossary.json
```

`merge_glossaries(layers) -> Glossary`: fold the `terms` maps, fold the
`replacements` maps, then emit `Glossary(terms=[spelling for each surviving
term], replacements={raw key: replacement for each surviving rule})`. A
disabled entry is absent from the result, so neither the decoder bias nor the
corrections ever see it — and it remains visible in the raw layers, which is
what lets the editor strike it through.

`profile._load_glossary(event_dir, channel_dir, workspace_root)` gains the
workspace argument and the default layer, mirroring `_load_lexicon` including
its per-layer problem collection. `config["glossary"]` stays a `Glossary` and
every consumer is unchanged.

`workspace.py` gains `GLOSSARY_FILE = "glossary.json"` and
`glossary_path(root)`, which — like `moments_path` — creates nothing: an absent
file is the normal state.

**This retires the documented wholesale rule.** `_load_glossary`'s docstring
currently argues that merging two term lists "has no obviously correct result".
That argument is answered rather than ignored: the ambiguity it names ("add to
the channel's" vs "these and only these") is resolved by making *add* the rule
and giving *only these* an explicit spelling — disable what you do not want.
The docstring is rewritten to say so, and `CLAUDE.md`'s glossary/lexicon
divergence paragraph, written three commits ago, is corrected: after this
change the two files layer identically.

### The built-in default

`glossary.DEFAULT_TERMS: tuple[str, ...]` — the Nordschleife's corner and
section names, biasing the decoder before it errs: Nordschleife, Hatzenbach,
Hocheichen, Quiddelbacher Höhe, Flugplatz, Schwedenkreuz, Aremberg,
Fuchsröhre, Adenauer Forst, Metzgesfeld, Kallenhard, Wehrseifen, Ex-Mühle,
Bergwerk, Kesselchen, Klostertal, Steilstrecke, Karussell, Hohe Acht,
Wippermann, Eschbach, Brünnchen, Pflanzgarten, Schwalbenschwanz, Galgenkopf,
Döttinger Höhe, Antoniusbuche, Tiergarten, Hohenrain, Kleines Karussell,
Stefan-Bellof-S, Mutkurve.

`glossary.DEFAULT_REPLACEMENTS: dict[str, str]` — only the mis-hearings the
real transcript actually produced, longest key first being irrelevant here
because `_replacement_keys` already sorts by token count:

```
"schwab schwanz"    -> "Schwalbenschwanz"
"shriver schwanz"   -> "Schwalbenschwanz"
"kleine carousel"   -> "Kleines Karussell"
"kleinica or sell"  -> "Kleines Karussell"
"carousel"          -> "Karussell"
"galgen cop"        -> "Galgenkopf"
"galbenkopf"        -> "Galgenkopf"
"geigenkop"         -> "Galgenkopf"
"kessichen"         -> "Kesselchen"
"boyacht"           -> "Hohe Acht"
```

Nothing is invented: every key above was observed. `Kleine carousel` is listed
before `carousel` for readability only — `apply` already prefers the
two-token key over the one-token one at the same position, which is the
behaviour that keeps `Kleines Karussell` intact.

`glossary_admin.adopt_default` copies the default into the workspace layer
**additively**, byte-for-byte the fix `lexicon_admin.adopt_default` needed
after the final review: `{**DEFAULT, **own}`, own entries preserved and
winning, so adopting never changes what a transcript currently produces and is
idempotent.

### Reaching the stream decoder — the plumbing that is missing

Three signatures gain a glossary, each defaulting to `EMPTY` so no existing
caller or test stub changes meaning:

- `subprocess_decoder(audio_path, start, length, *, glossary=EMPTY, ...)` —
  writes the glossary as JSON into the same `TemporaryDirectory` as the wav and
  passes that path as `argv[3]`, the parameter `_decode_worker.main` has always
  accepted and never received. The worker is unchanged.
- `transcribe_stream(video_id, workspace_dir, *, glossary=EMPTY, ...)` — passes
  it to `decoder(...)` and applies it at assembly (below).
- `detect.detect_moments` — passes `config["glossary"]` into
  `transcriber(...)`. It already holds `config`, so nothing new is threaded
  through the studio's job runner or the CLI.

The injected-seam rule holds: the existing `decoder` and `transcriber` fakes in
`tests/test_stream_transcribe.py` and `tests/test_detect.py` must accept the
new keyword, which is deliberate — a stub that silently ignored the glossary
would let this whole feature regress unnoticed. The plan names each stub.

### Applying at assembly, and why the chunk cache stays raw

`transcribe_stream` applies `glossary.apply(words, glossary)` to the assembled
word list immediately before building `StreamTranscript` and writing
`transcript.json`. Per-chunk cache files keep the **raw** decode output.

That split is the design, not an accident:

- A glossary change takes effect on the next assembly, with no re-decode. The
  9 cached chunks of `V9nVNEQNdR4` — roughly 30 minutes of Whisper — stay
  valid, and the corner names still land.
- Corrections may span a chunk boundary, because the words of two adjacent
  chunks are adjacent in the assembled list. Applying per chunk before caching
  would lose exactly those matches.
- For streams this closes the wart `transcribe.py`'s docstring documents for
  clips ("Nothing here invalidates a cache because the glossary changed… a
  known limitation, not an oversight"). The clip path keeps that limitation;
  only the stream path escapes it, and for the stream path it is the expensive
  half that is cached.

The consequence is stated plainly: **chunks decoded before a glossary change
keep the old decoder bias.** The correction half still fixes their text, but a
term the decoder could have heard correctly with a hotword remains a guess.
Recovering the bias means deleting `streams/<video_id>/chunks/` and decoding
again. The chunk cache key stays `(video_id, start, length)`; fingerprinting
the glossary into it would invalidate hours of decode on every keystroke in the
editor, which is the wrong trade for a best-effort bias.

### Backend — admin module and routes

`glossary_admin.py`, pure (no FastAPI), a near-mirror of `lexicon_admin.py`:

- `GlossaryAdminError.kind ∈ {"bad_name", "not_found", "bad_glossary"}` →
  400/404/400.
- `read(root, *, channel=None, event=None) -> {"scope", "own", "effective",
  "problems"}`. `own` is the requested scope's own layer in file shape;
  `effective` maps each entry to its value **and its source layer**, and
  **keeps disabled entries** with the layer that disabled them — the same
  deliberate divergence from the scoring merge that `lexicon_admin.read`
  carries, and for the same reason: an editor must show that a channel
  disabled `carousel`, struck through rather than absent. `problems` names any
  layer that failed to load, so one malformed file degrades that layer to empty
  instead of 500ing the route (the bug the lexicon's final review found).
- `update(root, terms, replacements, *, channel=None, event=None)` — overwrites
  that one layer after validating through `glossary.parse_layer`, so a bad
  payload never reaches disk.
- `adopt_default(root)` — additive, as above.

Routes, thin over that module, registered before the SPA fallback, every
`{channel}`/`{event}` segment through `pathnames.validate_segment` before any
filesystem touch:

- `GET`/`PUT /api/glossary`
- `GET`/`PUT /api/channels/{channel}/glossary`
- `GET`/`PUT /api/channels/{channel}/events/{event}/glossary`
- `POST /api/glossary/adopt-default`

### Frontend

One `GlossaryEditor` component mounted at three scopes exactly as
`MomentsEditor` is (a Card in `SettingsScreen`, a `Tabs.Tab` in
`ChannelScreen`, a `Drawer` in `App.tsx`), and built to the same
load/dirty/save shape. It differs from the moments editor in one way: two
sub-lists rather than one — **Terms** (decoder bias, a name and an
enabled/disabled state) and **Corrections** (what Whisper hears → what it
should say). Both carry per-entry ownership: an inherited row is read-only
until Override or Disable creates an own entry, and a disabled row renders
struck through with a badge naming the layer that disabled it.

Pure logic lives in `glossaryLayers.ts` (row shaping, provenance, ownership
mutations, the payload builder) with Vitest tests, keeping Vite's fast-refresh
boundary component-only — the same split `momentsLexicon.ts` has.

**Scrolling is a mandatory acceptance criterion.** The default alone is ~32
terms and 10 corrections, so the component owns its own fixed-height flex
column with the two lists as the scrolling region (`flex: 1 1 auto;
minHeight: 0; overflowY: auto`), verified at a short viewport, exactly as
`MomentsEditor` does after the standing rule.

### Error handling

Unchanged in kind. A malformed `glossary.json` at any layer is collected as a
profile defect string by `_load_glossary` and reported with every other defect
in one `ProfileError`; the admin module degrades that layer to empty and names
it in `problems`; every layer being absent is not an error — the built-in
default alone is a working glossary. A glossary failure never affects rendering,
and a stream whose glossary is broken still transcribes, uncorrected.

## Reprocessing `V9nVNEQNdR4`

Two stages, deliberately separated so the cheap one lands first:

1. **Assembly only (seconds).** With the default adopted, re-run detection on
   the stream. The 9 cached chunks are reused, `glossary.apply` corrects the
   assembled words, and `transcript.json` plus the hooks of the newly written
   moments carry the real corner names. This is the whole correction half.
2. **Full re-decode (~30 min), optional.** Delete
   `streams/V9nVNEQNdR4/chunks/` and run again to get the hotword bias too,
   which is also the run that can fill chunk 6 — lost to the documented 600 s
   per-chunk timeout on the last run.

Stage 1 is part of this feature's acceptance. Stage 2 is the operator's call
afterwards, and comparing the two transcripts is the honest measurement of what
the bias half is worth.

## Testing

- **`glossary.parse_layer`/`load`:** both `terms` shapes; `false` and `null`
  disabling; a duplicate after normalisation within one layer; wrong types;
  the length and control-character caps; a missing file → `EMPTY_LAYER`.
- **`merge_glossaries`:** all four layers combine; a more specific layer wins a
  term's spelling and a replacement's value; a falsy winner drops the entry
  from the produced `Glossary` while remaining in the raw layer; absent layers
  are skipped; the default alone produces a usable glossary.
- **`_load_glossary`:** four layers resolve; ERF's existing channel glossary
  now *adds* to the default instead of replacing it (the regression this
  feature exists to prevent); a malformed layer is a problem string, not a
  raise.
- **`subprocess_decoder`:** with a non-empty glossary the runner receives a
  fourth argv element, and the file at that path parses to the glossary's
  terms and replacements; with `EMPTY` the argv is unchanged (three elements),
  pinning that no existing behaviour shifted.
- **`transcribe_stream`:** the glossary reaches the decoder; the assembled
  transcript is corrected while the chunk cache files stay raw; a correction
  spanning a chunk boundary is applied; a cached run with a *changed* glossary
  produces newly corrected output without decoding anything.
- **`detect_moments`:** `config["glossary"]` reaches the transcriber; a moment's
  hook carries the corrected spelling.
- **`glossary_admin`:** an accepted update is one `profile.load` accepts;
  provenance is correct per layer; a disabled entry survives in `effective`
  with its source; `adopt_default` preserves own entries and is idempotent; a
  malformed layer yields `problems` rather than an exception.
- **Studio API:** `GET`/`PUT` at all three scopes; `PUT` writes only its own
  layer; the segment guard refuses a bad `{channel}`/`{event}`;
  `POST …/adopt-default` writes the workspace file.
- **Vitest:** the row-shaping, provenance and payload helpers in
  `glossaryLayers.ts`.
- **Playwright E2E:** add a correction in Settings and assert
  `<workspace>/glossary.json` on disk; assert it shows as inherited in the
  channel tab; disable an inherited term at event scope and assert the file
  records the disable and the row renders struck through; adopt the default and
  assert the workspace file gains the corner entries.
- **Fixture:** `tests/fixtures/channels/erf/glossary.json` stays as it is —
  its `very very → Rei Racing` rule is what proves additivity, since it must
  survive alongside the default rather than replacing it. Overlay hashes are
  untouched (the glossary plays no part in rendering); confirm by running
  `tests/test_event_layer_no_regression.py`.
- Full pytest suite green, `npm test` green, `python3 tools/lint.py` green,
  `npm run build` committed (`static/`).

## Out of scope (explicitly)

- **Per-track glossary packs.** The default is one list, always active. If a
  channel ever races another circuit, the right shape is a registry of adoptable
  named packs rather than a second always-on default; not built until there is
  a second track.
- **Invalidating the chunk cache on a glossary change** — argued against above.
- Any change to the clip path's cache behaviour (`transcribe.transcribe` keeps
  its documented limitation).
- Mining a transcript for glossary suggestions.
- Fuzzy or phonetic matching. `apply` stays exact-token matching; a new
  mis-hearing is a new key.
- Re-tuning detection knobs.

## Notable risks / decisions carried forward

- **`carousel → Karussell` is a global rule with a real false positive.** Road
  America, Sears Point and Watkins Glen all have a Carousel; an
  English-language broadcast of one would be rewritten to German. It is
  included because it is the single most frequent mis-hearing measured (5×) and
  because every layer can disable it — that escape hatch is the reason a
  disable mechanism exists at all rather than being symmetry for its own sake.
  This is the exemplar to point at if the disable path is ever "simplified"
  away.
- **The wholesale rule is being retired, not bypassed.** Its docstring's
  argument is answered explicitly in the code and in `CLAUDE.md`; a future
  reader who finds only half of that will re-introduce the override. Both must
  land in the same task.
- **A glossary change silently does not re-bias cached chunks.** Documented in
  `transcribe_stream`'s docstring and in `CLAUDE.md`, with the
  delete-`chunks/` recovery named, because a silent half-effect is exactly the
  class of failure the logging feature was built to end.
- **The default is invisible on disk until adopted**, so the editor must render
  inherited rows with their source or an operator will not understand where
  `Karussell` came from — the same obligation the moments editor carries.
