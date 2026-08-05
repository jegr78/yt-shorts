# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `README.md` first — it documents the workflow, the profile format and how to
set up a new channel. This file covers what the README does not: the constraints
that are expensive to violate and the reasons behind them.

## Commands

There is no `pyproject.toml` and no build step. `PYTHONPATH=src` is mandatory for
every Python invocation. The only config file is a minimal `pytest.ini` whose sole
job is to mute two benign third-party deprecation warnings (Pillow `getdata`,
Starlette TestClient-over-httpx) by message, so the run summary stays clean while
any NEW warning still surfaces - it is not a general test config.

```bash
PYTHONPATH=src .venv/bin/pytest -q                          # full suite
PYTHONPATH=src .venv/bin/pytest tests/test_overlay.py -q    # one file
PYTHONPATH=src .venv/bin/pytest -q -k "logo"                # by name
PYTHONPATH=src .venv/bin/pytest tests/test_render.py::TestWindowAlignment -q

bin/yt-shorts harvest|render|gallery|migrate|studio <channel>/<event>  # e.g. erf/community-clips-back-catalogue
.venv/bin/python tools/convert_fonts.py                     # woff2 -> ttf, one-off

python3 tools/lint.py                                       # ruff + in-house guards (no PYTHONPATH)
python3 tools/lint.py --fix                                 # auto-fix what ruff safely can
```

`bin/yt-shorts` has an absolute shebang pointing at this repo's `.venv`. **A venv
is not relocatable** — if the project directory ever moves, delete `.venv`,
recreate it (`python3 -m venv .venv`, then install `pillow fonttools brotli
pytest faster-whisper`) and fix that shebang. Skipping `faster-whisper` leaves a
venv that can still render, but every clip silently degrades to "no subtitles"
(see the Architecture note below) and `tests/test_transcribe.py` fails at
import.

`render` hits the network and re-encodes; a six-clip run takes minutes. Avoid it
in an edit-test loop — the overlay is testable without it.

## Linter

`ruff.toml` + `tools/lint.py` are a fast, mechanical pre-commit gate. Ruff is an
external binary, NOT vendored and NOT in the venv (`brew install ruff`); `lint.py`
imports nothing from the project, so it needs no `PYTHONPATH`. There is no CI yet —
this is the whole gate, run it before committing. The rule set (see `ruff.toml`)
is deliberately narrow: a correctness/bug gate (`F`, `E9`, `PLE`, `B`, `SIM115`,
`RET503`, `N805`, `PLW0108`), **not** a style gate — pure-style rules and line
length stay OFF so the linter never argues about formatting the codebase has
settled. If you touch the rule set, the tree must stay green (`python3
tools/lint.py` exit 0), the same way the suite must.

`lint.py` adds two pure AST guards for classes ruff has no rule for, unit-tested
in `tests/test_lint.py` (part of the pytest suite): **empty-except** flags a
handler whose whole body is `pass`/`...` with no explanatory comment (a silent
swallow), exempting the accepted idioms — an in-handler comment, a deliberate
`raise` in the try, and handlers catching only benign control/optional-import
types; add one word of *why* and it clears. **procedure-return-value-used** flags
`x = proc()` / `return proc()` where the same-file callee only ever returns None.
Both `check_*` functions must return `[]` for the whole repo — the two
`test_*_repo_is_clean` tests pin that, so a new silent swallow fails the suite.
`_python_files` discovers every tracked `*.py` PLUS extensionless files with a
python shebang — that last clause is why `bin/yt-shorts` (the CLI, no `.py`
suffix) is linted at all; the reference project this was ported from only scanned
`*.py`.

## Tests

The suite must not depend on the operator's workspace — it has to pass
identically whether `~/YT-Shorts-Data` exists or not, and on a fresh
checkout with neither `~/YT-Shorts-Data` nor `YT_SHORTS_DATA` set (where
`workspace.resolve()` would otherwise fall through to the repository's
`channels/`, which does not exist). `tests/conftest.py` enforces this: an
autouse fixture points `profile.CHANNELS_DIR` at
`tests/fixtures/channels/` for every test. `tests/fixtures/channels/erf/`
is the ERF channel as a test fixture, owned by the suite — `channel.json`,
`brand.json`, `glossary.json`, `layout.py`, `fonts/`, and the source list
for `community-clips-back-catalogue` — not rendered output, transcripts,
`clips.json` or `index.html`. It is a separate copy from whatever ERF data
lives in an operator's actual workspace and must not drift out of sync
with `tests/test_event_layer_no_regression.py`'s pinned overlay hashes: if
changing this fixture changes one of those hashes, the fixture broke
fidelity with the channel it represents — fix the fixture, never re-pin
the hash to match. Do not add a test that calls `workspace.resolve()` with
no overrides, or that repoints `profile.CHANNELS_DIR` at
`~/YT-Shorts-Data` — either reintroduces the dependency this fixture
exists to remove.

