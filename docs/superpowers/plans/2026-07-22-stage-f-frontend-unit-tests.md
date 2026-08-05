# Stage F — Frontend unit tests (Vitest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add Vitest + Testing-Library and a targeted set of unit tests for the frontend's real client logic (formatters, effective-window reconstruction, upload-url extraction, word equality, job polling), extracting pure helpers into their own modules following the repo's `words.ts` convention.

**Architecture:** Vitest runs in a jsdom environment as a separate `npm test`, not folded into pytest. Pure helpers move out of component files into `format.ts` and `window.ts` (like `words.ts`) so they are testable without weakening Vite's fast-refresh boundary. No behaviour changes; E2E stays green.

**Tech Stack:** Vitest, @testing-library/react, @testing-library/jest-dom, jsdom. Vite 8, React 19, TypeScript 6.

## Global Constraints

- Work in `src/yt_shorts/studio/web/`. `node_modules` is present and stays untracked; the built `src/yt_shorts/studio/static/` stays committed.
- **No behaviour changes.** The extraction moves code; components keep importing the same functions. The full `pytest` suite (600, E2E included) must stay green, and `npm run build` must still typecheck.
- Vitest is **separate** from pytest — `npm test`, documented, never called from the Python suite.
- English only. Imperative commit messages. Do not touch `~/YT-Shorts-Data`, `/Users/jegr/racecast/`, ffmpeg, or the Python `.venv`.

---

## Task 1: Vitest tooling

**Files:** `package.json`, `vite.config.ts` (or new `vitest.config.ts`), `src/test-setup.ts`

- [ ] **Step 1: Install**

```bash
cd src/yt_shorts/studio/web
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

- [ ] **Step 2: Add the `test` script** to `package.json` scripts:

```json
"test": "vitest run"
```

- [ ] **Step 3: Configure the jsdom environment.** Add a `test` block to
`vite.config.ts` (Vitest reads Vite config). Because `defineConfig` from `vite`
does not type the `test` key, either import `defineConfig` from `vitest/config`
or add a `/// <reference types="vitest/config" />` triple-slash directive at the
top. Config:

```ts
test: {
  environment: 'jsdom',
  globals: true,
  setupFiles: ['./src/test-setup.ts'],
}
```

- [ ] **Step 4: Setup file** `src/test-setup.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **Step 5: Verify the runner works** with a trivial smoke test
`src/smoke.test.ts`:

```ts
import { describe, expect, it } from 'vitest'

describe('vitest', () => {
  it('runs', () => {
    expect(1 + 1).toBe(2)
  })
})
```

Run: `npm test`
Expected: 1 passing test.

- [ ] **Step 6:** Delete `src/smoke.test.ts` (it was only to prove the runner).
- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/studio/web/package.json src/yt_shorts/studio/web/package-lock.json src/yt_shorts/studio/web/vite.config.ts src/yt_shorts/studio/web/src/test-setup.ts
git commit -m "Set up Vitest with a jsdom environment"
```

---

## Task 2: Extract and test the duration formatters

**Files:** create `src/format.ts`, `src/format.test.ts`; modify `components/ClipList.tsx`, `components/StreamPanel.tsx`

**Interfaces:**
- Produces: `format.formatDuration(seconds: number): string`, `format.formatStreamDuration(seconds: number | null): string` — the exact functions currently inside those two components.

- [ ] **Step 1: Write the failing test** `src/format.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { formatDuration, formatStreamDuration } from './format'

describe('formatDuration', () => {
  it('formats minutes:seconds.tenths with zero-padded seconds', () => {
    expect(formatDuration(72.3)).toBe('1:12.3')
  })
  it('pads seconds below ten', () => {
    expect(formatDuration(65.0)).toBe('1:05.0')
  })
  it('handles zero', () => {
    expect(formatDuration(0)).toBe('0:00.0')
  })
})

describe('formatStreamDuration', () => {
  it('shows hours:minutes:seconds for a long stream', () => {
    expect(formatStreamDuration(3661)).toBe('1:01:01')
  })
  it('handles null', () => {
    expect(formatStreamDuration(null)).toBe('—')
  })
})
```

Note: the exact expected strings must match the CURRENT implementations. Before
writing the assertions, read `formatDuration` in `ClipList.tsx` and
`formatStreamDuration` in `StreamPanel.tsx` and encode what they actually produce
(the null sentinel, the hour/minute-only cases). If an assertion above does not
match the current output, fix the assertion to the real behaviour — this is a
characterization test of code that already ships, not a redefinition.

- [ ] **Step 2: Run to verify it fails** — `npm test` → cannot resolve `./format`.

- [ ] **Step 3: Extract.** Create `src/format.ts` with both functions moved
verbatim from the components (including their doc comments). In `ClipList.tsx`
and `StreamPanel.tsx`, delete the local definitions and
`import { formatDuration } from '../format'` /
`import { formatStreamDuration } from '../format'`.

