---
name: detection-and-providers
description: Whole-stream transcription, moment detection, the three model providers and their key-secrecy rules, the weighted lexicon fallback, the layered glossary and the per-venue track packs. Read BEFORE touching stream_transcribe.py, detect.py, moment_scan.py, moments.py, lexicon.py, glossary.py, tracks.py or anything under yt_shorts/providers/ - it carries measured findings and hard prohibitions the code alone does not explain.
---

# Detection, transcription and model providers

Moved here VERBATIM out of the repository-root `CLAUDE.md`. Nothing was
reworded: this project's own record is that restating one of these rules is how
it becomes false. The root file keeps the prohibitions and points here.

**Whole-stream transcription is `stream_transcribe.py`, chunked and resumable.**
A stream is hours long - **over two hours of Whisper decode for an eight-hour
race on this machine**, and that number is a correction rather than a rounding:
this file said "~1h for an 8h race, measured" until it was measured again on
2026-07-31 against `Esm9vv5-PdU` (8 h 19 min), where a 10-minute chunk took
about 2.5 minutes and there were 50 of them. It is a reading of THIS machine,
not a property of the tool - a different CPU moves it - so re-measure rather
than trusting either figure, and note that everything downstream (the queue's
`cpu` pool limit of 1, the decision to split transcription out of detection)
was sized against the two-hour reading, not the one-hour one. So it is
not the short-clip path `transcribe()` handles. It downloads the audio once
(yt-dlp `bestaudio`) because the chunked decode below reads every window from
that one file - this used to also be kept for a loudness signal moment
detection read; that signal was deleted along with the loudness-ranking engine
(see "Moment detection" below), so decoding is now the only reason the audio is
kept. It splits the audio into fixed windows, decodes each in a **killable
subprocess** (`_decode_worker.py`), caches each chunk under
`streams/<video_id>/chunks/` in the workspace, and assembles `transcript.json`.
The subprocess is the point: it makes the per-chunk timeout real, which is the
"Unbounded decode" note's resolution for the stream case (a signal or thread
cannot stop a hung decode). It shares the one decode core
(`transcribe.decode_wav`) with clip transcription, so a clip and a chunk decode
identically. `downloader`/`decoder` are injected for testing (no network, no real
model), exactly as D1 injects its yt-dlp `runner`. Everything under
`streams/<video_id>/` is derived, re-derivable runtime data - never the repo,
never editorial. Its user-facing trigger depends on WHO asks. `bin/yt-shorts
detect` still transcribes on demand - `detect_moments` calls
`transcribe_stream` itself, and a CLI operator is watching a terminal while
it happens. The STUDIO does not, at either of its two entry points: both the
detect route and a queued `detect` entry run `start_detect_job`'s own default
(`detect_moments` bound to `detect.require_cached_transcript`), which reads a
transcript already on disk and REFUSES (`TranscriptNotCached`) rather than
starting an hour of Whisper decode nobody is watching. The studio's way to
get one is a `transcribe` job of its own (`POST /api/jobs` with kind
`transcribe`, which `studio.worker` starts via `start_transcribe_job`). The
two studio paths agreed only from Task 6 on: the detect route used to pass
`detect_fn=detect_moments` explicitly and so kept the old
transcribe-on-demand behaviour alive over HTTP after the default had already
changed underneath it.

**Moment detection was rebuilt: a model scores the transcript now, and the old
speech-rate/loudness engine is DELETED, not superseded.** The operator judged
the original engine unusable - every suggestion 12 seconds long, torn out of
context, seemingly arbitrary - and four defects were proved by measurement on
this workspace's own data, not asserted:
- Ranking by integrated loudness over a broadcast-normalised stream spread the
  top 20 across just 6.2 LU, so noise decided the order and the strongest
  moment landed at rank 10. The obvious repair - loudness DYNAMICS against a
  local median - was tried and REFUTED: the fifteen strongest excursions
  turned out to be the intermission jingle and audio dropouts, the loudest of
  all a transmission fault. Loudness measures production artifacts here, so it
  was deleted rather than reweighted - there is no loudness signal anywhere in
  the replacement.
