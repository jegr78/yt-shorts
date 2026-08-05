# Code- & Security-Review Fixes — Implementation Plan

> **For agentic workers:** executed inline in-session (executing-plans style) with atomic commits and a green suite between waves. Steps use checkbox (`- [ ]`) tracking.

**Goal:** Remediate all 21 findings from the 2026-07-23 full code + security review of YT-Shorts, keeping the 786-test pytest suite and the Vitest suite green, without changing rendered-overlay output.

**Architecture:** Small, targeted, defense-in-depth fixes grouped into atomic commits by subsystem. No dependency changes. The dominant theme is completing the "every path segment is validated before any filesystem touch" invariant on the routes that skipped it (the `profile.load`/`exec_module` surface), plus secret-at-rest hardening, subprocess-argument safety, and browser-CSRF hardening on the localhost studio.

**Tech Stack:** Python 3 (`PYTHONPATH=src`, no pyproject), FastAPI/uvicorn (optional dep), Pillow, faster-whisper, yt-dlp/ffmpeg subprocesses; React + Vite + Mantine + TypeScript frontend (Vitest), built output committed under `studio/static/`.

## Global Constraints (verbatim, from CLAUDE.md)

- `PYTHONPATH=src` is mandatory for every Python invocation; tests: `PYTHONPATH=src .venv/bin/pytest -q`.
- Rendered overlays must stay **byte-identical** — `tests/test_event_layer_no_regression.py` pins SHA-256; no fix here may change overlay output.
- FastAPI/google stay **lazy/optional** — never add a module-scope `import fastapi`/`import google...` outside the studio package / the designated lazy sites.
- Frontend logic lives in standalone modules (not exported from components); after any `web/src` change run `npm test` **and** `npm run build`, then commit the regenerated `studio/static/`.
- Privacy is always `private`; secrets never logged/committed; `manual` channels refused at all upload paths — do not weaken.

### Design decisions locked for this plan
- **yt-dlp:** restrict source URLs to `http`/`https` scheme + prepend `--` before the positional URL. No host allowlist (would break legitimate non-YouTube sources).
- **CSRF (M1):** an Origin-check middleware on mutating methods (POST/PUT/PATCH/DELETE): if `Origin` is present its hostname must be `127.0.0.1`/`localhost`, else 403; absent Origin passes (CLI, TestClient, curl). Blocks browser CSRF and DNS-rebinding without breaking tests.
- **Font cap (H3):** reject on `Content-Length > MAX_FONT_BYTES` and cap the streamed read, both before buffering.

---

## Wave 1 — Path/segment validation (root cause: H1, M2, L-list_events, media video_id)

### Task 1 — `profile.load` + studio helpers validate segments (H1)
**Files:** Modify `src/yt_shorts/profile.py` (`load`, ~468), `src/yt_shorts/studio/api.py` (`_load_profile` 238, `_load_clip_or_404` 277, `_channel_config` 265). Test: `tests/test_profile.py`, `tests/test_studio_api.py`.
- [ ] Test: `profile.load("../x/evt")`, `load("chan/..")`, `load("a/b/c")` raise `ProfileError`; a valid `erf/<event>` still loads.
- [ ] Test: studio `GET /api/channels/erf/events/..%2f../...` style + a clip name `..` return 400/404, not a traversal read.
- [ ] Impl: in `profile.load`, after the split, `validate_segment(channel_name, what="channel")` / `event_name` wrapped so `ValueError`→`ProfileError`. In `_load_profile`, validate both and raise `HTTPException(400)` before calling. In `_load_clip_or_404`, validate `name`. In `_channel_config`, validate `channel`.
- [ ] Run `pytest tests/test_profile.py tests/test_studio_api.py -q`; commit.

### Task 2 — `TokenStore.path` validates `channel_id` (M2)
**Files:** Modify `src/yt_shorts/auth.py` (`TokenStore.path` 30). Test: `tests/test_auth.py`.
- [ ] Test: `TokenStore(dir).path("../../etc/hosts")` raises `AuthError`/`ValueError`; a real `UC...` id works; connect/disconnect with a traversal id → error, no file touched outside `auth/`.
- [ ] Impl: `validate_segment(channel_id, what="channel id")` in `path()` (raise `AuthError`), so `load`/`save`/`forget` all inherit it.
- [ ] Run `pytest tests/test_auth.py tests/test_studio_api.py -q`; commit.

### Task 3 — `workspace_listing.list_events` validates channel (L-list)
**Files:** Modify `src/yt_shorts/workspace_listing.py` (54). Test: `tests/test_workspace_listing.py`.
- [ ] Test: `list_events(dir, "../..")` returns `[]` (or raises consistently), never enumerates outside.
- [ ] Impl: `validate_segment` guard at top; on invalid return `[]`.
- [ ] Run tests; commit.

### Task 4 — `stream_transcribe` validates `video_id` (media L4)
**Files:** Modify `src/yt_shorts/stream_transcribe.py` (`_stream_dir` ~59). Test: `tests/test_stream_transcribe.py`.
- [ ] Test: a `video_id` with `/`/`..` raises before building `streams/<id>`.
- [ ] Impl: `validate_segment(video_id, what="video id")` before `_stream_dir`/download URL.
- [ ] Run tests; commit.

