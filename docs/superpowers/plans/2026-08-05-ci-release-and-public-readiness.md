# CI, release-please and public readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the project a test gate, an automated release, the files a public repository needs — and then create that repository.

**Architecture:** Six workflows under `.github/workflows/`, every action pinned by commit SHA. `ci.yml` runs seven jobs: lint, frontend, a five-leg test matrix, and a binary smoke. `release-please.yml` opens the Release PR; `release.yml` builds the four platform binaries on the tag. Workflows are the one thing here that cannot be tested by running them before the repository exists, so `tests/test_workflows.py` pins their invariants in the ordinary suite. The repository is created only at the very end, on the operator's explicit go-ahead.

**Tech Stack:** GitHub Actions, release-please (`release-type: simple`), pytest, ruff, oxlint/vitest/Vite, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-08-05-ci-release-and-public-readiness-design.md`

**Reference project:** `<racecast-repo>` — **read-only**, never write there. Its `.github/workflows/` is the model for `release.yml`, `release-please.yml`, `codeql.yml`, `gitleaks.yml` and `pr-title-lint.yml`.

## Global Constraints

- **`PYTHONPATH=src` is mandatory for every LOCAL Python invocation.** Full suite: `PYTHONPATH=src .venv/bin/pytest -q`. In CI the package is installed editable (`pip install -e ".[all,dev]"`), so CI uses plain `python -m pytest` — the two differ on purpose, do not "fix" either to match the other.
- **`python3 tools/lint.py` must exit 0** before every commit. It needs no `PYTHONPATH`.
- **Every `uses:` in every workflow is pinned to a 40-character commit SHA, with the human-readable version in a trailing comment.** Never a tag, never a branch. `tests/test_workflows.py` enforces this.
- **These are the SHAs to use.** They were fetched from the reference project and from the GitHub API; do not invent or "update" any of them:
  ```
  actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1                  # v7.0.1
  actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97              # v7.0.0
  actions/setup-node@820762786026740c76f36085b0efc47a31fe5020                # v7.0.0
  astral-sh/ruff-action@278981a28ce3188b1e39527901f38254bf3aac89             # v4.1.0
  amannn/action-semantic-pull-request@48f256284bd46cdaab1048c3721360e808335d50  # v6.1.1
  github/codeql-action/init@f205ea1c3313d32999d8d6a48b4f6530d4437b38         # v4
  github/codeql-action/analyze@f205ea1c3313d32999d8d6a48b4f6530d4437b38      # v4
  googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7  # v5
  ```
  If a task needs an action not on this list, fetch its SHA from the API and record where it came from — never write a SHA you did not read from a command's output.
- **The suite needs `ffmpeg`, measured: 61 tests fail without it.** It does NOT need `yt-dlp` (2544 passed with yt-dlp stubbed). Install ffmpeg through the platform's own package manager, not a third-party action — this project already prefers a pinned checksum-verified binary over a wrapper action for gitleaks, and one fewer third-party action in the supply chain is worth ~30 seconds.
- **Language is English** — workflows, comments, docs, commit messages. Conventional Commits.
- **Nothing in Tasks 1-5 touches the network or any remote.** The repository does not exist yet, and Task 6 is gated on the operator.
- **The six pinned overlay hashes must not move**, and the suite must stay green (2544 passing at the start of this plan).

---

### Task 1: `ci.yml` and the invariants that keep it honest

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_workflows.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the job names `lint`, `frontend`, `test (<os>, <version>)` and `binary-smoke`, which Task 6 registers as the ruleset's required status checks; and `tests/test_workflows.py`'s invariants, which every later task's workflow must satisfy.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflows.py`:

```python
"""What must stay true of every GitHub Actions workflow in this repository.

Workflows are the one thing here that cannot be tested by running them until
the repository exists, so these pin the invariants a later edit could quietly
break. They are deliberately textual: parsing the YAML would let a malformed
file pass as "no jobs found", which is the vacuous-assertion trap this project
has already been bitten by three times.
"""

import re
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# `uses: owner/repo@<40 hex>` with an optional trailing `# v1.2.3` comment.
PINNED = re.compile(r"^\s*-?\s*uses:\s+\S+@[0-9a-f]{40}\s*(#.*)?$")
USES = re.compile(r"^\s*-?\s*uses:\s+")


def _workflows():
    return sorted(WORKFLOW_DIR.glob("*.yml"))


class TestThereAreWorkflowsAtAll:
    """Every assertion below iterates over the workflow files. An empty glob
    would make all of them pass while testing nothing - so this one comes
    first and fails loudly instead."""

    def test_the_workflow_directory_exists(self):
        assert WORKFLOW_DIR.is_dir(), f"no {WORKFLOW_DIR}"

    def test_ci_is_one_of_them(self):
        assert (WORKFLOW_DIR / "ci.yml").is_file()