- 54% of candidates (43 of 79 on a 98-minute qualifying) had no marker hit at
  all - speech rate alone made "Set up seems to be working. All right. That's
  good." a detected moment.
- The 12-second window was fixed AND misaligned: emphasis at tick t was caused
  by speech in `[t, t+6)` while the clip was `[t-8, t+4)`, so eight silent
  seconds were included and the last two seconds of the triggering speech were
  cut.
- The transcript evidence that produced a score was discarded after scoring,
  leaving an operator nothing to check a suggestion against.

`moments.find_candidates`, `rank_moments`, `measure_loudness_ffmpeg` and
`LoudnessMoment` no longer exist; do not resurrect the shape "score candidates,
then rank by an independent loudness pass" - it was tried on this project's own
data and it does not work.

**The replacement is a PACKAGE plus four modules, and none of them may import
FastAPI.** It was five modules once - `claude_client.py` (+ `_anthropic.py`)
was the Anthropic API boundary, and both are gone, not renamed. The vendor
boundary is now `providers/`, and everything else below is unchanged by that
move.

`providers/` is the registry and the seam. `providers/__init__.py` holds
`PROVIDERS` (id -> module), `DEFAULT_PROVIDER` (`"anthropic"`), `ordered()`
(the display order: the default first, then the rest alphabetically - the
Settings screen renders it, so it must not depend on dict insertion order),
`get(id)` (which raises `UnknownProvider` rather than guessing a default - a
typo must be reported, never silently answered with Anthropic), and `CONTRACT`.
`providers/_shared.py` holds everything a provider needs that does not know
which providers exist - `ModelError`, `MissingKey`, `SdkUnavailable`, `Usage`,
the key-file helpers, `sdk_installed`, `require` - and that split is what
breaks the import cycle: `__init__` imports the three provider modules at
module scope, so a provider importing `_shared` never imports its own package
while it is still executing. The module-scope import is deliberate: a syntax
error in a provider nobody has a key for fails in the suite rather than for
whoever first pastes that key.

