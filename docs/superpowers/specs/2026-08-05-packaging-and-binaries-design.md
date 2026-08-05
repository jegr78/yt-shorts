# Packaging & binaries (wheel + per-OS binary, external tool management) — design

Date: 2026-08-05
Status: approved (design), ready for implementation plan

Block A of three. See "The other two blocks" at the end for B and C, which are
scoped here only far enough to fix the interfaces this block must not break.

## Motivation

The repository is about to go public on GitHub. Today it has no `pyproject.toml`,
no version, no `CHANGELOG.md`, no `.github/`, and no git remote at all; the
current branch is `master`. There is no way to install it other than cloning it
and building a venv by hand against a dependency list that exists only as prose
in `CLAUDE.md`.

`/Users/jegr/Documents/github/gt-racing-broadcast` (racecast) is the reference
project and already solves the same problems: PyInstaller binaries on a CI
matrix, release-please, a wiki generated from the repo, and — the piece this
block leans on hardest — `src/scripts/install_tools.py`, which installs and
updates the external runtime tools through each platform's own package manager.

Three differences make racecast's answers not directly transplantable, and they
are what this design is mostly about:

| | racecast | YT-Shorts |
|---|---|---|
| Dependencies | pure stdlib | fastapi, uvicorn, pillow, fonttools, faster-whisper (→ ctranslate2, onnxruntime, av, numpy), google/openai/anthropic SDKs |
| External binaries | none required to build | `ffmpeg`, `ffprobe`, `yt-dlp` — called by bare name at ~8 sites |
| Entrypoint | `src/racecast.py` with a `main()` | `bin/yt-shorts`, dispatch inside `if __name__ == "__main__":` (lines 690-790), plus `sys.path.insert(ROOT / "src")` |
| Frontend | none | Vite/React → `studio/static/` (committed, 22 files) |
| Version | `version.txt` + release-please manifest | does not exist |

## Decided requirements

Each of these was decided explicitly during design; the reasoning is in the
matching Architecture section.

- **Both artefact shapes.** An sdist/wheel is the primary path (`pipx install`,
  and the native wheels resolve per platform on their own); PyInstaller binaries
  per OS serve the operator who has no Python.
- **ffmpeg/ffprobe/yt-dlp stay external**, and the tool ships a way to install
  and update them: `yt-shorts install-tools`, ported from racecast, plus
  `yt-shorts doctor` to verify.
- **`--onedir`, not `--onefile`.**
- **Grouped extras**: base / `[transcribe]` / `[cloud]` / `[all]` / `[dev]`.
- **Four release platforms.** Windows x64, macOS arm64, Linux x64, Linux arm64.
  macOS Intel (`macos-13`) is deliberately NOT built.
- **The Homebrew bootstrap is adopted** from racecast, confirmation gate and all.
- **No studio button in this block.** The install/check functions are cut so a
  later route can call them; the button itself is out of scope.

## Architecture

### A1 — An importable entrypoint

`console_scripts` and PyInstaller can both only call `module:function`. The
dispatch in `bin/yt-shorts`'s `__main__` block is unreachable to either, and
`sys.path.insert(0, str(ROOT / "src"))` is repository-relative — wrong inside a
wheel and wrong inside a frozen bundle.

The contents of `bin/yt-shorts` move **verbatim** into `src/yt_shorts/cli.py`.
The `__main__` block becomes `def main(argv=None) -> int` (returning instead of
`raise SystemExit`). `bin/yt-shorts` stays, as a shim:

```python
#!/…/.venv/bin/python
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from yt_shorts.cli import main
raise SystemExit(main())
```

Why the shim stays rather than being deleted: every invocation documented in
`README.md` and `CLAUDE.md` keeps working, the absolute-shebang venv workflow is
untouched, and `lint.py`'s `_python_files` still discovers the file through its
python shebang — the clause that exists specifically so this extensionless CLI
gets linted at all.

One test file must follow: `tests/test_cli.py` loads the CLI through
`SourceFileLoader` precisely because the file has no `.py` suffix. That is a
single helper, `_load_cli()`, which becomes a normal `import yt_shorts.cli`;
every test in the file goes through it.

Two additions to the CLI itself, both required by later sections:

- `--version`, which prints the resolved version (see A2) and exits 0.
- `--no-browser` for `studio`. `cmd_studio`'s `open_url` is injectable as a
  keyword today but cannot be reached from the command line, and the binary
  smoke test needs it.

### A2 — `pyproject.toml` and one version

