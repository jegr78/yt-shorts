# Contributing

This file is for people changing the code. If you only want to run the tool,
see the README's [Installing](README.md#installing) and
[Requirements](README.md#requirements) sections instead.

## Setup

```bash
git clone https://github.com/jegr78/yt-shorts.git
cd yt-shorts
python3 -m venv .venv
.venv/bin/pip install -e ".[all,dev]"
```

That `pip install` builds the studio's frontend, so a developer clone needs
Node on `PATH`; see [The frontend](#the-frontend) below. An operator does not —
the release binaries carry it.

`bin/yt-shorts` re-execs itself into the `.venv` beside it (see the file's own
docstring), so it works from any clone without activating anything — as long as
you created that `.venv` above. The `pip install -e` also puts an equivalent
`yt-shorts` entry point on your venv's `PATH` (`.venv/bin/yt-shorts <command>`,
or `yt-shorts <command>` once the venv is activated); the two are the same
command.

## The external tools

The suite depends on `ffmpeg` for real: 61 tests fail without it. It does not
need `yt-dlp`. Once the venv above is installed:

```bash
.venv/bin/yt-shorts install-tools   # ffmpeg, ffprobe, yt-dlp
.venv/bin/yt-shorts doctor          # what is installed, what works, what to fix
```

## Running things

```bash
PYTHONPATH=src .venv/bin/pytest -q   # the suite
python3 tools/lint.py                # before every commit — ruff + two in-house AST guards, no PYTHONPATH needed
```

CI installs the package editable and runs plain `pytest` instead; both forms
are correct in their own context — locally there is no installed package on
`sys.path`, so `PYTHONPATH=src` stands in for it.

The Playwright E2E tests (`tests/test_studio_e2e.py`) need the `page` fixture
from the `pytest-playwright` plugin — already brought in by the `[dev]` extra
above — plus a downloaded browser, which is not something `pip` can install:

```bash
.venv/bin/python -m playwright install chromium
```

Without the browser they skip cleanly, with a reason, rather than failing — a
clone with no browser download must not be blocked. Without the plugin,
though, they do not skip: they error on every test with `fixture 'page' not
found`, which is why it is a `dev` dependency and not left for you to add by
hand.

## The frontend

```bash
cd src/yt_shorts/studio/web
npm ci
npm run build
npm test        # Vitest — required before committing a frontend change, alongside npm run build
```

`src/yt_shorts/studio/static/` is the build output of
`src/yt_shorts/studio/web/` and is **git-ignored**. Build it once after a
clone, and again after any change under `web/` — the Python suite's 116 E2E
tests serve that directory and fail without it. Nobody using a release binary
or a `pip`-installed wheel needs Node: both build the frontend on the way in
(`tools/build-binary.py`, `hatch_build.py`).

**A build deletes `static/` before rewriting it** (Vite's `emptyOutDir`).
Never run it while a test suite or an E2E run is in flight — a request
landing mid-build gets a 500 or a 404 instead of the page, which reads
exactly like a flaky test rather than what it is.

## Commit messages and PR titles

This repository uses [Conventional Commits](https://www.conventionalcommits.org/)
(`fix:`, `feat:`, `docs:`, ...). The **PR title** matters as much as any commit
message: PRs are squash-merged, and release-please parses the squash commit's
subject line — taken from the PR title — to decide the next version and
changelog entry. A malformed title fails the PR title lint check.

## Where the deeper rules live

`CLAUDE.md` documents the constraints that are expensive to violate — read it
before touching anything it calls out. `docs/superpowers/specs/` holds the
design behind each subsystem, one file per stage.