**No test starts real background work, and that is now structural rather than
a habit.** `Worker.drain_once()` is called at around forty sites across four
test files, and its whole job is to turn a queue entry into a
`studio.jobs.start_*_job` call — a real `EventLock`, a real thread, and for
`transcribe`/`detect` a real yt-dlp download, a real Whisper decode or a real
paid model call. A bare `queue.enqueue("transcribe", …)` followed by
`drain_once()` reaches all of it, and until `tests/conftest.py`'s autouse
`_no_real_job_starter` existed the only thing in the way was a comment and the
habit of stubbing the right name — a guard that fails open, silently, for
every test written next. Every starter is now replaced, for every test, by one
that fails the test loudly; a test that genuinely means to drive a real
starter (with the expensive thing inside it stubbed) opts in by requesting the
`real_job_starters` fixture, which is how it says so out loud.

Three details of that fixture are load-bearing. It patches the names on the
`yt_shorts.studio.jobs` MODULE, because `worker._start_transcribe` and friends
call `jobs.start_transcribe_job` through the module at call time — the same
from-import hazard `_isolated_resolved_workspace` above exists for. The
refusal is `pytest.fail`, whose `Failed` is a **BaseException**, and that is
the point: a plain `Exception` was caught by `Worker._start`'s own blanket
handler, turned into that entry's failure reason, and left a GREEN test that
had in fact called the real starter. And `ran` is the belt to that braces — a
teardown assertion for anything that still swallows the refusal, which is not
hypothetical: in the E2E suite the starter runs on the live server's own
thread, where no raised exception can reach the test body at all.

**Two ways a green run can lie, both measured on this branch rather than
feared.** Neither is caught by reading the summary line.

**A duplicate test-class name makes pytest silently DROP the shadowed class.**
Python rebinds the name, so the first `class TestFoo` simply ceases to exist,
and pytest collects only the survivor — with **no warning of any kind**. When
it happened here, collection fell from 218 to 216 and the run reported "216
passed", which looks exactly like a healthy run. Reproduce it in ten seconds:
put two same-named classes with one test each in a file and collect it —
one test is reported, not two. The only thing in this repo that catches it is
ruff's `F811` (in the `F` set, so already enabled) via `python3 tools/lint.py`,
which flags the redefinition. That is the concrete reason **lint is not
optional before trusting a green suite**: the suite cannot report tests it
never knew about.

**`TestClient` (httpx) normalises literal dot segments before sending, so a
traversal test written the obvious way tests nothing.** A request to
`/api/x/../../etc/passwd` is resolved by the CLIENT: what reaches the app is
`/etc/passwd`, which never matches the route under test — it lands on the SPA
fallback — so the assertion passes whether or not the route's own guard exists.
The PERCENT-ENCODED form is not normalised: `%2e%2e` arrives as a literal `..`
in the path parameter and does reach the handler, which is where
`pathnames.validate_segment` (or a closed registry) can actually be exercised.
Measured directly against a two-route app: `../../etc/passwd` → handler never
called; `%2e%2e` → handler called with `".."`. `%2e%2e%2f…` is a third case
again — the decoded slash means it no longer matches a single-segment route, so
it falls through to a catch-all; that is still a real answer about what cannot
be reached, but it is not a test of the guard. All eight tests in
`tests/test_studio_api.py` that use `%2e%2e` were checked against this and are
sound; several already carry a comment saying exactly which of the three cases
they are, and
`test_a_traversal_shaped_provider_id_writes_nothing` names the limitation in
its own body — with
`test_a_dot_segment_provider_id_reaches_the_route_and_is_refused` covering the
normalised cases by handing the ASGI app a scope whose path already contains
the dot segment, which is the only way to deliver one. Write a new traversal
assertion the encoded way, or drive ASGI directly, and say in the test which
one you are actually exercising. Same
family as the `scroll_into_view_if_needed()` finding recorded below: a helper
that quietly does the work for you turns an assertion into a tautology.

**The E2E block shares ONE uvicorn server per module, and an entire
application per TEST.** Every test in `tests/test_studio_e2e.py` used to start
and stop its own uvicorn server. Measured over the file's 116 tests, that went
from ~95s to ~66s - a difference explained almost entirely by per-test server
overhead that was not real work: ~83ms apiece waiting for a fresh server to
answer its readiness probe, and ~150ms apiece (over a second when the page had
a poll in flight) inside uvicorn's graceful shutdown, whose main loop only
notices `should_exit` on a 0.1s tick. The file's own browser work, roughly
56s, is untouched by this change. The full suite went from ~219s to ~184s; the
per-test fixture overhead that remains is ~8s, so there is no second win of
this shape left to take here.

`_AppSwitch` is how: a tiny ASGI app, the only thing the module's server ever
sees, forwarding to whichever app the current test installed. **What is shared
is the listening socket, its port, the uvicorn server, its event loop and its
thread - nothing else.** Every test still gets a brand-new `create_app()`, its
own `JobStore`, `JobQueue`, `jobs.json`, `Worker`, `tmp_path` and
`profile.CHANNELS_DIR`, installed for that test and REMOVED at its teardown,
plus a fresh Playwright context (so cookies, storage and cache are clean even
though the origin is now constant across tests). Between two tests the switch
holds no app at all, so a request from a page that has not finished dying gets
a 503 and can never be handed the next test's app.