---

## Wave 2 — Secret-at-rest (H2)

### Task 5 — token & auth dir written 0600/0700
**Files:** Modify `src/yt_shorts/auth.py` (`TokenStore.save` 37). Test: `tests/test_auth.py`.
- [ ] Test: after `save`, `auth/` mode == 0o700 and `token-*.json` mode == 0o600 (mask with 0o777).
- [ ] Impl: `self.auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)` then ensure `chmod(0o700)`; write via `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` (or `write_text` then `chmod(0o600)`).
- [ ] Run tests; commit.

---

## Wave 3 — Subprocess argument safety (H4, L3, youtube channel_url)

### Task 6 — http(s)-only URL guard + `--` terminator for yt-dlp
**Files:** New helper `require_http_url` in `src/yt_shorts/clipid.py` (URLs already live here) or `pathnames.py`; Modify `src/yt_shorts/harvest.py` (63), `render.py` (`ytdlp_command` 34), `youtube.py` (`list_streams` 34). Test: `tests/test_harvest.py`, `tests/test_render.py`, `tests/test_youtube.py`.
- [ ] Test: a `url`/`clip_url`/`channel_url` starting with `-` or a `file://`/non-http scheme is rejected before subprocess; a normal https URL builds a command whose arg list contains `"--"` immediately before the URL.
- [ ] Impl: `require_http_url(value)` raises `ValueError` unless scheme ∈ {http, https}; call it at each boundary; insert `"--"` before the positional URL in all three arg lists. (`video_id` branch already embeds in a full `https://…` string — leave.)
- [ ] Run tests; commit.

---

## Wave 4 — Studio HTTP surface (H3, M1, L1, L2)

### Task 7 — font upload size cap before buffering (H3)
**Files:** Modify `src/yt_shorts/studio/api.py` (`upload_font` ~375). Test: `tests/test_studio_api.py`.
- [ ] Test: POST font with `Content-Length` > 10 MB → 413 and body never fully read; a valid small font still uploads.
- [ ] Impl: check `request.headers["content-length"]` vs `font_admin.MAX_FONT_BYTES` → 413; then read via `request.stream()` accumulating with a hard cap, aborting past the limit.
- [ ] Run tests; commit.

### Task 8 — Origin CSRF guard middleware (M1)
**Files:** Modify `src/yt_shorts/studio/api.py` (`create_app`). Test: `tests/test_studio_api.py`.
- [ ] Test: POST with `Origin: http://evil.com` → 403; with `Origin: http://127.0.0.1:8765` → normal; with no Origin → normal; GET with foreign Origin → normal (reads unaffected).
- [ ] Impl: an `@app.middleware("http")` that, for method ∈ {POST,PUT,PATCH,DELETE} and a present `Origin`, parses host and 403s unless host ∈ {127.0.0.1, localhost}.
- [ ] Run tests; commit.

### Task 9 — connect dedupe + JobStore eviction (L1)
**Files:** Modify `src/yt_shorts/studio/jobs.py` (`JobStore`, `start_connect_job`). Test: `tests/test_studio_jobs.py`.
- [ ] Test: a second connect for a channel with one in-flight is refused (no second thread); `JobStore` caps/evicts finished jobs beyond a bound.
- [ ] Impl: track in-flight connect per channel; refuse duplicate; add a size/TTL eviction of terminal jobs to `JobStore`.
- [ ] Run tests; commit.

### Task 10 — generic error details, log server-side (L2)
**Files:** Modify `src/yt_shorts/studio/api.py` (`_load_edit_or_500` 285, `brand_preview` 430, `_load_channel` 261). Test: `tests/test_studio_api.py`.
- [ ] Test: a corrupt `edit.json` → 500 body contains no absolute workspace path.
- [ ] Impl: return generic `detail`; `logging.getLogger(__name__).warning(...)` the specifics.
- [ ] Run tests; commit.

---

## Wave 5 — Admin correctness (M3, L5, L6, L7)

### Task 11 — channel rename/delete acquire locks (M3)
**Files:** Modify `src/yt_shorts/channel_admin.py` (`rename_channel` 109, `delete_channel` 124). Test: `tests/test_channel_admin.py`.
- [ ] Test: rename/delete while an event lock is held → `ChannelAdminError(kind="locked")`; a held lock acquired *after* the pre-check still can't be clobbered (acquire each event lock for the op).
- [ ] Impl: acquire every event's `EventLock` for the duration (context-managed), releasing on exit; keep the friendly 409 mapping.
- [ ] Run tests; commit.

### Task 12 — font in-use check scans event brand overrides (L5)
**Files:** Modify `src/yt_shorts/font_admin.py` (`delete_font` 75). Test: `tests/test_font_admin.py`.
- [ ] Test: a font referenced only by `events/<e>/brand.json` `fonts.hook` → delete refused 409 `in_use`.
- [ ] Impl: also scan `events/*/brand.json` `fonts.hook`/`small` (casefold/samefile identity) in addition to channel brand.
- [ ] Run tests; commit.

