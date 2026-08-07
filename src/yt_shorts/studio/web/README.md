# YT-Shorts Studio — frontend

React + Vite + Mantine (TypeScript). This is the *source* for the studio's
page; `src/yt_shorts/studio/api.py` serves the *built* output from
`../static/`, which is git-ignored: `hatch_build.py` builds it into a wheel,
`tools/build-binary.py` into the release binary, and CI's frontend job builds
it once and hands it to the other jobs as an artifact — so nobody installing
the result needs Node (see [Studio](https://github.com/jegr78/yt-shorts/wiki/Studio)
on the wiki).

```bash
npm install
npm run dev      # local dev server against a running studio API
npm run build    # typechecks (tsc -b) then builds into ../static/
npm test         # Vitest unit tests (jsdom)
```

**`npm run build` (`tsc -b`) is the real type-check here - bare `npx tsc --noEmit`
is INERT and gives zero signal, silently.** Measured, not assumed: from this
directory, `npx tsc --noEmit --listFiles` lists ZERO files, and appending a
deliberate type error (`const x: number = "oops"`) to `src/api.ts` still let
bare `npx tsc --noEmit` exit 0 with no output, while `npx tsc -b` and
`npx tsc --noEmit -p tsconfig.app.json` both caught it as TS2322. Root cause:
`tsconfig.json` is solution-style (`"files": []`, only `"references"` to
`tsconfig.app.json`/`tsconfig.node.json`), which is the normal shape for a
Vite + TS project - bare `tsc` obeys `files: []` and checks nothing at all
rather than following the references. Only `tsc -b` (build mode, which
`npm run build` already runs) or pointing `--noEmit` at a project file
directly actually type-checks anything. Do not "quick-check" a change with
bare `npx tsc --noEmit` and trust a clean exit - run `npm run build` (or
`npx tsc -b`) instead.

## Seven-screen router

The page is a small hand-rolled client-side router (no router dependency),
`useRoute.ts` + `Root.tsx`, over seven screens:

- `/` — the **channels** list (`GET /api/channels`).
- `/settings` — the workspace-level **settings** screen (connection state per channel).
- `/logs` — the workspace-level **logs** screen (central log, its archive, and job logs).
- `/jobs` — the workspace-level **Jobs** screen (the job queue: running, queued,
  recently finished).
- `/{channel}` — that channel's **events** (`GET /api/channels/{channel}/events`).
- `/{channel}/{event}` — the **editor** (`App.tsx`), the clip-review UI.
- `/{channel}/{event}/streams/{video_id}` — the **stream** screen (`StreamScreen.tsx`),
  one level deeper than the editor: a stream's transcript, detected moments and
  zoom lane, for creating a clip from a long stream by hand or from a hit.

`navigate()` uses `history.pushState` and a `popstate` listener, so Back/Forward
and a reload work; the API's SPA fallback serves `index.html` for any of these
paths so a deep link survives a refresh. The editor sets the active
`{channel, event}` scope (`api.setScope`) and every event-scoped fetch builds its
URL from `scopedApi.ts` (`eventBase`/`channelBase`) — so a scope bug is a wrong
URL a unit test catches, not a silent 404.

`npm test` runs the unit tests for the pure logic — the scoped-URL builder and
route parsing (`scopedApi.ts`), the duration formatters (`format.ts`), the
effective-window reconstruction (`window.ts`), word equality (`words.ts`),
upload-url extraction and copy formatting (`api.ts`/`upload.ts`), the
model-provider labels/blockers and cost disclosure (`providers.ts`), the brand
form's hex/ready-to-save/font-filename rules (`brand.ts`), the plan's own rules
- state labels, allowed actions, stop warnings (`jobs.ts`), the stream list's
playlist filter and bulk-action plan (`streams.ts`), and the
job-polling hook. Pure helpers deliberately live in their own modules rather than
being exported from a component, so Vite fast-refresh stays component-only and
they are testable. Vitest is separate from the repository's Python `pytest`
suite; the integrated flows are covered by the Playwright E2E there.

`vite.config.ts` sets `base: '/'` (absolute asset references — required because
the router puts a deep path like `/erf/<event>` in the address bar, and a
relative base would resolve assets against that path and 404) and
`build.outDir: '../static'`. After changing anything here, run `npm run build` —
the E2E tests serve whatever `../static/` currently holds, so a stale build is a
stale page under test. Do not commit `../static/`; it is ignored.

Talks to the API in `../api.py` - read that file's routes and field shapes
before changing `src/api.ts`, which mirrors them.
