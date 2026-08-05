# Closing the Review Minors Implementation Plan

> **STATUS: COMPLETE, merged into `master` as 65d2a41.** The
> unchecked `- [ ]` boxes below are the execution skill's per-step
> artefact and were never ticked; they are NOT open work. The
> authority on what was done is `git log` plus the ledger at
> `.superpowers/sdd/progress.md`. All six tasks landed, plus the whole-branch review's six Minor findings and two intermittent test failures whose causes are recorded in `CLAUDE.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every finding the `stream-playlists` reviews recorded and did not fix, so the branch's review record holds nothing that was merely noted.

**Architecture:** Eleven findings across four layers, grouped into five tasks by file and by test cycle. Two are behaviour-preserving refactors, three are one-line honesty fixes in the UI, three are missing tests, and three tighten an assertion or a key. Nothing here changes a contract.

**Tech Stack:** Python 3 (stdlib), FastAPI, pytest, React + Mantine + TypeScript, Vitest, Playwright (inside pytest).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q` (currently **2335 passed**).
- `python3 tools/lint.py` must exit 0 before every commit. Lint is not optional before trusting a green suite: a duplicate test-class name is dropped silently by pytest and only ruff's `F811` catches it.
- **Bare `npx tsc --noEmit` is INERT here.** `npm run build` (which runs `tsc -b`) is the real type-check. Run it from `src/yt_shorts/studio/web/`.
- The built frontend at `src/yt_shorts/studio/static/` is **committed**. Every task that changes `web/src/` ends with `npm run build` and commits `static/` — the Playwright E2E serves from it.
- `npm test` (Vitest, jsdom, currently **532 passed**) runs from `src/yt_shorts/studio/web/`.
- Pure modules stay pure: `youtube.py` and `detect.py` import no FastAPI; `streams.ts` and `jobs.ts` import no React.
- **NEVER use `git checkout`, `git restore` or `git stash` to undo a change.** Standing rule in this repository — it has destroyed unsaved work here twice. Copy aside with `cp`, restore with `cp`, verify with `diff` plus `git diff --quiet HEAD -- <file>`.
- macOS: no bare `timeout`. Use `gtimeout` or the Bash tool's own timeout.
- E2E is slow (~80 s). Run it in the background and collect the result rather than blocking.

## What is being closed

Recorded in `.superpowers/sdd/progress.md` as M1–M13. **M10 and M11 were already fixed** by the whole-branch review's fix wave (`45ac06d`) — verified: `rowsRef` seeds `settled` in `useQueuedEntries.ts`, and `busyLegs.size > 0` gates the bar's buttons. The remaining eleven are below.

| | Finding | Task |
| --- | --- | --- |
| M1 | no test for a video in two playlists | 1 |
| M2 | a video listed twice in one playlist over-counts `Playlist.count` | 1 |
| M3 | `list_playlists` does not validate the id it parses | 1 |
| M4 | `detect.py` has three copies of validate-then-join | 2 |
| M5 | argument order inconsistent between the new and old helpers | 2 |
| M6 | effect key `ids.join(',')` collides if an id contains a comma | 3 |
| M7 | the same id set in a different order restarts the poll | 3 |
| M8 | the playlist `Select` shows the raw sentinel `*all*` while loading | 4 |
| M9 | `FailedPlaylist.reason` is carried on the wire and displayed nowhere | 4 |
| M12 | the skip-note E2E assertion uses `.first` | 5 |
| M13 | an E2E `get_by_text("Transcript")` matches as a substring | 5 |

---

## File Structure

**Modify:**
- `src/yt_shorts/youtube.py` — dedupe a repeated member (M2)
- `tests/test_youtube.py` — M1, M2, M3
- `src/yt_shorts/detect.py` — one path helper instead of three, one argument order (M4, M5)
- `src/yt_shorts/studio/api.py` — two call sites follow the new order (M5)
- `tests/test_detect.py` — call sites follow the new order, plus a test that the three helpers agree (M4, M5)
- `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts` — the effect key (M6, M7)
- `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.test.tsx` — pins M7
- `src/yt_shorts/studio/web/src/components/StreamPanel.tsx` — the loading label and the failure reason (M8, M9)
- `tests/test_studio_e2e.py` — two tightened assertions (M12, M13)

**Deliberately NOT touched:** `stream_transcribe.py`'s own `_stream_dir` is a FOURTH copy of the same path build, and it stays. `detect` imports `stream_transcribe`, so the dependency cannot run the other way without a cycle. Task 2 records that in a comment rather than leaving the next reader to rediscover it.

---

### Task 1: `youtube.py` — the catalogue's three loose ends

**Files:**
- Modify: `src/yt_shorts/youtube.py` (`list_playlist_videos`)
- Test: `tests/test_youtube.py`

**Interfaces:**
- Consumes: the existing `Playlist`, `Video`, `PlaylistContents`, `Catalogue`, `list_playlists`, `list_playlist_videos`, `channel_catalogue`, `_PLAYLIST_ID`, `YouTubeError`.
- Produces: no new names. `list_playlist_videos` gains duplicate-collapsing; the rest is tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_youtube.py`. Note the two new module-level constants go beside the existing `PLAYLIST_LINES`/`MEMBER_LINES`:

