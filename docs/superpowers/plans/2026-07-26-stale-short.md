# Stale Rendered Short Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a re-render, the studio plays — and downloads — the new short instead of the old one, without a hard browser reload.

**Architecture:** Two layers cause this. The player's URL is a constant, so a refetched clip payload is byte-identical and the mounted `<video>` never reloads; and `get_short` sends no `Cache-Control`, so the browser may answer from cache without asking. The fix gives the URL a version token derived from the file's own `stat()`, sets a cache policy that is truthful for a versioned URL, and refetches when the operator returns to the window — which is the only way a CLI render can reach the UI, since the CLI and the studio deliberately do not know about each other.

**Tech Stack:** FastAPI + Starlette `FileResponse`, React + Mantine + TypeScript + Vitest, pytest + Playwright.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q`.
- `python3 tools/lint.py` (NO `PYTHONPATH`) must print `All checks passed!` before every commit.
- Frontend gates, run from `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint`, `npm test`. Only Task 3 runs `npm run build` and commits `src/yt_shorts/studio/static/`.
- The Playwright E2E serves the COMMITTED bundle in `src/yt_shorts/studio/static/`, so a frontend change is invisible to the E2E until a rebuild.
- `src/yt_shorts/captions.py` must not appear in this branch's diff, and the six SHA-256 hashes in `tests/test_event_layer_no_regression.py` must never be re-pinned. Nothing here renders; if either moves, something is wrong.
- The version token is **opaque**. Its shape (`"<mtime_ns>-<size>"`) is an implementation detail of one function: a test may pin that it CHANGED, never what it equals, and the client must never parse it.
- A stale, garbage or absent `v` **must still serve the current file**. It is a cache key, not a precondition — refusing it would turn a bookmarked link, or a request already in flight when a render lands, into a 404.
- `immutable` must never be a lie: the hard cache policy is returned only when the requested token MATCHES the file's current version.
- The studio writes `edit.json` and nothing else. Nothing in this plan writes anything; every route touched here is a read.
- Test hygiene: a test that would still pass if the behaviour under test were removed is a defect. This project has shipped that twice, including an E2E guard that compared a blob object URL and therefore could never fail.
- macOS has no bare `timeout`; use `gtimeout <seconds> <cmd>` to bound anything that might hang.

## File structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/studio/api.py` | `_short_version` helper; `short_version` in `_summary`; the cache policy in `get_short`. All three are small additions to existing units — no new module: the token is one `stat()` and belongs beside the payload builder that publishes it. |
| `src/yt_shorts/studio/web/src/api.ts` | `ClipSummary.short_version`; `shortUrl(name, version)` |
| `.../components/ClipEditor.tsx` | the `<video>` passes the version |
| `.../components/ManualUploadPanel.tsx` | the download link passes the version |
| `.../App.tsx` | the focus/visibility refetch |

---