Three rules follow, and the first and third are the ones a later change would
break without noticing - `TestTheSharedServerSharesNothingButTheSocket`
already pins the second, so a regression there fails loudly on its own:
- **The server fixture is MODULE-scoped, not session-scoped.** Nothing this
  file starts may outlive it. `tests/test_studio_worker.py`'s own
  `test_no_studio_e2e_server_thread_survives_into_this_module` is the guard
  that actually checks this - it looks for a stray `_ServerThread` (or any
  uvicorn-named thread) still alive when that module starts, which pytest
  collects after this one. That test exists because the OTHER thread checks
  in that file (`TestTheThread`, `TestCreateAppWiring`) filter by
  `t.name == worker_module.THREAD_NAME` - the worker's own thread name,
  never a uvicorn server's - so a `scope="session"` mutation here was proven,
  by execution, to slip past every one of them and let the full suite pass
  regardless. This fixture also asserts its own thread is really dead at its
  own teardown, which only catches this file outliving itself, not the next
  module outliving it.
- **The app must stay per test.**
  `TestTheSharedServerSharesNothingButTheSocket` pins both halves across its
  own tests - that no two tests are handed the same app object (checked
  across exactly the class's first two tests, not the whole module), and that
  the server owns no app between tests (checked with its own install/clear
  pair rather than depending on an app having been left installed by an
  earlier test - the original version of that test passed run ALONE even
  under a neutered `clear()`, for exactly that reason). Both were verified to
  FAIL under mutation (a `clear()` that no-ops, a `live_studio` that reuses
  one app), so they are not the vacuous assertions they would have been while
  each test ran its own server.
- **The lifespan protocol is answered by the switch, not forwarded.** That is
  only safe because `create_app()` registers no startup or shutdown handler -
  it builds everything in the constructor. `_serving` now asserts exactly
  that (`app.router.on_startup`/`on_shutdown` both empty) on every install: a
  real `@app.on_event("startup")` added to `create_app()` was proven, by
  execution, to run under `TestClient` yet silently never run through this
  shared server, with nothing in the suite noticing before this assertion
  existed.

Parallelism (`pytest-xdist`) was deliberately NOT added: after this change
only ~8s of the file is fixture overhead and the rest is real browser work, so
it would mean a new dependency and re-validating 2298 tests for parallel
safety to attack a cost this change already reduced.

**NEVER run `npm run build` while an E2E run is in flight. The E2E suite is
not flaky; a concurrent build makes it look flaky, and that is measured
rather than suspected.** `web/vite.config.ts` sets `emptyOutDir: true` on an
`outDir` of `../static`, so a build DELETES `src/yt_shorts/studio/static/`
before it rewrites it. `api.py` serves `index.html` from that path with a
`FileResponse`, which opens the file when the REQUEST arrives - not at
startup - so a page loaded inside that window gets a 500, renders nothing at
all, and whichever assertion came next dies of "element(s) not found" after
its timeout. What to grep the captured stderr for is Starlette's own
`RuntimeError: File at path .../static/index.html does not exist.`, raised
FROM a `FileNotFoundError` - both names appear in the traceback and only the
second one is the exception actually raised.

There is a second window with the same signature and a different status
code: `STATIC_DIR.is_dir()` is evaluated ONCE, in `create_app()`. A build
that straddles a `create_app()` rather than a request leaves the SPA
fallback unregistered for the life of that app, so its pages get a 404 from
the catch-all instead. Same blank page, same "element(s) not found", no 500
anywhere - do not conclude from a missing 500 that a build was not the
cause.

The signature is what makes this worth writing down, because it reads exactly
like flakiness: it hits a DIFFERENT test each time (whoever happened to be
loading a page), it never fails a content assertion (there is no content to
be wrong), and it does not reproduce under either of the two things a person
guesses first. Both were tried: an 8-core synthetic CPU load gave 124/124 in
90s, and two deliberately concurrent full E2E runs gave 124/124 and 124/124 -
because neither of them deletes a directory. Three rebuilds during one run
reproduced it immediately. The trigger in practice is a review or mutation
proof running in another process, since each of those builds twice.

Two consequences. Locally: serialise strictly - build, wait, then test. In
CI: the build must be its own step BEFORE pytest and must not be
parallelised with it (it has to run anyway, to check that the committed
`static/` still matches `web/src/`). A failure of this shape is a race with a
build until proven otherwise; read the captured stderr for the
`FileNotFoundError` before calling anything intermittent.

**A TERMINAL JOB STATUS IS NOT A RELEASED LOCK, and two tests were failing
intermittently on exactly that.** Every `start_*_job` in `studio/jobs.py`
calls `job.finish(...)` inside its runner's `try`; the `EventLock` (and, for
connect, the per-channel guard) is released in the `finally` AFTER it. So a
poller that observes `done` can still find the event locked, and a test that
starts a second job for the same event at that instant gets a `LockError`
from the starter - which reads as an intermittent defect in the LOCK rather
than as the test having asked the wrong question. `TestJobRecordsItsCancelToken`,
which chains five starters against one event, failed about one run in TWO
when run alone.

