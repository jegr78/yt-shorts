# CI, release-please and public readiness — design

Date: 2026-08-05
Status: approved (design), ready for implementation plan

Block B of three. Block A (packaging and binaries) is merged; Block C (the wiki)
follows. This block gives the project a test gate, an automated release, and the
files a public repository needs — then creates the repository.

## Motivation

Block A made the project installable: a wheel, a per-OS PyInstaller binary,
`yt-shorts install-tools` and `yt-shorts doctor`. Nothing verifies any of that
except a person running commands by hand, and there is still no remote.

The repository's history was condensed to a single commit before this block, so
the public record starts at `0.1.0` with a clean slate. The full 706-commit
history is preserved outside the repository as a git bundle.

`/Users/jegr/Documents/github/gt-racing-broadcast` (racecast) is again the
reference, and again not directly transplantable — it is pure stdlib with no
frontend, no test that needs an external binary, and no Python package to
publish. Every deviation below is one of those differences.

## What was measured, not assumed

These four measurements shape the design. Each was run against this repository
before the design was written.

| Question | Answer | Consequence |
|---|---|---|
| Does the suite need real `ffmpeg`? | **Yes** — 61 tests fail without it; `tests/test_trim.py::TestWithRealFfmpeg` is named for it | CI must install ffmpeg on **every** runner. It is not preinstalled on the macOS or Windows images. |
| Does it need `yt-dlp`? | **No** — 2544 passed with `yt-dlp` replaced by a stub that always exits 127 | Do not spend runner time installing it for the test jobs. |
| What happens to the E2E suite with no browser? | It **skips itself** — `tests/test_studio_e2e.py::_chromium_available()` | The same pytest command can run on every runner; the browser's presence decides whether 124 E2E tests run. |
| Is the frontend build input pinned? | `package-lock.json` is committed | `npm ci` is reproducible; whether Vite's *output* is byte-identical is a separate claim this design refuses to assume (see B1). |

## Decided requirements

- **Seven CI jobs, no cross product.** Full Python axis on ubuntu; the ship
  version only on macOS and Windows.
- **`tools/lint.py` runs in CI alongside ruff.** racecast runs only ruff; this
  project's two in-house AST guards live in `lint.py`, and `F811` there is the
  only thing that catches a duplicate test-class name — which pytest drops
  silently, reporting a smaller number that looks like a healthy run.
- **No PyPI.** The wheel and sdist attach to the GitHub release.
- **A branch ruleset on `main`** with required status checks, linear history, no
  force-push, and Code Owner review.
- **`master` → `main`**, matching every workflow this block writes.
- **Owner `jegr78`, repository name `yt-shorts`, public**, personal account.
- **The repository is created only after the operator has seen the finished
  local result.** Nothing in this block pushes anything before that gate.

## Architecture

### B1 — `ci.yml`: seven jobs, and the one that could lie

```
lint          ubuntu        ruff (pinned) + python3 tools/lint.py
frontend      ubuntu        npm ci → tsc → oxlint → vitest → build → staleness gate
test          ubuntu 3.13   full suite WITH Chromium
              ubuntu 3.12   full suite (E2E skips)
              ubuntu 3.14   full suite (E2E skips)
              macos 3.13    full suite (E2E skips)
              windows 3.13  full suite (E2E skips)
binary-smoke  ubuntu        PyInstaller build + smoke test
```

Every runner installs `ffmpeg` — measured as necessary above — through a pinned
action rather than the platform package manager, because `brew install ffmpeg`
costs minutes per run while a static build costs seconds.

**The job that could lie, and the guard against it.** The E2E suite skipping
itself when Chromium is absent is what makes this matrix cheap. It also means
that if `playwright install chromium` fails on the 3.13 runner, 124 tests
vanish **silently** and the suite still reports success. So on that one runner
the E2E file also runs as its **own step**, which fails if the summary reports
skips. Silence is not success — the same reasoning `CLAUDE.md` applies to a
green suite that dropped a shadowed test class.

**The staleness gate.** `src/yt_shorts/studio/static/` is the committed build
output and `src/yt_shorts/studio/web/` is its source. Someone who edits the
source and forgets to rebuild ships a studio that does not match its code. The
`frontend` job builds and then runs `git diff --exit-code` on `static/`.

Whether Vite produces byte-identical output from an identical lockfile and a
pinned Node version is **a claim, not a fact**. The implementation must build
twice in one CI run and compare before the gate is trusted. If it does not
hold, the gate degrades to "the build succeeds, and tsc, oxlint and vitest
pass", with the reason recorded in the workflow — not silently dropped.

The hazard `CLAUDE.md` records — never run `npm run build` while an E2E run is
in flight, because the build deletes `static/` and the pages served from it go
blank — is **structurally absent in CI**: these are separate runners with
separate working directories. It remains a local rule.

### B2 — A side effect that closes an open item from Block A