```python
# The same video listed twice inside ONE playlist. yt-dlp does not
# ordinarily produce this - YouTube rejects a duplicate in a playlist - but
# the parser must not turn it into a size of 2.
DUPLICATE_MEMBER_LINES = "\n".join(json.dumps(d) for d in [
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
    {"id": "xQlD7MkC-Eo", "title": "ERF 24h Nürburgring 2026 | Part 3",
     "duration": 28431, "view_count": 1800},
])

# A playlists tab carrying an id that could alter the URL it is
# interpolated into. Never observed from YouTube; this pins what happens if
# it ever is.
HOSTILE_PLAYLIST_LINES = "\n".join(json.dumps(d) for d in [
    {"_type": "url", "id": "PLaaa", "title": "2026 Nürburgring 24 Hour"},
    {"_type": "url", "id": "PL&list=somebody-elses", "title": "Hostile"},
])


class TestAVideoInSeveralPlaylists:
    """`playlist_ids` is a LIST, and this is the property it is a list FOR.

    No ERF video sits in two playlists today - that is an observation of one
    channel on one day, recorded as such in `channel_catalogue`'s docstring,
    and exactly the kind of observation a later change would quietly encode
    as a guarantee. This pins the general case instead.
    """

    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": PLAYLIST_LINES,
            # Both playlists hold the SAME video.
            "list=PLaaa": MEMBER_LINES,
            "list=PLbbb": MEMBER_LINES,
        })

    def test_both_playlists_are_recorded_on_the_one_video(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["xQlD7MkC-Eo"].playlist_ids == ["PLaaa", "PLbbb"]

    def test_the_video_appears_once_in_the_union(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        ids = [v.video_id for v in catalogue.videos]
        assert ids.count("xQlD7MkC-Eo") == 1

    def test_a_playlist_only_video_in_two_playlists_is_also_recorded_once(self):
        # newvid0001 is NOT in the Streams tab, so it enters the union from a
        # playlist fetch - the branch that CREATES the Video rather than
        # finding it. Both memberships must still land on the one object.
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        by_id = {v.video_id: v for v in catalogue.videos}
        assert by_id["newvid0001"].playlist_ids == ["PLaaa", "PLbbb"]
        assert [v.video_id for v in catalogue.videos].count("newvid0001") == 1


class TestADuplicateInsideOnePlaylist:
    def test_it_is_collapsed_rather_than_counted_twice(self):
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(DUPLICATE_MEMBER_LINES))
        assert [v.video_id for v in contents.videos] == ["xQlD7MkC-Eo"]

    def test_it_is_not_reported_as_unavailable(self):
        """A duplicate is NOT a loss and must not be counted as one.

        `unavailable` says "this playlist holds a video you cannot have" -
        a deleted or private entry. Collapsing a repeat of a video that IS
        in the list loses nothing an operator could act on, so counting it
        there would make the dropdown claim a loss that did not happen.
        """
        contents = list_playlist_videos(
            "PLaaa", runner=runner_returning(DUPLICATE_MEMBER_LINES))
        assert contents.unavailable == 0

    def test_the_playlist_size_matches_what_is_offered(self):
        runner = runner_for({
            "/streams": "", "/playlists": PLAYLIST_LINES,
            "list=PLaaa": DUPLICATE_MEMBER_LINES, "list=PLbbb": "",
        })
        catalogue = channel_catalogue(CHANNEL, runner=runner)
        sizes = {p.id: p.count for p in catalogue.playlists}
        assert sizes["PLaaa"] == 1


class TestAnUnusablePlaylistIdFromTheTab:
    """The id `list_playlists` reads is not validated there, and this pins
    what makes that safe rather than leaving it an accident of call order.

    `list_playlist_videos` validates, `channel_catalogue` catches, and the
    playlist is REPORTED as failed - so a hostile id never reaches a URL and
    never silently shrinks the catalogue either.
    """

    def _runner(self):
        return runner_for({
            "/streams": LINES,
            "/playlists": HOSTILE_PLAYLIST_LINES,
            "list=PLaaa": MEMBER_LINES,
        })

    def test_no_url_is_ever_built_from_it(self):
        runner = self._runner()
        channel_catalogue(CHANNEL, runner=runner)
        assert not any("somebody-elses" in url for url in runner.calls)

    def test_it_is_reported_as_a_failed_playlist_not_dropped(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [p.id for p in catalogue.playlists] == ["PLaaa"]
        assert [f.title for f in catalogue.failed_playlists] == ["Hostile"]

    def test_the_rest_of_the_catalogue_is_served(self):
        catalogue = channel_catalogue(CHANNEL, runner=self._runner())
        assert [v.video_id for v in catalogue.videos] == [
            "xQlD7MkC-Eo", "2O_lQrxEHWo", "Esm9vv5-PdU", "newvid0001"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: `TestADuplicateInsideOnePlaylist` fails (two videos, size 2). The other two classes may already pass — that is fine and is the point of M1 and M3, which are missing-test findings rather than defects. **Report which passed on the first run**, because a test that was green before the change is evidence about the code, not about your work.

- [ ] **Step 3: Collapse a duplicate member**

In `src/yt_shorts/youtube.py`, inside `list_playlist_videos`, add a seen-set and extend the docstring:

```python
    An entry with no title is a deleted or private video: it is dropped
    (nothing can ever be transcribed from it) and COUNTED, so a size shown
    to an operator is never quietly smaller than the playlist itself.

    A video listed TWICE in the same playlist is collapsed to one and is
    deliberately NOT counted as `unavailable`. The two drops are different
    in kind: a titleless entry is a video the operator cannot have, which is
    a loss worth reporting, while a repeat of a video that is already in the
    list costs them nothing. Counting it would make the dropdown report a
    loss that did not happen - the same dishonesty as hiding a real one,
    pointed the other way.