- [ ] **Step 4: Run tests + build**

Run: `npm test` → format tests pass.
Run: `npm run build` → typechecks, rebuilds `../static/`.

- [ ] **Step 5: Confirm no behaviour change** — the pytest E2E renders these; run
the studio E2E: `cd ../../../../.. && PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q`. Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/format.ts src/yt_shorts/studio/web/src/format.test.ts src/yt_shorts/studio/web/src/components/ClipList.tsx src/yt_shorts/studio/web/src/components/StreamPanel.tsx src/yt_shorts/studio/static
git commit -m "Extract the duration formatters into a tested module"
```

---

## Task 3: Extract and test `withWindow`

**Files:** create `src/window.ts`, `src/window.test.ts`; modify `components/ClipEditor.tsx`

**Interfaces:**
- Produces: `window.withWindow(base, ...)` — the exact effective-window
  reconstruction currently inside `ClipEditor.tsx` (read it and keep its
  signature and behaviour verbatim).

- [ ] **Step 1: Read `withWindow` in `ClipEditor.tsx`** (around line 53) and note
its exact signature and the fields it rebuilds (`detected_window` preserved,
`effective_window` = override ?? base's).

- [ ] **Step 2: Write the failing test** `src/window.test.ts`, encoding the real
behaviour — the PATCH response carries no window fields, so `withWindow` restores
`detected_window` from `base` and sets `effective_window` to the override when one
is given (the changed-window handler) or to `base.effective_window` otherwise:

```ts
import { describe, expect, it } from 'vitest'
import { withWindow } from './window'

// Build a minimal ClipDetail-shaped base; only the window fields matter here.
const base = {
  detected_window: [92, 104] as [number, number],
  effective_window: [92, 104] as [number, number],
}

describe('withWindow', () => {
  it('keeps the detected window from the base', () => {
    const merged = withWindow(base as never, {} as never)
    expect(merged.detected_window).toEqual([92, 104])
  })
  it('applies an explicit effective-window override', () => {
    const merged = withWindow(base as never, {} as never, [95, 108])
    expect(merged.effective_window).toEqual([95, 108])
  })
  it('falls back to the base effective window without an override', () => {
    const merged = withWindow(base as never, {} as never)
    expect(merged.effective_window).toEqual([92, 104])
  })
})
```

Adjust the argument shapes to `withWindow`'s real signature as read in Step 1;
the three behaviours (detected preserved, override wins, base fallback) are the
contract to lock in.

- [ ] **Step 3: Run to verify it fails** — `npm test` → cannot resolve `./window`.

- [ ] **Step 4: Extract** `withWindow` into `src/window.ts` verbatim (with its doc
comment); `ClipEditor.tsx` imports it from `../window` and drops the local copy.

- [ ] **Step 5: Run tests + build + E2E** — `npm test`, `npm run build`, then the
studio E2E green.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/window.ts src/yt_shorts/studio/web/src/window.test.ts src/yt_shorts/studio/web/src/components/ClipEditor.tsx src/yt_shorts/studio/static
git commit -m "Extract the effective-window reconstruction into a tested module"
```

---

## Task 4: Test the already-pure helpers

**Files:** create `src/words.test.ts`, `src/api.extractUploadUrl.test.ts`

- [ ] **Step 1: Write `src/words.test.ts`** for `wordsEqual`:

```ts
import { describe, expect, it } from 'vitest'
import { wordsEqual } from './words'

const w = (start: number, end: number, text: string) => ({ start, end, text })

describe('wordsEqual', () => {
  it('is true for identical lists', () => {
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 1, ' a')])).toBe(true)
  })
  it('is false when a length differs', () => {
    expect(wordsEqual([w(0, 1, ' a')], [])).toBe(false)
  })
  it('is false when any field differs', () => {
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 1, ' b')])).toBe(false)
    expect(wordsEqual([w(0, 1, ' a')], [w(0, 2, ' a')])).toBe(false)
  })
})
```

- [ ] **Step 2: Read `extractUploadUrl` in `api.ts`** (around line 285), then write
`src/api.extractUploadUrl.test.ts` encoding its real behaviour — it pulls the URL
for a clip out of a completed job. Build a `Job`-shaped object per `api.ts`'s
`Job`/`ClipResult` types and assert: the URL is returned on a done job that
carries it, and `null` when absent/not done. Match the exact shape `extractUploadUrl`
reads (its result key / log parsing — read before asserting).