The window was wide because of what stood in front of the release:
`finish_job_log(job)` GZIPS a file. The release now goes FIRST in all five
starters' `finally` and in `start_connect_job`'s - the event is done being
written the moment the records are made, and holding its lock while a log is
compressed is dead time that costs the next job for that event a `LockError`,
a `defer` and a re-queue. That narrows the gap; it does not close it, and it
cannot be closed from the `finally`.

So the RULE, which is what matters here: **anything that needs "the event is
free" must ask `EventLock.is_held()` rather than infer it from a job
status.** `tests/test_studio_jobs.py`'s `_wait_for`/`_wait_for_job` take an
`unlocks=` argument for this and their docstrings carry the reasoning; all
three of that file's "the lock must be released" tests pass it. Production
already copes without any of this - the worker's `defer` puts a locked entry
back with a reason, which is the mechanism's normal behaviour, not a
failure - so do not "fix" it by widening a sleep or by relabelling a
`LockError`.

**The connect guard has NO equivalent public predicate, and the nearest one
is wider than it looks.** `_active_connects` is private, so a test that needs
"this channel's connect is fully over" uses `JobStore.any_running()` - which
is workspace-GLOBAL (any running job of any kind, plus any channel's
in-flight connect) and answers the narrower question only because the store
it is asked of holds nothing else. Do not read it as a per-channel check, and
do not restate this rule as "ask `any_running()` for the connect guard"
without that qualification.

**Both releases are now NESTED, not merely reordered, and the nesting is the
point.** `event_lock.release()` (and `job_store.end_connect`) sit in their own
`try:` whose `finally:` carries the logging. Ordering two best-effort steps
only moves the problem: with the release LAST, a raise out of a log call
skipped it and left a lock file for the stale-pid takeover to clear; with it
FIRST but unguarded, an `OSError` out of `unlink` would leave that job's log
handler open and its file never compressed. Neither may depend on the other
having succeeded.

## Hard constraints

Violating any of these produces output that looks fine in review and is broken in
the player.

**ffmpeg here is built without `libfreetype` and `libass`.** `drawtext` and
`subtitles` do not exist. Every glyph and shape is drawn in Pillow and composited
as a PNG via the `overlay` filter. Do not reinstall or upgrade ffmpeg: the
separate `racecast` broadcast project depends on this exact binary. Available and
in use: `split`, `scale`, `boxblur`, `overlay`, `setsar`.

**`setsar=1` must stay at the end of the filter chain.** The background branch
stretches the source non-proportionally to portrait, and ffmpeg compensates by
setting a non-square sample aspect ratio. Without the final `setsar=1` the files
are 1080x1920 pixels but carry `SAR 256:81`, and every player stretches them back
to 16:9. This shipped once and was only caught by a human watching the result.

**Never crop the picture.** No `crop`, no `force_original_aspect_ratio=increase`
in the sharp branch. The timing tower and leaderboard are burned into the source
video; cropping destroys the information the format exists to preserve. Sources
that are not 16:9 are fitted inside the window with
`force_original_aspect_ratio=decrease` and centered.

**One failed clip must never abort a run.** `harvest.harvest` and the CLI's render
loop isolate failures per entry, record the reason with its exception type, and
report everything at the end. Both return exit code 1 when anything failed — which
is why the README tells operators not to chain the commands with `&&`.