**Build backend: hatchling.** It handles a `src/` layout and package data
without configuration acrobatics.

**Entry point:** `[project.scripts] yt-shorts = "yt_shorts.cli:main"`.

**Python:** `requires-python = ">=3.12"`. The development venv runs 3.14.6; the
binaries ship on **3.13**; the test axis covers 3.12, 3.13 and 3.14.

**Grouped extras**, mirroring how the codebase already treats these dependencies
— `subtitle_pipeline` has a documented degrade-to-"no subtitles" path and
`_google.py` raises `GoogleUnavailable`, so optionality is an existing design
property, not one invented here:

| Group | Contents |
|---|---|
| base | `pillow`, `fonttools`, `brotli`, `fastapi`, `uvicorn` |
| `[transcribe]` | `faster-whisper` (pulls ctranslate2, onnxruntime, av, numpy — roughly 300 MB) |
| `[cloud]` | `openai`, `anthropic`, `google-genai`, `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2` |
| `[all]` | `[transcribe]` + `[cloud]` |
| `[dev]` | `pytest`, `httpx`, `playwright` |

Someone who only wants to render does not pay 300 MB for a transcription model
runtime. The binaries are built with `[all]` installed.

**Package data.** `src/yt_shorts/studio/static/**` must ship in the wheel.
Equally important and easy to miss: `src/yt_shorts/studio/web/` sits *inside* the
package directory and contains `node_modules/`. It must be excluded explicitly,
or every sdist carries a node_modules tree.

**Version, one source.** `release-please`'s `release-type: simple` writes
`version.txt` at the repository root — that is its contract, so `version.txt`
exists as release-please's target. The value that code reads lives in
`src/yt_shorts/__version__.py`, carrying an `x-release-please-version`
annotation so release-please updates it in the same commit; hatchling reads its
version from that file. Nothing else declares a version.

A frozen binary is the one exception, and it is why `yt_shorts/version.py`
exists as a tiny resolver: `tools/build-binary.py --version v1.2.0` writes a
`VERSION` file into the bundle, and `resolve()` prefers that file when
`sys.frozen` is set, falling back to `__version__.py` otherwise. Without this the
binary built from tag `v1.2.0` would report `1.2.0`, and the smoke test's
`version in stdout` check — the whole point of which is to prove the built
artefact knows what it is — could not be written honestly. This is racecast's
mechanism, unchanged.

### A3 — `install-tools` (fixes) and `doctor` (verifies)

Two commands over one tool table, which is racecast's `install-tools`/`preflight`
split. The tool set is much smaller than racecast's four-plus-speedtest:
`ffmpeg`, `ffprobe`, `yt-dlp`. `ffprobe` is not separately installable — it ships
with ffmpeg — so there are three names for the *presence* check and two packages
for the *install*.

| | winget | brew | apt | pacman |
|---|---|---|---|---|
| ffmpeg | `Gyan.FFmpeg` | `ffmpeg` | `ffmpeg` | `ffmpeg` |
| yt-dlp | `yt-dlp.yt-dlp` | `yt-dlp` | **managed** | `yt-dlp` |

**yt-dlp on Linux gets the managed install, ported verbatim.** racecast's reason
applies here identically: apt's yt-dlp lags upstream far enough to fail
YouTube's bot check — the same YouTube this project downloads every clip from.
`install_ytdlp_binary()` comes across as-is: a pinned version, its SHA-256
verified against the release's own `SHA2-256SUMS`, `chmod 0o700`, and an
injectable `opener` so it is testable without network. `ytdlp_asset_tag()` maps
`(sys.platform, machine)` to the x86_64/aarch64 asset.

#### The managed tool directory and PATH

This is the part that would have been missing without racecast, and without it
the managed install is pointless: a yt-dlp downloaded into a directory that is
not on PATH is invisible to `render.py`'s `["yt-dlp", …]`. So there must be an
equivalent of racecast's `_ensure_tool_path()` — exactly one call in
`cli.py::main()`, immediately after `workspace.resolve()`, prepending the managed
directory to `os.environ["PATH"]`. The studio inherits it for free: `cmd_studio`
runs uvicorn **in the same process**, and jobs run on threads of that process. One
seam, no special case.

**The directory is `<workspace>/.tools/`, and the obvious name would have been a
bug.** `workspace.resolve()`'s last resort returns `root = repo_channels.parent`
— the **repository root** itself. Under that fallback `<workspace>/bin` *is*
`<repo>/bin/`, where `bin/yt-shorts` lives, and `<workspace>/tools` is the
repository's own `tools/`. Both would have had `install-tools` drop a binary into
the source tree. `.tools` collides with nothing and follows the precedent
`.studio.lock` already sets for a dotfile at the workspace root. It gets a
rooted `.gitignore` entry (`/.tools/`) for exactly the reason the existing
`/jobs.json`, `/moments.json` and `/settings.json` entries carry.