```

and in the loop, after the `title` check:

```python
    videos: list[Video] = []
    seen: set[str] = set()
    unavailable = 0
    for line in output.splitlines():
        ...
        if not entry.get("title"):
            unavailable += 1
            continue
        if entry["id"] in seen:
            continue        # a repeat of a video already offered - see above
        seen.add(entry["id"])
        videos.append(Video(...))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube.py -q`
Expected: PASS, every test in the file.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/youtube.py tests/test_youtube.py
git commit -m "fix(youtube): collapse a duplicate playlist member, and pin two properties

A video listed twice in one playlist made Playlist.count claim a size the
list does not have. Collapsed, and deliberately NOT counted as
unavailable: a repeat costs the operator nothing, unlike a deleted entry.

Plus the two properties that were correct-by-reading and untested: a
video in two playlists keeps both memberships and appears once, and an
unusable playlist id from the tab never reaches a URL - it is reported as
a failed playlist rather than silently dropped."
```

---

### Task 2: `detect.py` — one path helper, one argument order

**Files:**
- Modify: `src/yt_shorts/detect.py`
- Modify: `src/yt_shorts/studio/api.py:1903-1904`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `validate_segment`, `ANALYSIS_FILENAME`, `WINDOWS_DIRNAME`, `TRANSCRIPT_FILENAME`.
- Produces: `stream_dir(workspace_dir, video_id)`, `has_cached_transcript(workspace_dir, video_id)`, `has_analysis(workspace_dir, video_id)` — **all three with `workspace_dir` FIRST**. `analysis_path` and `windows_dir` keep their existing signatures and are rebuilt on `stream_dir`.

