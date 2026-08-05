# Stage F — Frontend unit tests (Vitest)

**Date:** 2026-07-22
**Scope:** give the studio frontend its first unit tests. Today the React app is
covered only by TypeScript compilation, oxlint, and Playwright E2E; the
client-side logic that accrued through D2b and E (effective-window
reconstruction, job polling, upload-state display, formatters) is exercised only
end-to-end, which is coarse and slow for edge cases. This stage adds Vitest +
Testing-Library and a small, targeted set of unit tests for that logic — not a
blanket retrofit of every component.

## Why now, and why small

The Python pipeline is densely unit-tested; the frontend, until D2b, was thin
display over the API and E2E covered wiring adequately. D2b and E added real
client logic — `withWindow` reconstructs the effective window the PATCH response
does not echo back (the D2b implementer explicitly flagged it as "worth
double-checking"), `useJobPolling` drives async polling with retries and
cleanup, upload state is read and displayed. These have edge cases a full browser
round-trip is a poor, expensive way to test. This stage targets exactly that
logic and stops there.

## What gets tested

Pure logic and one hook — the parts where a bug would be invisible to `tsc` and
awkward to catch in E2E:

- **`wordsEqual`** (`words.ts`, already a pure module) — the equality that drives
  the unsaved-changes state and the preview's saved-vs-staged decision.
- **`extractUploadUrl`** (`api.ts`, already exported, pure) — pulling the uploaded
  URL out of a completed job's result.
- **`formatDuration`** and **`formatStreamDuration`** — the tabular time
  formatters, with their real edge cases (the tenths digit, the hour boundary,
  zero-padding, a null duration).
- **`withWindow`** — the effective-window reconstruction after a PATCH, including
  the case the D2b implementer flagged: the response carries no
  `detected_window`/`effective_window`, so the client rebuilds them, and only the
  handler that changed the window passes an override.
- **`useJobPolling`** — polls until a job leaves "running", retries a transient
  fetch error at the normal interval (never a tight loop), resets when the job id
  changes, does nothing with a null id, and cleans up its timer on unmount.

## A small refactor, following the repo's own convention

`formatDuration`, `formatStreamDuration`, and `withWindow` currently live inside
component files and are not exported. `words.ts` already documents the repo's
convention for this exact situation: pure logic lives in its own module rather
than being exported from a component, "so Vite's fast-refresh boundary stays
component-only." So this stage extracts those helpers into their own pure modules
(`format.ts`, `window.ts`) — matching the existing pattern, enabling unit tests
without weakening fast-refresh, and leaving the components importing them. No
behaviour changes; the E2E suite must stay green through the extraction.

## Tooling

- Dev dependencies: `vitest`, `@testing-library/react`, `@testing-library/jest-dom`,
  `jsdom` (and `@testing-library/user-event` only if a test genuinely needs it).
- A `test` script in `package.json` (`vitest run` for CI-style one-shot; `vitest`
  for watch is the default dev invocation).
- Vitest configured for the `jsdom` environment (needed by the hook test), with
  the Testing-Library matchers set up. Config lives in `vite.config.ts`'s `test`
  block or a `vitest.config.ts`, whichever is cleaner given the existing Vite
  config.

## Integration

Vitest stays a **separate** JS test runner, run via `npm test` — it is **not**
folded into the Python `pytest` suite (decided: keep pytest fast and free of a
Node coupling, exactly as `npm run build` is already separate). It is documented
in README and CLAUDE.md as a required check before committing frontend changes,
alongside `npm run build`. The built `static/` remains committed; `node_modules`
stays untracked.

## Testing (of this stage itself)

- `npm test` runs green with the new unit tests.
- `npm run build` still typechecks (the extraction did not break types).
- The full `pytest` suite, E2E included, stays green (the extraction changed no
  behaviour).

## Not in scope

- Unit-testing every existing component (App, ClipList rendering, panels) — E2E
  already covers the integrated flows; this stage targets the logic E2E covers
  poorly.
- Visual/snapshot testing.
- A CI pipeline (none exists in the repo yet); this stage adds the runnable tests
  and documents the command, so a future CI can call `npm test`.
- Replacing any Playwright E2E — Vitest complements it, it does not substitute for
  the real-browser integration tests.