Block A's plan carries a follow-up: the binary smoke test's `install-tools`
step really does install software on an agent that lacks ffmpeg, because
`install_tools.run()` installs what is missing even with no flags.

The `binary-smoke` job installs **both** ffmpeg and yt-dlp before building. The
smoke's `install-tools` step then finds everything present and returns early,
so it probes rather than installs. No code change, only ordering — and it is
the one job where installing yt-dlp is worth the seconds.

### B3 — `release-please.yml` and `release.yml`

`release-type: simple`. `.release-please-manifest.json` is `{".": "0.1.0"}` —
required rather than optional here, because the condensed history carries no
tags for release-please to derive a baseline from. `extra-files` keeps
`src/yt_shorts/__version__.py` in lockstep through its
`x-release-please-version` annotation, so the package version and the release
never disagree.

`RELEASE_PLEASE_TOKEN` is a fine-grained PAT with a documented permission set,
and the workflow falls back to `GITHUB_TOKEN` when it is absent. The fallback
needs its own dispatch step: a tag created with the default token does **not**
trigger on-tag workflows, so without it, merging the Release PR would produce a
tag and a release with no binaries attached.

`release.yml` builds on the four platforms Block A decided — Windows x64,
macOS arm64, Linux x64, Linux arm64 — runs `tools/build-binary.py --version
"$TAG"`, and uploads each archive plus the wheel and sdist.

**`pr-title-lint.yml` is not optional.** GitHub uses the PR *title* as the
default squash-commit subject, and that subject is what release-please parses.
A free-text title means no Release PR, with nothing reporting an error.

### B4 — The files a public repository needs

`CONTRIBUTING.md`, `.github/CODEOWNERS` (`* @jegr78`),
`.github/pull_request_template.md`, `.github/dependabot.yml` for **three**
ecosystems — github-actions, npm and pip — where racecast needs only the first,
because this project has a `package.json` and a `pyproject.toml`.

`codeql.yml` analyses **Python and TypeScript**; racecast has no frontend.

`gitleaks.yml` installs a version-pinned, SHA-256-verified binary rather than
the wrapper action, which requires an organisation licence.

**Every action is pinned by commit SHA, never by tag.**

### B5 — Verifying workflows that cannot run yet

Workflows are the one thing in this project that cannot be tested by running
them until the repository exists. Three checks stand in:

1. `actionlint` locally over `.github/workflows/` — syntax, expression and
   shell errors, run as an implementation step rather than a permanent gate
   (it is an external binary and `tools/lint.py` deliberately depends on
   nothing).
2. `tests/test_workflows.py`, in the suite, pinning the invariants that must
   survive every future edit: every `uses:` carries a 40-character SHA; no
   workflow still names `master`; the `ci.yml` job names match the list the
   ruleset requires, because a renamed job silently deadlocks every PR waiting
   on a check that can never report.
3. A first real run: after the push, the workflows either go green or they do
   not, and that is the acceptance test.

### B6 — The rename, the remote, and the gate

Locally: `git branch -m master main`, and every workflow written against `main`.

Then **stop**. The repository is created only on the operator's explicit
go-ahead, and in this order:

1. `gh repo create jegr78/yt-shorts --public --source=. --push`
2. Watch the first CI run; fix what it finds.
3. Apply the branch ruleset via `gh api` — required checks (`lint`,
   `frontend`, `test (…)` for each matrix leg, `binary-smoke`), linear history,
   no force-push, Code Owner review. Applying it before CI has passed once
   would block the very PR that fixes CI.
4. Enable the repository's own secret-scanning push protection.

**One item stays with the operator:** `RELEASE_PLEASE_TOKEN` is a fine-grained
PAT only they can mint. Its required scopes are documented in the workflow
header. Without it the fallback path runs, degraded but working.

## Testing

- `tests/test_workflows.py` — the invariants in B5.2, as ordinary pytest cases,
  so a later edit that unpins an action or renames a job fails the suite.
- The existing 2544 tests must stay green, and `python3 tools/lint.py` must
  exit 0 — unchanged from Block A.
- The workflows' own acceptance test is the first CI run against the real
  repository (B5.3).

## Out of scope

- **PyPI publishing.** Decided against; the release assets are the distribution.
- **macOS notarisation.** Unsigned binaries stay quarantined; Block C's wiki
  documents the `xattr` workaround.
- **The wiki**, its sync tooling and the README split — Block C.
- **Any change to rendering, overlay, detection or upload behaviour.** The six
  pinned overlay hashes must not move.

## Hand-off items

1. `RELEASE_PLEASE_TOKEN` — the operator mints it; scopes documented in
   `release-please.yml`'s header.
2. The repository description and topics, set at creation.
3. `[project.urls]` in `pyproject.toml` currently carries a provisional URL
   (`https://github.com/jegr78/YT-Shorts`); it must match the real repository
   name `yt-shorts` once created.