**Why this order:** `analysis_path(workspace_dir, video_id)`, `windows_dir(workspace_dir, video_id)` and `stream_transcribe._stream_dir(workspace_dir, video_id)` all put the workspace first. The three helpers added by the playlist branch are the outliers, so they move.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_detect.py`:

```python
class TestTheStreamPathHelpersAgree:
    """One path build, reached three ways.

    `analysis_path` and `windows_dir` predate `stream_dir` and each carried
    its own `validate_segment` + join. Three copies of one rule is three
    chances for one of them to drift; this pins that they cannot.
    """

    def test_every_helper_builds_under_the_same_stream_directory(self, tmp_path):
        base = detect.stream_dir(tmp_path, "vid123")
        assert detect.analysis_path(tmp_path, "vid123") == \
            base / detect.ANALYSIS_FILENAME
        assert detect.windows_dir(tmp_path, "vid123") == \
            base / detect.WINDOWS_DIRNAME

    @pytest.mark.parametrize("call", [
        lambda ws: detect.stream_dir(ws, "../../auth"),
        lambda ws: detect.analysis_path(ws, "../../auth"),
        lambda ws: detect.windows_dir(ws, "../../auth"),
    ])
    def test_each_one_still_validates_its_own_segment(self, tmp_path, call):
        """Routing through `stream_dir` must not become BORROWING its guard.

        CLAUDE.md's rule is that a write op validates its own path segment
        rather than relying on a caller having done it. Building on a helper
        that validates keeps that true - the check still runs inside the
        call, before any filesystem touch - and this is what pins it, so the
        refactor cannot be read as having dropped the rule.
        """
        with pytest.raises(ValueError):
            call(tmp_path)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py::TestTheStreamPathHelpersAgree -q`
Expected: FAIL — `stream_dir(tmp_path, "vid123")` raises `ValueError` today, because the current signature takes `video_id` first and a `PosixPath` is not a safe segment.

- [ ] **Step 3: Flip the order and build the two old helpers on `stream_dir`**

In `src/yt_shorts/detect.py`:

```python
def stream_dir(workspace_dir, video_id: str) -> Path:
    """Where a stream's derived data lives - the one place that name is
    built, and the one place `video_id` is validated for it.

    `workspace_dir` comes FIRST, matching `analysis_path`, `windows_dir` and
    `stream_transcribe._stream_dir`. Raises ValueError for an id that is not
    one safe path segment, which is what lets the helpers below build on it
    without borrowing anyone's guard.
    """
    validate_segment(video_id, what="video id")
    return Path(workspace_dir) / "streams" / video_id


def has_cached_transcript(workspace_dir, video_id: str) -> bool:
    ...
    return _has(workspace_dir, video_id, TRANSCRIPT_FILENAME)


def has_analysis(workspace_dir, video_id: str) -> bool:
    ...
    return _has(workspace_dir, video_id, ANALYSIS_FILENAME)


def _has(workspace_dir, video_id: str, filename: str) -> bool:
    try:
        directory = stream_dir(workspace_dir, video_id)
    except ValueError:
        return False  # names no file this workspace could hold
    return (directory / filename).is_file()
```

and replace the two older bodies, keeping their reasoning but stating the new mechanism:

```python
def analysis_path(workspace_dir: str | Path, video_id: str) -> Path:
    # Built on `stream_dir`, which validates - NOT borrowing a caller's
    # guard, which is the thing CLAUDE.md's rule forbids. video_id becomes
    # a path segment and arrives from the URL path of
    # POST …/streams/{video_id}/detect, which does not validate it itself;
    # the check still runs inside this call, before any filesystem touch,
    # exactly as it did when this function spelled it out. What changed is
    # that the rule now lives in ONE place instead of three, so the three
    # cannot drift apart. Raises ValueError. (stream_transcribe._stream_dir
    # is a fourth copy and stays one: `detect` imports `stream_transcribe`,
    # so it cannot import back without a cycle.)
    return stream_dir(workspace_dir, video_id) / ANALYSIS_FILENAME