**One exception: the Whisper decode inside `transcribe()` has no timeout.**
`transcribe.TIMEOUT_SECONDS` bounds only the ffmpeg audio extraction that
precedes it, not the decode call itself, which can in principle hang forever —
this is the one place the guarantee above does not hold. This was investigated
deliberately (see `transcribe.py`'s module docstring, "Unbounded decode") and
left undone: a signal-based timeout cannot be relied on to fire inside a hung
C extension, a thread-based one cannot actually stop the decode (Python threads
are not killable) and would just leave it running in the background, and a
process-based one that could really kill it needs a restructuring of how the
model is invoked that is out of scope for a small fix. A timeout that misfires
on a slow-but-healthy decode would abort a good clip, which is worse than this
gap. If a render appears stuck, see that same docstring for what to check and
how to recover (Ctrl-C, or `kill -9` if that doesn't respond, then re-run).

**`/Users/jegr/racecast/` is read-only.** It is the source of ERF's brand assets;
the fonts have already been converted into ERF's `channels/erf/fonts/` in the
workspace (not the repository — see "Where the data lives" below). The same
three files also live in `tests/fixtures/channels/erf/fonts/`, the suite's
own copy — see "Tests" below.

## Architecture

The picture is 1080x1920: a blurred, full-frame copy of the source video at the
back, the sharp 16:9 picture fitted into a fixed window over it, and a
translucent brand overlay on top. The overlay is transparent exactly where the
window is.

Two seams carry the design, and both are worth understanding before changing
anything:

**Data lives apart from code, in a workspace resolved by `workspace.py`**
(`YT_SHORTS_DATA`, then `~/YT-Shorts-Data`, then the repository's own
`channels/` as a last resort). Within it, `clipid.py` gives each clip an
identity from its source URL rather than its title, `clipstore.py` gives
every clip one directory named from that identity, and `editorial.py` is
the additive layer a human's own corrections (title, status, transcript
fixes) live in, separately from anything a derivation step writes. The rule
that follows from this and must not be violated: **derived data (harvested
timecodes, cached transcripts, rendered shorts) is never edited in place,
and editorial data is never written by a derivation step** — `migrate.py`
is what carries an old-layout event's data across into this shape, copying
and verifying by checksum rather than moving, so a bad copy is caught
before anything old is ever deleted.

**Harvesting and rendering do not know each other.** Between them sits only
`clips.json` with timecodes. That is where a future moment-detector (Stage 2,
transcript-based) attaches without touching the renderer.

**`overlay.py` knows nothing channel-specific.** It draws the base veil, the
opaque edges, the optional logo and the hook. The channel's or event's own
decoration is an optional `decorate(draw, config, window_top, window_bottom)` in
a `layout.py`, which `profile.py` loads and passes through as `config["decorate"]`.
`build_overlay` keeps the signature `(hook, footer, config)` and merely calls
whatever it finds. This is why a new channel needs no code.

**Band opacity is applied to the image, not passed into the drawing.**
`brand.json`'s optional `bands` (`{"top": 1.0, "bottom": 1.0}`, event-
overridable like every other section) scales the alpha of the overlay's two
band SURFACES - the veil, the channel decoration and the edge accents.
`overlay._fade_bands` does it at exactly one point in `build_overlay`: after
those three are drawn and BEFORE the logo, hook and footer, when the image
holds nothing but surfaces. That position is the design. Threading a factor
into `config["decorate"]` instead would push the responsibility into every
channel's `layout.py`, where the next one written would silently forget it -
whereas fading the image reaches a decoration this module has never seen.

A factor of `1.0` is SKIPPED rather than applied as an identity multiply.
Measured, not assumed: with the skip removed the six pinned hashes still
match, so Pillow's identity multiply happens to be lossless today - the skip
buys independence from that, not the byte-identity itself, and saves a crop
and a paste on every render. An absent `bands`, an absent key and `1.0` are
all the same request, which is why no existing profile needed migrating. The
video window's rows are never touched at any factor: they are alpha 0 and
must stay so.
`profile.load` normalises `config["bands"]` AFTER validation, and not for the
reason it looks like: `overlay.band_opacities` cannot raise, it is
deliberately tolerant. The hazard is that it OVERWRITES `config["bands"]`
with its sanitised result, so calling it first would hand `_validate_brand`
an already-clean dict and every defect it exists to report would vanish -
`{"top": true}` would reach it as `{"top": 1.0}`. Moving that line above the
raise fails five of `TestBandValidation`'s tests.

**`palette.py` proposes a channel palette from its logo** - quantise the
opaque pixels, then assign the darkest sufficiently-common swatch to `base`
(the veil sits behind white text, so darkness outranks prominence), the most
CHROMATIC of the candidates that clear both a contrast floor against that
base and a share floor to `edge`, and white or near-black to `text` by WCAG
contrast against the chosen base.

Each of those three filters was added because the version without it picked
something absurd on a real logo, so do not simplify them away. Ranking by
`colorsys` saturation alone - the first version - is degenerate at BOTH
lightness extremes: it reports 1.00 for near-black and near-white alike, so
a 0.5%-share anti-aliasing fleck won `edge` on the ERF logo and produced a
frame with contrast 1.31 against its own base, i.e. invisible. The contrast
floor alone fixes only the black end. Chroma is not degenerate at either.
The share floor is what stops a fleck winning on colourfulness, and the last
resort tier - reached only when NOTHING clears the contrast floor - ranks by
contrast rather than chroma, because at that point no candidate is properly
visible and visibility is the only thing left worth maximising. It is pure and Pillow-only, like
`pathnames.py`; the studio calls in via `GET …/brand/palette` and never the
reverse. It is a PROPOSAL: the route writes nothing, the editor fills its
fields and offers the swatches as chips, and a mark with one colour yields
`base` and `text` alone rather than inventing the rest.

**`profile.py` is the layering.** It resolves `<channel>/<event>`, deep-merges the
event's `brand.json` over the channel's leaf by leaf, resolves fonts, assets and
`layout.py` event-first, and validates the result. Everything downstream receives
one flat `config` dict and never learns whether a value came from the event or the
channel.