Second half of the same concern, which racecast measured as its issue #38: **a
macOS binary launched by double-click from Finder inherits a truncated PATH**
(`/usr/bin:/bin:/usr/sbin:/sbin`) with no Homebrew in it, so a brew-installed
ffmpeg or yt-dlp looks missing. The moment the studio is started by
double-clicking, YT-Shorts hits this identically. So when frozen **and** on
darwin, the Homebrew bin directories are prepended too. `augment_path` only ever
adds directories that genuinely exist and are genuinely absent, so a terminal
launch with a full PATH is left untouched.

#### `--update` is not a side feature

yt-dlp goes stale in weeks, not years. `yt-shorts install-tools --update`
becomes the documented step **before every session**, the role racecast gives it
before every event. On Linux `--update` also refreshes the pinned yt-dlp binary.

#### Windows

`windows_fresh_path()` is ported: winget updates the registry, not the running
process, so without re-reading the registry PATH `install-tools` reports "still
missing" immediately after its own successful install. `install_exit_ok()` comes
with it, so winget's "no applicable update" exit code counts as success.

#### macOS Homebrew bootstrap

Adopted from `installer_common.bootstrap_brew`: when brew is absent, offer the
official installer behind a `[y/N]` prompt, with `--yes` to skip the prompt. It
runs as the current user and prompts for sudo itself. Declining leaves the
manual guide, which is also what a machine with no supported package manager
gets.

#### Shape

Pure decision helpers at the top — `pick_manager`, `missing_tools`,
`install_commands`, `update_commands`, `ytdlp_asset_tag`, `manual_guide`,
`ensure_tool_path` — and a `main()` that performs. That is racecast's split and
also this codebase's style: every seam injectable (`which=`, `run=`, `opener=`),
so `tests/test_install_tools.py` runs with no package manager and no network.

`install_tools.py` joins the list in `CLAUDE.md` of modules that **must not
import FastAPI**: it is reachable from the CLI, which runs in a venv that may
never have installed it.

#### `doctor`

Reports and points at `install-tools`; it repairs nothing. It checks the three
binaries with `--version`; **the four ffmpeg filters this project actually
uses** — `overlay`, `boxblur`, `scale`, `setsar` — through `ffmpeg -filters`,
rather than trusting an assumption about how ffmpeg was built; the Python
version; the optional imports; and that the workspace resolves and is
writable. Exit 0 when everything required is present, 1 otherwise. Checks are
a pure `checks(*, which=…, run=…) -> list[Check]`, so they are testable
without subprocesses.

Not built: a check of "on Linux, the glibc floor of the shipped binary",
promised earlier in this section. It was not built deliberately, once
implementation reached it - a glibc mismatch prevents a frozen binary from
starting at all, so `doctor` could never run to report it; there is no
version of this check that is reachable from inside the process it would be
checking.

### A4 — `tools/build-binary.py`

**`--onedir`, not racecast's `--onefile`.** racecast's binary is stdlib-small, so
onefile costs nothing there. Here the bundle carries ctranslate2, onnxruntime,
av and numpy — an estimated 250-400 MB — and `--onefile` unpacks that into a
temporary directory on **every single invocation**; a `yt-shorts doctor` would
take seconds. The release archive therefore contains a directory, not one file.

What PyInstaller cannot find on its own and the build must state explicitly:

- `--add-data src/yt_shorts/studio/static → yt_shorts/studio/static`.
  `api.py:186` computes `STATIC_DIR = Path(__file__).parent / "static"`; under
  PyInstaller `__file__` points inside `_MEIPASS`, so this works **provided** the
  bundled data mirrors the package layout.
- `--hidden-import` for uvicorn's string-loaded protocol modules
  (`uvicorn.lifespan.on`, `uvicorn.protocols.http.h11_impl`,
  `uvicorn.loops.asyncio`, and the websockets implementations).
- `--collect-data` for `faster_whisper`, `av` and `onnxruntime`.

**The smoke test runs inside the build**, as it does in racecast: the build has
not succeeded until the built artefact has

1. answered `--version` with the version passed to the build,
2. run `doctor` (proving the bundle layout and the optional imports are alive),
3. run `install-tools` with no arguments without crashing, recognising the
   already-present tools,