def windows_dir(workspace_dir: str | Path, video_id: str) -> Path:
    """Where a stream's SCORED windows are cached, beside its chunks.

    Validates through `stream_dir` for exactly the reason `analysis_path`
    does, and by the same mechanism: the guard runs inside this call rather
    than being assumed of a caller. Raises ValueError.
    """
    return stream_dir(workspace_dir, video_id) / WINDOWS_DIRNAME
```

Update `require_cached_transcript`'s path line to the new order:

```python
    path = stream_dir(workspace_dir, video_id) / TRANSCRIPT_FILENAME
```

- [ ] **Step 4: Update the two route call sites**

In `src/yt_shorts/studio/api.py` (around line 1903):

```python
                 "has_transcript": has_cached_transcript(root, video.video_id),
                 "has_analysis": has_analysis(root, video.video_id)}
```

- [ ] **Step 5: Update the existing helper tests to the new order**

In `tests/test_detect.py`'s `TestWhatAStreamAlreadyHas`, every call swaps its two arguments — e.g. `detect.has_cached_transcript(tmp_path, "vid123")`, `detect.stream_dir(tmp_path, "../etc")`. Change only the argument order; do not touch what any test asserts.

- [ ] **Step 6: Run the affected suites**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_detect.py tests/test_studio_api.py -q`
Expected: PASS. `tests/test_studio_api.py` is what catches a missed route call site.

- [ ] **Step 7: Full suite, lint, commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q      # 2335 expected
python3 tools/lint.py
git add src/yt_shorts/detect.py src/yt_shorts/studio/api.py tests/test_detect.py
git commit -m "refactor(detect): one stream-path build, one argument order

analysis_path, windows_dir and stream_dir each carried their own
validate_segment plus the same join - three chances for one to drift.
The two older ones now build on stream_dir, which validates, so the
guard still runs inside each call: this is not borrowing a caller's
guard, which is the thing the rule actually forbids, and a new test pins
that all three still refuse a traversal segment.