Profile validation collects *all* defects and reports them together
(`ProfileError`), because someone typing a new profile should not need five runs
to find five typos. `REQUIRED_*` lists in `profile.py` define what is mandatory.

`timecode.with_padding` and `render.Source`'s `video_id` path with
`--download-sections` are deliberate, tested groundwork for Stage 2 and unused in
Stage 1. Do not remove them as dead code.

**Subtitles are an optional layer, attached at one point.** `transcribe.py`
turns a downloaded clip into cached words, `captions.py` groups those words into
short caption lines, and `subtitle_track.py` draws them as PNGs and concatenates
them into a transparent alpha track (this ffmpeg has no `libass`, so there is no
other way to get text on screen). `subtitle_pipeline.make_subtitle_provider` is
the only place that wires the three together and honours the editorial
correction, and hands the result to `render.build_short`; any failure anywhere
in that chain degrades to "no subtitles" for that one clip rather than failing
the render (see that function's own docstring). Both `cmd_render` in
`bin/yt-shorts` and the studio's job runner (`yt_shorts.studio.jobs`) call it -
neither keeps its own copy, so a studio-initiated render burns in the same
captions a CLI render would. `subtitle_pipeline.py` lives outside
`yt_shorts/studio/` on purpose: `bin/yt-shorts` needs it importable in a venv
that never installed FastAPI (see `studio.api`'s own docstring on why FastAPI
stays confined to that package).

**A word's text carries its own boundary, and the studio's text field cannot.**
faster-whisper marks the START of a word with a leading space, and
`captions._to_caption` joins the tokens with `""` to rely on exactly that -
which is what makes `" C"`, `".L"`, `".R."` render as `C.L.R.` instead of
`C .L .R.`. A human types `Rei`, not `" Rei"`, so a hand-corrected word used to
glue itself to its predecessor and render as `IT'SREIRACING`.
`editorial.normalise_word_boundaries` restores the boundary: a text beginning
with a letter or digit (`str.isalnum()`, Unicode-aware for the German names
this project is full of) gets exactly one leading space; empty, already-spaced
and punctuation-led text is returned untouched, which is both what keeps the
function idempotent and how a continuation is deliberately written. The
discriminator is measured, not assumed - across the 11518 decoder tokens in
this workspace's transcripts, 91 carry no leading space and every one of them
starts with `.` (71) or `-` (20). Not one starts with a letter or a digit, at
any index. Those counts are a snapshot of a corpus that keeps growing, so
re-measure rather than trusting the numbers; what has to hold is the property,
and a letter-led continuation token appearing one day would break the rule
rather than merely dating it.

It is called from the TWO routes where words arrive from a client - the words
`PATCH` and the preview `POST` - and deliberately NOT from `editorial.save`.
Both, because normalising only on save would show `IT'SREIRACING` in the live
preview while the rendered short said `IT'S REI RACING`; not `save`, because
`save` is handed a complete `Edit` and rewriting its content would make every
round-trip test lie about what it stores. The studio's own field DISPLAYS
`word.text.trim()` while keeping the RAW text in state - trimming into state
instead would make every word differ from its saved form and every clip would
open showing "Unsaved changes". `captions.py` is untouched by all of this,
which is also why the six pinned overlay hashes cannot move.

**The transcript editor can add and remove rows, and the timings it writes are
advisory.** Whisper drops words it cannot hear - on sung audio, whole phrases -
and it does NOT leave a gap where they belong: it stretches the last word it
recognised across them, which is how one word ends up spanning 7.5 seconds.
So `words.ts`'s `insertWordAfter` splits the target row's span in half rather
than filling the gap after it, because decoder timings are contiguous
(`words[i].end` IS `words[i+1].start`) and there is usually no gap at all. One
rule, no lookahead, works identically on the last row, and it cannot produce an
overlap. The split is only a SEED - both numbers stay editable, as they always
were.

`findWordProblems` flags a row whose `start` precedes the previous row's `end`,
or whose `end` precedes its own `start`. **Contiguous rows are clean** - that is
the decoder's normal shape, and flagging it would put a red border on every row
of every clip. It WARNS and never blocks: the data model permits such a list and
refusing to save would trap an operator in a state the tool itself let them
reach. Same stance as `quota`. There is deliberately no auto-sorting either -
re-ordering rows under the operator's cursor while they type is worse than the
problem it solves.

**But an overlap is NOT cosmetic, and the warning must keep saying so.**
`captions.group_words` will happily group an overlapping list, which is why an
earlier draft of this paragraph claimed "the renderer accepts it" - that was
false. `subtitle_track._validate` REFUSES a caption list that is unsorted or
overlapping, `subtitle_pipeline` catches the resulting `ValueError` in its
blanket degrade-to-"no subtitles" handler, and the clip renders with NO captions
at all rather than mistimed ones. The two flags are not equally serious either -
an `inverted` row is usually absorbed into its neighbour's caption and validates
fine; it is the `overlap` that costs the clip its subtitles. Anyone rewording
that Alert must keep the consequence in it.

**And the loss is now REPORTED, which for a while it was not.**
`make_subtitle_provider`'s notes used to be `print(..., file=sys.stderr)`. That
is fine for the CLI, whose operator is watching a terminal, and useless for the
studio, whose render runs on a background thread nobody reads stderr from - so a
studio render that lost every caption simply succeeded and said nothing,
anywhere. The notes now go through a `note()` helper that writes to BOTH an
injected `logger` and an optional `on_note` callback, because two different
people look in two different places:
- the logger is the durable record. It is named in the **`ytshorts.*`** tree,
  NOT `__name__` - `logsetup.configure_logging` sets up `ytshorts` with
  `propagate = False`, so a `yt_shorts.subtitle_pipeline` logger would sit in a
  different tree and reach neither the central log nor the CLI's TTY handler.
  `bin/yt-shorts` configures that tree on every invocation, so a CLI note still
  reaches the operator's terminal (on stdout now, via the TTY handler, rather
  than stderr) and additionally persists in `logs/yt-shorts.log`. The studio
  injects `job_logger(job)`, so the note lands in `logs/jobs/render-<id>.log`.