```ts
import { describe, expect, it } from 'vitest'
import { extractUploadUrl } from './api'

// Shape the Job/result exactly as api.ts's types define; adjust to the real
// fields extractUploadUrl reads.
describe('extractUploadUrl', () => {
  it('returns null when the job has no matching upload url', () => {
    const job = { id: 'j', status: 'running', results: {}, log: [] }
    expect(extractUploadUrl(job as never, 'clipname')).toBeNull()
  })
  // Add the positive case using the real done-job shape read from api.ts.
})
```

Complete the positive case against the real implementation; do not assert a shape
the function does not read.

- [ ] **Step 3: Run** — `npm test` → all pass.
- [ ] **Step 4: Commit**

```bash
git add src/yt_shorts/studio/web/src/words.test.ts src/yt_shorts/studio/web/src/api.extractUploadUrl.test.ts
git commit -m "Test word equality and upload-url extraction"
```

---

## Task 5: Test `useJobPolling`

**Files:** create `src/hooks/useJobPolling.test.tsx`

**Interfaces:** consumes `useJobPolling`, mocks `getJob` from `../api`.

- [ ] **Step 1: Write the test** using `renderHook`, Vitest fake timers, and a
mocked `getJob`. Cover: a running→done sequence stops polling; a null id yields
null and never calls `getJob`; a transient rejection retries at the interval
rather than tight-looping; changing the id re-polls; unmount clears the timer.

```tsx
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useJobPolling } from './useJobPolling'
import * as api from '../api'

const running = { id: 'j', status: 'running', results: {}, log: [] }
const done = { id: 'j', status: 'done', results: {}, log: [] }

describe('useJobPolling', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

  it('does nothing and returns null for a null id', () => {
    const spy = vi.spyOn(api, 'getJob')
    const { result } = renderHook(() => useJobPolling(null))
    expect(result.current).toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('polls until the job leaves running', async () => {
    const spy = vi.spyOn(api, 'getJob')
      .mockResolvedValueOnce(running as never)
      .mockResolvedValueOnce(done as never)
    const { result } = renderHook(() => useJobPolling('j'))
    // first poll resolves to running, schedules another; advance the interval
    await vi.waitFor(() => expect(result.current?.status).toBe('running'))
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(result.current?.status).toBe('done'))
    // no further polls after done
    const callsAfterDone = spy.mock.calls.length
    await vi.advanceTimersByTimeAsync(2000)
    expect(spy.mock.calls.length).toBe(callsAfterDone)
  })

  it('retries a transient error at the interval, not in a tight loop', async () => {
    const spy = vi.spyOn(api, 'getJob')
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValue(done as never)
    renderHook(() => useJobPolling('j'))
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    // it must NOT have retried synchronously
    expect(spy).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(750)
    await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })
})
```

The exact timer/async interleaving may need small adjustments to satisfy Vitest's
fake-timer + microtask ordering; the behaviours to lock in are the five listed
above. If `renderHook`'s `waitFor` fights the fake timers, use
`vi.advanceTimersByTimeAsync` to drive time and assert on `spy.mock.calls`.

- [ ] **Step 2: Run** — `npm test` → the hook tests pass.
- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/hooks/useJobPolling.test.tsx
git commit -m "Test the job-polling hook's transitions and retry"
```

---

## Task 6: Documentation

**Files:** `README.md`, `CLAUDE.md`, `src/yt_shorts/studio/web/README.md`

- [ ] **Step 1: README (repo)** — in the studio/development area, note that the
frontend has unit tests run with `npm test` (Vitest, jsdom), a required check
before committing frontend changes, alongside `npm run build`; and that it is
separate from the Python `pytest` suite.
- [ ] **Step 2: CLAUDE.md** — one line under the studio boundary: the frontend's
pure logic lives in its own modules (`words.ts`, `format.ts`, `window.ts`) — not
exported from components, so Vite fast-refresh stays component-only — and is unit-
tested with Vitest (`npm test`), which is not part of the pytest run.
- [ ] **Step 3: web/README.md** — the `npm test` command and what it covers.
- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md src/yt_shorts/studio/web/README.md
git commit -m "Document the frontend unit tests"
```

---

## Verification for the branch

- `npm test` green (all new unit tests).
- `npm run build` typechecks and rebuilds `static/`; `static/` committed; `node_modules` untracked.
- Full `pytest` suite green, E2E included — the extraction changed no behaviour.
- The three extracted modules (`format.ts`, `window.ts`, and the pre-existing `words.ts`) are pure and imported by their components; no component re-exports them.

## Self-review notes

- Every test is a characterization of code that already ships — assertions match current behaviour, verified by reading the source before asserting (Tasks 2, 3, 4). A test that only passes because it restated the implementation is avoided by choosing edge cases (padding, hour boundary, null, override-vs-fallback, retry-not-tight-loop) that a wrong implementation would fail.
- No behaviour change anywhere; the E2E suite is the backstop, run after each extraction.
- Vitest stays out of pytest by decision; documented, not wired in.