stream_dir/has_cached_transcript/has_analysis take workspace_dir FIRST
now, matching analysis_path, windows_dir and
stream_transcribe._stream_dir. The three added by the playlist branch
were the outliers."
```

---

### Task 3: `useQueuedEntries` — an effect key that cannot collide or churn

**Files:**
- Modify: `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts`
- Test: `src/yt_shorts/studio/web/src/hooks/useQueuedEntries.test.tsx`

**Interfaces:**
- Consumes/produces: no signature change. `useQueuedEntries(ids)` and `useQueuedJob(id)` keep their exact shapes.

- [ ] **Step 1: Write the failing test**

Append inside `describe('useQueuedEntries', …)` in `hooks/useQueuedEntries.test.tsx`:

```tsx
  it('does not restart polling when the same ids arrive in a different order', async () => {
    // The effect keys on the id SET, not on the array. A caller that
    // rebuilds its list - App derives `streamEntryIds` from Object.values
    // of a map it mutates - can hand over the same ids in another order,
    // and restarting would drop every id's known state for a poll and
    // re-fetch the whole plan for nothing.
    const listJobs = vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ running: [entry({ id: 'a', state: 'running' })],
             queued: [entry({ id: 'b' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids), { initialProps: { ids: ['a', 'b'] } })
    await act(async () => { await Promise.resolve() })
    const callsBefore = listJobs.mock.calls.length

    rerender({ ids: ['b', 'a'] })
    await act(async () => { await Promise.resolve() })

    expect(listJobs.mock.calls.length).toBe(callsBefore)
    expect(result.current.byId.a.running).toBe(true)
  })

  it('starts clean for a genuinely different id set', async () => {
    // The other half, so the fix above cannot be "never restart".
    vi.spyOn(api, 'listJobs').mockResolvedValue(
      plan({ queued: [entry({ id: 'a' }), entry({ id: 'c' })] }))
    const { result, rerender } = renderHook(
      ({ ids }) => useQueuedEntries(ids), { initialProps: { ids: ['a'] } })
    await act(async () => { await Promise.resolve() })
    expect(result.current.byId.a.entry?.id).toBe('a')

    rerender({ ids: ['c'] })
    expect(result.current.byId.c.entry).toBeNull()
  })
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd src/yt_shorts/studio/web && npm test -- useQueuedEntries.test.tsx`
Expected: the reorder test FAILS — the key changes, the effect re-runs, and `listJobs` is called again.

- [ ] **Step 3: Key on the sorted set, unambiguously**

In `hooks/useQueuedEntries.ts`, replace the key line:

```ts
  // The effect keys on the id SET, and on nothing else about the array.
  //
  // Sorted, because a caller can hand over the same ids in another order -
  // App derives `streamEntryIds` from `Object.values` of a map it mutates -
  // and restarting the poll for that would drop every id's known state for
  // a tick and re-fetch the whole plan for no new information.
  //
  // JSON, not `join(',')`, because a join is ambiguous by construction:
  // `['a,b']` and `['a','b']` produce the same string. Ids are server-minted
  // uuid4 hex today and cannot contain a comma - which is exactly the kind
  // of "cannot happen" that a later id format change turns into a silent
  // collision, where two different tracked sets would share one effect and
  // one set's state would be served for the other's.
  const key = JSON.stringify([...ids].sort())
```

- [ ] **Step 4: Run both hook suites**

Run: `cd src/yt_shorts/studio/web && npm test -- useQueuedEntries.test.tsx useQueuedJob.test.tsx`
Expected: PASS. **`useQueuedJob.test.tsx` must pass unedited** — it is the proof the wrapper's rules are untouched.

- [ ] **Step 5: Prove the reorder test is not vacuous**

```bash
cp src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts /tmp/uqe.bak
# change the key back to ids.join(',') by hand
cd src/yt_shorts/studio/web && npm test -- useQueuedEntries.test.tsx
# expect the reorder test to FAIL
cp /tmp/uqe.bak src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts
diff /tmp/uqe.bak src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts
git diff --quiet HEAD -- src/yt_shorts/studio/web/src/hooks/useQueuedEntries.ts
```

Report which tests went red under the mutation. **Restore with `cp`, never `git checkout`.**

- [ ] **Step 6: Build, full Vitest, commit**

```bash
cd src/yt_shorts/studio/web && npm test && npm run build && cd -
python3 tools/lint.py
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "fix(studio-web): an effect key that cannot collide or churn

ids.join(',') is ambiguous by construction - ['a,b'] and ['a','b'] make
the same string - and it changed when the same ids arrived in another
order, restarting the poll and dropping every id's state for a tick.
Sorted JSON fixes both, and a test pins the reorder half."
```

---

### Task 4: `StreamPanel` — two things the operator should see

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/StreamPanel.tsx`

**Interfaces:**
- Consumes: `playlistOptions`, `ALL_STREAMS`, `StreamCatalogue['failed_playlists']`.
- Produces: no new exports.

- [ ] **Step 1: Give the filter a label before the catalogue arrives**

`options` is `[]` until the fetch resolves, while `value` is already `ALL_STREAMS` — so Mantine's `Select` has no option to derive a label from and shows the raw sentinel. Replace the `options` derivation:

```ts
  // Until the catalogue arrives there are no options, and a Select whose
  // value matches none of them renders the raw sentinel (`*all*`) at the
  // operator. One placeholder option carries the label through that window;
  // the count is left off deliberately rather than guessed, since nothing
  // has been counted yet.
  const options = catalogue
    ? playlistOptions(catalogue)
    : [{ value: ALL_STREAMS, label: 'All streams', count: null as number | null }]
```

**Do NOT change the render.** An earlier draft of this step had the `data`
mapping append the count itself — that was wrong, and applying it would have
shipped "All streams (25) (25)". `playlistOptions` already folds the count
into `label` via `withCount` (including the " + N unavailable" form), and
`PlaylistOption.label`'s own doc comment says a caller must not append a
count on top of it. The existing `label: option.label` therefore already
handles both cases correctly: the placeholder carries a bare "All streams",
and a real option carries its complete label.

`count` is kept on the placeholder object only so the local array's shape
matches `PlaylistOption`. If `PlaylistOption['count']` is typed `number`,
widen the LOCAL array's type at the call site rather than changing
`streams.ts` — the pure module's contract should not gain a null for a
rendering concern. Nothing reads that field in this component.

- [ ] **Step 2: Show WHY a playlist could not be loaded**

The alert names each failed playlist and drops `reason`, which is the only thing that says whether to retry or to look elsewhere. Replace its body:

```tsx
          <Stack gap={4}>
            {catalogue.failed_playlists.map((failure) => (
              <Text size="xs" key={failure.title}>
                <strong>{failure.title}</strong> — {failure.reason}
              </Text>
            ))}
            <Text size="xs" c="dimmed">
              Their streams may be missing from this list unless another
              playlist also holds them. Refresh to try again.
            </Text>
          </Stack>
```

The reason is built from an exception's own message in `channel_catalogue`, which for this module is a yt-dlp failure against a public playlist URL — no token, no key, nothing from `auth/` (see `youtube.py`'s docstring: no API key, no OAuth anywhere in it).