- `on_note` is for a caller that surfaces notes in a channel of its own.
  `studio.jobs._render_one` collects them and records them as the clip's
  `reason`, which `RenderPanel` already renders - so the operator sees it in the
  render panel, where they are actually looking. The clip stays `done`: the
  short really did render, and calling it failed would be a lie that also breaks
  the kept/rendered flow.

**Trimming deliberately does the opposite of the caption case above, and
that is not an oversight.** A `TrimError` raised after a render's
`build_short` already succeeded marks the clip `failed` in both render
paths (`studio.jobs._render_one` and `cmd_render`), and makes `cmd_render`
exit 1 - unlike a lost caption track, which stays `done` with the loss
recorded as a `reason`. The two cases look alike (something went wrong
after a good encode) but are not: a dropped caption is a degraded short -
still the video the operator asked for, watchable, uploadable, just
quieter - while an unapplied trim means the file actually on disk is NOT
what the operator asked for at all (still the full untrimmed render).
Calling that `done` would tell the same lie the
caption case is built to avoid, in the opposite direction: it would say
the operator's own instruction was carried out when it was not. A studio
operator sees this in the render panel's `reason`, same as a caption
loss; a CLI operator additionally learns it from the exit code, which is
the other reason `failed` is right here where it would be wrong there -
trimming has no "silently degrade and keep going" reading the way an
optional subtitle layer does.

All three rules are pure functions in `words.ts` beside `wordsEqual`, not in the
component, so they are unit-tested without rendering and Vite's fast-refresh
boundary stays component-only. Nothing server-side was needed for any of this:
`editorial` validates only that each word has `start`/`end`/`text`,
`transcript.based_on` is a checksum of the DERIVED words (conflict detection,
not a length constraint), and an empty word list is a legitimate state meaning
"this clip has no captions".

**The display trim has one sharp edge, and the obvious reading of it is
wrong.** Because the rendered value is trimmed while state holds the raw text,
a space typed at either END of a word survives only as the operator's LAST
keystroke: it is stored, but it is invisible in the field, and the very next
keypress reads back the trimmed DOM value and silently destroys it. The
tempting conclusion - that this is harmless because the server recomputes the
boundary anyway - is false, and measured false in a real browser. The server
recomputes it only for ALNUM-led text. For a correction that genuinely starts a
new word but begins with punctuation (`(pit)`, `"Rei"`, `#12`), a hand-typed
leading space is the ONLY boundary mechanism there is, and this is exactly the
input that makes it invisible and fragile. The supported way to write that case
is the one a word dict already allows: put both words into a SINGLE row's text,
which `captions.py` documents and which no trim can damage. Do not "simplify"
this on the premise that a typed boundary space has no job left to do - it has
one, in the punctuation-led case, and removing the escape hatch would leave a
silently glued caption with no way to fix it.

Note where the guard for all of this lives: reverting the trim, or moving it
into state, leaves `tsc`, `oxlint` and every Vitest case green, because this
project covers component behaviour by E2E (`tests/test_studio_e2e.py`'s
`test_a_hand_typed_word_keeps_its_boundary`), not by component tests.

### Where the rest of this document now lives

This file was 178k characters and every one of them loaded into every session.
The subsystem chapters were moved out WORD FOR WORD - not rewritten, because
this project's own record is that restating a rule here is how it becomes
false. Each destination says so at its top. What stays below is what has to be
true before you open any file: the seams above, the prohibitions here, the test
rules, and `## Hard constraints`.