4. started `studio --no-browser` against a temporary `YT_SHORTS_DATA`, served
   the SPA `index.html` on `GET /`, and answered one JSON API route.

Step 4 is the one that catches a missing `static/` — the exact failure class
`CLAUDE.md` describes as a blank page and "element(s) not found", where nothing
reports an error anywhere.

**Not bundled**: Whisper models (fetched from HuggingFace at runtime, hundreds of
MB, model-dependent), ffmpeg and yt-dlp (that is what `install-tools` is for),
`node_modules`, and the repository's `templates/` — `channel_admin.py` carries
its own embedded copy of the only file that matters, pinned by
`tests/test_channel_admin.py`.

### A5 — Platforms

Four runners: `windows-latest` (x64), `macos-latest` (arm64), `ubuntu-latest`
(x64), `ubuntu-24.04-arm` (arm64). macOS Intel is deliberately not built; the
macOS wheels are the heaviest part of the matrix and Intel Macs are not in the
target picture.

Assets: `yt-shorts-windows-x64.zip`, `yt-shorts-macos-arm64.tar.gz`,
`yt-shorts-linux-x64.tar.gz`, `yt-shorts-linux-arm64.tar.gz`, each holding the
onedir tree; plus the sdist and wheel, built once on ubuntu.

**Unsigned macOS binaries are quarantined by Gatekeeper.** Notarisation needs a
paid Apple Developer account and is out of scope for this block. The workaround
(`xattr -d com.apple.quarantine`) belongs prominently in the wiki, not in a
footnote — otherwise it arrives as a bug report.

## Testing

New, in the existing style — pure functions with injected seams, assertions on
what actually happens rather than on a function having returned:

- `tests/test_install_tools.py` — the decision helpers across all four package
  managers; `ytdlp_asset_tag` over platform/arch pairs; `install_ytdlp_binary`
  with an injected `opener`, including a **checksum mismatch raising** rather
  than writing a file; `ensure_tool_path` adding the managed directory exactly
  once and leaving a full PATH untouched; the frozen-darwin Homebrew case.
- `tests/test_doctor.py` — each check against injected `which`/`run`, including
  an ffmpeg whose `-filters` output lacks one of the four required filters.
- `tests/test_packaging.py` — `pyproject.toml`, `__version__.py` and
  `version.txt` agree; the built wheel contains `studio/static/index.html`; the
  built wheel and sdist contain **no** `web/` and no `node_modules`.
- `tests/test_cli.py` — switched to a normal import; `--version` and
  `--no-browser` covered.

`tools/build-binary.py` is picked up by `tools/lint.py` automatically. The whole
suite must stay green and `python3 tools/lint.py` must exit 0, as always.

## Out of scope

- A studio "install tools" button. The functions are cut so a later route can
  call them; the route and the button are not built here.
- macOS notarisation and code signing.
- Publishing to PyPI. This block produces the wheel and attaches it to the
  GitHub release; whether it also goes to PyPI is a decision for block B.
- Any change to rendering, overlay, detection or upload behaviour. The six
  pinned overlay hashes must not move, and nothing in this block touches the
  drawing path.

## The other two blocks

Scoped only far enough to fix the interfaces this block must not break; each
gets its own spec.

**B — CI, release-please, public readiness.** Seven workflows on racecast's
model, every action pinned by SHA. The tiered matrix: ubuntu runs the full suite
including the Playwright E2E, macOS and Windows run the non-E2E tests on the
ship Python only, and the Python axis (3.12/3.13/3.14) is ubuntu-only. The npm
build is **its own job before pytest and never parallel with it** — the
measured pseudo-flakiness recorded in `CLAUDE.md` — plus a gate that the
committed `static/` matches a fresh build. `release-please` with a PAT and the
documented `GITHUB_TOKEN` fallback. `pr-title-lint` is not optional: release-please
parses the squash subject. Plus the MIT `LICENSE`, `CONTRIBUTING.md`,
`CODEOWNERS`, dependabot, a gitleaks scan over the **whole** history, and
renaming `master` to `main`.

**C — Wiki.** `docs/wiki/*.md` (at the repository root, not inside the package —
racecast keeps its wiki under `src/docs/wiki/`, but here anything under
`src/yt_shorts/` would ship in the wheel), `tools/sync-wiki.py`,
`tools/check-wiki-links.py` and a dispatch-only `wiki.yml`. The 84k README's 20
chapters move across verbatim and the README becomes a landing page. The macOS
quarantine workaround from A5 lands here.