`CONTRACT` is EIGHT names, named in code rather than only in prose so the
conformance suite can iterate it: `PROVIDER_ID`, `KEY_FILENAME`,
`DEFAULT_MODEL`, `PRICES`, `PACKAGE`, `INSTALL`, `VERIFIED`, `make_caller`.
`make_caller(api_key, *, model, max_tokens, sdk, usage)` returns
`call(system, user, schema) -> dict`, identical across all three - which is
what lets `moment_scan.scan`'s unconstrained `caller` take any of them.
`tests/test_provider_contract.py` is the conformance suite: it parameterises
over `providers.ordered()` and holds EVERY registered provider to nine
behavioural properties - the three key-secrecy wraps (client, request,
response read), a non-JSON answer becoming a `ModelError`, accepting
`moment_scan`'s own schema and returning its answer, `usage` recorded BEFORE
the response is read, the API's own token counts accumulated, unreadable usage
costing the bookkeeping and not the answer, and no `usage` argument at all
still working - plus the key file's own rules and a subprocess check that
`import yt_shorts.providers` pulls in no vendor SDK. A fourth provider
inherits all of it the moment it enters `_MODULES`, which is the registration
point: `PROVIDERS` and `ordered()` are both DERIVED from that tuple, so
inserting into `PROVIDERS` instead yields a provider `ordered()` never returns
and this suite therefore never exercises. The only other thing it adds to THAT
file is its own fake SDK in `FAKES`; two registry pins in
`tests/test_studio_api.py` need widening besides (see README.md's "Adding a
fourth provider", which states the whole recipe and was measured against it).

The three providers are `anthropic_api.py`, `gemini_api.py` and
`openai_api.py`. Each keeps its key at `<workspace>/auth/<PROVIDER_ID>.json`
(a raw key string or `{"api_key": ...}` - both shapes, via the shared
`load_api_key`), alongside `client_secret.json` and `token-<id>.json`, under
the same gitignored, never-logged rules. **Whether a provider has ever been run
against its real service is what that module's own `VERIFIED` flag records, and
the flag is the authority** - do not restate the current values here, where they
would date, and do not read a count of measured providers out of this paragraph
either. What a `True` is BACKED BY sits beside that module's `DEFAULT_MODEL`: a
dated bake-off comment naming the stream, the models, the counts and the cost,
in the shape any later measurement should copy. A cost figure there is only
worth what its SOURCE is, and the two sources are not equivalent: a number
computed from the API's OWN reported token counts (`providers.Usage`) is a
measurement, while one from `estimate.py`'s character count is not - that
calculation was calibrated against a script measuring itself the same way, and
it runs low by more than a factor of two on the one row since checked against
the API (`claude-opus-5`: an estimated `~$0.062` against a measured `$0.1362`).
Label an estimate as an estimate where it stands, rather than letting the two
sit in one column looking alike; the corrected cross-provider table lives in
README.md's "How far each provider has actually been measured", and is not
restated here or in the other providers' comments.

**And a bake-off's moment COUNT is one sample of a number that moves.** The
same `claude-opus-5` over the same stream has returned 7, 10 and 11 moments on
three runs. What varies is the weakly-scored TAIL - the two logged runs agree
on 9 of 10, and the strong moments every provider is scored on are stable -
so a count is a reading, not a constant, and any cross-provider "agreement"
figure is approximate because the reference it is scored against would have
been a slightly different list on a different day. A discrepancy between two
counts is therefore not automatically a bookkeeping error, and the way this
one was settled is the shape to copy: the workspace's central log named the
run and the second, `moments.json`'s `created_at` matched it, and a third run
established the spread. Do not "fix" one of two disagreeing counts into the
other without that kind of evidence.

Back to the flag itself. A `False` means nobody has spent money on that
provider yet, and
the answer then is DISCLOSURE rather than silence - the studio marks it at the
moment of choosing (see "An unverified provider" below). Every provider is
fully TESTED either way, against a fake SDK; what `VERIFIED` records is the one
thing no test in this repo can establish.

**Three optional SDKs now, not one, and all three are lazy.** `anthropic`,
`google-genai` and `openai` are OPTIONAL dependencies, imported LAZILY inside
each provider's own `_sdk()`, exactly like the google upload libraries and
FastAPI - so a venv that installed NONE of them still starts, renders,
transcribes and uploads. Two properties follow, both pinned rather than
asserted: `import yt_shorts.providers` pulls in no vendor SDK
(`test_importing_the_package_pulls_in_no_vendor_sdk`, a subprocess that reads
`sys.modules`), and `create_app()` plus a real `GET /api/settings` pulls none
either (`test_rendering_the_settings_page_pulls_in_no_vendor_sdk`, same
mechanic one layer up) - which is why `_shared.sdk_installed` answers with
`importlib.util.find_spec` rather than a try/import: reporting three
providers' state must not import three vendor SDKs into the studio process.
That second test matches on the FULL package name (`google.genai`), not its
top level, because `google` is a namespace package this project's YouTube
upload stack legitimately occupies - and because `find_spec` imports the
parent namespace of a dotted `PACKAGE` as a side effect, which `sdk_installed`
documents. `google` being present is expected; the vendor SDK itself must not
be.

`moment_scan.py` groups a transcript's words into ~12-second numbered lines,
splits them into hour-long windows with a two-minute overlap, renders a window
as plain numbered text, calls the model, and validates and merges what comes
back. `moments.py` is now just the shared `Moment` type, `CATEGORIES`,
`activity_curve` (a LOCAL signal - no key, no network - so the stream-overview
strip stays useful before detection has ever run), and `lexicon_moments`, the
offline fallback engine (see the lexicon section below, which this rewrite
keeps rather than deletes). `detect.py` ties `stream_transcribe` to whichever
engine ran and writes `streams/<video_id>/moments.json` - never a clip; the
studio runs it as a background job under the SAME `EventLock` a render takes
(`studio.jobs.start_detect_job`). `clip_from_moment.py` is the only place a
chosen window ever becomes a clip.

**Detection writes an analysis, never a clip - the change of contract is the
point.** While it wrote clip directories unprompted, precision had to be high
or the operator's clip list filled with work to clear out. Now it only
DISPLAYS, so it may be generous: a weak suggestion costs a glance instead of a
cleanup. A clip exists when the operator picks a window (`clip_from_moment.
create_clip`) and asks for one, and at no other time - true as design, and now
true in practice too: the stream view (above) is that picker, `POST
…/streams/{video_id}/clips` is its one write, and README.md's "Moment
detection" section describes the operator's flow through it. There is deliberately NO
per-stream cap on how many moments a stream yields - the operator rejected that
explicitly ("sometimes 5, sometimes 100"), which is also why the old fixed
`top_n` config key is gone. `moment_scan.MAX_PER_WINDOW` (12) is a different
thing and stays: it bounds how many candidates ONE hour-long window can
contribute after the model already scored it, a sanity cap against a
degenerate answer, not a target count for the stream.

**The model returns LINE NUMBERS, never timestamps.** `moment_scan.render_window`
hands the model `<line index>\t<clock>\t<text>` rows and asks for a
`start_line`/`end_line` pair back; `validate_moment` looks the real start/end up
locally from the same line list. A model is unreliable at clock arithmetic and
would return plausible-looking times twenty seconds off - the exact failure
this rewrite exists to remove, in a new costume. A line number either exists in
`lines_by_index` or it does not, so a hallucinated window is structurally
impossible rather than merely unlikely. `validate_moment` NEVER raises - a
malformed candidate (bad category, a score outside 0..10, a line number that
does not exist, a non-string `reason`) is dropped and logged, and the window
still counts; one bad moment costs one moment, the same "one failed clip must
never abort a run" stance one layer down.

**The key-secrecy rule cost two review rounds to get right on the FIRST
provider, and it is now a shared rule every provider is held to.** Every
exception escaping a vendor SDK is wrapped in `ModelError` whose message is
built from the original exception's TYPE NAME only, never its text, because a
third-party library's message may quote the request, and the request carries
the API key. THREE entry points all wrap this way, in all three provider
modules: building the client (`Anthropic(api_key=...)` / `Client(api_key=...)`
/ `OpenAI(api_key=...)`, which take the key as an argument), the request call
(`messages.create` / `interactions.create` / `responses.create`), and the
RESPONSE-READING path (the vendor's own stop/status field, the answer read,
`json.loads`) - that third one was found by a review after being missed twice
on Anthropic: a fake SDK whose `response.content` raised
`AttributeError("... sk-ant-XXXX ...")` on attribute access used to reach the
caller as that bare `AttributeError`, key and all.

Where that is PINNED has changed with the move, and the change is the point.
It used to be one module's own test
(`test_an_unwrapped_response_reading_exception_is_wrapped_without_the_key`).
It is now three of `tests/test_provider_contract.py`'s nine behavioural
properties, parameterised over `providers.ordered()` - one per entry point,
each driving a fake SDK that raises an exception carrying the key in its text
and asserting the key does not survive into what the caller sees. A fourth
provider inherits all three the moment it enters `PROVIDERS`, rather than
having to remember a rule that took two rounds to get right the first time.

The consequence for every consumer: `ModelError` and
`MissingKey` messages may be logged in full - they are built from a type name
or from the model's own answer, never from a request. Anything else must be
logged by TYPE NAME only, as `detect._caller_from_config` and `moment_scan.
scan` both do. This must not be weakened into "the production caller wraps
everything anyway" - that makes the safety property BORROWED from a caller the
function cannot inspect, and `moment_scan.scan` takes an unconstrained `caller`
callable, so it cannot assume anything about what reaches it.

**WHICH vendor answers is `brand.json`'s `detect` section, validated by
`profile._validate_detect`.** Two optional keys: `provider` (a
`providers.PROVIDERS` id) and `model` (a non-empty string). Four rules, each
deliberate:
- **An absent section, an explicit `null` section, and an explicit `null` for
  either key all mean "unset"** - `providers.DEFAULT_PROVIDER` and that
  provider's own `DEFAULT_MODEL`. This is the opposite of `subtitles`, where a
  null survives to an AttributeError at render time and so has to be refused;
  here both consumers (`detect.detect_moments` and the studio's `estimate`
  route) already write `config.get("detect", {}) or {}` and
  `settings.get(key) or default` - `dict.get`'s default argument does NOT
  cover a key present with value `None`, only its absence.
- **An unknown provider is a REPORTED DEFECT**, collected like every other
  profile defect, never a silent fall back to the default. A typo that quietly
  ran a different vendor than the operator asked for is exactly the silent
  substitution this project keeps paying for. The `isinstance` check comes
  first and is not tidiness: a list or dict here is unhashable, and
  `x in PROVIDERS` would raise `TypeError` out of a function whose whole
  contract is to COLLECT defects rather than throw on the first one.
- **A MODEL name is deliberately NOT checked against the vendor's catalogue.**
  That would mean carrying three vendors' model lists in this repo and
  re-checking them monthly. A model the vendor does not know fails at call
  time, is wrapped as `ModelError`, and falls back to the lexicon with the
  same loud log a missing key produces.
- **`detect` is CHANNEL-scoped only.** `event_brand_admin.OVERRIDE_SECTIONS`
  excludes it and `update_event_brand` refuses it BY NAME rather than dropping
  it silently (the studio's PUT route refuses it a step earlier), for the same
  reason `upload` is excluded: both decide whose credentials and whose bill an
  operation spends, which is a property of the channel, not of one event's
  look.

**The "one engine per run" rule below now has a measurement behind it, not
only an argument.** Over an eight-hour race (2026-07-31, `Esm9vv5-PdU`, 9
windows) the three providers scored on visibly different scales:
`claude-opus-5` returned the MOST moments (39) and the FEWEST strong ones (2 at
>= 8.0); `gpt-5.6-terra` returned 33 and 8. An 8.0 from one is not a claim
about the same thing as an 8.0 from another, so a hit list mixing two engines
would rank by a number that means two things at once. Pairwise agreement over
that stream was 38-67% and no model's list was a subset of another's - which
also means the rule costs something real: one engine per run is a decision to
forgo what the other would have found, taken because an uninterpretable ranking
is worse. See README's provider section for the full comparison.

**One engine per run, and a fallback that announces itself.** A window that
fails mid-scan is recorded in `missing_windows` and does NOT fall back to the
lexicon for that one window - two scoring scales sitting in one moments list
would make the ranking meaningless in a way the operator cannot see (see
`moment_scan.scan`'s own docstring). Falling back to the lexicon engine for a
WHOLE run - no key at `<workspace>/auth/<provider>.json`, that provider's SDK
not installed, or the model unreachable - is different:
`detect._caller_from_config` returns `None` and
logs why (naming the provider, because "no API key" is useless diagnosis when
three of them could be the one meant), `detect_moments` then runs
`lexicon_moments` instead and logs a
second time ("detected with the lexicon engine - reduced quality") so an
operator reading either the job log or the CLI's terminal is told, not left to
notice a suspiciously plain result. This project has already paid for a silent
degradation once (see subtitle_pipeline's history above); it does not repeat
it here.

**`moments.json` carries two provider-shaped fields, and they answer different
questions.** `engine` is what actually ran - `model:<name>`, `lexicon`, or
`none` - and stays the sole authority on what produced the moments.
`configured_provider` is what the profile asked for, written unconditionally,
even when `engine` is `"none"` (zero words, where `_caller_from_config` is
never called and no key is ever consulted) or `"lexicon"` (a missing key or an
unreachable model). It is named `configured_provider`, not `provider`,
specifically so a payload can never be misread as `{"engine": "none",
"provider": "anthropic"}` naming a vendor that was never even attempted. A
badge or log line built from one field without the other is exactly the
silent-degradation-looks-fine failure this project keeps paying for (see
subtitle_pipeline's history and the paragraph above) - always read them
together. Both fields are ALWAYS present on the wire: `GET
…/streams/{video_id}/moments` defaults `configured_provider` to null on both
paths it can answer on - the never-analysed synthesis and an analysis written
before the field existed - because the frontend's `StreamAnalysis` declares it
non-optional.

**An unverified provider, and what it costs, are disclosed at the moment of
choosing.** The Settings screen's "Model providers" block holds the workspace's
per-provider API key (paste, replace, forget - never pre-filled, because the
server never returns one), and the channel Brand editor's "Moment detection"
section picks which provider a channel uses. BOTH surfaces mark a provider
whose `VERIFIED` is false, because an operator choosing one in the editor must
not have to have visited Settings to learn it. The editor additionally shows
that provider's `PRICES` row for the selected model plus its cheapest priced
model beside it (`providers.ts`'s `priceSentence`, fed by a `prices` field on
`GET /api/settings`'s provider rows). That comparison exists because a
provider's `DEFAULT_MODEL` is not always its cheapest entry - picking cheapest
is the argument the Anthropic bake-off refuted - and an operator paying the
difference should see it while choosing rather than on an invoice. Whether a
given gap is EARNED is a question each module answers for itself beside its own
`DEFAULT_MODEL`, and the answer moves as measurements land; read it there, not
here. Those numbers are a dated per-million rate and a FLOOR
(both `gemini_api` and `openai_api` document tiering their flat table cannot
express, and this project's endurance streams reach it), so the wording says
"from" and names the floor. It must never be presented as a bill or multiplied
out into a total.

**A moment's clip identity is PATH-encoded and ROUNDED, and both halves
matter.** `clip_from_moment.moment_url` puts `video_id` and the rounded
start/end in the URL PATH (moved here unchanged from the retired
`moment_entry.py`), because `clipid.canonical_url` STRIPS the query string - a
`?...` identity would collapse every moment in a stream to one clip. Because
the times are ROUNDED into the path, two genuinely different windows inside
the same rounded second collide - `(10.1, 20.3)` and `(10.2, 20.4)` both round
to `.../10-20` - so `create_clip` REFUSES rather than overwriting
(`ClipIdentityCollision`), because the directory's `edit.json` (a human's title
correction) and `transcript.json` (decoded against the FIRST window's audio
span) would survive describing a clip whose own `clip.json` now claims the
SECOND window. A separate `ClipIdentityUnreadable` covers a directory whose
`clip.json` cannot be parsed at all: proceeding would let `clipstore.
write_clip` independently fail to recognise the same corrupted file and mint a
second, differently-named directory sharing the identical identity suffix,
silently. Both were reproduced, not theorised - see `tests/test_clip_from_moment.py`.
Re-picking the SAME window stays idempotent within `WINDOW_TOLERANCE_SECONDS`
(0.01 s), which exists for JSON round-trip float noise, not for real
differences.

**A moment's window is editorial.** The detected `start`/`end` are derived;
nudging them in the studio writes an `editorial.Edit.window` override into
`edit.json` only, and `render.source_for_clip` fetches the EFFECTIVE window
(`editorial.effective_window`: the override if set, else the detected one). The
channel lexicon is `moments.json`, loaded like `glossary.json`, kept separate
because it marks excitement rather than correcting proper nouns.

**And the transcript cache follows that trim, which for a while it did not.**
`subtitle_pipeline.transcript_source` is the identity the cache is keyed on:
`clip["url"]` for an untrimmed clip, byte for byte, and the effective window
folded into the PATH once an override exists. Both halves matter. It used to
be `clip["url"]` unconditionally, which an editorial trim never changes (it
writes `edit.json`; `clip.json`'s url keeps naming the DETECTED window,
because that url IS the clip's identity) - so trimming six seconds off the
front re-downloaded the shorter passage, hit the cache, and burned in captions
offset by exactly those six seconds, silently. And an untrimmed clip must keep
the ORIGINAL string: any other value would miss the cache once for every clip
in every workspace, at minutes of Whisper apiece.

The window rides in the path and never a query or fragment, for the same
reason `clip_from_moment.moment_url` puts a moment's own window there:
`clipid.canonical_url` strips both before comparing, so `?w=` would be erased
and the whole thing would silently do nothing.

Note what this deliberately is NOT: deleting `transcript.json` when the window
is edited. That would be a new write outside `edit.json` from the studio's
edit path - the boundary this file has already restated wrongly six times -
where re-keying instead reuses the mechanism `transcribe()` was built with,
"the cache correctly refusing to hand back someone else's transcript", which
already re-derives AND announces itself through the note channel.

**The lexicon is now the FALLBACK engine, used only when no model caller is
configured** (`detect._caller_from_config` returns `None` - see "One engine
per run" above) - it used to be the only engine there was, and everything
below about its weighting, its matching rule and its layering is unchanged by
the rewrite and still load-bearing: `moments.lexicon_moments` is a real,
tested scoring path an operator without an Anthropic key depends on, not a
vestige.

**The lexicon is weighted, not a flat marker list.** `moments.json`'s
`markers` maps each marker to a weight `0 <= w <= 10` (`lexicon.py`); both
file shapes are accepted, so an existing hand-written list keeps working -
`["crash", "contact"]` means every marker at weight 1.0. Weights exist
because counting every marker equally does not survive contact with real
commentary: measured on a 98-minute ERF qualifying transcript, the ten
incident-only markers this file used to ship scored THREE hits total, while
`pole` occurred 19 times as ordinary chatter - and since an unweighted hit
added exactly 1.0 and the candidate threshold IS 1.0, marking `pole` at
weight 1 would have flagged nearly every mention of it as its own
candidate. With weights, one `crash` (3.0) crosses the threshold alone and
`pole` (0.3) needs four mentions in the same window to do the same.
Markers are matched LONGEST FIRST and every match claims its span in the
window's text: the LONGEST overlapping match always claims the span, and
weight is never the tie-break, so a phrase and its own substring never
both score - `super pole` claims its span before `pole` can match inside
it. This is an absolute rule, not a preference, and it has a deliberate
consequence an operator can be surprised by: with the shipped defaults,
`"super pole sitter"` scores `pole sitter` (0.5), NOT `super pole` (1.0) -
`pole sitter` (11 characters) is longer than `super pole` (10) and claims
the span first, even though `super pole` carries the higher weight (see
`TestOverlappingMarkers.test_super_pole_sitter_scores_the_longer_phrase_not_the_higher_weight` in
`tests/test_moments.py`). Two markers of the exact same length that
overlap are resolved deterministically too - by weight, then alphabetically
- rather than by which layer happened to introduce the marker first. A
weight-0 marker is skipped before matching even starts, so a disabled
marker can never claim a span and thereby suppress another marker's match -
suppression is refused by design, the same reason a negative weight is
refused (`lexicon._weight`).

The lexicon is **additive across four layers, most specific wins**: the
built-in default (`lexicon.DEFAULT_MARKERS`, in code) is least specific,
then the workspace's own `moments.json`, then the channel's, then the
event's - a later layer's weight for the same marker overrides an earlier
one, and a weight of `0` at any layer disables a marker inherited from a
less specific one. The glossary shares the same **additive, most-specific-
wins layering with a disable-by-falsy escape hatch** (see the glossary
section below) - but no longer the same LAYER COUNT: the glossary has a
fifth layer, the venue pack an event selects with `"track"`, which the
lexicon has no equivalent of (excitement markers are not venue-specific the
way corner names are; see "Shipped vocabulary is scoped to a circuit, not
global" below and `profile._load_lexicon`'s own docstring). The two files
sat beside each other behaving differently in the layering RULE for exactly
three commits; that divergence is gone, and the reasoning that justified it
("a glossary is a set of exact corrections for one event") did not survive
a corner-name list that is a fact about a racetrack rather than about one
event.

`lexicon_admin.py` is the studio's write path onto this - pure, no FastAPI,
like the other admin modules - and its routes are a thin mapping of its
`kind` to 400/404. The built-in default lives in CODE, not on disk, so it
is invisible to a plain directory listing until an operator adopts it
(`adopt_default`, which copies it into the workspace layer) - this is why
the editor must show every inherited row together with the layer that set
it, not just whatever a scope's own file happens to contain. Because of
that, `lexicon_admin.read`'s `effective` deliberately KEEPS a weight-0
entry (struck through in the editor) where `profile.merge_lexicons` DROPS
it before scoring - correct for rendering, wrong for an editor that needs
to show a disabled inherited marker rather than make it vanish; the two
merges are built from the same primitives on purpose and must agree on
every non-zero marker.

Changing a weight changes detection output, but the two engines are sensitive
to it differently. The lexicon fallback's score is a direct function of a
marker's weight (`lexicon_moments`/`_count_markers`), so any edit to it -
not only enabling or disabling a marker - changes that engine's output. The
model engine only ever sees marker NAMES: `moment_scan.build_system_prompt`
lists every marker with weight > 0 in the system prompt and never its number,
so a magnitude change alone (1.0 -> 2.0) does nothing to the model's answer -
only crossing zero (enabling or disabling a marker) changes what the model
is told. Re-running `detect_moments` over an already-detected stream after a
weight edit at any of the lexicon's four layers can therefore change the
candidate set under either engine, but the model engine only reacts to an
edit that crosses zero - both are expected outcomes, not a bug to chase.

**The glossary is additive across up to five layers, and it finally reaches
stream transcription.** `glossary.json` corrects proper nouns Whisper does
not know, at both ends: `terms` bias the decoder before it errs
(`hotwords`), `replacements` correct its output after (`apply`). Layer
parsing lives in `glossary.py` (`GlossaryLayer`, `parse_layer`, `load`) so
`glossary_admin` validates a payload exactly the way `profile.load` validates
a file; `profile.merge_glossaries` folds the layers - the built-in default
(`glossary.DEFAULT_LAYER`, EMPTY - see "Shipped vocabulary is scoped to a
circuit, not global" below), the event's own track pack when it selects one
(`tracks.py`), the workspace's `glossary.json`, the channel's, the event's -
most specific winning per entry, with `false` for a term and `null` for a
replacement DISABLING one inherited from a less specific layer. An
empty-string replacement is refused (it would make `apply` delete the
matched words): `null` is how you disable.

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
requests to faster-whisper (see `glossary.hotwords`). That payload carries
**terms only, never replacements**: `transcribe.decode_wav` applies
replacements unconditionally, so sending them to the worker would cache
CORRECTED text in `streams/<video_id>/chunks/` instead of raw decode
output, defeating both a correction spanning a chunk boundary (see below)
and the "a glossary edit needs no re-decode" property - a re-assembly could
no longer tell a chunk decoded before the edit from one decoded after it.

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
inherited entry rather than make it vanish. It has no `adopt_default`, unlike
`lexicon_admin`: the built-in default is empty by design now, so there is
nothing worth copying into the workspace layer - the vocabulary an operator
wants is a `track` selection instead (see "Shipped vocabulary is scoped to a
circuit, not global" below), which `read` and `update` carry as their own
`track` field rather than as adoptable rows.

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

The D-stage arc (D1 discovery, D2a transcription, D2b detection) is complete.
