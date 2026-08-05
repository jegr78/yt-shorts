# YT-Shorts Studio — frontend

React + Vite + Mantine (TypeScript). This is the *source* for the studio's
page; `src/yt_shorts/studio/api.py` serves the *built* output from
`../static/`, which is committed to the repository so the tool runs from a
clone with no npm install (see the repository root `README.md`, "Studio").

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

## Six-screen router

The page is a small hand-rolled client-side router (no router dependency),
`useRoute.ts` + `Root.tsx`, over six screens:

- `/` — the **channels** list (`GET /api/channels`).
- `/settings` — the workspace-level **settings** screen (connection state per channel).
- `/logs` — the workspace-level **logs** screen (central log, its archive, and job logs).
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
model-provider labels/blockers and cost disclosure (`providers.ts`), and the
job-polling hook. Pure helpers deliberately live in their own modules rather than
being exported from a component, so Vite fast-refresh stays component-only and
they are testable. Vitest is separate from the repository's Python `pytest`
suite; the integrated flows are covered by the Playwright E2E there.

`vite.config.ts` sets `base: '/'` (absolute asset references — required because
the router puts a deep path like `/erf/<event>` in the address bar, and a
relative base would resolve assets against that path and 404) and
`build.outDir: '../static'`. After changing anything here, run `npm run build`
and commit the updated `../static/` alongside the source change - the two must
never drift apart.

Talks to the API in `../api.py` - read that file's routes and field shapes
before changing `src/api.ts`, which mirrors them.
