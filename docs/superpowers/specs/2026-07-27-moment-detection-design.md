# Moment detection an operator can actually use

## Why

Detection as shipped is not usable, and the operator's verdict — "das liefert
keinen Mehrwert und ist nicht die erhoffte Unterstützung" — is supported by
measurement rather than impression. Reproduced on this workspace's own
`streams/V9nVNEQNdR4` (the 98-minute ERF qualifying, 5577 words, default
lexicon only — no `moments.json` exists at any layer):

**The ranking is near-arbitrary.** `rank_moments` sorts candidates by
integrated loudness alone and discards the emphasis score that selected them.
The stream is broadcast-normalised: the top 20 moments span 6.2 LU, most within
~2 LU, so noise decides the order. Rank 1 is `emphasis=1.14, lexicon=0.00`
("at least the qualifying so our 14 drivers so far…"); the strongest moment in
the stream — `emphasis=5.00, lexicon=4.00`, "Will we see purple? Yeah, we will
see purple." — lands at rank 10. **Nine of the top 20 have zero lexicon hits.**

**Loudness is not an excitement signal on this material, in any form.** Testing
the obvious repair — short-term loudness relative to a ±2-minute local median —
refutes it: the known-good moments score +3.4 to +5.9 LU, while the worst
chatter scores +11.4 LU, and the fifteen strongest dynamic peaks in the whole
stream contain no racing moment at all. They are the intermission jingle
(61:06, 69:31, 80:37 — no words), and audio dropouts (78:06 "We have seen
buffered rings, so no why?", 79:46 "Look. We're on the... lag coming from...").
The single loudest excursion of the stream, +20.7 LU, is a transmission fault.
Loudness measures production artifacts, so it is removed rather than reweighted.

**Speech rate alone crosses the threshold.** `rate_score = count/baseline - 1`
against a threshold of 1.0 means a window with twice the median word density is
a candidate with no lexical evidence at all. **43 of 79 candidates (54%) have no
marker hit** — that is the pre-race block from 4:00 to 20:00, where "Set up
seems to be working. All right. That's good." is a detected moment.

**Every window is 12 seconds, and misaligned.** `preroll=8.0, postroll=4.0` is
fixed. Worse, emphasis at tick `t` is caused by speech in `[t, t+6)` while the
clip is `[t-8, t+4)`: eight seconds before anything happened are included, and
the last two seconds of the speech that triggered the hit are cut off. This is
the mechanism behind "komplett aus dem Kontext gerissen" — at rank 10 the payoff
("we will see purple") sits at the very end of the clip with no build-up.

**Review is impossible because the evidence is discarded.** `build_entry` writes
`start`, `end` and a hook of six words joined from around the peak. The stream
transcript holds the words for that window and they are simply not carried over,
and no route serves them. The operator sees a sentence fragment, two numbers, no
video and no text.

Two smaller findings recorded here because they inform the design: Whisper hears
"Super Pole" as **"Super Bowl"** throughout, and no glossary layer corrects it —
so any purely lexical matcher misses those moments by construction. And the top
20 are written as 20 clip directories the operator must then clear out.

## Scope

This replaces how moments are FOUND and how they are REVIEWED. It does not
touch rendering, the overlay, subtitles, upload, or the clip editor. The six
pinned overlay hashes must not move.

## Decisions

1. **Detection writes an analysis, never a clip.** `detect` produces
   `streams/<video_id>/moments.json` beside the transcript and creates no clip
   directories. A clip exists only when the operator selects a window and asks
   for one.

   The operator's reasoning is decisive and more general than the current bug:
   a stream may hold 5 worthwhile moments or 100, so any fixed count is wrong —
   `top_n=20` is removed with no replacement. The deeper effect is on the
   quality bar. While detection writes artifacts unprompted, its precision must
   be high or the clip list fills with work; once it only DISPLAYS, it may be
   generous, because a weak suggestion costs a glance instead of a cleanup. This
   is what makes the feature useful before it is excellent.

2. **A language model reads the transcript and names the moments.** The
   alternative — repairing the lexicon (categories, speech-rate as amplifier
   only, derived windows) — was considered and is retained as the fallback
   (decision 3), but not as the primary: keyword matching cannot separate "into
   the barrier" (a crash) from "the barriers here are new" (chatter), and it is
   hostage to decode errors like "Super Bowl". A model also produces the
   per-moment REASON the review list needs, which a matcher would have to fake
   from marker names.

   Cost is not an argument at this scale. An 8-hour stream is ~27,000 words
   ≈ ~68,000 input tokens across windows, ~4,000 output: roughly **9 ct with
   Haiku 4.5, 18 ct with Sonnet 5** (introductory pricing; ~26 ct from
   2026-09-01). Over 50 streams the two differ by under 10 EUR, so the model is
   chosen on quality, not price.

   **Requests are synchronous, not batched.** The Batch API halves the price but
   may take up to 24 hours; trading an hour of latency for four cents is a bad
   deal when the operator is waiting for the list. Batching stays available as a
   config switch for a deliberate overnight run across many streams. Prompt
   caching of the shared instruction block is likewise not assumed: Haiku 4.5
   only caches prefixes from 4096 tokens and the instructions plus lexicon fall
   below that (Sonnet 5 caches from 1024, so it would benefit) — which narrows
   the price gap between the two slightly and changes nothing about the order of
   magnitude.

   > **Superseded on 2026-07-29 by the bake-off this decision itself called
   > for.** The default is now `claude-opus-5`. Over the 98-minute qualifying,
   > against the four known-good moments, Haiku found 2 moments and none of the
   > four, Sonnet 7 and one, Opus 7 and three. The reasoning below is kept as
   > written because it is why the question was left open rather than guessed —
   > but read it as the hypothesis the measurement refuted, not as current
   > design. See `claude_client.DEFAULT_MODEL`'s own note for the numbers and
   > for the prompt defect that had to be fixed before any of them meant
   > anything.

   **Default `claude-haiku-4-5`, configurable per channel.** The task is
   classification against stated criteria, which is Haiku's strength. The three
   places where a smaller model is genuinely at risk here — recall across a long
   document, calibrated scoring across the whole stream, and the inference from
   "Super Bowl" to Super Pole — are addressed for the first two by windowing
   (decision 4). The third can cost a missed moment, never a false one. Because
   the model is a config value, this is settled by measurement, not argument:
   see "Model bake-off" below.

3. **The repaired lexicon stays as an offline fallback, and says so.** With no
   key or no network, the run falls back to lexicon scoring and records
   `engine: "lexicon"`. Both engines write the same `moments.json` shape, so the
   UI reads one field rather than branching.

   A silent downgrade to the weaker engine would reproduce exactly the failure
   this spec exists to fix, so the fallback is announced in the job log AND at
   the hit list ("detected without a model — reduced quality"). This is the
   `make_subtitle_provider` lesson applied one layer up: a degradation nobody is
   told about is indistinguishable from a success.

4. **The transcript is processed in ~1-hour windows with ~2 minutes of
   overlap.** An 8-hour transcript fits in one request on every candidate model,
   so this is not a context limit — it is about consistency and blast radius.
   Per-window scoring stays calibrated (hour 6 is judged like hour 1), progress
   is reportable, and one failed window costs one hour rather than the run. The
   overlap keeps a moment on a seam from being lost; duplicates are merged by
   window overlap, higher score wins.

5. **The model returns LINE NUMBERS, not timestamps.** The transcript window is
   presented as numbered lines of ~10–15 seconds each; a moment is
   `start_line`/`end_line`, and the real times are looked up from the words
   ourselves.

   Models are unreliable at arithmetic over clock times and would return
   plausible timestamps that are twenty seconds off — the current failure in a
   new costume. A line number either exists in the window or does not, which
   makes a hallucinated boundary structurally impossible and validation
   trivial.

6. **Nothing the model returns is trusted unvalidated.** Each moment must have
   both line numbers present in its window with `start_line <= end_line`, a
   resulting duration of 5–90 seconds, a category from the five, and a score in
   0–10. A moment failing any check is dropped and logged; the window still
   counts. A hard cap of 12 moments per hour is a runaway guard, matching the
   instruction-level target density of 3–6 per hour.

7. **The activity curve is computed locally, not by the model.** The model
   returns discrete moments; the overview strip needs a continuous signal. Word
   density plus marker density per minute gives it, at no cost — and, crucially,
   it exists without a model at all. The curve is therefore labelled "activity"
   rather than "importance", which is what it honestly is; the model's moments
   are the discrete markers drawn over it.

8. **`rank_moments` and `measure_loudness_ffmpeg` are deleted.** Not reweighted
   — measured useless (see Why). This also removes a per-candidate ffmpeg
   subprocess from every run.

9. **All model-facing and model-produced text is English**, including `reason`
   and `hook_suggestion`, per the project's language rule. The German umlauts in
   YouTube titles remain the existing exception.

## Architecture

Everything below the studio stays pure and FastAPI-free, like the other
non-studio modules.

**`moment_scan.py` (new, pure).** Words in, candidate moments out. Owns
windowing, request construction, schema validation and seam merging. **The model
call is an injected parameter**, exactly as `detect_moments` already injects
`transcriber` and `measure_loudness` — so the whole module tests with no
network, no key and no cost.

**`claude_client.py` (new).** The only module that imports `anthropic`, and
it does so **lazily inside its functions**, the way `google_oauth.py` treats the
Google libraries. A venv without `anthropic` must still start, render and
transcribe. Reads the key from `<workspace>/auth/anthropic.json`.

**`moments.py` (kept, repaired).** The fallback engine: markers grouped into the
five categories with category weights, speech rate as an amplifier that can
never trigger alone, windows derived from the matched span. `rank_moments` and
`measure_loudness_ffmpeg` go.

**`detect.py` (kept, restructured).** Selects the engine, writes
`moments.json`, writes no clips.

**`clip_from_moment.py` (new, pure).** Builds a clip entry from an
operator-chosen window. `moment_entry.py` is absorbed into it; its path-encoded
identity (`moment_url`) is unchanged, so two windows are two clips and the same
window chosen twice is the same clip.

The seam: **detection writes analysis; the operator writes clips.**

## The model contract

Per window, the request carries the fixed instruction (the five categories and
their ranking — start/finish, incidents, sporting highlights, race control,
commentator reaction — the target density, and "return fewer rather than
padding"), the channel's merged `moments.json` markers as vocabulary hints, and
the numbered transcript lines. The transcript has already had `glossary.apply`
run at assembly, so corner names arrive correct.

The response is a strict JSON schema (supported on both candidate models):

```
start_line       int
end_line         int
category         start_finish | incident | highlight | race_control | reaction
score            number, 0-10
reason           string, one sentence, English
hook_suggestion  string, short, English
```

## Studio surface

A fourth router level: `/{channel}/{event}/streams/{video_id}`, reached from the
event screen's existing stream list. Layout: a hit list on the left (sortable,
filterable by category, showing score, category and reason), and on the right a
small expandable YouTube player, an overview strip over the whole stream, a
zoom lane over the current few minutes where boundaries are dragged, and the
transcript. The player is small by default and expands to an overlay on demand.

| Route | Purpose |
|---|---|
| `GET …/streams/{video_id}/moments` | curve, moments, `engine`, timestamp |
| `GET …/streams/{video_id}/transcript` | the stream transcript for the text pane |
| `POST …/streams/{video_id}/detect` | exists; now writes `moments.json` |
| `POST …/streams/{video_id}/estimate` | token count and cost preview |
| `POST …/streams/{video_id}/clips` | create a clip from `{start, end, hook}` |

`{video_id}` goes through `pathnames.validate_segment` before any filesystem
touch; all routes are registered before the SPA fallback; the detect job takes
the event's `EventLock` like any render.

The transcript is served whole — ~2 MB for 8 hours over localhost, which makes
client-side search instant and paging pointless.

**The screen is useful with no API call at all**: transcript, search, curve,
manual window selection and clip creation all work without a key. Detection adds
markers to a screen that already functions.

**Acceptance criterion:** the screen must scroll to every element at a short
viewport — hit list, zoom lane, transcript and player all reachable. Verified in
a real browser before sign-off, not only in tests.

## Error handling

The run never dies, and the two failure modes are deliberately different.

**Engine unavailable at the start of a run** — no key configured, or the first
request cannot reach the API at all — selects the lexicon engine for the WHOLE
run, announced in the job log and at the hit list.

**A window fails mid-run** (network blip, rate limit, unusable response,
truncated JSON, or a refusal — `stop_reason` is checked before content is read)
is dropped, logged with its index, and recorded in `moments.json` as
`missing_windows`, mirroring `stream_transcribe`'s `missing_chunks` — so a gap
can be reported rather than silently read as "nothing happened there". It does
NOT fall back to the lexicon for that window: mixing two scoring scales inside
one list would make the ranking meaningless in a way the operator could not see,
which is the exact defect this spec exists to remove. One engine per run.

The API key must never reach a log line. `tests/test_logging_secrets.py` gains a
case pinning that a failed model request cannot leak it.

## Testing

No network, no key, no model, no cost — the model call is injected and stubbed.
Pinned: every rejection rule individually (line number outside the window,
inverted range, over-long window, unknown category, malformed JSON — each
dropped, none raising); seam merging keeping the higher score; the 12-per-hour
cap; a failed window not stopping the run and appearing in `missing_windows`;
the repaired lexicon engine over the same categories. Frontend logic (window
maths, merging, formatting) lives in its own modules and is covered by Vitest,
outside component files so Vite's fast-refresh boundary stays component-only.
E2E inside pytest: open a stream, search the transcript, set a window, create a
clip, plus the short-viewport scroll criterion.

Unmoved: the six pinned overlay hashes, and `python3 tools/lint.py` at exit 0.

**Model bake-off (a plan step, not a test).** Haiku 4.5, Sonnet 5 and Opus 5 run
once over the 98-minute qualifying with a real key, and the three lists are
compared against the known-good moments ("we will see purple" 1:34:05, "no
contact with the barrier" 1:14:13, "What a lap" 50:14, 39:46) and against the
known-bad ones that currently rank first (23:24, 4:25). The result sets the
default. Total cost well under one euro.

## Billing note

The Messages API bills per token through an Anthropic API organisation and is
NOT covered by a Claude Pro/Max subscription. This was confirmed in practice:
the operator created an API organisation and key, and a verification request
against `claude-haiku-4-5` succeeded (12 in / 4 out), so key, credit and
connectivity are all live.

The key is stored at `<workspace>/auth/anthropic.json`, alongside
`client_secret.json` and `token-<id>.json` under the same gitignored,
never-logged rules.

**The loader accepts two shapes**, because the file as created holds the raw
key rather than JSON despite its extension: a bare `sk-ant-…` string, or a JSON
object with an `api_key` field. It strips surrounding whitespace and never
rewrites the file. Being tolerant here costs three lines and avoids an
error whose message ("expecting value: line 1 column 1") explains nothing to
an operator who did exactly what they were asked to do.

**Files under `auth/` are created with mode 600.** The two key files were found
at 644 — world-readable — and tightened to match the token files beside them.
Any future write into `auth/` sets 600 explicitly rather than inheriting the
umask.

## Out of scope

Automatic clip creation in any form; changes to rendering, subtitles, the
overlay or upload; migrating previously auto-detected clips (none exist in
`N24-2026`, and nothing deletes them elsewhere); cookies for unlisted streams.