class TestEveryActionIsPinnedToASha:
    """A tag or branch ref is mutable: whoever controls it can change what runs
    with this repository's token. Dependabot updates the SHAs; this keeps them
    SHAs."""

    def test_no_uses_line_carries_a_tag_or_branch(self):
        offenders = []
        for path in _workflows():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if USES.match(line) and not PINNED.match(line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, "unpinned action refs:\n  " + "\n  ".join(offenders)

    def test_at_least_one_action_is_actually_used(self):
        """Guards the guard: a workflow set with no `uses:` at all would pass
        the test above without exercising it."""
        text = "\n".join(p.read_text(encoding="utf-8") for p in _workflows())
        assert PINNED.search(text)


class TestNothingStillNamesMaster:
    """The default branch is `main`. A workflow left pointing at `master`
    simply never triggers - silently."""

    def test_no_workflow_mentions_master(self):
        offenders = [f"{p.name}" for p in _workflows()
                     if "master" in p.read_text(encoding="utf-8")]
        assert not offenders, f"workflows still naming master: {offenders}"


class TestTheJobNamesTheRulesetDependsOn:
    """`main`'s ruleset requires these checks by name. A renamed job does not
    fail a PR - it deadlocks it, waiting forever for a check that can no
    longer report. Renaming a job here means updating the ruleset too."""

    REQUIRED = ("lint:", "frontend:", "test:", "binary-smoke:")

    @pytest.mark.parametrize("job", REQUIRED)
    def test_ci_defines_the_job(self, job):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert f"\n  {job}" in text, f"ci.yml no longer defines {job!r}"


class TestTheTestMatrixStaysFiveLegs:
    """Three Python versions on ubuntu, the ship version alone on macOS and
    Windows. `exclude` (not `include`) is what keeps the job names shaped
    `test (<os>, <version>)`, which is what the ruleset matches on."""

    def test_the_matrix_excludes_four_combinations(self):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert text.count("- {os: macos-latest") + text.count("- {os: windows-latest") == 4

    def test_the_ship_version_is_3_13(self):
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert 'python-version: "3.13"' in text or "'3.13'" in text


class TestTheEndToEndGuard:
    """tests/test_studio_e2e.py skips itself when Chromium is absent. That is
    what makes the matrix cheap, and it is also how a green run could hide 124
    missing tests. ci.yml must fail when they skip on the one runner that
    installs the browser."""

    def test_ci_greps_the_summary_for_skips_and_exits_nonzero(self):
        # Deliberately specific: asserting merely that the word "skipped"
        # appears would pass on the explanatory COMMENT alone, with the guard
        # itself deleted. Pin the mechanism, not a word.
        text = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
        assert "grep -qE '[0-9]+ skipped'" in text, \
            "ci.yml no longer greps the E2E summary for skips"
        assert "set -o pipefail" in text, \
            "without pipefail the piped pytest exit code is lost and a failing E2E run reads as green"
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q`
Expected: FAIL — `AssertionError: no …/.github/workflows`, because the directory does not exist yet.

- [ ] **Step 3: Write the workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

# The test gate. Seven jobs, deliberately NOT a cross product: the Python axis
# (3.12/3.13/3.14) runs on ubuntu only, while the OS axis runs on the pinned
# ship version (3.13) alone — macOS and Windows runners are the slow, scarce
# ones and they catch path/process bugs, not version drift.
#
# NB: the job names below ("lint", "frontend", "test (<os>, <ver>)",
# "binary-smoke") are the ruleset's required status checks. Renaming one does
# not fail a PR — it DEADLOCKS it, waiting on a check that can never report.
# Change a name here and update the ruleset in the same breath.
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

defaults:
  run:
    shell: bash

jobs:
  lint:
    # ruff AND tools/lint.py. The second is not redundant: it carries two
    # in-house AST guards ruff has no rule for (a silently swallowed except, a
    # procedure whose return value is used), and its ruff pass includes F811 —
    # the only thing that catches a duplicate test-class name, which pytest
    # drops SILENTLY while reporting a smaller number that looks healthy.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: astral-sh/ruff-action@278981a28ce3188b1e39527901f38254bf3aac89  # v4.1.0
        with:
          version: "0.15.16"   # pinned: a new ruff rule must not break CI silently
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: "3.13"
      - name: In-house AST guards
        run: python3 tools/lint.py

  frontend:
    # The studio's frontend, and the gate that keeps the COMMITTED build output
    # honest. src/yt_shorts/studio/static/ is committed deliberately so the tool
    # runs from a clone with no npm install; someone who edits web/src/ and
    # forgets to rebuild ships a studio that does not match its own source.
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: src/yt_shorts/studio/web
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0
        with:
          node-version: "26.4.0"   # exact: the staleness gate below compares build OUTPUT
          cache: npm
          cache-dependency-path: src/yt_shorts/studio/web/package-lock.json
      - run: npm ci
      - run: npm run lint
      - run: npm test
      - run: npm run build          # tsc -b && vite build
      - name: The committed static/ must match a fresh build
        # Absolute, not `.`: a step-level working-directory resolves against
        # github.workspace rather than the job default above, and relying on
        # that subtlety is how a path check silently runs in the wrong tree.
        working-directory: ${{ github.workspace }}
        run: |
          if ! git diff --exit-code -- src/yt_shorts/studio/static/; then
            echo "::error::src/yt_shorts/studio/static/ is stale. Run 'npm run build' in src/yt_shorts/studio/web and commit the result."
            exit 1
          fi

  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python-version: ["3.12", "3.13", "3.14"]
        # exclude, not include: it keeps the job names shaped
        # "test (<os>, <ver>)", which is what the ruleset matches on.
        exclude:
          - {os: macos-latest, python-version: "3.12"}
          - {os: macos-latest, python-version: "3.14"}
          - {os: windows-latest, python-version: "3.12"}
          - {os: windows-latest, python-version: "3.14"}
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: ${{ matrix.python-version }}

      # ffmpeg is a REAL dependency of the suite, measured: 61 tests fail
      # without it (tests/test_trim.py::TestWithRealFfmpeg is named for it).
      # yt-dlp is NOT — the suite passes 2544/2544 with it stubbed out — so it
      # is deliberately absent here. Native package managers rather than a
      # third-party action: one fewer thing in the supply chain that can run
      # with this job's token.
      - name: Install ffmpeg (Linux)
        if: runner.os == 'Linux'
        run: sudo apt-get update && sudo apt-get install -y ffmpeg
      - name: Install ffmpeg (macOS)
        if: runner.os == 'macOS'
        run: brew install ffmpeg
      - name: Install ffmpeg (Windows)
        if: runner.os == 'Windows'
        run: choco install ffmpeg -y --no-progress
      - name: Prove ffmpeg is really there
        run: ffmpeg -version

      - name: Install the package and its test extras
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[all,dev]"

      # Chromium on ONE leg only. Everywhere else the E2E suite skips itself
      # (tests/test_studio_e2e.py::_chromium_available), which is exactly what
      # makes this matrix affordable.
      - name: Install Chromium for the E2E suite
        if: matrix.os == 'ubuntu-latest' && matrix.python-version == '3.13'
        run: python -m playwright install --with-deps chromium

      - name: Run the suite
        run: python -m pytest -q

      # THE GUARD. If `playwright install` fails, 124 E2E tests skip and the
      # run above still goes green — a smaller number that looks healthy. So on
      # the one leg that installs a browser, run the E2E file alone and fail if
      # anything skipped. Silence is not success.
      - name: The E2E suite must actually run, not skip
        if: matrix.os == 'ubuntu-latest' && matrix.python-version == '3.13'
        run: |
          set -o pipefail
          python -m pytest tests/test_studio_e2e.py -q | tee /tmp/e2e.log
          if grep -qE '[0-9]+ skipped' /tmp/e2e.log; then
            echo "::error::The E2E suite SKIPPED. Chromium is missing, and 124 tests just vanished from a green run."
            exit 1
          fi

  binary-smoke:
    # CI must exercise everything release.yml exercises, so a PR that breaks
    # the binary goes red here rather than at the v* tag. ffmpeg AND yt-dlp are
    # installed first, on purpose: the smoke test runs `install-tools`, which
    # INSTALLS what is missing even with no flags. With both present it probes
    # and returns early instead of running a package manager mid-build.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: "3.13"
      - name: Install ffmpeg and yt-dlp
        run: |
          sudo apt-get update
          sudo apt-get install -y ffmpeg
          python -m pip install --upgrade pip
          python -m pip install yt-dlp
      - name: Install the package and PyInstaller
        run: |
          python -m pip install -e ".[all]"
          python -m pip install pyinstaller
      - name: Build the binary and smoke-test it
        run: python tools/build-binary.py --version ci-smoke
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q`
Expected: PASS.

- [ ] **Step 5: Check the YAML with actionlint**

```bash
brew install actionlint
actionlint .github/workflows/ci.yml
```
Expected: no output (actionlint is silent on success). Fix anything it reports. If `brew install` is unavailable, say so in the report rather than skipping the check silently.

- [ ] **Step 6: Measure the claim the staleness gate rests on**

The gate assumes Vite produces byte-identical output from the same lockfile and Node version. **Verify it rather than trusting it** — and note that `npm run build` deletes `src/yt_shorts/studio/static/` before rewriting it, so do this only when no E2E run is in flight:

```bash
cd src/yt_shorts/studio/web
npm ci
npm run build && (cd ../../../.. && git status --short src/yt_shorts/studio/static/)
npm run build && (cd ../../../.. && git status --short src/yt_shorts/studio/static/)
```
Expected: both `git status` calls print nothing — the rebuild reproduced the committed bytes exactly.

If either prints changes, the gate cannot stand as written. **Do not delete the job.** Replace the `git diff --exit-code` step with a comment recording what was measured, and keep `npm run lint`, `npm test` and `npm run build` as the gate — a build that fails to compile is still caught. Report exactly what you observed either way, and restore the working tree with `git restore src/yt_shorts/studio/static/` if the rebuild dirtied it.

- [ ] **Step 7: Full suite, lint, commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git add .github/workflows/ci.yml tests/test_workflows.py
git commit -m "ci: seven jobs, and a guard against an E2E suite that skips itself"
```

---

### Task 2: release-please and the release build

**Files:**
- Create: `.github/workflows/release-please.yml`
- Create: `.github/workflows/release.yml`
- Create: `release-please-config.json`
- Create: `.release-please-manifest.json`

**Interfaces:**
- Consumes: `tools/build-binary.py --version <tag>` and `src/yt_shorts/__version__.py`'s `x-release-please-version` annotation, both from Block A; `tests/test_workflows.py` from Task 1.
- Produces: the `v*` tag contract that `release.yml` triggers on.

- [ ] **Step 1: Write the release-please configuration**

Create `release-please-config.json`:

```json
{
  "$schema": "https://raw.githubusercontent.com/googleapis/release-please/main/schemas/config.json",
  "release-type": "simple",
  "include-component-in-tag": false,
  "packages": {
    ".": {
      "extra-files": ["src/yt_shorts/__version__.py"]
    }
  }
}
```

Create `.release-please-manifest.json`:

```json
{
  ".": "0.1.0"
}
```

This file is **required**, not optional: the repository's history was condensed
to a single commit and carries no tags, so release-please has nothing to derive
a baseline from. Without it the first release would be proposed as 1.0.0.

- [ ] **Step 2: Write `release-please.yml`**

Create `.github/workflows/release-please.yml`:

```yaml
name: release-please
#
# SECURITY: RELEASE_PLEASE_TOKEN is a long-lived PAT. This workflow is
# push-triggered on `main` only, so untrusted PR code can never reach it, but
# the token is broad — keep it tightly scoped and rotate it on a schedule.
# Required permissions for a FINE-GRAINED PAT scoped to THIS repository only:
#   - Contents:      Read and write   (commit the release branch, push the tag)
#   - Pull requests: Read and write   (open/update the Release PR)
#   - Issues:        Read and write   (autorelease: pending/tagged PR labels)
#   - Workflows:     Read and write   (the `gh workflow run` dispatch fallback)
#   - Metadata:      Read-only        (mandatory baseline for any fine-grained PAT)
# Set no other scopes and no other repositories. If the secret is absent or
# expired the job falls back to GITHUB_TOKEN — degraded; see the steps below.
on:
  push:
    branches: [main]
permissions:
  contents: write
  pull-requests: write
  issues: write           # the autorelease PR labels use the issues API
  actions: write          # `gh workflow run` (the dispatch fallback) needs this
env:
  RP_TOKEN: ${{ secrets.RELEASE_PLEASE_TOKEN }}
jobs:
  release-please:
    runs-on: ubuntu-latest
    steps:
      # With the PAT, the bot's branch pushes and tags are user events: CI runs
      # on the Release PR (so its required checks can turn green) and the tag
      # push triggers release.yml natively. Falls back to GITHUB_TOKEN when the
      # secret is missing — then CI on the Release PR needs a manual
      # close/reopen, and the dispatch step below builds the binaries.
      - uses: googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7  # v5
        id: release
        with:
          token: ${{ secrets.RELEASE_PLEASE_TOKEN || github.token }}
      # A tag created with the default GITHUB_TOKEN does NOT trigger on-tag
      # workflows. Without this dispatch, merging the Release PR in fallback
      # mode would produce a tag and a release with no binaries attached.
      # Skipped when the PAT is in use — the tag push already did it.
      - name: Build the binaries for the new tag (GITHUB_TOKEN fallback)
        if: ${{ steps.release.outputs.release_created && env.RP_TOKEN == '' }}
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ steps.release.outputs.tag_name }}
        run: >
          gh workflow run release.yml
          --ref "$TAG"
          --repo "${{ github.repository }}"