- [ ] **Step 3: Build and check the E2E still passes**

Run: `cd src/yt_shorts/studio/web && npm run build`
Expected: no TypeScript errors.

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q` (background it)
Expected: 123 passed. `test_a_failed_playlist_is_named_rather_than_silently_missing` asserts on the title, which the new markup still renders.

- [ ] **Step 4: Commit with the built output**

```bash
python3 tools/lint.py
git add src/yt_shorts/studio/web/src src/yt_shorts/studio/static
git commit -m "fix(studio-web): a filter label while loading, and why a playlist failed

The Select showed its raw sentinel (*all*) until the catalogue arrived,
because it had no option to take a label from. And FailedPlaylist.reason
was carried on the wire and rendered nowhere - the alert named the
playlist but not whether to retry or look elsewhere."
```

---

### Task 5: two E2E assertions that could pass on a broken build

**Files:**
- Modify: `tests/test_studio_e2e.py`

- [ ] **Step 1: Pin the skip note on BOTH action rows**

`test_a_stream_that_already_has_a_transcript_is_skipped_and_said_so` uses `.first` on a note that legitimately renders twice — beside "Queue transcription for selected" and beside "Queue transcription and detection for selected". So it would still pass if the note stopped rendering beside one of them. Replace the `.first` assertion:

```python
        # The note renders beside BOTH action rows that skip this leg -
        # the transcribe-only one and the transcribe+detect one - so `.first`
        # would have kept passing if it stopped rendering beside either.
        # Counting is what pins it; the note naming the LEG rather than the
        # video is the whole reason it appears twice (see streams.ts).
        note = page.get_by_text(re.compile(
            r"1 transcription skipped: already transcribed", re.I))
        expect(note).to_have_count(2)
        expect(note.first).to_be_visible()
```

- [ ] **Step 2: Make the badge assertion exact**

In the same test, `expect(page.get_by_text("Transcript")).to_be_visible()` matches as a case-insensitive SUBSTRING, so it also matches "transcription" — safe today only because the bulk bar is not yet rendered at that point in the test, and a strict-mode error rather than a silent pass if it were. Make it exact:

```python
        # `get_by_text` is a case-insensitive SUBSTRING match by default, so
        # this would also match "transcription" - which the bulk bar renders
        # a few lines below, once a row is ticked. Exact, so a later addition
        # cannot turn this into a strict-mode error in a test that is about
        # the row's badge.
        expect(page.get_by_text("Transcript", exact=True)).to_be_visible()
