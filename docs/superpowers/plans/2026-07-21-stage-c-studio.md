# Stage C — Studio Implementation Plan

**Goal:** A local editor for reviewing and correcting shorts before they are rendered: change a title, fix what Whisper misheard, keep or discard a clip, and see the result immediately instead of after a three-minute re-encode.

**Architecture:** A FastAPI app in its own package `src/yt_shorts/studio/`, started by `bin/yt-shorts studio <channel>/<event>`, serving a single dependency-free HTML page. The pipeline stays importable without it. The studio writes `edit.json` and nothing else.

**Tech Stack:** FastAPI 0.139, uvicorn 0.51, pydantic 2.13 (installed in `.venv`); Pillow and ffmpeg as before; vanilla JavaScript with no build step.

## Global Constraints

- `PYTHONPATH=src` is mandatory. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` — 430 tests pass at the start of this plan.
- **FastAPI is an optional dependency.** `bin/yt-shorts harvest|render|gallery|migrate` must keep working with it uninstalled; only `studio` may require it, and its absence must produce a readable message, not an `ImportError` traceback.
- **The studio writes `edit.json` and nothing else.** Never `clip.json`, never `transcript.json`. That is what keeps every derived artifact safe to delete.
- **`EventLock` guards rendering**, in the studio exactly as in the CLI. Two writers on one event is what destroyed reference files earlier in this project.
- **`setsar=1` stays the final step of the filter chain**; rendered output is unchanged by this stage.
- One failed clip never aborts a batch; subtitle failures degrade to "no subtitles".
- English only. Imperative commit messages. No modification of `.venv` beyond the FastAPI install, ffmpeg, or `<racecast-runtime>/` (read-only).
- Tests must not depend on `~/YT-Shorts-Data`; `tests/conftest.py` pins `profile.CHANNELS_DIR` to `tests/fixtures/channels`. Keep it that way.

---

## Task 1: Keep the raw clip

**Files:** `src/yt_shorts/render.py`, `bin/yt-shorts`, `tests/test_render.py`

`build_short` deletes `raw.mp4` after a successful build. The studio needs it: a preview frame must come from the *clean* picture, and `short.mp4` already has captions burned in.

- `build_short` gains `keep_raw: bool = False`, keyword-only. When true, the raw file survives a successful build; everything else about cleanup is unchanged.
- `cmd_render` passes `keep_raw=True` — from here on the raw clip is a cache, not scratch.
- Document the disk cost in the docstring and the README: roughly the size of the source clip per clip, and that deleting `raw.mp4` is always safe because it re-downloads.

Tests: raw survives with `keep_raw=True`, is removed without it, and a failed build still leaves everything in place for investigation.

Commit: `Keep the raw clip so a preview has a clean frame to draw on`

---

## Task 2: Preview rendering

**Files:** create `src/yt_shorts/preview.py`, `tests/test_preview.py`

The one piece of real logic in this stage, and the reason the editor is usable at all.

**Produces:** `preview.build(clip_dir, config, at: float, words, hook, footer, ffmpeg="ffmpeg") -> bytes` — a PNG of what the finished short looks like at time `at`.

- Extract one frame from `raw.mp4` at `at` with ffmpeg.
- Compose it exactly as `render.compose` does — blurred background, the sharp picture fitted into the window, the brand overlay — but in Pillow rather than ffmpeg, so it costs milliseconds.
- Draw the caption that `captions.group_words` puts at that timestamp, using `overlay.build_caption`, so the typography is the same code the encoder uses.
- Return PNG bytes. No file is written.

**The correctness question that matters:** the preview must agree with what `compose` later produces. It cannot be pixel-identical — one path is Pillow, the other ffmpeg's scaler — but the geometry must match: window position and size, caption position and size, footer. Test that by rendering a real short and comparing the preview against a frame extracted from it: the caption's bounding box must land within a few pixels.

Missing `raw.mp4` raises a clear error naming the clip; the caller turns it into a 409.

Commit: `Draw a preview frame without re-encoding the clip`

---

## Task 3: The API

**Files:** create `src/yt_shorts/studio/__init__.py`, `src/yt_shorts/studio/api.py`, `tests/test_studio_api.py`

FastAPI app built by `create_app(profile) -> FastAPI`. Tested with `fastapi.testclient.TestClient`; no server is started in tests.

- `GET /api/clips` — every clip: directory name, harvested title, effective title, status, whether a short exists, whether a transcript exists, whether `edit.json` exists, duration.
- `GET /api/clips/{name}` — the above plus the effective word list and whether the correction conflicts with the derived transcript.
- `PATCH /api/clips/{name}` — body may carry `title`, `status`, `words`. Writes `edit.json` through `editorial.save` **only**. Setting `words` records `based_on` from the current derived transcript. A `null` title clears the override.
- `GET /api/clips/{name}/preview?at=<seconds>` — PNG from Task 2. 409 with a readable message when `raw.mp4` is absent.
- `GET /api/clips/{name}/short` — streams `short.mp4`, 404 when absent.

Validation is pydantic models; an unknown clip is 404, a bad status is 422. Every route is tested for its success case and its failure case.

Commit: `Serve clips and their editorial layer over an API`

---

## Task 4: Render jobs

**Files:** create `src/yt_shorts/studio/jobs.py`, extend `api.py`, `tests/test_studio_jobs.py`

- `POST /api/render` — body names clips (or all non-discarded ones). Starts one background job, returns its id. **Refuses with 409 while a job is already running for this event**, and takes `EventLock` for the duration.
- `GET /api/jobs/{id}` — state (`running`/`done`/`failed`), per-clip results, and the collected log lines.
- A failing clip does not stop the job; its reason is recorded with its exception type, exactly as `cmd_render` does.
- Jobs live in memory and do not survive a restart. Say so in the docstring — it is a deliberate limit for a single-user local tool, not an oversight.

Tests use a stubbed `build_short`; no real render, no network.

Commit: `Run renders as background jobs the studio can poll`

---

## Task 5: The page

**Files:** create `src/yt_shorts/studio/static/index.html`, extend `api.py` to serve it

One page, vanilla JavaScript, no build step, no CDN.

- Left: the clip list with status badges, and a filter for discarded clips.
- Right, for the selected clip: video player, title field, status buttons, the transcript as editable word rows, and the preview image with a time slider.
- Editing a word updates the preview after a short debounce. Saving is explicit, not on every keystroke.
- A conflict is shown as a banner naming what happened, not a silent state.
- Render buttons: this clip, or all kept clips. Job progress polls `GET /api/jobs/{id}`.

Keep the styling minimal and legible. No framework.

Commit: `Add the studio page`

---

## Task 6: Wiring and documentation

**Files:** `bin/yt-shorts`, `README.md`, `CLAUDE.md`

- `bin/yt-shorts studio <channel>/<event>` starts uvicorn on a local port, prints the URL, and does not open a browser by itself.
- FastAPI missing produces: what to install, and the note that the rest of the tool works without it.
- README: a Studio section — what it does, how to start it, that the preview needs `raw.mp4` and how to get one, and that jobs do not survive a restart.
- CLAUDE.md: the studio package's boundary — it writes `edit.json` only, and it takes `EventLock` for renders.

Commit: `Document the studio and wire it into the CLI`

---

## Verification for the branch

- Full suite green, with FastAPI installed.
- **The CLI works with FastAPI uninstalled.** Verify in a scratch virtualenv without it: `harvest`, `render` and `gallery` import and run; `studio` reports what to install.
- Rendered output unchanged: `render.py`'s filter chain untouched apart from Task 1's cleanup flag.
- The preview agrees geometrically with a real rendered frame (Task 2).