| Subject | Now lives in | Loads when |
|---|---|---|
| The studio: G1-G4, the versioned short url, trimming, event/channel/brand/font CRUD, the stream view, the catalogue, the job queue, the worker, the Jobs screen, the bulk actions | `src/yt_shorts/studio/CLAUDE.md` | you work under `src/yt_shorts/studio/` |
| Whole-stream transcription, moment detection, the three model providers, the lexicon fallback, the glossary, the track packs | skill `detection-and-providers` | invoked, or read it directly |
| Upload to YouTube (stage E) | skill `upload-to-youtube` | invoked, or read it directly |
| Logging and observability (`logsetup.py`) | skill `logging-and-observability` | invoked, or read it directly |

**The prohibitions from those chapters stay HERE, because a "never do X" rule
must not sit in a file that might not be loaded when it matters.** Each is the
short form; the reasoning, the measurements and the history of how each one
was got wrong are in the file named above.

- **The studio never redefines transcribed data or an existing clip's WINDOW.**
  It edits `edit.json` (via `editorial.save`, the only writer); it may replace
  its own rendered short; it may create a clip from a picked window, or
  idempotently correct that same clip's hook/title on an EXACT re-pick - never
  its window, and never a colliding, genuinely different one. Do not restate
  this as a bare "never touches clip.json": that phrasing has been wrong six
  times.
- **A studio render takes the SAME `EventLock` `cmd_render` does**, acquired
  before the background job starts. This is what stopped a repeat of the
  incident that destroyed reference files (see `.superpowers/sdd/progress.md`,
  `stage-2a`).
- **Privacy defaults to `private`.** A non-private or scheduled upload happens
  only on an explicit, confirmed, per-upload operator choice, enforced on BOTH
  server and client. `manual` channels never API-upload. There is no
  auto-publish on any derived signal.
- **Secrets live in `<workspace>/auth/` and never touch the repo, a log, a
  queue entry or an HTTP response.** Every place external-tool text is written
  runs it through `logsetup.shorten_urls` first - there are five such sites and
  no exceptions. Every exception escaping a vendor SDK is wrapped in
  `ModelError` built from the TYPE NAME only, never the message, because the
  message can quote the request and the request carries the key.
- **One engine per run.** A window that fails mid-scan is recorded in
  `missing_windows` and does NOT fall back to the lexicon for that one window;
  falling back for a WHOLE run is different, and announces itself in the log.
  Two scoring scales in one moments list is a ranking that means two things at
  once.
- **`job_queue.py`, `subtitle_pipeline.py`, `upload_policy.py`, `pathnames.py`,
  `logsetup.py`, `trim.py`, `install_tools.py` and `youtube.py` must not import
  FastAPI**, and `logsetup.py` must not import anything from this project - the
  CLI runs in a venv that may have installed neither.
- **A queued upload is always private, refused at both ends**, and `upload` has
  no stop at any level - the UI must never offer one.
- **`enqueue` REFUSES a params key whose name looks like a secret, and refuses
  a nested dict at any depth.** A refusal, never a redaction.
- **A stop (`cancel.Stopped`) is never relabelled a failure**, and nothing in
  this project tries to kill a thread.

## Verifying changes

**Extracted frames are not proof.** `ffmpeg -i x.mp4 -frames:v 1 out.png` ignores
the sample aspect ratio, so a distorted video yields a correct-looking still. Six
such stills were reviewed before the SAR bug was noticed by a human watching the
actual video. To see what a player shows, apply the SAR:

```bash
ffprobe -v error -select_streams v \
  -show_entries stream=width,height,sample_aspect_ratio,display_aspect_ratio,pix_fmt \
  -of csv=p=0 file.mp4                      # must be 1080,1920,1:1,9:16,yuv420p
ffmpeg -v error -y -i file.mp4 -vf "scale=iw*sar:ih" -frames:v 1 /tmp/proof.png
```

**Refactors are held to byte-identical output.** Any change that is not meant to
alter appearance must leave the six reference overlays untouched, byte for byte.
This caught a subtle regression the test suite could not: keeping the hook's
original centering formula with a raised clamp is algebraically different from
re-centering in the remaining space, even when the logo reserves zero height.

```bash
PYTHONPATH=src .venv/bin/python -c "
from yt_shorts.profile import load
from yt_shorts.overlay import build_overlay
p = load('erf/community-clips-back-catalogue')
for name, hook in {'a':'WHAT IS HAPPENING?!?','b':'Jegr and the Barbie','c':'rei got sliced',
                   'd':'Forcing a SC','e':'Speedy!','f':'Jegr Tunes'}.items():
    build_overlay(hook, p.channel['footer'], p.config).save(f'/tmp/after/{name}.png')
"
```

`tests/test_event_layer_no_regression.py` pins this with SHA-256.

The overlay tests measure pixels — where white text actually lands, which alpha a
region carries — rather than asserting that a function returned. Keep that style:
the dimension-only assertions are what let the SAR bug through. When touching the
hook layout, the stress cases that matter are an 80-character single word, 100
words and 500 words, each with and without a logo.

## Language

The project is English: code, data fields, docs and folder names. German
survives only as a proper noun: YouTube video titles in clip data, and the
Nürburgring's own section names in `tracks.py`.