```

- [ ] **Step 3: Run it**

Run: `PYTHONPATH=src .venv/bin/pytest "tests/test_studio_e2e.py::TestPlaylistFilterAndBulkQueueing" -q`
Expected: 7 passed. If `to_have_count(2)` fails, **do not change it to the observed number** — find out how many rows render the note and why, and report it.

- [ ] **Step 4: Prove the tightened assertion bites**

```bash
cp src/yt_shorts/studio/web/src/streams.ts /tmp/streams.bak
# make bulkPlan's `note` return null for the 'both' action only, by hand
cd src/yt_shorts/studio/web && npm run build && cd -
PYTHONPATH=src .venv/bin/pytest "tests/test_studio_e2e.py::TestPlaylistFilterAndBulkQueueing" -q
# expect the skip-note test to FAIL on the count
cp /tmp/streams.bak src/yt_shorts/studio/web/src/streams.ts
diff /tmp/streams.bak src/yt_shorts/studio/web/src/streams.ts
cd src/yt_shorts/studio/web && npm run build && cd -
git status --short          # must be clean
```

Report what went red. **Restore with `cp`, never `git checkout`.**

- [ ] **Step 5: Full suite, lint, commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q      # 2335 expected
python3 tools/lint.py
git add tests/test_studio_e2e.py
git commit -m "test(studio-e2e): two assertions that could pass on a broken build

The skip note renders beside both action rows that skip that leg, so
.first would have kept passing if it stopped rendering beside either -
counted now. And get_by_text('Transcript') matched as a substring,
including the bar's own 'transcription'; exact now."
```

---

### Task 6: record what was closed

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Record the two rules that came out of this**

Two of these findings are rules rather than fixes, and belong where the next author reads them. Add to `CLAUDE.md` beside the existing catalogue and hook sections:

```markdown
**Two drops, two different answers, and the difference is the rule.**
`list_playlist_videos` COUNTS a titleless entry as that playlist's
`unavailable` and does NOT count a video listed twice, which it simply
collapses. The test that pins this is not pedantry: `unavailable` means
"this playlist holds a video you cannot have", so counting a duplicate
there would report a loss that did not happen - the same dishonesty as
hiding a real one, pointed the other way. Anything else this parser ever
drops has to be sorted into one of those two boxes before it is written.

**Building a path helper on another one is not borrowing its guard.**
`analysis_path` and `windows_dir` are `stream_dir(...) / <name>` now, and
each used to spell out its own `validate_segment`. The rule they carried -
a write op validates its own path segment rather than relying on a caller
having done it - is unchanged and still true: the check runs inside the
call, before any filesystem touch. What changed is that it lives in one
place instead of three. `tests/test_detect.py`'s
`TestTheStreamPathHelpersAgree` pins both halves, so the refactor cannot
later be read as having dropped the rule. `stream_transcribe._stream_dir`
is a fourth copy and stays one: `detect` imports `stream_transcribe`, so
it cannot import back without a cycle.
```

- [ ] **Step 2: Verify the whole tree one last time**

```bash
PYTHONPATH=src .venv/bin/pytest -q      # 2335 + the tests added above
python3 tools/lint.py
cd src/yt_shorts/studio/web && npm test && npm run build && cd -
git status --short                       # clean
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: the two rules that came out of closing the review minors

Which drops get counted and which do not, and why building a path helper
on another one does not weaken the validate-your-own-segment rule."
```

---

## Self-Review

**Coverage.** Every open finding maps to a task: M1/M2/M3 → 1, M4/M5 → 2, M6/M7 → 3, M8/M9 → 4, M12/M13 → 5, with 6 recording the two that are rules. M10 and M11 are verified already fixed in `45ac06d` and are not in the plan.

**Type consistency.** `stream_dir`/`has_cached_transcript`/`has_analysis` are defined with `workspace_dir` first in Task 2 and used that way in the same task's route and test edits; `analysis_path`/`windows_dir` keep their existing `(workspace_dir, video_id)` signatures, so no caller outside `detect.py` changes for them. `PlaylistOption['count']` is widened only at the render site in Task 4, never in `streams.ts`.

**One thing every task shares that is easy to skip:** three tasks change `web/src/`, and each must run `npm run build` and commit `static/`. A source change without its rebuild leaves the Playwright suite testing the previous page — which would make Task 5's mutation step, in particular, prove nothing at all.