### Task 13 — brand_admin `_validate` mirrors profile (L6)
**Files:** Modify `src/yt_shorts/brand_admin.py` (`_validate` 83). Test: `tests/test_brand_admin.py`.
- [ ] Test: a stored brand with a broken `output`/`logo`/`upload` is rejected by `update_brand` (not silently rewritten).
- [ ] Impl: run `profile._validate_brand/_validate_logo/_validate_upload` against the merged brand (lazy import already used for subtitles). Keep `output` unpatchable.
- [ ] Run tests; commit.

### Task 14 — migrate keys on canonical URL (L7)
**Files:** Modify `src/yt_shorts/migrate.py` (163, 194). Test: `tests/test_migrate.py`.
- [ ] Test: a transcript whose `source` differs from `clips.json` `url` only by a `?si=` param / trailing slash is mapped (not `unmapped`).
- [ ] Impl: key `by_url` on `clipid.canonical_url(...)` and look up with the same.
- [ ] Run tests; commit.

---

## Wave 6 — Pipeline correctness (L8, L9, L10, M6)

### Task 15 — harvest rejects end<start / negative duration (L8)
**Files:** Modify `src/yt_shorts/harvest.py` (`read_metadata` 42). Test: `tests/test_harvest.py`.
- [ ] Test: `read_metadata` with `section_end < section_start` (or `duration: -5`) → a `ClipEntry` with a descriptive `error`, not `None`.
- [ ] Impl: order/sign check after parsing → error entry.
- [ ] Run tests; commit.

### Task 16 — safe description templating (L9)
**Files:** Modify `src/yt_shorts/youtube_upload.py` (`build_metadata` 21). Test: `tests/test_youtube_upload.py`.
- [ ] Test: a template with an unknown `{placeholder}` or stray `{` raises a clear `UploadError` naming the template, not `KeyError`/`ValueError`.
- [ ] Impl: catch format errors → `UploadError`, or `format_map` with a guarded mapping (block attribute/index access).
- [ ] Run tests; commit.

### Task 17 — quota atomic write (L10)
**Files:** Modify `src/yt_shorts/quota.py` (`book_insert` 43). Test: `tests/test_quota.py`.
- [ ] Test: behavior unchanged; write goes through a temp file + `os.replace`.
- [ ] Impl: write temp then `os.replace`; (warning-only, no lock needed).
- [ ] Run tests; commit.

### Task 18 — moments O(words+ticks) sweep (M6)
**Files:** Modify `src/yt_shorts/moments.py` (`find_candidates`/`bin_words` 47). Test: `tests/test_moments.py`.
- [ ] Test: results **identical** to current output on the existing fixtures (add an equivalence assertion vs a small hand-computed set); scoring unchanged.
- [ ] Impl: sort words once by `start`, two-pointer window for per-tick counts, compute each bin once and reuse for rate + `_count_markers`.
- [ ] Run full `pytest tests/test_moments.py -q`; commit.

---

## Wave 7 — Frontend (M4, M5, L4-href, L11) + rebuild

### Task 19 — router decode try/catch + error boundary (M4)
**Files:** Modify `web/src/scopedApi.ts` (`parseRoute` 48), `web/src/main.tsx`. Test: `web/src/scopedApi.test.ts` (+ boundary).
- [ ] Vitest: `parseRoute("/%")` / malformed escape → falls back to raw segment / channels screen, does not throw.
- [ ] Impl: try/catch around `decodeURIComponent`; add a top-level `<ErrorBoundary>` in `main.tsx`.

### Task 20 — useJobPolling terminal on 404 / failure cap (M5)
**Files:** Modify `web/src/hooks/useJobPolling.ts` (30). Test: `web/src/hooks/useJobPolling.test.tsx`.
- [ ] Vitest: a 404 (`ApiError.status===404`) stops polling and surfaces an error state; N consecutive errors stop and surface.
- [ ] Impl: treat 404 as terminal; cap consecutive failures.

### Task 21 — uploaded-URL scheme check (L4)
**Files:** Modify `web/src/api.ts` (`extractUploadUrl` 640) or `UploadPanel.tsx` (213/245). Test: `web/src/api.extractUploadUrl.test.ts`.
- [ ] Vitest: a non-`http(s)` uploaded URL renders as plain text, not an `href`.
- [ ] Impl: verify `new URL(x).protocol` ∈ {http:, https:} before using as `href`.

### Task 22 — drop dead `title` arg (L11)
**Files:** Modify `web/src/components/StreamPanel.tsx` (56) or `App.tsx` handler. 
- [ ] Impl: consume `title` in the detect notification, or drop it from the signature + call site.

### Task 23 — rebuild + commit static
- [ ] `cd web && npm test` (all green) then `npm run build`.
- [ ] Commit regenerated `src/yt_shorts/studio/static/` together with the source (per CLAUDE.md).

---

## Verification (between waves + at end)
- After each Python wave: `PYTHONPATH=src .venv/bin/pytest -q` → all pass.
- After frontend: `npm test` + `npm run build`.
- Final: full `pytest -q` (expect ≥ 786 + new tests), Vitest green, `git log --oneline` shows atomic commits.