### Task 1: The server states the short's version, and stops inviting a stale cache

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (`_summary:351-357`, `get_short:1473-1483`, plus one new helper above `_summary`)
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Produces: `_short_version(directory: Path) -> str | None` — the token, or `None` when no short exists.
- Produces: `short_version` in every clip payload (`_summary` feeds the list at `api.py:1338` and both detail responses at `:1350` and `:1413`, so one addition covers all three), non-null exactly when `has_short` is true.
- Produces: `GET …/clips/{name}/short?v=<token>` — same bytes for any `v`; `Cache-Control: private, max-age=31536000, immutable` when `v` matches, `private, no-cache` otherwise.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_studio_api.py`. `EVENT_PREFIX`, `clip_entry`, `clipstore` and the `event_dir` / `client` / `studio_profile` fixtures already exist in that file. Testing a private helper is established there (it already imports `from yt_shorts.overlay import _footer_top`); add `from yt_shorts.studio import api as studio_api` to the imports if no equivalent name is already present, and report which form you used.

```python
class TestShortVersion:
    """The rendered short's URL is otherwise a constant, so a re-render leaves
    the studio's <video> pointing at the same src, React never touches the
    attribute, and the element keeps the resource it already loaded - the
    stale-player bug. This token is what makes the src change when the file
    does, whoever re-rendered it.
    """

    def test_an_absent_short_has_no_version(self, event_dir, studio_profile):
        directory = clipstore.write_clip(event_dir, clip_entry())
        assert studio_api._short_version(directory) is None

    def test_two_calls_with_no_change_agree(self, event_dir, studio_profile):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(b"one render")
        assert studio_api._short_version(directory) == studio_api._short_version(directory)

    def test_replacing_the_bytes_changes_the_token(self, event_dir, studio_profile):
        """Pins that it CHANGED, never what it equals - the shape is an
        implementation detail the client never parses. The two payloads
        deliberately differ in LENGTH as well as content, so this cannot
        depend on filesystem timestamp resolution.
        """
        directory = clipstore.write_clip(event_dir, clip_entry())
        path = clipstore.short_path(directory)
        path.write_bytes(b"old render")
        before = studio_api._short_version(directory)
        path.write_bytes(b"a new render of a completely different length")
        assert studio_api._short_version(directory) != before

    def test_the_list_and_the_detail_both_carry_it(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        listed = client.get(f"{EVENT_PREFIX}/clips").json()[0]
        detail = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert listed["has_short"] is True
        assert listed["short_version"]
        assert detail["short_version"] == listed["short_version"]

    def test_a_clip_with_no_short_reports_null(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        detail = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert detail["has_short"] is False
        assert detail["short_version"] is None


class TestShortCachePolicy:
    """`v` is a cache KEY, not a precondition. Every case serves the same
    bytes; only the policy differs, and the hard policy is handed out only
    when the token still identifies those bytes."""

    PAYLOAD = b"pretend this is an mp4"

    def _seed(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(self.PAYLOAD)
        return directory

    def _version(self, client, directory):
        return client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()["short_version"]

    def test_a_matching_token_may_be_cached_hard(self, event_dir, client):
        directory = self._seed(event_dir)
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": self._version(client, directory)})
        assert response.status_code == 200
        assert response.content == self.PAYLOAD
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"

    @pytest.mark.parametrize("params", [None, {"v": "0-0"}, {"v": "not-a-token"}])
    def test_absent_stale_and_garbage_all_revalidate_and_all_serve_the_file(
            self, event_dir, client, params):
        directory = self._seed(event_dir)
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short", params=params)
        assert response.status_code == 200, "a token is never a precondition"
        assert response.content == self.PAYLOAD
        assert response.headers["cache-control"] == "private, no-cache"

    def test_a_token_that_was_valid_before_a_re_render_must_revalidate(
            self, event_dir, client):
        """The reason the match check exists: without it we would hand a
        client `immutable` for a year while serving bytes that had already
        changed under the token it asked for."""
        directory = self._seed(event_dir)
        stale = self._version(client, directory)
        replacement = b"a completely different render, longer than the first"
        clipstore.short_path(directory).write_bytes(replacement)

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": stale})
        assert response.status_code == 200
        assert response.content == replacement
        assert response.headers["cache-control"] == "private, no-cache"

    def test_a_missing_short_is_still_a_404_even_with_a_token(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": "0-0"})
        assert response.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k "ShortVersion or ShortCachePolicy"
```
Expected: FAIL — `_short_version` does not exist, `short_version` is absent from the payloads, and there is no `cache-control` header to read.

- [ ] **Step 3: Add the helper**

Directly above `_summary` in `src/yt_shorts/studio/api.py`:

```python
def _short_version(directory: Path) -> str | None:
    """A token that changes exactly when the rendered short's bytes do.

    The studio's player URL is otherwise a constant: after a re-render the
    refetched clip payload is byte-identical, React never touches the
    <video>'s src attribute, and the element keeps the resource it already
    loaded - so the operator watches the OLD short until a hard browser
    reload. Putting this in the URL makes the src change when the file
    changes, whoever changed it: the studio, a CLI render, another session.

    (mtime_ns, size) rather than a content hash: hashing a multi-megabyte
    video on every clip-list request is O(size) per clip and a list holds
    dozens, where this is one stat() - the same syscall class as the
    .exists() it replaces. It is also the identity Starlette's own
    FileResponse derives its ETag from, so this reuses the server's existing
    notion of file identity instead of inventing a second, disagreeing one.

    OPAQUE by contract: the client passes it through and never parses it.
    Tests pin that it changed, not what it equals.
    """
    try:
        info = clipstore.short_path(directory).stat()
    except OSError:
        return None
    return f"{info.st_mtime_ns}-{info.st_size}"
```

- [ ] **Step 4: Publish it in the clip payload**

In `_summary`, replace the `has_short` line so both keys come from ONE stat
rather than a separate `.exists()` call:

```python
def _summary(directory: Path, clip: dict, edit: editorial.Edit) -> dict:
    short_version = _short_version(directory)
    return {
        "name": directory.name,
        "harvested_title": clip.get("hook", ""),
        "effective_title": editorial.effective_title(edit, clip.get("hook", "")),
        "status": edit.status,
        "has_short": short_version is not None,
        # The player's cache-busting token - see _short_version. Emitted here
        # so the clip list and both clip-detail responses carry it without
        # three separate additions.
        "short_version": short_version,
```

Leave every other key in `_summary` exactly as it is.

- [ ] **Step 5: Give `get_short` a truthful cache policy**

Replace the body of `get_short`:

```python
    @app.get(EV + "/clips/{name}/short")
    def get_short(channel: str, event: str, name: str, v: str | None = None):
        profile = _load_profile(channel, event)
        directory, _clip = _load_clip_or_404(profile, name)
        target = clipstore.short_path(directory)
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail=f"No short rendered yet for clip: {name!r}",
            )
        # `v` is a cache KEY, never a precondition: a stale or garbage token
        # still serves the current file, because refusing it would turn a
        # bookmarked link - or a request already in flight when a render
        # lands - into a 404, a new failure mode invented to protect against
        # nothing. It is read for exactly one purpose: a token that MATCHES
        # the file's current version identifies bytes that cannot change, so
        # THAT response may be cached hard, which is what keeps scrubbing and
        # seeking fast. Anything else must be revalidated, because such a URL
        # carries no identity and caching it is the staleness bug this exists
        # to fix. Checking the match is what keeps `immutable` from being a
        # lie in the window where a render lands between a payload read and
        # the video fetch.
        fresh = v is not None and v == _short_version(directory)
        policy = ("private, max-age=31536000, immutable" if fresh
                  else "private, no-cache")
        return FileResponse(str(target), media_type="video/mp4", filename=target.name,
                            headers={"Cache-Control": policy})
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q
python3 tools/lint.py
```
Expected: all pass (including the existing `TestShort` and `TestListClips`); `All checks passed!`.

If `TestListClips` fails on an exact-payload comparison, that is a real signal, not noise: report the failure and the assertion rather than editing the test, since a new key in `_summary` is expected to be additive.

- [ ] **Step 7: Confirm `FileResponse` did not overwrite the header**

Starlette sets its own stat-derived headers (`etag`, `last-modified`,
`content-length`) on a `FileResponse`. Confirm ours survives alongside them,
rather than assuming:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k "ShortCachePolicy" -x
```
Expected: PASS. Report in your report that the `cache-control` assertions
passed, which is the evidence the `headers=` argument is honoured.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "fix(studio): version the rendered short's URL and stop inviting a stale cache"
```

---

### Task 2: The client asks for the version it was told about

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts` (`ClipSummary` at :53-76, `shortUrl` at :673-675)
- Modify: `src/yt_shorts/studio/web/src/components/ClipEditor.tsx:334`
- Modify: `src/yt_shorts/studio/web/src/components/ManualUploadPanel.tsx:80`
- Modify: `src/yt_shorts/studio/web/src/App.tsx` (new effect after the render-completion effect, which ends at :158)
- Test: `src/yt_shorts/studio/web/src/api.shortUrl.test.ts` (new file)

**Interfaces:**
- Consumes: `short_version` from Task 1's payload — non-null exactly when `has_short` is true.
- Produces: `shortUrl(name: string, version: string | null): string`. `ClipDetail extends ClipSummary`, so the field is available on both.

- [ ] **Step 1: Write the failing test**

Create `src/yt_shorts/studio/web/src/api.shortUrl.test.ts`. `setScope(channel, event)` is exported from `./api` (`api.ts:31`) and is how the router establishes the event scope; call it first or `shortUrl` has no base to build on.

```ts
import { describe, expect, it } from 'vitest'
import { setScope, shortUrl } from './api'

describe('shortUrl', () => {
  it('carries the version as a query parameter', () => {
    // Without this the URL is a constant, so a re-render leaves the mounted
    // <video> pointing at the same src and the browser answers from cache.
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', '17-42')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?v=17-42',
    )
  })

  it('omits the query entirely for a deliberate null', () => {
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', null)).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short',
    )
  })

  it('encodes a token that would otherwise break the URL', () => {
    // Defensive: today's token is digits and a hyphen. The encoding is here
    // so the shape of the token stays an implementation detail of the server
    // (see _short_version) rather than a constraint on this function.
    setScope('erf', 'e1')
    expect(shortUrl('clip-a', 'a b&c')).toBe(
      '/api/channels/erf/events/e1/clips/clip-a/short?v=a%20b%26c',
    )
  })
})
```

Confirm the exact base path `setScope('erf', 'e1')` produces by reading
`api.ts`'s `eventScope` and `scopedApi.ts`'s `eventBase` before running — if it
differs from `/api/channels/erf/events/e1`, use the real one and report what it
is. Do not change `eventBase` to match this test.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd src/yt_shorts/studio/web && npm test -- shortUrl
```
Expected: FAIL — `shortUrl` currently takes one argument and appends no query.

- [ ] **Step 3: Add the field to the type**

In `api.ts`'s `ClipSummary`, directly after `has_short: boolean`:

```ts
  /** Opaque token that changes whenever the rendered short's bytes do (see
   * _short_version in studio/api.py); null exactly when has_short is false.
   * It belongs in the player's URL: without it the <video> src is a
   * constant, so a re-render leaves the element pointing at the same URL and
   * the browser answers from its own cache. Never parsed here - passed
   * through. */
  short_version: string | null
```

- [ ] **Step 4: Version the URL builder**

Replace `shortUrl`:

```ts
/**
 * The rendered short's URL, carrying the file's version so that a changed
 * file means a changed URL - which is what a mounted <video> needs, since it
 * only reloads when its src actually changes, and what stops the browser
 * answering from a cache keyed on a URL that never moves.
 *
 * `version` is REQUIRED rather than optional deliberately. Both callers must
 * pass it - the player in ClipEditor and the DOWNLOAD LINK in
 * ManualUploadPanel - and a stale short in the download link means an
 * operator hand-uploading the wrong video to YouTube, which is worse than a
 * stale preview. Making the parameter required turns "someone forgot at one
 * call site" into a tsc failure instead of something a test has to notice.
 * Pass null for the deliberately unversioned form.
 */
export function shortUrl(name: string, version: string | null): string {
  const base = `${eventScope()}/clips/${encodeURIComponent(name)}/short`
  return version === null ? base : `${base}?v=${encodeURIComponent(version)}`
}
```

- [ ] **Step 5: Pass it at both call sites**

`components/ClipEditor.tsx:334`:

```tsx
              <video controls src={shortUrl(clip.name, clip.short_version)} style={{ width: '100%', maxHeight: 360 }} />
```

`components/ManualUploadPanel.tsx:80`:

```tsx
            href={shortUrl(clip.name, clip.short_version)}
```

Both components already receive the clip (`ClipEditor`'s `clip` prop and
`ManualUploadPanel`'s `{ clip }`), and both already read `clip.has_short`, so
no prop plumbing is needed. If `tsc` reports a third call site, that is
information the plan did not have — fix it the same way and report it.

- [ ] **Step 6: Refetch when the operator comes back to the window**

In `App.tsx`, directly after the render-completion effect that ends at line 158
(`}, [job?.status])`) and before the detect-completion comment:

```tsx
  // A render started OUTSIDE this tab - by the CLI, or in another tab - has
  // nothing to notify us with: the studio's job runner and the CLI
  // deliberately do not know about each other (see CLAUDE.md), so the only
  // evidence a render happened is the files on disk. Refetching when the
  // operator returns to the window is what turns "alt-tab back from the
  // terminal" into a fresh player: the clip's short_version changes, and
  // with it the <video> src.
  //
  // BOTH events, because neither covers the case alone. Switching browser
  // TABS reliably fires visibilitychange; alt-tabbing to another
  // APPLICATION does not do so dependably across platforms and browser
  // versions, and `focus` is what carries that case. When both fire we do
  // two small idempotent GETs, which is a better trade than picking one
  // event and missing half the situations.
  //
  // Safe for unsaved edits, and not by luck: ClipEditor resets its staged
  // title/words/window only when clip.name CHANGES (see its own comment on
  // exactly this), so replacing the clip prop for the same clip leaves an
  // in-progress correction untouched.
  useEffect(() => {
    function refreshIfVisible() {
      if (document.visibilityState === 'hidden') return
      refreshClips()
      if (selectedName) {
        getClip(selectedName).then(setSelectedClip).catch(() => undefined)
      }
    }
    window.addEventListener('focus', refreshIfVisible)
    document.addEventListener('visibilitychange', refreshIfVisible)
    return () => {
      window.removeEventListener('focus', refreshIfVisible)
      document.removeEventListener('visibilitychange', refreshIfVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedName])
```

The `eslint-disable` matches the existing effects in this file (`refreshClips`
is redefined every render); keep it, or `npm run lint` will fail on the
exhaustive-deps rule.

- [ ] **Step 7: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web
npx tsc -b
npm run lint
npm test
```
Expected: tsc exit 0 (this is what proves no call site forgot the version),
oxlint clean, all Vitest pass including the three new cases.

Do NOT run `npm run build` — Task 3 owns the committed bundle.

- [ ] **Step 8: Prove the required parameter is load-bearing**

Delete the second argument at ONE call site (`clip.short_version` in
`ClipEditor.tsx`) and run `npx tsc -b`. Confirm it FAILS with an
argument-count error, then restore it by editing the line back — not with
`git checkout`, which would also discard the other call site's change. Report
the real error text. This is the evidence that the design decision in Step 4
actually holds.

- [ ] **Step 9: Commit**

```bash
git add src/yt_shorts/studio/web/src
git commit -m "fix(studio-web): request the short by version, and refresh on focus"
```

---

### Task 3: Docs, the E2E guard, bundle, full verification

**Files:**
- Modify: `CLAUDE.md`
- Test: `tests/test_studio_e2e.py` (new class)
- Modify: `src/yt_shorts/studio/static/**` (rebuilt, committed)

- [ ] **Step 1: Add the E2E guard**

Append a new class to `tests/test_studio_e2e.py`, after
`class TestPreviewUnavailable` (find it and put the new class immediately
before the next `class`; report where you placed it). The names used here —
`clip_entry`, `CLIP_URL`, `editor_url`, `clipstore`, and the `event_dir` /
`live_server` / `page` fixtures — are the ones neighbouring tests use;
`clipstore.short_path(...).write_bytes(b"fake mp4 bytes")` is the established
way this file gives a clip a short (see line 208).

```python
class TestStaleShortRefresh:
    """The operator's bug: after a re-render the studio kept playing the old
    short until a hard browser reload. Two layers caused it - a player URL
    that never changed, so the mounted <video> had no reason to reload, and a
    response with no Cache-Control, so the browser could answer from cache
    without asking. This is the only automated guard on the first layer.
    """

    def test_a_re_rendered_short_reaches_the_player_without_a_reload(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        clipstore.short_path(directory).write_bytes(b"the first render")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        player = page.locator("video")
        player.wait_for(timeout=5000)
        before = player.get_attribute("src")
        assert "?v=" in before, f"the player URL carries no version: {before}"

        # A CLI render, as far as the browser is concerned: the bytes change
        # with nothing in the page having asked for it.
        clipstore.short_path(directory).write_bytes(
            b"a second render, of a different length entirely")

        # The focus path. Dispatched rather than driven through the real
        # window manager: page.bring_to_front() is unreliable headless, and
        # what is under test is our handler, not the browser's event
        # delivery.
        page.evaluate("window.dispatchEvent(new Event('focus'))")

        page.wait_for_function(
            "(previous) => document.querySelector('video')?.getAttribute('src') !== previous",
            arg=before,
            timeout=5000,
        )
        after = player.get_attribute("src")
        assert "?v=" in after
        assert after != before, "the player is still pointing at the old render"
```

If a locator does not resolve, fix the SELECTOR and report exactly what you
changed and why — never weaken an assertion. This file has been caught twice by
locators that resolved to the wrong thing: a bare `.last` on a control two
editors both render, and an assertion against a blob object URL that differs on
every refire and therefore could never fail. Note that `src` here is a real
URL, not a blob, which is what makes comparing it meaningful.

- [ ] **Step 2: Build the bundle, then run the new E2E**

The E2E serves the COMMITTED bundle, so Task 2's changes are invisible to it
until this build runs. Build BEFORE pytest or the test fails for the wrong
reason.

```bash
cd src/yt_shorts/studio/web
npm run lint
npx tsc -b
npm test
npm run build
cd -
PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q -k "StaleShortRefresh"
```
Expected: every frontend gate clean, build exit 0, the new test passes.

- [ ] **Step 3: Mutation-check the guard**

The E2E is the only thing that can catch layer A, so prove it fails when the
fix is removed. Each mutation needs a rebuild before the E2E can see it.

1. In `ClipEditor.tsx`, change the player back to `src={shortUrl(clip.name, null)}`, rebuild, run the test. Expected: FAILS on the `"?v=" in before` assertion.
2. Restore, rebuild. Then remove the focus/visibility effect from `App.tsx`, rebuild, run the test. Expected: FAILS at `wait_for_function` — the src never changes.
3. Restore, rebuild, and confirm `git status` is clean apart from the intended changes, and that the test passes again.

Report the real outcome of each. Restore by editing the code back, never with
`git checkout` on a file that also holds a change you want to keep.

- [ ] **Step 4: Update `CLAUDE.md`**

In the Architecture section, immediately after the paragraph that begins
**"The studio app is workspace-level (stage G1)."**, insert:

```markdown
**A rendered short is served by a VERSIONED url, and that is not decoration.**
`GET …/clips/{name}/short` used to be a constant path returning a bare
`FileResponse`, and both halves of that were stale-video bugs. The path never
changed, so after a re-render the refetched clip payload was byte-identical,
React never touched the `<video>`'s `src`, and the element kept the resource it
had already loaded - the operator watched the OLD short until a hard browser
reload. And the response carried NO `Cache-Control`, so the browser fell back to
heuristic freshness and could answer from its own cache without making a request
at all (it also ignored `If-None-Match`, so a revalidation re-downloaded the
whole video).

`_short_version` fixes the first half: one `stat()`, `(st_mtime_ns, st_size)`,
emitted as `short_version` beside `has_short` in `_summary` - so the clip list
and both detail responses carry it - and appended by the client as `?v=`. It is
deliberately NOT a content hash: hashing a multi-megabyte video per clip on
every list request is O(size) where this is one syscall, and `(mtime, size)` is
already the identity Starlette's own `FileResponse` derives its ETag from. The
token is OPAQUE - tests pin that it changed, never what it equals, and the
client never parses it.

Two rules about `v` that are easy to get wrong. It is a cache KEY, never a
precondition: a stale or garbage token still serves the current file, because
refusing it would turn a bookmarked link - or a request already in flight when a
render lands - into a 404. And the hard policy (`private, max-age=31536000,
immutable`) is returned ONLY when the token MATCHES the file's current version;
everything else gets `private, no-cache`. That match check is what keeps
`immutable` from being a lie in the window where a render lands between a
payload read and the video fetch.

`shortUrl(name, version)` takes the version as a REQUIRED parameter, so `tsc`
fails if a call site forgets it. That matters because the second caller is
`ManualUploadPanel`'s DOWNLOAD link, and a stale short there means an operator
hand-uploading the wrong video to YouTube - a wrong artifact on the channel, not
a stale preview.

The last piece is `App.tsx` refetching the clip list and the open clip on BOTH
`window`'s `focus` and `document`'s `visibilitychange`. A render started by the
CLI is a different process: the studio's job runner and the CLI deliberately do
not know about each other, so the only evidence is the files on disk, and coming
back to the window is the trigger. Both events, because switching browser TABS
reliably fires `visibilitychange` while alt-tabbing to another APPLICATION does
not do so dependably. This is safe for unsaved edits only because `ClipEditor`
resets its staged edits when `clip.name` changes and not on every prop update -
if that ever becomes an unconditional sync, this refetch starts destroying
corrections.
```

- [ ] **Step 5: Run every gate**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git diff --stat src/yt_shorts/captions.py
```
Expected: the whole suite passes, `All checks passed!`, and an EMPTY diff for `captions.py`.

- [ ] **Step 6: Name the pinned hashes separately**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS. Report it on its own line — nothing in this plan renders, so a
move here means something is badly wrong.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(studio): document the versioned short url; e2e; rebuild static"
```

- [ ] **Step 8: Operator check (not the implementer's job)**

Open a clip with a rendered short in the studio, re-render it from the CLI,
alt-tab back to the browser, and confirm the player shows the new short with no
reload. Then, on a `manual` channel, confirm the "Download short" button
downloads the new file.

---

## Self-Review

**Spec coverage.** Decision 1 (server states the version, client puts it in the
URL) → Tasks 1 and 2. Decision 2 (`(mtime_ns, size)`, not a hash) → Task 1
Step 3, with the reasoning in the docstring. Decision 3 (opaque) → a Global
Constraint plus Task 1's "pins that it CHANGED" test. Decision 4 (a stale `v`
still serves the file) → `test_absent_stale_and_garbage_all_revalidate_and_all_serve_the_file`
and `test_a_token_that_was_valid_before_a_re_render_must_revalidate`. Decision 5
(policy, and `immutable` never a lie) → those same two tests plus
`test_a_matching_token_may_be_cached_hard`. Decision 6 (focus refetch, both
events, safe for unsaved edits) → Task 2 Step 6 and Task 3's mutation 2.
Decision 7 (no 304 handling) → absent from every task, correctly. The spec's
"what changes" list maps onto the file-structure table; its testing list maps
onto Task 1's tests, Task 2's Vitest, and Task 3's E2E. The "free property"
(no cache-clearing on upgrade) needs no code.

**One spec refinement made here, deliberately.** The spec wrote
`shortUrl(name, version?)` with the version optional; this plan makes it
REQUIRED. That is strictly stronger — it turns "a call site forgot the version"
from something a test would have to notice into a `tsc` failure — and it
matters because the second call site is the manual-upload download link. Task 2
Step 8 proves the guarantee rather than asserting it.

**Placeholder scan.** No TBD/TODO; every code step carries its final code. Four
places ask the implementer to confirm a name against the file rather than trust
me — the private-import form in `tests/test_studio_api.py`, the exact base path
`setScope` produces, where to place the new E2E class, and any third `shortUrl`
call site `tsc` finds. Each says what to report.

**Type consistency.** `_short_version(directory: Path) -> str | None` is
defined in Task 1 and consumed as the payload's `short_version` in Task 2.
`shortUrl(name: string, version: string | null): string` is defined in Task 2
Step 4 and used with exactly that arity at both call sites in Step 5 and in all
three Vitest cases. `short_version` is spelled identically in the Python
payload, the TypeScript interface, and both components. `ClipDetail extends
ClipSummary`, so the single field addition serves the list and the detail.

**Breaking-change audit.** `has_short` keeps its exact meaning (`stat()`
succeeding is equivalent to the `.exists()` it replaces, including returning
false for a broken symlink), and `short_version` is purely additive, so no
existing assertion should move. Task 1 Step 6 names the one place that could
disagree — an exact-payload comparison in `TestListClips` — and instructs the
implementer to report it rather than edit it, because an additive key failing
there would mean the payload is asserted more strictly than I found it to be.