```

- [ ] **Step 3: Write `release.yml`**

Create `.github/workflows/release.yml`:

```yaml
name: Release
on:
  push:
    tags: ["v*"]
  workflow_dispatch:        # dispatched by release-please.yml with --ref <tag>
permissions:
  contents: write
defaults:
  run:
    shell: bash
jobs:
  create-release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - name: Create the GitHub release (idempotent)
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release create "${{ github.ref_name }}" --generate-notes || true

  wheel:
    # The wheel and sdist are OS-independent — build them once, not per runner.
    needs: create-release
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: "3.13"
      - name: Build the wheel and sdist
        run: |
          python -m pip install --upgrade pip build
          python -m build
      - name: Attach them to the release
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release upload "${{ github.ref_name }}" dist/* --clobber

  binary:
    needs: create-release
    strategy:
      fail-fast: false
      matrix:
        include:
          - {os: windows-latest,    asset: yt-shorts-windows-x64.zip}
          - {os: macos-latest,      asset: yt-shorts-macos-arm64.tar.gz}
          - {os: ubuntu-latest,     asset: yt-shorts-linux-x64.tar.gz}
          - {os: ubuntu-24.04-arm,  asset: yt-shorts-linux-arm64.tar.gz}
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0
        with:
          python-version: "3.13"
      - name: Install the package and PyInstaller
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[all]"
          python -m pip install pyinstaller
      # ffmpeg and yt-dlp so the build's own smoke test probes rather than
      # installs — `install-tools` with no flags still installs what is missing.
      - name: Install ffmpeg and yt-dlp (Linux)
        if: runner.os == 'Linux'
        run: sudo apt-get update && sudo apt-get install -y ffmpeg && python -m pip install yt-dlp
      - name: Install ffmpeg and yt-dlp (macOS)
        if: runner.os == 'macOS'
        run: brew install ffmpeg && python -m pip install yt-dlp
      - name: Install ffmpeg and yt-dlp (Windows)
        if: runner.os == 'Windows'
        run: choco install ffmpeg -y --no-progress && python -m pip install yt-dlp
      - name: Build the binary and smoke-test it
        run: python tools/build-binary.py --version "${{ github.ref_name }}"
      # --onedir: dist/bin/yt-shorts/ is a DIRECTORY, not a single file. The
      # bundle carries ctranslate2, onnxruntime, av and numpy; a one-file build
      # would unpack ~300 MB to a temp dir on every single invocation.
      - name: Package the release asset
        run: |
          cd dist/bin
          case "${{ matrix.asset }}" in
            *.zip)    python -m zipfile -c "../../${{ matrix.asset }}" yt-shorts ;;
            *.tar.gz) tar czf "../../${{ matrix.asset }}" yt-shorts ;;
            *)        echo "Unknown asset format: ${{ matrix.asset }}"; exit 1 ;;
          esac
      - name: Upload the release asset
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release upload "${{ github.ref_name }}" "${{ matrix.asset }}" --clobber
```

- [ ] **Step 4: Verify with actionlint and the suite**

```bash
actionlint .github/workflows/release-please.yml .github/workflows/release.yml
PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q
```
Expected: actionlint silent; the workflow invariants still pass — in particular every new `uses:` is SHA-pinned and no file names `master`.

- [ ] **Step 5: Prove the release-please config parses**

```bash
python3 -c "import json; print(json.load(open('release-please-config.json'))['release-type']); print(json.load(open('.release-please-manifest.json'))['.'])"
```
Expected: `simple` and `0.1.0`.

- [ ] **Step 6: Confirm the version annotation release-please will rewrite**

```bash
grep -n "x-release-please-version" src/yt_shorts/__version__.py
cat version.txt
```
Expected: the annotation is present on the `__version__` line, and `version.txt` reads `0.1.0`. Both must already be true from Block A — if either is missing, stop and report rather than editing them here.

- [ ] **Step 7: Lint and commit**

```bash
python3 tools/lint.py
git add .github/workflows/release-please.yml .github/workflows/release.yml release-please-config.json .release-please-manifest.json
git commit -m "ci: release-please, and a release build for four platforms"
```

---

### Task 3: The guard workflows

**Files:**
- Create: `.github/workflows/codeql.yml`
- Create: `.github/workflows/gitleaks.yml`
- Create: `.github/workflows/pr-title-lint.yml`
- Create: `.github/dependabot.yml`

**Interfaces:**
- Consumes: `tests/test_workflows.py` from Task 1.
- Produces: nothing later tasks import.

- [ ] **Step 1: Write `codeql.yml`**

Create `.github/workflows/codeql.yml`:

```yaml
name: CodeQL

# Static security analysis (free for public repositories). BOTH languages: the
# reference project this is modelled on has no frontend, but this repository
# ships a React studio, and its TypeScript is as much a part of the attack
# surface as the Python.
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: "27 4 * * 1"  # weekly catch-all, independent of pushes

permissions:
  contents: read

jobs:
  analyze:
    name: Analyze (${{ matrix.language }})
    runs-on: ubuntu-latest
    permissions:
      security-events: write  # required to upload CodeQL results
    strategy:
      fail-fast: false
      matrix:
        language: [python, javascript-typescript]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
      - name: Initialize CodeQL
        uses: github/codeql-action/init@f205ea1c3313d32999d8d6a48b4f6530d4437b38  # v4
        with:
          languages: ${{ matrix.language }}
          queries: security-and-quality
      - name: Perform CodeQL Analysis
        uses: github/codeql-action/analyze@f205ea1c3313d32999d8d6a48b4f6530d4437b38  # v4
```

- [ ] **Step 2: Write `gitleaks.yml`**

Create `.github/workflows/gitleaks.yml`:

```yaml
name: Secret scan

# Defense in depth on top of GitHub's native push protection. Uses the gitleaks
# BINARY directly rather than the wrapper action, which requires an
# organisation licence — and pins it by version with a verified checksum, the
# same treatment this project gives the yt-dlp binary it downloads.
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
        with:
          fetch-depth: 0  # full history, so the git scan can inspect every commit

      - name: Install gitleaks (pinned + checksum-verified)
        # A compromised upstream 'latest' would otherwise run in CI with this
        # job's token. Bump VER and SHA256 together, both read from the
        # release's own checksums.txt — never write a checksum you did not read
        # from that file.
        env:
          VER: "8.30.1"
          SHA256: "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
        run: |
          set -euo pipefail
          curl -sSfL -o gitleaks.tar.gz \
            "https://github.com/gitleaks/gitleaks/releases/download/v${VER}/gitleaks_${VER}_linux_x64.tar.gz"
          echo "${SHA256}  gitleaks.tar.gz" | sha256sum -c -
          tar -xzf gitleaks.tar.gz gitleaks
          sudo install gitleaks /usr/local/bin/gitleaks
          gitleaks version

      - name: Scan git history for secrets
        run: gitleaks git --no-banner --redact --verbose .
```

The `VER`/`SHA256` pair above is carried over from the reference project. **Verify it still matches upstream before committing:**

```bash
curl -sSfL https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt | grep linux_x64
```
If the value differs, or 8.30.1 no longer exists, take the current release's version and its checksum from that same file and use those. Record in your report which you used and where it came from.

- [ ] **Step 3: Write `pr-title-lint.yml`**

Create `.github/workflows/pr-title-lint.yml`:

```yaml
name: PR Title Lint
#
# Guards the squash-merge subject. GitHub uses the PR *title* as the default
# squash-commit subject, and release-please parses that subject for Conventional
# Commits (feat:/fix:/… -> version bump -> Release PR). A free-text title means
# release-please sees nothing releasable and opens no Release PR — with nothing
# anywhere reporting an error. This fails such a PR before it can be merged.
#
# pull_request_target (not pull_request): runs in the base-repo context so the
# check can be reported on PRs from forks too. It does NOT check out or run PR
# code — the action only reads the title via GITHUB_TOKEN — so there is no
# untrusted-code execution path here.
on:
  pull_request_target:
    types: [opened, edited, reopened, synchronize]
permissions:
  pull-requests: read
jobs:
  title:
    name: Validate PR title
    runs-on: ubuntu-latest
    steps:
      - uses: amannn/action-semantic-pull-request@48f256284bd46cdaab1048c3721360e808335d50  # v6.1.1
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 4: Write `dependabot.yml`**

Create `.github/dependabot.yml`:

```yaml
version: 2
# THREE ecosystems, where the reference project needs only the first: this
# repository has a pyproject.toml and a package.json as well as pinned action
# SHAs. Each gets one grouped PR per week rather than one per dependency.
updates:
  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: "ci"
    groups:
      actions:
        patterns: ["*"]

  - package-ecosystem: pip
    directory: "/"
    schedule:
      interval: weekly
    commit-message:
      prefix: "build"
    groups:
      python:
        patterns: ["*"]

  - package-ecosystem: npm
    directory: "/src/yt_shorts/studio/web"
    schedule:
      interval: weekly
    commit-message:
      prefix: "build"
    groups:
      frontend:
        patterns: ["*"]
```

- [ ] **Step 5: Verify**

```bash
actionlint .github/workflows/codeql.yml .github/workflows/gitleaks.yml .github/workflows/pr-title-lint.yml
python3 -c "
import sys
try:
    import yaml
except ImportError:
    sys.exit('pyyaml not installed - skip this check and say so in the report')
for f in ['.github/dependabot.yml']:
    d = yaml.safe_load(open(f))
    print(f, '->', [u['package-ecosystem'] for u in d['updates']])
"
PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q
```
Expected: actionlint silent; three ecosystems listed; workflow invariants pass.

- [ ] **Step 6: Lint and commit**

```bash
python3 tools/lint.py
git add .github/workflows/codeql.yml .github/workflows/gitleaks.yml .github/workflows/pr-title-lint.yml .github/dependabot.yml
git commit -m "ci: CodeQL for both languages, a pinned gitleaks, and the PR-title gate release-please depends on"
```

---

### Task 4: The files a public repository needs

**Files:**
- Create: `CONTRIBUTING.md`
- Create: `.github/CODEOWNERS`
- Create: `.github/pull_request_template.md`
- Modify: `pyproject.toml` (`[project.urls]`)

**Interfaces:**
- Consumes: nothing.
- Produces: `CODEOWNERS`, which Task 6's ruleset requires reviews from.

- [ ] **Step 1: Fix the provisional repository URL**

Block A wrote `[project.urls]` with a guessed URL. The repository will be named
`yt-shorts`, lowercase. Correct both entries in `pyproject.toml`:

```toml
[project.urls]
Homepage = "https://github.com/jegr78/yt-shorts"
Repository = "https://github.com/jegr78/yt-shorts"
```

Remove any "provisional" comment beside them — it is no longer provisional.

- [ ] **Step 2: Write `.github/CODEOWNERS`**

```
# Default reviewer for everything. With a branch ruleset that requires review
# from Code Owners, every PR is routed to the maintainer for approval.
* @jegr78
```

- [ ] **Step 3: Write `CONTRIBUTING.md`**

It must cover, accurately and briefly:

- **Setup:** clone, `python3 -m venv .venv`, `pip install -e ".[all,dev]"`, and that `bin/yt-shorts` carries an absolute shebang into the repository's own venv (so a contributor either recreates it or uses the installed `yt-shorts`).
- **The external tools:** `ffmpeg` is required to run the suite — 61 tests fail without it — and `yt-shorts install-tools` will install it.
- **Running things:** `PYTHONPATH=src .venv/bin/pytest -q` for the suite, `python3 tools/lint.py` before every commit, and `python -m playwright install chromium` for the E2E tests (which skip cleanly without it).
- **The frontend:** `npm ci && npm run build` in `src/yt_shorts/studio/web`, and that `src/yt_shorts/studio/static/` is the committed build output which must be rebuilt and committed with any frontend change. Include the warning that a build deletes `static/` before rewriting it, so it must never run while an E2E run is in flight.
- **Conventional Commits, and why the PR TITLE matters:** it becomes the squash subject that release-please parses.
- **Where the deeper rules live:** `CLAUDE.md` for the constraints that are expensive to violate, `docs/superpowers/specs/` for the design behind each subsystem.

- [ ] **Step 4: Write `.github/pull_request_template.md`**

Short. What changed and why; how it was verified (the suite, lint, and anything
run by hand); and a reminder that the PR title must be a Conventional Commit
because it becomes the squash subject.

- [ ] **Step 5: Verify the metadata change did not break packaging**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_packaging.py -q
```
Expected: PASS — the packaging tests build the wheel and sdist for real.

- [ ] **Step 6: Lint and commit**

```bash
python3 tools/lint.py
git add CONTRIBUTING.md .github/CODEOWNERS .github/pull_request_template.md pyproject.toml
git commit -m "docs: the files a public repository needs, and the real repository URL"
```

---

### Task 5: `master` → `main`, and the last local check

**Files:**
- Modify: nothing on disk — this task renames the branch and verifies the whole tree.

**Interfaces:**
- Consumes: every workflow from Tasks 1-3, all of which already target `main`.
- Produces: a branch named `main`, which Task 6 pushes.

- [ ] **Step 1: Confirm no workflow still names `master`**

```bash
grep -rn "master" .github/ || echo "clean"
PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q -k Master
```
Expected: `clean`, and the test passes. If anything matches, fix it before renaming — a workflow pointing at a branch that no longer exists never triggers, and never says so.

- [ ] **Step 2: Rename the branch**

```bash
git branch -m master main
git branch --show-current
```
Expected: `main`.

- [ ] **Step 3: Run everything, one last time, locally**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
actionlint .github/workflows/*.yml
bin/yt-shorts --version
bin/yt-shorts doctor
```
Expected: the suite green (2544 plus the workflow tests added in Task 1), lint 0, actionlint silent, the CLI reporting `0.1.0` and doctor exiting 0.

- [ ] **Step 4: Confirm what a public clone would actually contain**

```bash
git ls-files | wc -l
git ls-files | grep -iE "\.env$|secret|token|credential|client_secret|\.pem$|\.key$" || echo "no secret-shaped paths"
git log --oneline
```
Expected: a file count, no secret-shaped paths, and a short readable commit list starting from `Initial commit`.

- [ ] **Step 5: Commit nothing, report the state**

This task has no commit of its own — the rename is a ref change. Report the
branch name, the suite count, and the file count.

---

### Task 6: Create the repository — OPERATOR-GATED

**This task must not begin until the operator has seen the result of Task 5 and
explicitly said to proceed.** Everything before this point is local and
reversible; this is the step that makes the work public and cannot be undone.

**Files:** none — this task runs commands against GitHub.

- [ ] **Step 1: Confirm the account and that the name is free**

```bash
gh auth status
gh repo view jegr78/yt-shorts 2>&1 | head -3   # expect "Could not resolve"
```
Expected: authenticated as `jegr78`, and the repository does not exist yet. If it DOES exist, stop and report — do not push into it.

- [ ] **Step 2: Create it and push**

```bash
gh repo create jegr78/yt-shorts --public --source=. --remote=origin --push \
  --description "Turn race-stream clips into branded 1080x1920 YouTube Shorts"
```

- [ ] **Step 3: Watch the first run**

```bash
gh run list --limit 5
gh run watch --exit-status
```
The first run is the workflows' real acceptance test. Expect things to need
fixing — a missing system package, a path that behaves differently on Windows.
Fix them with ordinary commits on `main` (the ruleset is not applied yet, on
purpose) until every job is green. Record each fix and its cause in the report.

- [ ] **Step 4: Set the topics**

```bash
gh repo edit jegr78/yt-shorts --add-topic youtube,shorts,video,ffmpeg,racing,python
```

- [ ] **Step 5: Apply the branch ruleset — only after CI is green**

Applying it earlier would block the very PRs that fix CI. Create a ruleset on
`main` requiring: pull requests with one approving review from Code Owners;
the status checks `lint`, `frontend`, `binary-smoke` and each
`test (<os>, <version>)` leg by its exact name as `gh run view` reports it;
linear history; and no force-push or deletion.

Read the job names back from the real run rather than assuming them:

```bash
gh run view --json jobs -q '.jobs[].name'
```

Then apply the ruleset with `gh api --method POST repos/jegr78/yt-shorts/rulesets`.
Verify it afterwards with `gh api repos/jegr78/yt-shorts/rulesets` and report
what is enforced.

- [ ] **Step 6: Enable secret-scanning push protection**

```bash
gh api --method PATCH repos/jegr78/yt-shorts \
  -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'
```

- [ ] **Step 7: Report what is left for the operator**

`RELEASE_PLEASE_TOKEN` is a fine-grained PAT only the operator can mint. Report
the exact scopes from `release-please.yml`'s header, and state plainly what
happens without it: release-please still works through the `GITHUB_TOKEN`
fallback, but CI on the Release PR needs a manual close/reopen and the binaries
arrive via the dispatch step rather than the tag push.

---

## Self-review

**Spec coverage.** B1 → Task 1. B2 (the binary-smoke ordering that closes Block A's follow-up) → Task 1's `binary-smoke` job. B3 → Task 2. B4 → Tasks 3 and 4. B5 (verifying workflows that cannot run) → Task 1's `tests/test_workflows.py`, the actionlint steps in Tasks 1-3, and Task 6 Step 3. B6 → Tasks 5 and 6. The spec's out-of-scope items (PyPI, notarisation, the wiki) have no task, correctly.

**Two things this plan deliberately does NOT assume, and instead measures:**

1. **That Vite rebuilds byte-identically.** The staleness gate depends on it, so Task 1 Step 6 builds twice and compares, with a written fallback if it does not hold. Every previous plan defect on this project came from writing something plausible and never running it.
2. **That the gitleaks checksum carried over from the reference project is still current.** Task 3 Step 2 re-reads it from upstream before committing, the same rule Block A applied to the yt-dlp pins.

**Ordering constraint worth naming:** Task 6 Step 5 (the ruleset) must come after Step 3 (a green CI run), not before. A ruleset requiring checks that have never passed blocks the pull request that would make them pass — and with `main` protected, there is no way around it except disabling the rule you just created.

**Naming consistency:** the job names `lint`, `frontend`, `test`, `binary-smoke` appear in `ci.yml`, in `tests/test_workflows.py`'s `REQUIRED` tuple, and in Task 6's ruleset. They are one contract in three places; the test is what keeps them in step.
