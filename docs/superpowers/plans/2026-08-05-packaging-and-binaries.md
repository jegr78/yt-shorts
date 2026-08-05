# Packaging & binaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make YT-Shorts installable — as a wheel and as a per-OS binary — and give it a supported way to install and update the external tools (`ffmpeg`, `ffprobe`, `yt-dlp`) it shells out to.

**Architecture:** The CLI's dispatch moves out of `bin/yt-shorts`'s `__main__` block into an importable `yt_shorts/cli.py:main()`, which both `console_scripts` and PyInstaller can call; `bin/yt-shorts` stays as a shim so every documented invocation keeps working. `pyproject.toml` (hatchling) declares grouped extras so a render-only install does not pay for a 300 MB transcription runtime. `yt_shorts/install_tools.py` is ported from the racecast reference project: pure decision helpers plus a `main()` that drives winget/brew/apt/pacman, with a pinned, checksum-verified yt-dlp binary on Linux dropped into `<workspace>/.tools/`, which `ensure_tool_path()` prepends to `PATH` at CLI startup.

**Tech Stack:** Python 3.12+ (stdlib only for the new modules), hatchling, PyInstaller (`--onedir`), pytest.

**Reference project:** `<racecast-repo>` — **read-only**. The files ported from it are `src/scripts/install_tools.py`, `src/scripts/installer_common.py`, `src/racecast.py`'s `augment_path`/`_ensure_tool_path`, and `tools/build-binary.py`.

**Spec:** `docs/superpowers/specs/2026-08-05-packaging-and-binaries-design.md`

## Global Constraints

- **`PYTHONPATH=src` is mandatory for every Python invocation.** Full suite: `PYTHONPATH=src .venv/bin/pytest -q`. One file: `PYTHONPATH=src .venv/bin/pytest tests/test_x.py -q`.
- **`python3 tools/lint.py` must exit 0** before every commit. It needs no `PYTHONPATH`. Lint is not optional: it is the only thing in this repo that catches a duplicate test-class name, which pytest silently drops with no warning.
- **The six pinned overlay hashes in `tests/test_event_layer_no_regression.py` must not move.** Nothing in this plan touches the drawing path; if a hash changes, something is wrong with the change, not with the hash.
- **No test may start real background work.** `tests/conftest.py`'s autouse `_no_real_job_starter` replaces every `studio.jobs.start_*_job` with one that calls `pytest.fail`. Do not opt out of it in any test written here.
- **Never run `npm run build` while an E2E run is in flight.** A build deletes `src/yt_shorts/studio/static/` before rewriting it; a page loaded in that window gets a 500 or a 404 and the failure reads exactly like flakiness. Serialise strictly.
- **Language is English** — code, comments, docs, folder names, commit messages.
- **Commit messages are Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `build:`, `refactor:`), because release-please will parse them in block B.
- **New modules must not import FastAPI.** `install_tools.py` and `doctor.py` are reachable from the CLI, which runs in a venv that may never have installed it.
- **Never fabricate a checksum or a version number.** Task 5 fetches both from the upstream release and pins the fetched values.
- **A check that greps tool output must match the field it means, not the whole blob.** Measured on this project: `"scale" in ffmpeg_filters_output` is always true, because "grayscale" appears in the descriptions of `alphaextract` and `extractplanes`. A presence check that cannot fail is worse than no check, because it prints a confident claim. Parse the column; compare exactly; and pin it with a crafted input that fails against the loose version.
- **No test may reach the network.** `install_tools.run()` has injectable seams for `which`, `call` and `printer`, but `install_ytdlp_binary` is NOT one of them — it is reached through the module attribute, so a test that drives the Linux branch must `monkeypatch.setattr(it, "install_ytdlp_binary", …)`. `run()` swallows a download failure into its `failed` list, so an unpatched test does not fail; it silently downloads ~40 MB and passes. Check timing, not just green: a test in this file that takes more than a few hundredths of a second is doing something it should not.
- **`ffmpeg` must not be reinstalled or upgraded on this machine.** The separate racecast broadcast project depends on this exact binary (it is built without `libfreetype`/`libass`). `install-tools` is being *written* here, not run against this workstation; test it against injected seams, never against the real package manager.

---

### Task 1: Version resolution

The single source of the version, plus the one exception a frozen build makes. Everything else in this plan depends on it: `pyproject.toml` reads it, `--version` prints it, and the binary's smoke test asserts on it.

**Files:**
- Create: `src/yt_shorts/__version__.py`
- Create: `src/yt_shorts/version.py`
- Create: `version.txt`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `yt_shorts.version.resolve(read=None) -> str` and `yt_shorts.version.bundled_version_path() -> Path | None`. Task 2 calls `resolve()` for `--version`; Task 3 points hatchling at `src/yt_shorts/__version__.py`; Task 9's build writes the `VERSION` file `resolve()` prefers.

- [ ] **Step 1: Write the failing test**

Create `tests/test_version.py`:

```python
"""The version resolver: what the package declares, and what a frozen build overrides it with."""

from pathlib import Path

from yt_shorts import version


class TestTheDeclaredVersion:
    def test_an_unfrozen_process_reports_the_packages_own_version(self, monkeypatch):
        monkeypatch.delattr("sys._MEIPASS", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        assert version.resolve() == declared

    def test_there_is_no_bundle_path_when_not_frozen(self, monkeypatch):
        monkeypatch.delattr("sys._MEIPASS", raising=False)

        assert version.bundled_version_path() is None


class TestTheFrozenOverride:
    """tools/build-binary.py is handed the git TAG (`v1.2.0`) and writes it into
    the bundle, so a binary reports the tag it was built from - not the `1.2.0`
    in __version__.py. Without this the build's own smoke test could not check
    that the artefact knows what it is."""

    def test_a_bundled_version_file_wins(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)

        assert version.resolve(read=lambda path: "v9.9.9\n") == "v9.9.9"

    def test_the_bundle_path_sits_at_the_root_of_the_unpacked_bundle(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)

        assert version.bundled_version_path() == Path("/nowhere/VERSION")

    def test_an_unreadable_version_file_falls_back_rather_than_raising(self, monkeypatch):
        """A version string is not worth failing a command over."""
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        def explode(path):
            raise OSError("no such file")

        assert version.resolve(read=explode) == declared

    def test_an_empty_version_file_falls_back_too(self, monkeypatch):
        monkeypatch.setattr("sys._MEIPASS", "/nowhere", raising=False)
        from yt_shorts.__version__ import __version__ as declared

        assert version.resolve(read=lambda path: "  \n") == declared


class TestTheTwoFilesAgree:
    """version.txt is release-please's write target; __version__.py is what code
    reads. release-please updates both in one commit, so they must never drift."""

    def test_version_txt_matches_the_declared_version(self):
        from yt_shorts.__version__ import __version__ as declared

        root = Path(__file__).resolve().parent.parent
        assert (root / "version.txt").read_text(encoding="utf-8").strip() == declared
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_version.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.version'`

- [ ] **Step 3: Write the version files**

Create `src/yt_shorts/__version__.py`:

```python
"""The package version. release-please rewrites the literal below on every
release (see the annotation), and hatchling reads its version from this file -
so the published package and the running code can never disagree."""

__version__ = "0.1.0"  # x-release-please-version
```

Create `version.txt` with exactly:

```
0.1.0
```

Create `src/yt_shorts/version.py`:

```python
"""The version to report, and the one exception a frozen build makes.

`__version__.py` carries the number release-please writes, and hatchling reads
its version from that same file. A PyInstaller build is the exception:
`tools/build-binary.py` is handed the git TAG (`v1.2.0`), writes it into the
bundle as a `VERSION` file, and this resolver prefers that file when frozen.
Without it, a binary built from tag `v1.2.0` would report `1.2.0`, and the
build's own smoke test - whose whole job is to prove the artefact knows what it
is - could not be written honestly. Ported from racecast, unchanged in spirit.
"""

import sys
from pathlib import Path

from yt_shorts.__version__ import __version__ as _declared


def bundled_version_path() -> Path | None:
    """The `VERSION` file inside a PyInstaller bundle, or None when not frozen."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "VERSION" if base else None


def resolve(read=None) -> str:
    """The version to report. A readable, non-empty bundled `VERSION` wins;
    otherwise the declared package version. Never raises - a version string is
    not worth failing a command over. `read` is the injectable seam for tests."""
    path = bundled_version_path()
    if path is not None:
        read = read or (lambda p: p.read_text(encoding="utf-8"))
        try:
            text = read(path).strip()
        except OSError:
            text = ""
        if text:
            return text
    return _declared
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_version.py -q`
Expected: PASS, 7 tests.

- [ ] **Step 5: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/__version__.py src/yt_shorts/version.py version.txt tests/test_version.py
git commit -m "feat(version): one declared version, and the file a frozen build overrides it with"
```

---

### Task 2: An importable CLI entrypoint

`console_scripts` and PyInstaller can both only call `module:function`. The dispatch in `bin/yt-shorts`'s `if __name__ == "__main__":` block (lines 690-790) is unreachable to either, and `sys.path.insert(0, str(ROOT / "src"))` at line 14 is repository-relative — wrong in a wheel and wrong in a frozen bundle.

**Files:**
- Create: `src/yt_shorts/cli.py` (receives the whole of `bin/yt-shorts`, verbatim except the changes below)
- Modify: `bin/yt-shorts` (becomes a shim)
- Modify: `tests/test_cli.py:29-36` (`CLI_PATH` and `_load_cli`)

**Interfaces:**
- Consumes: `yt_shorts.version.resolve()` from Task 1.
- Produces: `yt_shorts.cli.main(argv: list[str] | None = None) -> int`. Task 3 points `[project.scripts]` at it; Task 6 adds one call inside it; Tasks 7 and 8 add two commands to it; Task 9's PyInstaller entry point is it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
class TestTheEntrypointIsImportable:
    """console_scripts and PyInstaller can only call `module:function`. The
    dispatch used to live in bin/yt-shorts's __main__ block, which neither can
    reach, so `main` has to be a real importable function."""

    def test_main_is_importable_and_callable(self):
        from yt_shorts.cli import main

        assert callable(main)

    def test_main_returns_an_exit_code_rather_than_raising_systemexit(self, capsys):
        from yt_shorts.cli import main

        assert main(["definitely-not-a-command"]) == 2
        assert "yt-shorts" in capsys.readouterr().err

    def test_no_arguments_prints_the_usage_and_returns_two(self, capsys):
        from yt_shorts.cli import main

        assert main([]) == 2
        assert "harvest" in capsys.readouterr().err


class TestTheVersionFlag:
    def test_version_prints_the_resolved_version_and_returns_zero(self, capsys):
        from yt_shorts import version
        from yt_shorts.cli import main

        assert main(["--version"]) == 0
        assert version.resolve() in capsys.readouterr().out

    def test_the_short_form_works_too(self, capsys):
        from yt_shorts.cli import main

        assert main(["-V"]) == 0
        assert capsys.readouterr().out.strip()


class TestTheNoBrowserFlag:
    """cmd_studio's `open_url` is injectable as a keyword but was unreachable
    from the command line. Task 9's binary smoke test needs it: a build agent
    has no browser and must not have one opened at it.

    These reach main()'s workspace resolution, which conftest.py's autouse
    `_isolated_resolved_workspace` already points at a fixture-owned root -
    that is why none of them touch YT_SHORTS_DATA. Setting that variable would
    do nothing anyway: the fixture patches the RESOLVER, not the environment."""

    def test_no_browser_suppresses_the_opener(self, monkeypatch):
        from yt_shorts import cli

        opened = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: opened.append(open_url) or 0)
        assert cli.main(["studio", "--no-browser"]) == 0
        opened[0]("http://127.0.0.1:8000/")  # the injected opener must do nothing

    def test_the_flag_is_not_mistaken_for_an_identifier(self, monkeypatch):
        """`studio` accepts 0 or 1 identifier; the flag must be stripped BEFORE
        that count is checked, or `studio --no-browser erf` would be refused."""
        from yt_shorts import cli

        seen = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: seen.append(identifier) or 0)
        assert cli.main(["studio", "--no-browser", "erf"]) == 0
        assert seen == ["erf"]

    def test_without_the_flag_the_real_opener_is_used(self, monkeypatch):
        from yt_shorts import cli

        seen = []
        monkeypatch.setattr(cli, "cmd_studio",
                            lambda identifier=None, *, open_url: seen.append(open_url) or 0)
        assert cli.main(["studio"]) == 0
        assert seen == [cli._open_browser_soon]
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q -k "Entrypoint or VersionFlag or NoBrowser"`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.cli'`

- [ ] **Step 3: Move the file**

```bash
git mv bin/yt-shorts src/yt_shorts/cli.py
```

Then edit `src/yt_shorts/cli.py`:

1. **Delete lines 1 and 13-14** — the shebang, `ROOT = ...` and `sys.path.insert(...)`. A module inside the package never needs to put its own package on the path.
2. **Delete every `# noqa: E402` comment** on the imports (lines 16-39). They existed only because `sys.path.insert` sat above the imports; with that gone the imports are at module top and the suppressions become lies.
3. **Add** to the imports: `from yt_shorts import version`.
4. **Keep the module docstring**, extended with the two new flags:

```python
"""Command line: yt-shorts {harvest|render|gallery|migrate|upload} <channel>/<event>
                yt-shorts auth <channel>
                yt-shorts studio [--no-browser] [<channel>[/<event>]]
                yt-shorts detect <channel>/<event> <video-id>   scan a stream for moments
                yt-shorts --version
"""
```

5. **Replace the whole `if __name__ == "__main__":` block** (from line 690 to the end) with the function below. Every line of the old block is preserved; the only changes are the `def`/indent, `raise SystemExit(x)` → `return x`, `sys.argv[1:]` → the `argv` parameter, and the three new blocks marked with comments.

```python
def main(argv: list[str] | None = None) -> int:
    """The command line, as one callable. Returns an exit code rather than
    raising SystemExit, so `console_scripts` and PyInstaller can both call it
    and so tests can assert on the code without catching an exception."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("--version", "-V"):
        print(f"yt-shorts {version.resolve()}")
        return 0

    # Strip `--no-browser` BEFORE the per-command argument counts below, so
    # `studio --no-browser erf` still reads `erf` as the identifier.
    no_browser = "--no-browser" in args
    args = [a for a in args if a != "--no-browser"]

    command = args[0] if args else None
    if command not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    # `studio` accepts 0 or 1 identifier (no arg opens the start screen; a
    # channel or channel/event deep-links); every other command needs exactly
    # one channel/event.
    if command == "studio":
        if len(args) > 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        identifier = args[1] if len(args) == 2 else None
    elif command == "detect":
        # detect takes TWO arguments: the event and the stream's video id.
        if len(args) != 3:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        identifier, video_id = args[1], args[2]
    else:
        if len(args) != 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        identifier = args[1]

    try:
        space = workspace.resolve()
    except workspace.WorkspaceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(space.describe(), file=sys.stderr)

    # Every command writes to the workspace's central log, and echoes to the
    # console on a TTY. Pruning runs once per invocation: it is the only
    # deletion authority (see logsetup.prune_old_logs) and a CLI run is the
    # natural, low-frequency moment to do it. Both are best-effort - a tool
    # that refuses to render because it could not open a log file would be
    # worse than one that renders without logging.
    try:
        log_dir = workspace.logs_dir(space.root)
        logsetup.configure_logging("ytshorts", log_dir / workspace.CENTRAL_LOG_NAME)
        logsetup.prune_old_logs(log_dir)
        logging.getLogger("ytshorts.cli").info("%s (%s)", " ".join(args),
                                               space.describe())
    except OSError as error:
        # Logging must never be the reason a render or a job dies - a workspace
        # whose logs/ cannot be created still lets the CLI proceed.
        print(f"WARNING: logging unavailable: {error}", file=sys.stderr)

    # migrate runs before profile_load: the profile - channel.json,
    # brand.json, fonts, layout.py - is itself part of what a migration
    # copies, so requiring one to already be loadable in the workspace
    # would make migrate unable to do its own job on a first run.
    if command == "migrate":
        return cmd_migrate(identifier)

    # auth runs before profile_load: it needs only the channel's channel.json
    # (for its YouTube id), not a full event profile, and its identifier is a
    # bare channel name (e.g. 'erf'), not 'channel/event'.
    if command == "auth":
        return cmd_auth(identifier, space.channels_dir, space.root / "auth")

    # studio runs before profile_load: it is workspace-level (no bound event),
    # lists the workspace's channels and resolves each event from the URL path.
    if command == "studio":
        opener = (lambda url: None) if no_browser else _open_browser_soon
        return cmd_studio(identifier, open_url=opener)

    # Imported here, not at module scope: yt_shorts.profile resolves the
    # workspace again at ITS OWN import time (CHANNELS_DIR = ...), and by
    # this point workspace.resolve() above has already succeeded once
    # against the same environment, so this second, internal resolution
    # cannot itself surface a NEW WorkspaceError - the operator has already
    # seen the understandable message above if there was one to see.
    from yt_shorts.profile import ProfileError
    from yt_shorts.profile import load as profile_load

    try:
        profile = profile_load(identifier)
    except ProfileError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if command == "harvest":
        return cmd_harvest(profile.event_dir)
    if command == "render":
        return cmd_render(profile.event_dir, profile.config, profile.channel["footer"])
    if command == "gallery":
        return cmd_gallery(profile.event_dir)
    if command == "upload":
        return cmd_upload(profile.event_dir, profile.config, profile.channel,
                          space.root / "auth", profile.channel_name)
    if command == "detect":
        return cmd_detect(profile.event_dir, profile.config, video_id, space.root,
                          profile.channel.get("channel_url"))
    print(f"ERROR: '{command}' is not implemented yet", file=sys.stderr)
    return 2
```

Note the one behaviour change worth knowing about: the log line now records the *filtered* `args`, not raw `sys.argv[1:]`, so `--no-browser` does not appear in the central log. That is correct — it logs the command, not the invocation's presentation flags.

- [ ] **Step 4: Write the shim**

Create `bin/yt-shorts` (make it executable — `git mv` took the old one away):

```python
#!<repo>/.venv/bin/python
"""Repo-local launcher for the yt-shorts command line.

The command itself lives in `yt_shorts.cli` so that `console_scripts` and
PyInstaller - which can only call `module:function` - can both reach it. This
file stays because every invocation in README.md and CLAUDE.md names it, and
because its absolute shebang is what makes the repo's venv the interpreter
without anyone having to activate it. A venv is NOT relocatable: if this
project directory moves, recreate .venv and fix the shebang above.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from yt_shorts.cli import main  # noqa: E402 - the path insert above must come first

raise SystemExit(main())
```

```bash
chmod +x bin/yt-shorts
```

- [ ] **Step 5: Point the existing tests at the module**

In `tests/test_cli.py`, replace lines 1-36 (the docstring's second paragraph, the `CLI_PATH` constant and `_load_cli`) with:

```python
"""Tests for the command line (`yt_shorts.cli`, invoked as bin/yt-shorts).

build_short is stubbed so the tests run without network and without ffmpeg:
the stub calls the real ytdlp_command() check (the same place where the real
failure originates), but instead of a real download it only creates an empty
target file.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from yt_shorts import clipstore
from yt_shorts import editorial
from yt_shorts import subtitle_pipeline
from yt_shorts.glossary import EMPTY as GLOSSARY_EMPTY
from yt_shorts.harvest import ClipEntry
from yt_shorts.lock import EventLock
from yt_shorts.profile import load as profile_load
from yt_shorts.render import ytdlp_command


def _load_cli():
    """The CLI module. It used to be loaded through SourceFileLoader because
    bin/yt-shorts has no .py suffix; the code now lives in the package, so a
    plain import is all this needs."""
    import yt_shorts.cli

    return yt_shorts.cli
```

Delete the now-unused `import importlib.util` and `from importlib.machinery import SourceFileLoader`. All four `_load_cli()` call sites keep working unchanged.

- [ ] **Step 6: Run the whole CLI file, then the whole suite**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q
PYTHONPATH=src .venv/bin/pytest -q
```
Expected: both PASS. The full suite matters here: `tests/test_lint.py`'s repo-clean guards and any test that references the CLI path run in it.

- [ ] **Step 7: Prove the shim still runs**

```bash
bin/yt-shorts --version
bin/yt-shorts        # expect the usage on stderr and exit 2
echo "exit: $?"
```
Expected: a version line; then the usage text and `exit: 2`.

- [ ] **Step 8: Lint, then commit**

```bash
python3 tools/lint.py
git add -A bin/yt-shorts src/yt_shorts/cli.py tests/test_cli.py
git commit -m "refactor(cli): a main() an entrypoint can actually call, and a shim where the CLI used to be"
```

---

### Task 3: `pyproject.toml`, the wheel, and the LICENSE

**Files:**
- Create: `pyproject.toml`
- Create: `LICENSE`
- Test: `tests/test_packaging.py`

**Interfaces:**
- Consumes: `src/yt_shorts/__version__.py` (Task 1), `yt_shorts.cli:main` (Task 2).
- Produces: an installable wheel and sdist; `pip install -e ".[dev]"` as the developer setup. No later task imports from this one.

The LICENSE lands here rather than in block B because `pyproject.toml` declares `license = "MIT"` and a declared license with no file is a lie the packaging metadata tells.

- [ ] **Step 1: Write the failing test**

Create `tests/test_packaging.py`:

```python
"""What the built artefacts must and must not contain.

These tests BUILD the package (hatchling, into a temp dir) rather than reading
pyproject.toml and trusting it. The include/exclude patterns are the part that
is easy to get subtly wrong, and a pattern that silently matches nothing looks
identical to one that works - right up until an sdist ships a node_modules tree.
"""

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The wheel and sdist, built once for this module. Skips rather than fails
    when `build` is not installed - packaging is not every contributor's venv."""
    pytest.importorskip("build", reason="pip install build")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([sys.executable, "-m", "build", "--outdir", str(out), str(ROOT)],
                   check=True, capture_output=True)
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1, f"unexpected artefacts: {list(out.iterdir())}"
    return wheels[0], sdists[0]


def _wheel_names(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def _sdist_names(sdist):
    with tarfile.open(sdist) as tf:
        return tf.getnames()


class TestTheStudioFrontendShips:
    """studio/static/ is the BUILT frontend, committed deliberately so the tool
    runs from a clone with no npm install. If it misses the wheel, the studio
    serves a blank page and every assertion downstream dies of 'element(s) not
    found' - with no error anywhere."""

    def test_the_wheel_carries_the_spa_entry_point(self, built):
        wheel, _ = built
        assert "yt_shorts/studio/static/index.html" in _wheel_names(wheel)

    def test_the_wheel_carries_the_static_assets(self, built):
        wheel, _ = built
        assets = [n for n in _wheel_names(wheel)
                  if n.startswith("yt_shorts/studio/static/assets/")]
        assert assets, "no built frontend assets in the wheel"


class TestTheFrontendSourceDoesNot:
    """src/yt_shorts/studio/web/ sits INSIDE the package directory and holds
    node_modules. Without an explicit exclude it rides along in every artefact."""

    def test_no_web_sources_in_the_wheel(self, built):
        wheel, _ = built
        assert not [n for n in _wheel_names(wheel) if "studio/web/" in n]

    def test_no_web_sources_in_the_sdist(self, built):
        _, sdist = built
        assert not [n for n in _sdist_names(sdist) if "studio/web/" in n]

    def test_no_node_modules_anywhere(self, built):
        wheel, sdist = built
        assert not [n for n in _wheel_names(wheel) + _sdist_names(sdist)
                    if "node_modules" in n]


class TestTheConsoleScript:
    def test_the_wheel_declares_the_yt_shorts_entry_point(self, built):
        wheel, _ = built
        with zipfile.ZipFile(wheel) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".dist-info/entry_points.txt"))
            text = zf.read(name).decode("utf-8")
        assert "yt-shorts = yt_shorts.cli:main" in text


class TestTheVersionIsNotDeclaredTwice:
    def test_the_wheel_version_matches_the_declared_version(self, built):
        wheel, _ = built
        from yt_shorts.__version__ import __version__ as declared

        assert f"-{declared}-" in wheel.name or wheel.name.startswith(f"yt_shorts-{declared}")


class TestTheLicenseShips:
    def test_a_license_file_exists_in_the_repository(self):
        assert (ROOT / "LICENSE").is_file()

    def test_the_wheel_carries_it(self, built):
        wheel, _ = built
        assert [n for n in _wheel_names(wheel) if n.endswith("LICENSE")]
```

- [ ] **Step 2: Run it to make sure it fails**

```bash
.venv/bin/pip install build
PYTHONPATH=src .venv/bin/pytest tests/test_packaging.py -q
```
Expected: FAIL — the build errors out because there is no `pyproject.toml`.

- [ ] **Step 3: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "yt-shorts"
dynamic = ["version"]
description = "Turn race-stream clips into branded 1080x1920 YouTube Shorts"
readme = "README.md"
requires-python = ">=3.12"
license = "MIT"
license-files = ["LICENSE"]
authors = [{ name = "Jens Gross" }]
keywords = ["youtube", "shorts", "video", "racing", "ffmpeg"]
classifiers = [
  "Environment :: Console",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: 3.14",
  "Topic :: Multimedia :: Video",
]

# The base install renders and runs the studio. Everything a render can degrade
# without - transcription, the cloud model providers, YouTube upload - is an
# extra, because the code already treats them that way: subtitle_pipeline
# degrades to "no subtitles" and _google.py raises GoogleUnavailable. Someone
# who only wants to render should not pay 300 MB for a model runtime.
dependencies = [
  "pillow>=11",
  "fonttools>=4.55",
  "brotli>=1.1",
  "fastapi>=0.115",
  "uvicorn>=0.34",
]

[project.optional-dependencies]
transcribe = ["faster-whisper>=1.1"]
cloud = [
  "openai>=1.60",
  "anthropic>=0.45",
  "google-genai>=1.0",
  "google-api-python-client>=2.150",
  "google-auth-oauthlib>=1.2",
  "google-auth-httplib2>=0.2",
]
all = ["yt-shorts[transcribe,cloud]"]
dev = ["pytest>=8", "httpx>=0.28", "playwright>=1.49", "build>=1.2"]

[project.scripts]
yt-shorts = "yt_shorts.cli:main"

[tool.hatch.version]
path = "src/yt_shorts/__version__.py"

[tool.hatch.build.targets.wheel]
packages = ["src/yt_shorts"]
# src/yt_shorts/studio/web/ is the frontend SOURCE (and node_modules). The BUILT
# output next to it, studio/static/, is what ships - see .gitignore's own note.
exclude = ["src/yt_shorts/studio/web"]

[tool.hatch.build.targets.sdist]
include = [
  "src/yt_shorts",
  "bin",
  "tools",
  "tests",
  "README.md",
  "CLAUDE.md",
  "LICENSE",
  "version.txt",
  "pytest.ini",
  "ruff.toml",
]
exclude = ["src/yt_shorts/studio/web"]
```

If `TestTheFrontendSourceDoesNot` still fails, the exclude pattern is not matching — try `"src/yt_shorts/studio/web/**"`. Let the test decide; that is why it builds rather than reads.

- [ ] **Step 4: Write the LICENSE**

Create `LICENSE` with the standard MIT text, `Copyright (c) 2026 Jens Gross`.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_packaging.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 6: Prove the wheel actually installs and runs**

```bash
.venv/bin/python -m build --outdir /tmp/ytshorts-dist .
python3 -m venv /tmp/ytshorts-check
/tmp/ytshorts-check/bin/pip install -q /tmp/ytshorts-dist/*.whl
/tmp/ytshorts-check/bin/yt-shorts --version
rm -rf /tmp/ytshorts-check /tmp/ytshorts-dist
```
Expected: the version line, from a venv that never saw this repository's `src/`.

- [ ] **Step 7: Lint, then commit**

```bash
python3 tools/lint.py
git add pyproject.toml LICENSE tests/test_packaging.py
git commit -m "build: an installable package, with the frontend source kept out of it"
```

---

### Task 4: `install_tools.py` — the decision helpers

Pure functions only: which package manager, which packages, which commands. No subprocess runs in this task. Ported from racecast's `src/scripts/install_tools.py` and `installer_common.py`, reduced to this project's two installable packages.

**Files:**
- Create: `src/yt_shorts/install_tools.py`
- Modify: `CLAUDE.md` (the "must not import FastAPI" list)
- Test: `tests/test_install_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces, all used by Tasks 5-8:
  - `TOOLS: tuple[str, ...]` — `("ffmpeg", "ffprobe", "yt-dlp")`
  - `PACKAGES: tuple[str, ...]` — `("ffmpeg", "yt-dlp")`
  - `pick_manager(platform: str, which=shutil.which) -> str | None`
  - `missing_tools(which=shutil.which) -> list[str]`
  - `install_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]`
  - `update_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]`
  - `manual_guide(platform, manager=None) -> str`
  - `install_exit_ok(manager, code) -> bool`
  - `confirmed(answer: str) -> bool`
  - `find_brew(which=shutil.which, exists=os.path.exists) -> str | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_install_tools.py`:

```python
"""The install-tools decision helpers: which manager, which packages, which
commands. Every seam is injected - no test here touches a real package manager,
and none may. This machine's ffmpeg is deliberately a specific build the
racecast project depends on (CLAUDE.md, "Hard constraints")."""

import pytest

from yt_shorts import install_tools as it


class TestTheToolSet:
    def test_three_names_are_checked_for_presence(self):
        assert it.TOOLS == ("ffmpeg", "ffprobe", "yt-dlp")

    def test_but_only_two_packages_are_installable(self):
        """ffprobe is not separately installable - it ships with ffmpeg."""
        assert it.PACKAGES == ("ffmpeg", "yt-dlp")


class TestPickingTheManager:
    def test_windows_uses_winget_when_present(self):
        assert it.pick_manager("win32", which=lambda n: "/w/winget") == "winget"

    def test_windows_without_winget_has_none(self):
        assert it.pick_manager("win32", which=lambda n: None) is None

    def test_macos_uses_brew_when_present(self):
        assert it.pick_manager("darwin", which=lambda n: "/opt/homebrew/bin/brew") == "brew"

    def test_macos_without_brew_has_none(self):
        """The caller then offers the Homebrew bootstrap - see Task 7."""
        assert it.pick_manager("darwin", which=lambda n: None) is None

    def test_linux_prefers_apt(self):
        assert it.pick_manager("linux", which=lambda n: "/usr/bin/" + n) == "apt"

    def test_linux_falls_back_to_pacman(self):
        which = lambda n: "/usr/bin/pacman" if n == "pacman" else None
        assert it.pick_manager("linux", which=which) == "pacman"

    def test_linux_with_neither_has_none(self):
        assert it.pick_manager("linux", which=lambda n: None) is None


class TestMissingTools:
    def test_all_present_is_empty(self):
        assert it.missing_tools(which=lambda n: "/usr/bin/" + n) == []

    def test_only_the_absent_ones_are_reported(self):
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        assert it.missing_tools(which=which) == ["yt-dlp"]


class TestInstallCommands:
    def test_winget_installs_one_package_per_command(self):
        cmds = it.install_commands("winget", ["ffmpeg", "yt-dlp"])
        assert len(cmds) == 2
        assert cmds[0][:4] == ["winget", "install", "--id", "Gyan.FFmpeg"]
        assert cmds[1][3] == "yt-dlp.yt-dlp"

    def test_brew_installs_everything_in_one_command(self):
        assert it.install_commands("brew", ["ffmpeg", "yt-dlp"]) == \
            [["brew", "install", "ffmpeg", "yt-dlp"]]

    def test_brew_is_invoked_by_absolute_path_when_given_one(self):
        """A freshly bootstrapped brew is not on this process's PATH."""
        cmds = it.install_commands("brew", ["ffmpeg"], brew_path="/opt/homebrew/bin/brew")
        assert cmds[0][0] == "/opt/homebrew/bin/brew"

    def test_apt_installs_only_ffmpeg(self):
        """yt-dlp on Linux is a managed download, not an apt package - apt's
        lags upstream far enough to fail YouTube's bot check."""
        cmds = it.install_commands("apt", ["ffmpeg", "yt-dlp"])
        assert cmds[-1][-1:] == ["ffmpeg"]
        assert not any("yt-dlp" in c for cmd in cmds for c in cmd)

    def test_apt_refreshes_its_index_first(self):
        """A fresh cloud VM cannot locate the package otherwise."""
        cmds = it.install_commands("apt", ["ffmpeg"])
        assert cmds[0][-2:] == ["apt-get", "update"]

    def test_apt_prepends_sudo_when_asked(self):
        cmds = it.install_commands("apt", ["ffmpeg"], sudo=True)
        assert all(cmd[0] == "sudo" for cmd in cmds)

    def test_apt_with_nothing_to_install_emits_nothing(self):
        assert it.install_commands("apt", ["yt-dlp"]) == []

    def test_pacman_installs_both_without_refreshing(self):
        """Deliberately NOT -Sy: refreshing without upgrading is Arch's
        partial-upgrade trap."""
        cmds = it.install_commands("pacman", ["ffmpeg", "yt-dlp"])
        assert len(cmds) == 1
        assert "-Sy" not in cmds[0]
        assert cmds[0][-2:] == ["ffmpeg", "yt-dlp"]

    def test_an_unknown_manager_emits_nothing(self):
        assert it.install_commands("nix", ["ffmpeg"]) == []


class TestUpdateCommands:
    def test_winget_upgrades_per_package(self):
        cmds = it.update_commands("winget", ["ffmpeg"])
        assert cmds[0][:2] == ["winget", "upgrade"]

    def test_brew_upgrades_in_one_command(self):
        assert it.update_commands("brew", ["ffmpeg", "yt-dlp"]) == \
            [["brew", "upgrade", "ffmpeg", "yt-dlp"]]

    def test_apt_upgrades_only_what_it_owns(self):
        cmds = it.update_commands("apt", ["ffmpeg", "yt-dlp"])
        assert "--only-upgrade" in cmds[-1]
        assert cmds[-1][-1] == "ffmpeg"

    def test_pacman_emits_nothing(self):
        """A per-package upgrade on a rolling release IS the partial upgrade to
        avoid; the correct action is the operator's own `pacman -Syu`."""
        assert it.update_commands("pacman", ["ffmpeg"]) == []


class TestTheManualGuide:
    @pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
    def test_every_platform_gets_both_tools_named(self, platform):
        # Case-INSENSITIVE: on Windows ffmpeg is named by its winget publisher
        # id, `Gyan.FFmpeg`. The guide names the tool either way, and asserting
        # the lowercase spelling would fail a correct guide.
        guide = it.manual_guide(platform).lower()
        assert "ffmpeg" in guide and "yt-dlp" in guide

    def test_the_pacman_guide_warns_against_a_bare_sy(self):
        assert "-Syu" in it.manual_guide("linux", manager="pacman")


class TestInstallExitCodes:
    def test_zero_is_success(self):
        assert it.install_exit_ok("brew", 0)

    def test_a_nonzero_code_is_a_failure(self):
        assert not it.install_exit_ok("brew", 1)

    @pytest.mark.parametrize("code", [0x8A15002B, 0x8A150061])
    def test_wingets_already_installed_codes_count_as_success(self, code):
        """subprocess reports them as unsigned DWORDs; PowerShell shows them
        signed. Normalised through the 32-bit mask."""
        assert it.install_exit_ok("winget", code)

    def test_those_codes_mean_nothing_for_another_manager(self):
        assert not it.install_exit_ok("apt", 0x8A15002B)


class TestConfirmed:
    @pytest.mark.parametrize("answer", ["y", "Y", "yes", " Yes "])
    def test_a_yes_is_a_yes(self, answer):
        assert it.confirmed(answer)

    @pytest.mark.parametrize("answer", ["", "n", "no", "later"])
    def test_everything_else_is_not(self, answer):
        assert not it.confirmed(answer)


class TestFindingBrew:
    def test_path_wins(self):
        assert it.find_brew(which=lambda n: "/usr/local/bin/brew",
                            exists=lambda p: False) == "/usr/local/bin/brew"

    def test_the_standard_locations_are_the_fallback(self):
        """A fresh bootstrap is not on this process's PATH - shellenv only runs
        in new shells - so brew must be found by absolute path."""
        assert it.find_brew(which=lambda n: None,
                            exists=lambda p: p == "/opt/homebrew/bin/brew") \
            == "/opt/homebrew/bin/brew"

    def test_apple_silicon_is_preferred_over_intel(self):
        assert it.find_brew(which=lambda n: None, exists=lambda p: True) \
            == "/opt/homebrew/bin/brew"

    def test_nothing_found_is_none(self):
        assert it.find_brew(which=lambda n: None, exists=lambda p: False) is None
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.install_tools'`

- [ ] **Step 3: Write the module**

Create `src/yt_shorts/install_tools.py`:

```python
"""`yt-shorts install-tools` - install and update the external runtime tools
(ffmpeg, yt-dlp) through the platform's own package manager: winget on Windows,
brew on macOS, apt or pacman on Linux.

Ported from the racecast broadcast project's src/scripts/install_tools.py and
installer_common.py, reduced to this project's two installable packages. Pure
decision helpers live at the top and are unit-tested without a package manager
anywhere near them; main() (added later in this module) performs the installs.

It never elevates privileges itself: winget and brew prompt for what they need,
and apt/pacman get an explicit `sudo` prefix because - unlike those two - they
need root and will not ask.

STDLIB ONLY, and no FastAPI: this is reachable from the CLI, which runs in a
venv that may never have installed it (see CLAUDE.md's list).
"""

import os
import shutil

# Three names are checked for presence; only two are installable. ffprobe has no
# package of its own anywhere - it ships with ffmpeg - so a missing ffprobe is
# reported as a missing ffmpeg install rather than as its own problem.
TOOLS = ("ffmpeg", "ffprobe", "yt-dlp")
PACKAGES = ("ffmpeg", "yt-dlp")

WINGET_IDS = {"ffmpeg": "Gyan.FFmpeg", "yt-dlp": "yt-dlp.yt-dlp"}
BREW_FORMULAE = {"ffmpeg": "ffmpeg", "yt-dlp": "yt-dlp"}
# apt gets ffmpeg only. yt-dlp on Linux is a pinned, checksum-verified direct
# download instead (see install_ytdlp_binary): apt's package lags upstream far
# enough that it no longer passes YouTube's bot check - and YouTube is where
# every clip in this project comes from.
APT_PACKAGES = {"ffmpeg": "ffmpeg"}
# Arch is the opposite case: both tools are current in the official `extra`
# repo, so pacman does all the work and no managed install applies.
PACMAN_PACKAGES = {"ffmpeg": "ffmpeg", "yt-dlp": "yt-dlp"}

# Standard Homebrew locations: Apple Silicon, then Intel. A fresh bootstrap is
# NOT on the current process's PATH (shellenv only runs in new shells), so brew
# must be invoked through the absolute path find_brew() returns.
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
BREW_INSTALLER = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"

# winget result codes that mean "the package is already there" - not failures:
# 0x8A15002B UPDATE_NOT_APPLICABLE (installed, no newer version in the source),
# 0x8A150061 PACKAGE_ALREADY_INSTALLED. subprocess reports them as unsigned
# DWORDs; PowerShell shows them signed - normalise through the 32-bit mask.
WINGET_ALREADY_INSTALLED = (0x8A15002B, 0x8A150061)


def confirmed(answer: str) -> bool:
    """True iff the operator's reply means yes."""
    return answer.strip().lower().startswith("y")


def install_exit_ok(manager: str, code: int) -> bool:
    """True iff this exit code means the package is (already) installed."""
    if code == 0:
        return True
    return manager == "winget" and (code & 0xFFFFFFFF) in WINGET_ALREADY_INSTALLED


def find_brew(which=shutil.which, exists=os.path.exists) -> str | None:
    """Absolute brew invocation path, or None. PATH first, then the standard
    install locations - which covers both a fresh bootstrap and an
    unconfigured PATH."""
    hit = which("brew")
    if hit:
        return hit
    for path in BREW_PATHS:
        if exists(path):
            return path
    return None


def pick_manager(platform: str, which=shutil.which) -> str | None:
    """The package manager for this platform, or None (-> the manual guide).
    On Linux apt wins when both are present, so a Debian box that happens to
    carry a pacman port keeps the path it has always used."""
    if platform.startswith("win"):
        return "winget" if which("winget") else None
    if platform == "darwin":
        return "brew" if which("brew") else None
    if which("apt-get"):
        return "apt"
    return "pacman" if which("pacman") else None


def missing_tools(which=shutil.which) -> list[str]:
    """The tools that are not resolvable, in TOOLS order."""
    return [t for t in TOOLS if not which(t)]


def install_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]:
    """The argv list(s) to install `packages` with `manager`. `sudo` prepends
    sudo to apt/pacman (Linux, non-root) - they need root and, unlike winget
    and brew, do not prompt for it."""
    packages = list(packages)
    if manager == "winget":
        return [["winget", "install", "--id", WINGET_IDS[p], "-e",
                 "--accept-source-agreements", "--accept-package-agreements"]
                for p in packages if p in WINGET_IDS]
    if manager == "brew":
        formulae = [BREW_FORMULAE[p] for p in packages if p in BREW_FORMULAE]
        return [[brew_path, "install"] + formulae] if formulae else []
    if manager == "apt":
        pkgs = [APT_PACKAGES[p] for p in packages if p in APT_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        # `apt-get update` first - a fresh or stale index (a just-created cloud
        # VM, say) cannot locate the packages otherwise.
        return [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y"] + pkgs]
    if manager == "pacman":
        pkgs = [PACMAN_PACKAGES[p] for p in packages if p in PACMAN_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        # Deliberately NOT `-Sy`: refreshing the package list without upgrading
        # the system is Arch's partial-upgrade trap (new packages link against
        # libraries the machine does not have yet). `-S --needed` installs
        # against the existing database; if that database is stale pacman says
        # "target not found", and the fix is the operator's own `pacman -Syu` -
        # never ours to force.
        return [pre + ["pacman", "-S", "--needed", "--noconfirm"] + pkgs]
    return []


def update_commands(manager, packages, brew_path="brew", sudo=False) -> list[list[str]]:
    """The argv list(s) to UPGRADE already-installed `packages`. winget's "no
    applicable update" exit code is whitelisted in install_exit_ok; brew exits
    0 for an up-to-date formula."""
    packages = list(packages)
    if manager == "winget":
        return [["winget", "upgrade", "--id", WINGET_IDS[p], "-e",
                 "--accept-source-agreements", "--accept-package-agreements"]
                for p in packages if p in WINGET_IDS]
    if manager == "brew":
        formulae = [BREW_FORMULAE[p] for p in packages if p in BREW_FORMULAE]
        return [[brew_path, "upgrade"] + formulae] if formulae else []
    if manager == "apt":
        pkgs = [APT_PACKAGES[p] for p in packages if p in APT_PACKAGES]
        if not pkgs:
            return []
        pre = ["sudo"] if sudo else []
        return [pre + ["apt-get", "update"],
                pre + ["apt-get", "install", "-y", "--only-upgrade"] + pkgs]
    if manager == "pacman":
        # Nothing to emit. Upgrading individual packages on a rolling release IS
        # the partial upgrade to avoid, and the correct action - `pacman -Syu` -
        # upgrades the whole machine, which is the operator's call and not a
        # side effect of `install-tools --update`. main() prints that pointer.
        return []
    return []


def manual_guide(platform: str, manager: str | None = None) -> str:
    """What to type when this module could not do it."""
    if manager == "pacman":
        return ("Install manually:  sudo pacman -S --needed ffmpeg yt-dlp\n"
                "  (if pacman reports 'target not found', its package list is stale -\n"
                "   run `sudo pacman -Syu` first; never `pacman -Sy` on its own)")
    if platform.startswith("win"):
        return ("Install manually with winget (one per line):\n"
                "  winget install --id Gyan.FFmpeg -e\n"
                "  winget install --id yt-dlp.yt-dlp -e")
    if platform == "darwin":
        return "Install manually:  brew install ffmpeg yt-dlp"
    return ("Install manually:  sudo apt-get update && sudo apt-get install -y ffmpeg\n"
            "yt-dlp is a managed install (apt's lags upstream and fails YouTube's\n"
            "bot check) - install-tools sets it up automatically. Manually:\n"
            "  https://github.com/yt-dlp/yt-dlp#installation")
```

- [ ] **Step 4: Run the tests and make sure they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py -q`
Expected: PASS, 33 tests.

- [ ] **Step 5: Record the FastAPI prohibition**

In `CLAUDE.md`, under "Hard constraints" → the prohibitions list, change the bullet that begins **`job_queue.py`, `subtitle_pipeline.py`, ...** so `install_tools.py` is named in it:

```
- **`job_queue.py`, `subtitle_pipeline.py`, `upload_policy.py`, `pathnames.py`,
  `logsetup.py`, `trim.py`, `install_tools.py` and `youtube.py` must not import
  FastAPI**, and `logsetup.py` must not import anything from this project - the
  CLI runs in a venv that may have installed neither.
```

- [ ] **Step 6: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/install_tools.py tests/test_install_tools.py CLAUDE.md
git commit -m "feat(install-tools): the decision helpers, one per package manager"
```

---

### Task 5: The managed yt-dlp download

apt's yt-dlp lags upstream far enough to fail YouTube's bot check — the same YouTube every clip here comes from. So Linux gets a pinned, SHA-256-verified standalone binary straight from yt-dlp's own releases. Ported from racecast's `install_ytdlp_binary`.

**Files:**
- Modify: `src/yt_shorts/install_tools.py` (append)
- Test: `tests/test_install_tools.py` (append)

**Interfaces:**
- Consumes: the module from Task 4.
- Produces: `YTDLP_VERSION: str`, `YTDLP_DOWNLOADS: dict[str, str]`, `ytdlp_asset_tag(platform, machine) -> str | None`, `ytdlp_download_url(tag, ver=YTDLP_VERSION) -> str`, `install_ytdlp_binary(dest_dir, tag, opener=None, downloads=None) -> str`. Task 7's `main()` calls the last one.

- [ ] **Step 1: Fetch the real version and checksums — do not invent them**

```bash
gh release view --repo yt-dlp/yt-dlp --json tagName -q .tagName
```

Take that tag as `YTDLP_VERSION`, then:

```bash
TAG=$(gh release view --repo yt-dlp/yt-dlp --json tagName -q .tagName)
curl -sSfL "https://github.com/yt-dlp/yt-dlp/releases/download/${TAG}/SHA2-256SUMS" \
  | grep -E '(^|\s)yt-dlp_linux(_aarch64)?$'
```

Two lines come back: `yt-dlp_linux` (x86_64) and `yt-dlp_linux_aarch64`. Those two hashes and that tag go into the constants below, verbatim. **If this command fails, stop and report it — never write a placeholder hash.** A wrong hash is not a cosmetic defect: the checksum gate is the only thing standing between a compromised release and a binary this tool executes.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_install_tools.py`:

Add `import hashlib` and `import os` to the file's imports (Task 4 needed
neither; every test below uses both), then append:

```python
class TestTheYtdlpAssetTag:
    """Only Linux gets the managed download. Windows and macOS have current
    packages in winget and brew."""

    def test_linux_x86_64(self):
        assert it.ytdlp_asset_tag("linux", "x86_64") == "linux"

    def test_linux_amd64_is_the_same_machine(self):
        assert it.ytdlp_asset_tag("linux", "AMD64") == "linux"

    def test_linux_aarch64(self):
        assert it.ytdlp_asset_tag("linux", "aarch64") == "linux_aarch64"

    def test_linux_arm64_is_the_same_machine(self):
        assert it.ytdlp_asset_tag("linux", "arm64") == "linux_aarch64"

    def test_an_unsupported_linux_arch_has_no_tag(self):
        assert it.ytdlp_asset_tag("linux", "riscv64") is None

    @pytest.mark.parametrize("platform", ["darwin", "win32"])
    def test_the_platforms_with_a_package_have_no_tag(self, platform):
        assert it.ytdlp_asset_tag(platform, "x86_64") is None

    def test_a_missing_machine_string_does_not_crash(self):
        assert it.ytdlp_asset_tag("linux", None) is None


class TestTheDownloadUrl:
    def test_it_names_the_pinned_version_and_the_asset(self):
        url = it.ytdlp_download_url("linux")
        assert it.YTDLP_VERSION in url
        assert url.endswith("/yt-dlp_linux")

    def test_every_pinned_tag_has_a_checksum(self):
        assert set(it.YTDLP_DOWNLOADS) == {"linux", "linux_aarch64"}
        assert all(len(h) == 64 for h in it.YTDLP_DOWNLOADS.values())


class TestInstallingTheBinary:
    def test_a_matching_checksum_writes_an_executable(self, tmp_path):
        blob = b"#!/not-really-yt-dlp\n"
        digest = hashlib.sha256(blob).hexdigest()

        path = it.install_ytdlp_binary(str(tmp_path), "linux",
                                       opener=lambda url: blob,
                                       downloads={"linux": digest})

        with open(path, "rb") as handle:      # a bare open() trips ruff's SIM115
            assert handle.read() == blob
        assert os.access(path, os.X_OK)

    def test_the_binary_is_named_yt_dlp(self, tmp_path):
        blob = b"x"
        path = it.install_ytdlp_binary(
            str(tmp_path), "linux", opener=lambda url: blob,
            downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert os.path.basename(path) == "yt-dlp"

    def test_it_is_owner_only(self, tmp_path):
        """This tool executes what it downloads; nobody else needs to."""
        blob = b"x"
        path = it.install_ytdlp_binary(
            str(tmp_path), "linux", opener=lambda url: blob,
            downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert oct(os.stat(path).st_mode)[-3:] == "700"

    def test_a_mismatched_checksum_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            it.install_ytdlp_binary(str(tmp_path), "linux",
                                    opener=lambda url: b"tampered",
                                    downloads={"linux": "0" * 64})

    def test_a_mismatched_checksum_writes_nothing_at_all(self, tmp_path):
        """The refusal must not leave a partial or unverified file behind for
        ensure_tool_path to later put on PATH."""
        with pytest.raises(RuntimeError):
            it.install_ytdlp_binary(str(tmp_path), "linux",
                                    opener=lambda url: b"tampered",
                                    downloads={"linux": "0" * 64})

        assert not (tmp_path / "yt-dlp").exists()

    def test_the_destination_directory_is_created(self, tmp_path):
        blob = b"x"
        dest = tmp_path / "deeper" / "still"
        it.install_ytdlp_binary(str(dest), "linux", opener=lambda url: blob,
                                downloads={"linux": hashlib.sha256(blob).hexdigest()})

        assert dest.is_dir()
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py -q -k "Ytdlp or DownloadUrl or InstallingTheBinary"`
Expected: FAIL — `AttributeError: module 'yt_shorts.install_tools' has no attribute 'ytdlp_asset_tag'`

- [ ] **Step 4: Append the implementation**

Add to `src/yt_shorts/install_tools.py` (and add `import hashlib` and `import urllib.request` to the imports at the top):

```python
# yt-dlp on Linux: apt's package lags upstream badly and no longer passes
# YouTube's bot check - and YouTube is where every clip in this project comes
# from. So Linux gets a pinned, SHA-256-verified standalone binary straight from
# yt-dlp's GitHub releases, into the managed tool dir. The release asset is a
# BARE executable (no archive), so there is no extraction step. Windows (winget)
# and macOS (brew) keep their yt-dlp package.
#
# To bump: read the new tag and its two hashes out of the release's own
# SHA2-256SUMS, never from anywhere else -
#   TAG=$(gh release view --repo yt-dlp/yt-dlp --json tagName -q .tagName)
#   curl -sSfL ".../releases/download/${TAG}/SHA2-256SUMS" | grep yt-dlp_linux
YTDLP_VERSION = "<the tag from Step 1>"
YTDLP_BIN_NAME = "yt-dlp"
YTDLP_URL_TMPL = ("https://github.com/yt-dlp/yt-dlp/releases/download/"
                  "{ver}/yt-dlp_{tag}")
YTDLP_DOWNLOADS = {
    "linux":         "<sha256 of yt-dlp_linux from Step 1>",
    "linux_aarch64": "<sha256 of yt-dlp_linux_aarch64 from Step 1>",
}


def ytdlp_asset_tag(platform: str, machine: str | None) -> str | None:
    """Map (sys.platform, platform.machine()) to a YTDLP_DOWNLOADS tag, or None
    for Windows/macOS (their package managers ship a current yt-dlp) and for
    unsupported architectures. Pure."""
    if platform.startswith("linux"):
        m = (machine or "").lower()
        if m in ("x86_64", "amd64"):
            return "linux"
        if m in ("aarch64", "arm64"):
            return "linux_aarch64"
    return None


def ytdlp_download_url(tag: str, ver: str = YTDLP_VERSION) -> str:
    return YTDLP_URL_TMPL.format(ver=ver, tag=tag)


def install_ytdlp_binary(dest_dir, tag, opener=None, downloads=None) -> str:
    """Download yt-dlp's standalone Linux binary for `tag`, verify its SHA-256
    against the pinned value, write it to dest_dir/yt-dlp and make it
    executable. Returns the path. Raises RuntimeError on a checksum mismatch -
    BEFORE anything is written, so a refusal never leaves an unverified file for
    ensure_tool_path to put on PATH. `opener` (url -> bytes) is the injectable
    seam for tests; it defaults to a cert-verified HTTPS GET."""
    downloads = downloads or YTDLP_DOWNLOADS
    want = downloads[tag]
    if opener is None:
        def opener(url):
            with urllib.request.urlopen(url, timeout=120) as response:  # nosec - pinned host, checksum-verified below
                return response.read()
    blob = opener(ytdlp_download_url(tag))
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise RuntimeError(f"yt-dlp download checksum mismatch for {tag}: {got} != {want}")
    os.makedirs(dest_dir, exist_ok=True)
    binpath = os.path.join(dest_dir, YTDLP_BIN_NAME)
    with open(binpath, "wb") as out:
        out.write(blob)
    os.chmod(binpath, 0o700)   # owner rwx only - this tool executes it, nobody else needs to
    return binpath
```

Replace the three `<…>` markers with the values fetched in Step 1.

- [ ] **Step 5: Run the tests and make sure they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py -q`
Expected: PASS.

- [ ] **Step 6: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/install_tools.py tests/test_install_tools.py
git commit -m "feat(install-tools): a pinned, checksum-verified yt-dlp for Linux"
```

---

### Task 6: The managed tool directory and PATH

Without this, Task 5 is pointless: a yt-dlp downloaded into a directory that is not on `PATH` is invisible to `render.py`'s `["yt-dlp", …]`.

**The directory is `<workspace>/.tools/`, and the obvious names are both bugs.** `workspace.resolve()`'s last resort returns `root = repo_channels.parent` — the repository root itself. Under that fallback `<workspace>/bin` **is** `<repo>/bin/`, where `bin/yt-shorts` lives, and `<workspace>/tools` is the repository's own `tools/`. Either would have `install-tools` drop a downloaded binary into the source tree.

**Files:**
- Modify: `src/yt_shorts/install_tools.py` (append)
- Modify: `src/yt_shorts/cli.py` (one call in `main()`)
- Modify: `.gitignore`
- Test: `tests/test_install_tools.py` (append)

**Interfaces:**
- Consumes: Task 4's module, Task 2's `main()`.
- Produces: `augment_path(current, candidates, exists=os.path.isdir) -> str | None`, `managed_tools_dir(workspace_root) -> Path`, `ensure_tool_path(workspace_root, environ=os.environ, frozen=None, platform=None) -> None`. Task 7's `main()` and Task 8's `doctor` both call `managed_tools_dir`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install_tools.py`:

```python
class TestAugmentPath:
    def test_a_missing_existing_dir_is_prepended(self):
        result = it.augment_path("/usr/bin", ["/managed"], exists=lambda p: True)

        assert result == os.pathsep.join(["/managed", "/usr/bin"])

    def test_candidate_order_is_preserved(self):
        result = it.augment_path("/usr/bin", ["/a", "/b"], exists=lambda p: True)

        assert result == os.pathsep.join(["/a", "/b", "/usr/bin"])

    def test_a_dir_already_on_path_is_not_added_again(self):
        assert it.augment_path("/managed:/usr/bin", ["/managed"],
                               exists=lambda p: True) is None

    def test_a_dir_that_does_not_exist_is_not_added(self):
        assert it.augment_path("/usr/bin", ["/nope"], exists=lambda p: False) is None

    def test_nothing_to_add_returns_none_so_the_caller_leaves_environ_alone(self):
        assert it.augment_path("/usr/bin", [], exists=lambda p: True) is None

    def test_an_empty_path_still_works(self):
        assert it.augment_path("", ["/managed"], exists=lambda p: True) == "/managed"


class TestTheManagedDirectory:
    def test_it_is_dot_tools_under_the_workspace_root(self, tmp_path):
        assert it.managed_tools_dir(tmp_path) == tmp_path / ".tools"

    def test_it_is_not_bin(self, tmp_path):
        """workspace.resolve()'s last resort returns the REPOSITORY root, where
        bin/ already holds bin/yt-shorts - a managed 'bin' would drop a
        downloaded yt-dlp into the source tree."""
        assert it.managed_tools_dir(tmp_path).name != "bin"

    def test_it_is_not_tools(self, tmp_path):
        """Same fallback, same problem: tools/ is this repository's own."""
        assert it.managed_tools_dir(tmp_path).name != "tools"


class TestEnsureToolPath:
    def test_the_managed_dir_goes_on_path(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / ".tools")

    def test_an_absent_managed_dir_changes_nothing(self, tmp_path):
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"] == "/usr/bin"

    def test_calling_it_twice_does_not_duplicate_the_entry(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")
        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="linux")

        assert env["PATH"].count(str(tmp_path / ".tools")) == 1

    def test_a_frozen_macos_launch_also_gets_the_homebrew_dirs(self, tmp_path, monkeypatch):
        """A binary launched by double-click from Finder inherits a truncated
        PATH (/usr/bin:/bin:/usr/sbin:/sbin) with no Homebrew in it, so a
        brew-installed ffmpeg looks missing. racecast measured this as its
        issue #38; it applies here the moment the studio is double-clicked."""
        (tmp_path / ".tools").mkdir()
        monkeypatch.setattr(it.os.path, "isdir",
                            lambda p: p in (str(tmp_path / ".tools"), "/opt/homebrew/bin"))
        env = {"PATH": "/usr/bin:/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=True, platform="darwin")

        assert "/opt/homebrew/bin" in env["PATH"].split(os.pathsep)

    def test_an_unfrozen_macos_run_does_not(self, tmp_path, monkeypatch):
        """A terminal launch already has a full PATH; adding to it would be
        noise, and the point of augment_path is to leave it alone."""
        (tmp_path / ".tools").mkdir()
        monkeypatch.setattr(it.os.path, "isdir",
                            lambda p: p in (str(tmp_path / ".tools"), "/opt/homebrew/bin"))
        env = {"PATH": "/usr/bin:/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=False, platform="darwin")

        assert "/opt/homebrew/bin" not in env["PATH"].split(os.pathsep)

    def test_a_frozen_linux_launch_does_not_get_homebrew_dirs(self, tmp_path):
        (tmp_path / ".tools").mkdir()
        env = {"PATH": "/usr/bin"}

        it.ensure_tool_path(tmp_path, environ=env, frozen=True, platform="linux")

        assert "/opt/homebrew/bin" not in env["PATH"]
```

And append to `tests/test_cli.py`:

```python
class TestTheCliPutsTheManagedToolsOnPath:
    """A yt-dlp that install-tools downloaded into <workspace>/.tools is
    invisible to render.py's ["yt-dlp", ...] until this runs. One call, right
    after the workspace resolves - the studio inherits it because cmd_studio
    runs uvicorn in the same process and jobs run on its threads."""

    def test_main_calls_ensure_tool_path_with_the_workspace_root(self, monkeypatch):
        from yt_shorts import cli, workspace

        seen = []
        monkeypatch.setattr(cli.install_tools, "ensure_tool_path",
                            lambda root: seen.append(root))
        monkeypatch.setattr(cli, "cmd_studio", lambda identifier=None, *, open_url: 0)

        assert cli.main(["studio", "--no-browser"]) == 0
        # conftest.py's autouse _isolated_resolved_workspace already points
        # resolve() at a fixture-owned root; assert against that rather than
        # setting YT_SHORTS_DATA, which the fixture would ignore (it patches
        # the RESOLVER, not the environment).
        assert seen == [workspace.resolve().root]
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py tests/test_cli.py -q -k "AugmentPath or ManagedDirectory or EnsureToolPath or ManagedToolsOnPath"`
Expected: FAIL — `AttributeError: … has no attribute 'augment_path'`

- [ ] **Step 3: Append the implementation**

Add to `src/yt_shorts/install_tools.py` (and `import sys` plus `from pathlib import Path` at the top):

```python
# Homebrew's bin dirs, prepended only for a FROZEN macOS launch - see
# ensure_tool_path.
BREW_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def managed_tools_dir(workspace_root) -> Path:
    """Where install-tools drops the binaries it downloads itself.

    `.tools`, not `bin` and not `tools`: workspace.resolve()'s last resort
    returns the REPOSITORY root, so under that fallback `<workspace>/bin` IS
    this repo's bin/ (holding bin/yt-shorts) and `<workspace>/tools` is its
    tools/. Either name would drop a downloaded binary into the source tree.
    A dotted name collides with nothing and follows the precedent .studio.lock
    already sets for a runtime file at the workspace root."""
    return Path(workspace_root) / ".tools"


def augment_path(current, candidates, exists=os.path.isdir) -> str | None:
    """The PATH `current` should become so the dirs in `candidates` are
    reachable: each candidate that exists on disk but is missing from PATH,
    prepended in candidate order ahead of the existing entries. Returns None
    when nothing needs adding, so the caller can leave the environment
    untouched. Pure. Ported from racecast."""
    have = current.split(os.pathsep) if current else []
    add = [d for d in candidates if exists(d) and d not in have]
    if not add:
        return None
    return os.pathsep.join(add + have)


def ensure_tool_path(workspace_root, environ=None, frozen=None, platform=None) -> None:
    """Prepend the tool dirs this module writes to but that are not on this
    process's PATH, so every subprocess spawned from here on resolves them.

    Two sources:
    * The managed tool dir (`<workspace>/.tools`), where install_ytdlp_binary
      drops its download. It is NEVER on the operator's shell PATH, so it is
      added on every platform.
    * Frozen on macOS only: a binary launched from Finder or the Dock inherits a
      truncated PATH (/usr/bin:/bin:/usr/sbin:/sbin) that omits Homebrew, so a
      brew-installed ffmpeg or yt-dlp looks missing (racecast's issue #38). A
      terminal launch already has a full PATH and is left alone.

    Only genuinely-missing dirs that exist on disk are added (augment_path), so
    this is a no-op in the normal case. `environ`, `frozen` and `platform` are
    the injectable seams for tests."""
    environ = os.environ if environ is None else environ
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    candidates = [str(managed_tools_dir(workspace_root))]
    if frozen and platform == "darwin":
        candidates += list(BREW_BIN_DIRS)
    new = augment_path(environ.get("PATH", ""), candidates)
    if new:
        environ["PATH"] = new
```

- [ ] **Step 4: Wire it into the CLI**

In `src/yt_shorts/cli.py`, add `from yt_shorts import install_tools` to the imports, and insert one call in `main()` immediately after `print(space.describe(), file=sys.stderr)`:

```python
    # A yt-dlp that `install-tools` downloaded into <workspace>/.tools is not on
    # the operator's shell PATH, so nothing that shells out by bare name would
    # find it. One call, here, before anything spawns a subprocess. The studio
    # inherits it: cmd_studio runs uvicorn IN THIS PROCESS and its jobs run on
    # this process's threads.
    install_tools.ensure_tool_path(space.root)
```

- [ ] **Step 5: Ignore the directory**

Append to `.gitignore`:

```
# The managed external tools (install_tools.managed_tools_dir,
# <workspace root>/.tools - where `yt-shorts install-tools` drops the pinned
# yt-dlp binary on Linux). Same reasoning as /jobs.json and /settings.json
# above: under the repo-fallback workspace that root IS the repository root.
# ROOTED so it matches only the repo-root directory.
/.tools/
```

- [ ] **Step 6: Run the tests and make sure they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py tests/test_cli.py -q
```
Expected: PASS.

- [ ] **Step 7: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/install_tools.py src/yt_shorts/cli.py tests/test_install_tools.py tests/test_cli.py .gitignore
git commit -m "feat(install-tools): a managed tool dir the CLI actually puts on PATH"
```

---

### Task 7: The `install-tools` command

**Files:**
- Modify: `src/yt_shorts/install_tools.py` (append `windows_fresh_path`, `bootstrap_brew`, `run`)
- Modify: `src/yt_shorts/cli.py` (`cmd_install_tools`, `COMMANDS`, dispatch, docstring)
- Test: `tests/test_install_tools.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 4-6.
- Produces: `windows_fresh_path(read_values=None) -> str | None`, `bootstrap_brew(assume_yes, input_fn=input, run=None, find=None) -> str | None`, `run(workspace_root, *, update=False, assume_yes=False, platform=None, machine=None, which=None, call=None, printer=print) -> int`. Task 8's `doctor` points the operator at this command by name; nothing imports `run`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_install_tools.py`:

```python
class TestTheWindowsFreshPath:
    """winget updates the REGISTRY, not this running process. Without re-reading
    it, install-tools reports 'still missing' immediately after its own
    successful install."""

    def test_the_hives_are_joined_in_order(self):
        result = it.windows_fresh_path(read_values=lambda: ["C:\\sys", "C:\\user"])

        assert result == os.pathsep.join(["C:\\sys", "C:\\user"])

    def test_empty_values_are_dropped(self):
        assert it.windows_fresh_path(read_values=lambda: ["C:\\sys", ""]) == "C:\\sys"

    def test_nothing_readable_is_none(self):
        assert it.windows_fresh_path(read_values=lambda: []) is None


class TestTheBrewBootstrap:
    def test_declining_installs_nothing(self):
        ran = []
        result = it.bootstrap_brew(False, input_fn=lambda prompt: "n",
                                   run=lambda url, argv: ran.append(url) or 0,
                                   find=lambda: "/opt/homebrew/bin/brew")

        assert result is None
        assert ran == []

    def test_accepting_runs_the_official_installer_and_returns_the_path(self):
        ran = []
        result = it.bootstrap_brew(False, input_fn=lambda prompt: "y",
                                   run=lambda url, argv: ran.append(url) or 0,
                                   find=lambda: "/opt/homebrew/bin/brew")

        assert ran == [it.BREW_INSTALLER]
        assert result == "/opt/homebrew/bin/brew"

    def test_yes_skips_the_prompt_entirely(self):
        asked = []
        it.bootstrap_brew(True, input_fn=lambda prompt: asked.append(prompt) or "n",
                          run=lambda url, argv: 0, find=lambda: "/opt/homebrew/bin/brew")

        assert asked == []

    def test_a_failed_installer_is_none_rather_than_a_bogus_path(self):
        assert it.bootstrap_brew(True, input_fn=lambda p: "y",
                                 run=lambda url, argv: 1,
                                 find=lambda: "/opt/homebrew/bin/brew") is None


class TestTheCommand:
    """`run()` drives the installs. Every seam is injected: no test here invokes
    a package manager, and none may."""

    def test_everything_present_and_no_update_runs_nothing(self, tmp_path):
        calls = []
        code = it.run(tmp_path, platform="linux", machine="x86_64",
                      which=lambda n: "/usr/bin/" + n,
                      call=lambda cmd: calls.append(cmd) or 0)

        assert code == 0
        assert calls == []

    def test_a_missing_tool_is_installed(self, tmp_path):
        calls = []
        which = lambda n: None if n == "ffmpeg" else "/usr/bin/" + n
        it.run(tmp_path, platform="linux", machine="x86_64", which=which,
               call=lambda cmd: calls.append(cmd) or 0)

        assert any("ffmpeg" in cmd for cmd in calls)

    def test_update_upgrades_what_is_already_there(self, tmp_path, monkeypatch):
        # install_ytdlp_binary MUST be patched here even though this test is
        # about apt's upgrade commands: with update=True the Linux download
        # branch fires regardless of what is missing, and run() swallows a
        # download failure into `failed`, which this test never inspects. Left
        # unpatched it silently pulls ~40 MB on every suite run and still
        # passes - measured at 6.26s against 0.02s for its siblings.
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: os.path.join(dest, "yt-dlp"))
        calls = []
        it.run(tmp_path, update=True, platform="linux", machine="x86_64",
               which=lambda n: "/usr/bin/" + n,
               call=lambda cmd: calls.append(cmd) or 0)

        assert any("--only-upgrade" in cmd for cmd in calls)

    def test_no_package_manager_is_a_failure_with_the_manual_guide(self, tmp_path):
        lines = []
        code = it.run(tmp_path, platform="linux", machine="x86_64",
                      which=lambda n: None, call=lambda cmd: 0,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("Install manually" in line for line in lines)

    def test_a_failed_command_is_reported_and_returns_one(self, tmp_path):
        lines = []
        which = lambda n: None if n == "ffmpeg" else "/usr/bin/" + n
        code = it.run(tmp_path, platform="linux", machine="x86_64", which=which,
                      call=lambda cmd: 1,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("did not complete" in line for line in lines)

    def test_linux_downloads_yt_dlp_rather_than_asking_apt_for_it(self, tmp_path, monkeypatch):
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append((dest, tag)) or
                            os.path.join(dest, "yt-dlp"))
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        it.run(tmp_path, platform="linux", machine="x86_64", which=which,
               call=lambda cmd: 0)

        assert downloaded == [(str(it.managed_tools_dir(tmp_path)), "linux")]

    def test_update_refreshes_the_pinned_yt_dlp_too(self, tmp_path, monkeypatch):
        """yt-dlp goes stale in weeks - `install-tools --update` before a
        session is the whole point of the flag."""
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append(tag) or
                            os.path.join(dest, "yt-dlp"))
        it.run(tmp_path, update=True, platform="linux", machine="x86_64",
               which=lambda n: "/usr/bin/" + n, call=lambda cmd: 0)

        assert downloaded == ["linux"]

    def test_macos_does_not_download_yt_dlp(self, tmp_path, monkeypatch):
        """brew ships a current one."""
        downloaded = []
        monkeypatch.setattr(it, "install_ytdlp_binary",
                            lambda dest, tag, **kw: downloaded.append(tag))
        which = lambda n: None if n == "yt-dlp" else "/opt/homebrew/bin/" + n
        it.run(tmp_path, platform="darwin", machine="arm64", which=which,
               call=lambda cmd: 0)

        assert downloaded == []

    def test_a_download_failure_is_reported_not_raised(self, tmp_path, monkeypatch):
        def explode(dest, tag, **kw):
            raise RuntimeError("checksum mismatch")

        monkeypatch.setattr(it, "install_ytdlp_binary", explode)
        lines = []
        which = lambda n: None if n == "yt-dlp" else "/usr/bin/" + n
        code = it.run(tmp_path, platform="linux", machine="x86_64", which=which,
                      call=lambda cmd: 0,
                      printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        assert code == 1
        assert any("checksum mismatch" in line for line in lines)
```

And append to `tests/test_cli.py`:

```python
class TestTheInstallToolsCommand:
    """Like TestTheNoBrowserFlag, these rely on conftest.py's autouse
    _isolated_resolved_workspace for the workspace - never on YT_SHORTS_DATA,
    which that fixture ignores."""

    def test_it_is_a_known_command(self):
        from yt_shorts import cli

        assert "install-tools" in cli.COMMANDS

    def test_it_takes_no_identifier(self, monkeypatch):
        from yt_shorts import cli

        monkeypatch.setattr(cli.install_tools, "run", lambda root, **kw: 0)
        assert cli.main(["install-tools"]) == 0

    def test_update_reaches_the_runner(self, monkeypatch):
        from yt_shorts import cli

        seen = {}
        monkeypatch.setattr(cli.install_tools, "run",
                            lambda root, **kw: seen.update(kw) or 0)
        cli.main(["install-tools", "--update"])

        assert seen["update"] is True

    def test_yes_reaches_the_runner(self, monkeypatch):
        from yt_shorts import cli

        seen = {}
        monkeypatch.setattr(cli.install_tools, "run",
                            lambda root, **kw: seen.update(kw) or 0)
        cli.main(["install-tools", "--yes"])

        assert seen["assume_yes"] is True
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py tests/test_cli.py -q -k "FreshPath or BrewBootstrap or TheCommand or InstallToolsCommand"`
Expected: FAIL — `AttributeError: … has no attribute 'windows_fresh_path'`

- [ ] **Step 3: Append the runner**

Add to `src/yt_shorts/install_tools.py` (add `import subprocess`, `import tempfile`, `import urllib.request` if not already imported):

```python
def _registry_path_values():
    """The system and user Path values from the Windows registry."""
    import winreg
    values = []
    for root, key in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, key) as handle:
                values.append(winreg.QueryValueEx(handle, "Path")[0])
        except OSError:
            pass  # key or value absent (e.g. no user Path) - skip that hive
    return values


def windows_fresh_path(read_values=None) -> str | None:
    """The PATH a NEW shell would get (system + user, from the registry).
    winget updates the registry, not running processes - this process's PATH
    predates anything installed during this run. None when there is nothing to
    read (non-Windows)."""
    if read_values is None:
        if not sys.platform.startswith("win"):
            return None
        read_values = _registry_path_values
    parts = [os.path.expandvars(v) for v in read_values() if v]
    return os.pathsep.join(parts) or None


def _which_with_fresh_path(fresh_path):
    """A which() that falls back to the registry PATH (Windows): a
    just-installed tool is not on THIS process's PATH yet, but a new shell
    will see it."""
    def probe(name):
        return shutil.which(name) or (
            shutil.which(name, path=fresh_path) if fresh_path else None)
    return probe


def _which_with_managed_dir(managed_dir, brew=None):
    """A which() that also looks in the managed tool dir (never on the user's
    shell PATH) and, on macOS, in brew's own bin dir (not on PATH right after a
    fresh bootstrap). ensure_tool_path does this for real runs; this probe lets
    install-tools confirm its own work without it."""
    prefix_bin = os.path.dirname(brew) if brew else None

    def probe(name):
        hit = shutil.which(name)
        if hit:
            return hit
        for directory in (str(managed_dir), prefix_bin):
            if directory:
                candidate = os.path.join(directory, name)
                if os.path.exists(candidate):
                    return candidate
        return None
    return probe


def _run_remote_script(url, argv):
    """Download `url` over cert-verified HTTPS to a temp file and run it
    visibly. No shell pipe - the operator saw the URL and confirmed first."""
    print("Downloading:", url)
    with urllib.request.urlopen(url, timeout=30) as response:  # nosec - operator-confirmed official URL
        body = response.read()
    # delete=False: the file must outlive the handle so the runner can read it.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
        tmp.write(body)
        path = tmp.name
    try:
        return subprocess.call(list(argv) + [path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass  # a temp file we could not remove is not worth failing over


def bootstrap_brew(assume_yes, input_fn=input, run=None, find=None) -> str | None:
    """Offer the official brew.sh installer (macOS). Returns the absolute brew
    path on success, None if declined or failed. The installer runs as the
    current user, prompts for sudo itself, and may download the Xcode Command
    Line Tools - a one-time setup that can take a while."""
    run = _run_remote_script if run is None else run
    find = find_brew if find is None else find
    print("Homebrew is required but not installed. Official installer:")
    print(" ", BREW_INSTALLER)
    print("  (runs as your user, asks for sudo; may download the Xcode Command")
    print("   Line Tools - this one-time setup can take a while)")
    if not assume_yes and not confirmed(input_fn("Bootstrap Homebrew now? [y/N] ")):
        print("aborted.")
        return None
    if run(BREW_INSTALLER, ["/bin/bash"]) != 0:
        return None
    return find()


def run(workspace_root, *, update=False, assume_yes=False, platform=None,
        machine=None, which=None, call=None, printer=print) -> int:
    """Install (and optionally upgrade) the external tools. Returns 0 when
    everything required is present afterwards, 1 otherwise. Every seam is
    injectable so this is testable without a package manager: `which` resolves
    tools, `call` runs a command, `printer` receives the operator-facing text."""
    platform = sys.platform if platform is None else platform
    if machine is None:
        import platform as _platform
        machine = _platform.machine()
    call = subprocess.call if call is None else call
    managed = managed_tools_dir(workspace_root)

    probe = which or _which_with_fresh_path(windows_fresh_path())
    missing = missing_tools(which=probe)
    if not missing and not update:
        printer("All external tools already installed: " + ", ".join(TOOLS))
        printer("  (run `yt-shorts install-tools --update` to upgrade them)")
        return 0
    if missing:
        printer("Missing tools: " + ", ".join(missing))

    brew = None
    if platform == "darwin":
        # find_brew goes through `probe`, not shutil.which: a test that injects
        # `which` must not be able to reach the real Homebrew - or, worse, a
        # machine without it, where bootstrap_brew would prompt on stdin.
        brew = find_brew(which=probe)
        if not brew:
            brew = bootstrap_brew(assume_yes)
        if not brew:
            printer("No supported package manager found.")
            printer(manual_guide(platform))
            return 1
        manager = "brew"
    else:
        manager = pick_manager(platform, which=probe)
        if manager is None:
            printer("No supported package manager found.")
            printer(manual_guide(platform))
            return 1

    # apt/pacman need root and, unlike winget/brew, will not prompt for it.
    sudo = manager in ("apt", "pacman") and hasattr(os, "geteuid") and os.geteuid() != 0
    # ffprobe has no package of its own; a missing one means ffmpeg is missing.
    wanted = [p for p in PACKAGES
              if p in missing or (p == "ffmpeg" and "ffprobe" in missing)]
    present = [p for p in PACKAGES if p not in wanted]

    cmds = []
    if update and present:
        printer("Updating installed tools: " + ", ".join(present))
        cmds += update_commands(manager, present, brew_path=brew or "brew", sudo=sudo)
        if manager == "pacman":
            printer("NOTE: on Arch these are repository packages - upgrade them with")
            printer("      the system:  sudo pacman -Syu   (a per-package upgrade would")
            printer("      leave the machine in a partial-upgrade state)")
    cmds += install_commands(manager, wanted, brew_path=brew or "brew", sudo=sudo)

    failed = []
    for cmd in cmds:
        printer("Running: " + " ".join(cmd))
        if not install_exit_ok(manager, call(cmd)):
            failed.append(" ".join(cmd))

    # yt-dlp on Linux: the pinned binary, not apt. Refreshed on --update too, so
    # the pre-session `install-tools --update` bumps it to the pinned-current
    # version - it goes stale in weeks.
    if manager in ("apt",) and ("yt-dlp" in missing or update):
        tag = ytdlp_asset_tag(platform, machine)
        if tag is None:
            printer("NOTE: no prebuilt yt-dlp for this OS/arch - install it manually:")
            printer("  https://github.com/yt-dlp/yt-dlp#installation")
        else:
            printer(f"Installing yt-dlp {YTDLP_VERSION} -> {managed} ...")
            try:
                install_ytdlp_binary(str(managed), tag)
                printer("  yt-dlp installed.")
            except Exception as error:   # network, checksum or write - report, do not crash
                failed.append(f"yt-dlp download ({error})")

    still = missing_tools(which=which or _which_with_managed_dir(managed, brew))
    if failed or still:
        printer("Some installs did not complete.")
        if failed:
            printer("Failed: " + "; ".join(failed))
        if still:
            printer("Still missing: " + ", ".join(still))
        printer(manual_guide(platform, manager))
        return 1
    printer("All tools " + ("up to date" if update else "installed")
            + ". Run `yt-shorts doctor` to verify.")
    return 0
```

- [ ] **Step 4: Wire the command into the CLI**

In `src/yt_shorts/cli.py`:

1. Add `"install-tools"` to `COMMANDS`.
2. Extend the module docstring with `yt-shorts install-tools [--update] [--yes]`.
3. In `main()`, strip the two flags alongside `--no-browser`:

```python
    update = "--update" in args
    assume_yes = "--yes" in args
    args = [a for a in args if a not in ("--no-browser", "--update", "--yes")]
```

(replacing the single `--no-browser` filter written in Task 2 — keep the `no_browser` line above it).

4. Add `install-tools` to the zero-identifier branch of the argument-count check, next to `studio`:

```python
    if command in ("studio", "install-tools"):
        if len(args) > 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        identifier = args[1] if len(args) == 2 else None
```

5. Dispatch it right after the `studio` branch — it is workspace-level and must run **before** `profile_load`, since a machine with no tools cannot render anything anyway:

```python
    # install-tools runs before profile_load: it is workspace-level and about
    # the machine, not about any one event.
    if command == "install-tools":
        return install_tools.run(space.root, update=update, assume_yes=assume_yes)
```

- [ ] **Step 5: Run the tests and make sure they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_install_tools.py tests/test_cli.py -q
```
Expected: PASS.

- [ ] **Step 6: Prove it on this machine — read-only**

```bash
bin/yt-shorts install-tools
```
Expected: `All external tools already installed: ffmpeg, ffprobe, yt-dlp` and exit 0. **Do not run `--update` on this workstation**: `ffmpeg` here is a specific build the racecast project depends on, and CLAUDE.md forbids upgrading it.

- [ ] **Step 7: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/install_tools.py src/yt_shorts/cli.py tests/test_install_tools.py tests/test_cli.py
git commit -m "feat(cli): yt-shorts install-tools, with the Homebrew bootstrap and the Windows registry PATH"
```

---

### Task 8: The `doctor` command

`install-tools` fixes; `doctor` reports and points at it. It repairs nothing.

**Files:**
- Create: `src/yt_shorts/doctor.py`
- Modify: `src/yt_shorts/cli.py` (`COMMANDS`, dispatch, docstring)
- Modify: `README.md` (a "Requirements" section)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: `install_tools.TOOLS` and `install_tools.managed_tools_dir` (Tasks 4, 6).
- Produces: `Check` (a frozen dataclass with `name: str`, `ok: bool`, `detail: str`, `required: bool`), `REQUIRED_FILTERS: tuple[str, ...]`, `checks(workspace_root=None, *, which=…, run=…, version=…) -> list[Check]`, `report(results, printer=print) -> int`. Nothing later imports these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_doctor.py`:

```python
"""`yt-shorts doctor`: what is checked, and what each answer means. Every seam
is injected - no test here runs ffmpeg."""

import sys

import pytest

from yt_shorts import doctor


def _run_ok(argv, **kwargs):
    """A runner standing in for every external tool: version output for a
    --version call, the full filter list for `ffmpeg -filters`."""
    if argv[1:] == ["-filters"]:
        return 0, " ".join(doctor.REQUIRED_FILTERS) + " crop drawbox"
    return 0, f"{argv[0]} 1.2.3"


def _found(name):
    return "/usr/bin/" + name


class TestTheHappyPath:
    def test_everything_required_is_ok(self, tmp_path):
        """Only the REQUIRED checks. The optional ones report what this venv
        happens to have installed, which differs between a developer's `[all]`
        install and CI's base one - asserting on them would make this test pass
        or fail for a reason that has nothing to do with doctor."""
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)

        assert results and all(c.ok for c in results if c.required)

    def test_it_reports_exit_zero(self, tmp_path):
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)

        assert doctor.report(results, printer=lambda *a: None) == 0


class TestTheExternalTools:
    @pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe", "yt-dlp"])
    def test_each_tool_gets_its_own_check(self, tmp_path, tool):
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)

        assert any(c.name == tool for c in results)

    def test_a_missing_tool_fails_its_check(self, tmp_path):
        which = lambda n: None if n == "yt-dlp" else _found(n)
        results = doctor.checks(tmp_path, which=which, run=_run_ok)

        assert not next(c for c in results if c.name == "yt-dlp").ok

    def test_a_missing_tool_names_the_command_that_fixes_it(self, tmp_path):
        which = lambda n: None if n == "yt-dlp" else _found(n)
        results = doctor.checks(tmp_path, which=which, run=_run_ok)

        assert "install-tools" in next(c for c in results if c.name == "yt-dlp").detail

    def test_a_present_but_unrunnable_tool_fails_too(self, tmp_path):
        """On PATH is not the same as working - a broken ffmpeg build, a bad
        symlink, a quarantined binary."""
        def run(argv, **kwargs):
            if argv[0].endswith("ffmpeg"):
                return 1, "cannot execute binary file"
            return _run_ok(argv, **kwargs)

        results = doctor.checks(tmp_path, which=_found, run=run)

        assert not next(c for c in results if c.name == "ffmpeg").ok

    def test_a_missing_tool_makes_the_report_exit_one(self, tmp_path):
        which = lambda n: None if n == "ffmpeg" else _found(n)
        results = doctor.checks(tmp_path, which=which, run=_run_ok)

        assert doctor.report(results, printer=lambda *a: None) == 1


class TestTheFfmpegFilters:
    """This ffmpeg is built without libfreetype and libass, so every glyph is
    drawn in Pillow and composited through these four filters. Checking them
    beats trusting an assumption about how someone's ffmpeg was built."""

    def test_the_four_filters_this_project_uses_are_named(self):
        assert set(doctor.REQUIRED_FILTERS) == {"overlay", "boxblur", "scale", "setsar"}

    def test_all_four_present_passes(self, tmp_path):
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)

        assert next(c for c in results if c.name == "ffmpeg filters").ok

    def test_one_missing_filter_fails_and_names_it(self, tmp_path):
        def run(argv, **kwargs):
            if argv[1:] == ["-filters"]:
                return 0, "overlay scale setsar"       # no boxblur
            return _run_ok(argv, **kwargs)

        check = next(c for c in doctor.checks(tmp_path, which=_found, run=run)
                     if c.name == "ffmpeg filters")

        assert not check.ok
        assert "boxblur" in check.detail

    def test_the_filter_check_is_skipped_when_ffmpeg_is_absent(self, tmp_path):
        """Reporting a missing filter when the binary itself is missing would
        be two failures for one cause."""
        which = lambda n: None if n == "ffmpeg" else _found(n)
        results = doctor.checks(tmp_path, which=which, run=_run_ok)

        assert not any(c.name == "ffmpeg filters" for c in results)


class TestThePythonVersion:
    def test_the_running_version_is_reported(self, tmp_path):
        check = next(c for c in doctor.checks(tmp_path, which=_found, run=_run_ok)
                     if c.name == "python")

        assert ".".join(str(n) for n in sys.version_info[:2]) in check.detail

    def test_a_version_below_the_floor_fails(self, tmp_path):
        check = next(c for c in doctor.checks(tmp_path, which=_found, run=_run_ok,
                                              version=(3, 11, 9))
                     if c.name == "python")

        assert not check.ok


class TestTheOptionalLayers:
    """faster-whisper and the cloud SDKs are extras. Their absence is reported
    but does not fail the run - the code degrades to 'no subtitles' rather than
    refusing to render, and doctor must say the same thing."""

    def test_an_absent_optional_import_is_not_required(self, tmp_path):
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)
        optional = [c for c in results if not c.required]

        assert optional, "no optional checks at all"

    def test_an_absent_optional_import_does_not_fail_the_report(self, tmp_path):
        results = [c for c in doctor.checks(tmp_path, which=_found, run=_run_ok)
                   if c.required] + [doctor.Check("faster-whisper", False, "not installed", False)]

        assert doctor.report(results, printer=lambda *a: None) == 0


class TestTheWorkspace:
    def test_a_writable_workspace_passes(self, tmp_path):
        check = next(c for c in doctor.checks(tmp_path, which=_found, run=_run_ok)
                     if c.name == "workspace")

        assert check.ok
        assert str(tmp_path) in check.detail

    def test_an_unresolvable_workspace_fails_rather_than_raising(self):
        check = next(c for c in doctor.checks(None, which=_found, run=_run_ok)
                     if c.name == "workspace")

        assert not check.ok


class TestTheReport:
    def test_every_check_appears_in_the_output(self, tmp_path):
        lines = []
        results = doctor.checks(tmp_path, which=_found, run=_run_ok)
        doctor.report(results, printer=lambda *a: lines.append(" ".join(str(x) for x in a)))

        for check in results:
            assert any(check.name in line for line in lines)
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_doctor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.doctor'`

- [ ] **Step 3: Write the module**

Create `src/yt_shorts/doctor.py`:

```python
"""`yt-shorts doctor` - what is installed, what works, and what to run when it
does not. It reports; `install_tools` repairs. Keeping the two apart is why
this module runs nothing but probes.

STDLIB ONLY, and no FastAPI: reachable from the CLI, which runs in a venv that
may never have installed it.
"""

import importlib.util
import subprocess
import sys
from dataclasses import dataclass

from yt_shorts import install_tools

# The floor pyproject.toml declares. Kept here as a literal rather than parsed
# out of the metadata: doctor has to work from a frozen bundle, which carries no
# pyproject.toml at all.
MIN_PYTHON = (3, 12)

# The four ffmpeg filters this project actually composites with. Every glyph and
# shape is drawn in Pillow and overlaid as a PNG, because the ffmpeg this was
# built against has neither libfreetype nor libass - so `drawtext` and
# `subtitles` are not what is checked for here, and their absence is fine.
REQUIRED_FILTERS = ("overlay", "boxblur", "scale", "setsar")

# Extras. Absent is a fact worth reporting, not a failure: subtitle_pipeline
# degrades to "no subtitles" and _google.py raises GoogleUnavailable rather
# than refusing to run.
OPTIONAL_IMPORTS = (
    ("faster-whisper", "faster_whisper", "transcription (pip install 'yt-shorts[transcribe]')"),
    ("fastapi", "fastapi", "the studio"),
    ("uvicorn", "uvicorn", "the studio"),
    ("openai", "openai", "the OpenAI moment provider (pip install 'yt-shorts[cloud]')"),
    ("anthropic", "anthropic", "the Anthropic moment provider (pip install 'yt-shorts[cloud]')"),
    ("google-genai", "google.genai", "the Gemini moment provider (pip install 'yt-shorts[cloud]')"),
    ("google-api-python-client", "googleapiclient", "YouTube upload (pip install 'yt-shorts[cloud]')"),
)

FIX = "run `yt-shorts install-tools`"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _run_tool(argv, timeout=15):
    """(exit code, combined output) for a probe, or (1, reason) when it could
    not be run at all. Never raises: doctor's job is to report a broken tool,
    not to die of one."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _first_line(text):
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def checks(workspace_root=None, *, which=None, run=None, version=None) -> list[Check]:
    """Every check, in report order. `which`, `run` and `version` are the
    injectable seams; nothing here writes anything anywhere."""
    import shutil

    which = shutil.which if which is None else which
    run = _run_tool if run is None else run
    version = tuple(sys.version_info[:3]) if version is None else version
    results = []

    running = ".".join(str(n) for n in version[:2])
    results.append(Check(
        "python", version[:2] >= MIN_PYTHON,
        f"{running} (needs {'.'.join(str(n) for n in MIN_PYTHON)} or newer)"))

    ffmpeg_path = None
    for tool in install_tools.TOOLS:
        path = which(tool)
        if path is None:
            results.append(Check(tool, False, f"not found on PATH - {FIX}"))
            continue
        code, output = run([path, "-version" if tool != "yt-dlp" else "--version"])
        if code != 0:
            results.append(Check(tool, False,
                                 f"found at {path} but would not run: {_first_line(output)}"))
            continue
        if tool == "ffmpeg":
            ffmpeg_path = path
        results.append(Check(tool, True, f"{path} - {_first_line(output)}"))

    # Only when ffmpeg itself answered: reporting a missing filter for a missing
    # binary would be two failures for one cause.
    if ffmpeg_path is not None:
        code, output = run([ffmpeg_path, "-filters"])
        # Parse the NAME column and compare EXACTLY. A substring test against
        # the whole output cannot fail for `scale`: "grayscale" appears in the
        # descriptions of alphaextract and extractplanes, which every ffmpeg
        # build ships - measured, not feared. `scale2ref` and `scale_vt` widen
        # it further.
        absent = [f for f in REQUIRED_FILTERS if f not in _filter_names(output)]
        results.append(Check(
            "ffmpeg filters", not absent,
            "all present" if not absent
            else f"missing: {', '.join(absent)} - this ffmpeg cannot composite the overlay"))

    for label, module, why in OPTIONAL_IMPORTS:
        found = importlib.util.find_spec(module) is not None
        results.append(Check(label, found,
                             "installed" if found else f"not installed - needed for {why}",
                             required=False))

    results.append(_workspace_check(workspace_root))
    return results


def _workspace_check(workspace_root) -> Check:
    """The workspace must resolve and be writable, or nothing this tool does
    can be saved."""
    from pathlib import Path

    if workspace_root is None:
        return Check("workspace", False,
                     "could not be resolved - set YT_SHORTS_DATA or create ~/YT-Shorts-Data")
    root = Path(workspace_root)
    probe = root / ".doctor-write-probe"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as error:
        return Check("workspace", False, f"{root} is not writable: {error}")
    return Check("workspace", True, f"{root} (writable)")


def report(results, printer=print) -> int:
    """Print every check and return 0 when everything REQUIRED passed. An
    absent optional layer is reported and forgiven - the code degrades rather
    than refusing, and this must say the same thing."""
    for check in results:
        if check.ok:
            mark = "ok  "
        else:
            mark = "FAIL" if check.required else "--  "
        printer(f"  {mark}  {check.name}: {check.detail}")
    broken = [c for c in results if c.required and not c.ok]
    if broken:
        printer("")
        printer(f"{len(broken)} required check(s) failed. {FIX.capitalize()}.")
        return 1
    printer("")
    printer("Everything required is in place.")
    return 0
```

- [ ] **Step 4: Wire the command into the CLI**

In `src/yt_shorts/cli.py`:

1. Add `"doctor"` to `COMMANDS` and to the zero-identifier branch:

```python
    if command in ("studio", "install-tools", "doctor"):
```

2. Add `from yt_shorts import doctor` to the imports and `yt-shorts doctor` to the module docstring.
3. Dispatch it next to `install-tools`, before `profile_load`:

```python
    # doctor runs before profile_load: it answers "can this machine run the
    # tool at all", which has nothing to do with any one event's profile.
    if command == "doctor":
        return doctor.report(doctor.checks(space.root))
```

- [ ] **Step 5: Run the tests and make sure they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_doctor.py tests/test_cli.py -q
```
Expected: PASS.

- [ ] **Step 6: Prove it against the real machine**

```bash
bin/yt-shorts doctor
echo "exit: $?"
```
Expected: every required check `ok` (this workstation has all three tools and an ffmpeg with those four filters), and `exit: 0`. This is read-only — it runs `-version` and `-filters` and nothing else.

- [ ] **Step 7: Document the requirement in the README**

Add a `## Requirements` section to `README.md`, directly after the opening paragraph and before `## Workflow after a race weekend`:

```markdown
## Requirements

Python 3.12 or newer, and three external tools on your PATH:

| Tool | Why |
|---|---|
| `ffmpeg` | every render — the picture is composited with `overlay`, `boxblur`, `scale` and `setsar` |
| `ffprobe` | ships with ffmpeg; used to verify durations and the rendered result |
| `yt-dlp` | every download, and stream discovery |

The tool installs and updates them for you:

```bash
yt-shorts install-tools            # winget / brew / apt / pacman, whichever this machine has
yt-shorts install-tools --update   # run this before a session — yt-dlp goes stale in weeks
yt-shorts doctor                   # what is installed, what works, what to fix
```

On Linux, `yt-dlp` is a pinned, checksum-verified download into
`<workspace>/.tools/` rather than an apt package: apt's version lags far enough
behind upstream that it no longer passes YouTube's bot check. The CLI puts that
directory on `PATH` itself, so nothing else needs configuring.
```

- [ ] **Step 8: Lint, then commit**

```bash
python3 tools/lint.py
git add src/yt_shorts/doctor.py src/yt_shorts/cli.py tests/test_doctor.py tests/test_cli.py README.md
git commit -m "feat(cli): yt-shorts doctor, which checks the four filters this project actually uses"
```

---

### Task 9: `tools/build-binary.py` and its smoke test

**Files:**
- Create: `tools/build-binary.py`
- Modify: `README.md` (a "Building a binary" subsection under `## Development`)

**Interfaces:**
- Consumes: `yt_shorts.cli:main` (Task 2), `yt_shorts.version.resolve()` (Task 1), the `doctor` and `install-tools` commands (Tasks 7, 8).
- Produces: `dist/bin/yt-shorts/` (an `--onedir` tree). Block B's `release.yml` calls `python tools/build-binary.py --version "$TAG"` and archives that directory.

`--onedir`, not racecast's `--onefile`: the bundle carries ctranslate2, onnxruntime, av and numpy, and `--onefile` unpacks all of it to a temp directory on **every single invocation** — a `yt-shorts doctor` would take seconds.

- [ ] **Step 1: Install PyInstaller**

```bash
.venv/bin/pip install pyinstaller
```

- [ ] **Step 2: Write the script**

Create `tools/build-binary.py`:

```python
#!/usr/bin/env python3
"""Build the standalone `yt-shorts` binary with PyInstaller and smoke-test it.
One binary per OS - run this on the OS you are targeting (CI runs a four-runner
matrix).

Usage: python3 tools/build-binary.py [--version vX.Y.Z] [--skip-smoke]
Output: dist/bin/yt-shorts/ (a directory, not a file - see ONEDIR below).

ONEDIR, deliberately. The bundle carries ctranslate2, onnxruntime, av and numpy;
--onefile would unpack several hundred MB into a temp dir on EVERY invocation,
so even `yt-shorts doctor` would take seconds. The release archive holds the
directory.

NOT bundled: ffmpeg and yt-dlp (that is what `yt-shorts install-tools` is for),
the Whisper models (fetched at runtime, hundreds of MB, model-dependent), and
the frontend SOURCE under studio/web/ - only its BUILT output, studio/static/,
which is committed.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
NAME = "yt-shorts"

# The built frontend. api.py computes STATIC_DIR as Path(__file__).parent /
# "static", and under PyInstaller __file__ points inside the bundle - so this
# works only because the DEST mirrors the package layout exactly.
STATIC_SRC = os.path.join(SRC, "yt_shorts", "studio", "static")
STATIC_DEST = os.path.join("yt_shorts", "studio", "static")

# uvicorn loads these by STRING at runtime, so PyInstaller's static analyser
# never sees them and the frozen studio dies with ModuleNotFoundError on its
# first request.
HIDDEN_IMPORTS = [
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
]

# Data files these packages carry beside their code (model configs, codec
# tables, ONNX runtime libraries). PyInstaller's hooks cover most of it; naming
# them is what makes the difference reproducible rather than hook-version luck.
COLLECT_DATA = ["faster_whisper", "av", "onnxruntime"]


def _pyinstaller_cmd():
    """The PyInstaller invocation. Prefers the executable on PATH; falls back to
    `python -m PyInstaller` when the module is importable but its wrapper script
    is not on PATH (common after a --user pip install on macOS)."""
    if shutil.which("pyinstaller"):
        return ["pyinstaller"]
    try:
        import PyInstaller  # noqa: F401 - importability check only
        return [sys.executable, "-m", "PyInstaller"]
    except ImportError:
        return None


def build(launcher, workdir, version_file, sep):
    """Run PyInstaller. Returns the path to the built launcher."""
    cmd = launcher + [
        "--onedir", "--name", NAME, "--clean", "--noconfirm",
        "--distpath", os.path.join(ROOT, "dist", "bin"),
        "--workpath", os.path.join(workdir, "build"),
        "--specpath", workdir,
        "--paths", SRC,
        # The version the binary reports: the TAG this build was handed, written
        # into the bundle root where yt_shorts.version.resolve() looks for it.
        "--add-data", f"{version_file}{sep}.",
        "--add-data", f"{STATIC_SRC}{sep}{STATIC_DEST}",
    ]
    for module in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", module]
    for package in COLLECT_DATA:
        cmd += ["--collect-data", package]
    cmd.append(os.path.join(SRC, "yt_shorts", "cli.py"))
    print("Running:", " ".join(cmd), flush=True)
    if subprocess.call(cmd) != 0:
        sys.exit("pyinstaller failed.")
    ext = ".exe" if os.name == "nt" else ""
    binary = os.path.join(ROOT, "dist", "bin", NAME, NAME + ext)
    if not os.path.isfile(binary):
        sys.exit(f"expected binary missing: {binary}")
    size = sum(os.path.getsize(os.path.join(dirpath, f))
               for dirpath, _, names in os.walk(os.path.join(ROOT, "dist", "bin", NAME))
               for f in names)
    print(f"Built {binary} ({size // (1024 * 1024)} MB on disk)")
    return binary


def smoke(binary, version):
    """The build has not succeeded until the artefact proves it works. Four
    things, each guarding a distinct way the bundle can be wrong."""
    def run(args, timeout=120):
        return subprocess.run([binary] + args, capture_output=True, text=True,
                              timeout=timeout)

    # 1. It knows what it is - proves the bundled VERSION file arrived and
    #    yt_shorts.version.resolve() prefers it.
    out = run(["--version"])
    if out.returncode != 0 or version not in out.stdout:
        sys.exit(f"smoke --version FAILED: rc={out.returncode} "
                 f"out={out.stdout!r} err={out.stderr!r}")

    # 2. doctor runs - proves the bundle layout and the optional imports live.
    #    Exit 1 is acceptable: a build agent legitimately has no ffmpeg. What
    #    must not happen is a traceback.
    doc = run(["doctor"])
    if doc.returncode not in (0, 1) or "python" not in doc.stdout:
        sys.exit(f"smoke doctor FAILED: rc={doc.returncode} "
                 f"out={doc.stdout!r} err={doc.stderr!r}")
    if "Traceback" in doc.stderr:
        sys.exit(f"smoke doctor FAILED: it raised: {doc.stderr!r}")

    # 3. install-tools inspects without crashing. Same tolerance, same reason -
    #    and no --update, so nothing is installed on the build agent.
    inst = run(["install-tools"])
    if "Traceback" in inst.stderr:
        sys.exit(f"smoke install-tools FAILED: it raised: {inst.stderr!r}")

    # 4. The studio serves its SPA. THIS is the check that catches a missing
    #    studio/static/ - the failure mode that produces a blank page and
    #    "element(s) not found" with no error reported anywhere.
    smoke_studio(binary)
    print("Smoke test OK (--version, doctor, install-tools, studio + SPA).")


def smoke_studio(binary):
    """Start the studio against a throwaway workspace, fetch the SPA and one
    API route, then stop it. The port is read from the studio's own 'Studio:
    <url>' line rather than assumed: _studio_port picks a free one when the
    preferred port is busy, which a build agent cannot rule out."""
    with tempfile.TemporaryDirectory() as workspace:
        env = os.environ.copy()
        env["YT_SHORTS_DATA"] = workspace
        proc = subprocess.Popen([binary, "studio", "--no-browser"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        try:
            url = None
            deadline = time.time() + 60
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                print("  studio:", line.rstrip())
                match = re.search(r"Studio: (http://\S+)", line)
                if match:
                    url = match.group(1).rstrip("/")
                    break
            if url is None:
                sys.exit(f"smoke studio FAILED: no 'Studio: <url>' line "
                         f"(rc={proc.poll()})")
            _await_spa(url)
            with urllib.request.urlopen(f"{url}/api/channels", timeout=10) as response:
                if response.status != 200:
                    sys.exit(f"smoke studio FAILED: /api/channels -> {response.status}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def _await_spa(url, attempts=40):
    """GET the SPA until it answers. A 404 here means studio/static/ never made
    it into the bundle: create_app() evaluates STATIC_DIR.is_dir() once, and
    without it the SPA fallback route is never registered at all."""
    last = ""
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url + "/", timeout=5) as response:
                body = response.read()
            if b"<div id=\"root\"" in body or b"<html" in body.lower():
                return
            last = f"unexpected body: {body[:200]!r}"
        except Exception as error:      # not listening yet, or a real failure
            last = str(error)
        time.sleep(0.5)
    sys.exit(f"smoke studio FAILED: the SPA never served ({last}). "
             "The usual cause is studio/static/ missing from the bundle.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="dev")
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()

    launcher = _pyinstaller_cmd()
    if launcher is None:
        sys.exit("pyinstaller not found (pip install pyinstaller).")
    if not os.path.isdir(STATIC_SRC):
        sys.exit(f"the built frontend is missing: {STATIC_SRC}\n"
                 "Build it first (cd src/yt_shorts/studio/web && npm run build) - "
                 "and never while an E2E run is in flight.")

    workdir = tempfile.mkdtemp(prefix="yt-shorts-build-")
    version_file = os.path.join(workdir, "VERSION")
    with open(version_file, "w", encoding="utf-8") as handle:
        handle.write(args.version + "\n")
    sep = ";" if os.name == "nt" else ":"

    binary = build(launcher, workdir, version_file, sep)
    if not args.skip_smoke:
        smoke(binary, args.version)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Build it, for real**

```bash
python3 tools/lint.py
PYTHONPATH=src .venv/bin/python tools/build-binary.py --version v0.1.0-local
```
Expected: PyInstaller runs (this takes minutes), then all four smoke checks print `OK`.

If it fails at check 4 with "the SPA never served", the `--add-data` DEST is wrong — compare the actual layout with what `api.py:186` expects:

```bash
find dist/bin/yt-shorts -path '*studio/static*' -name index.html
```

If it fails with a `ModuleNotFoundError` for something loaded by string, add it to `HIDDEN_IMPORTS` and rebuild.

- [ ] **Step 4: Check the binary against a clean environment**

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin ./dist/bin/yt-shorts/yt-shorts --version
env -i HOME="$HOME" PATH=/usr/bin:/bin ./dist/bin/yt-shorts/yt-shorts doctor; echo "exit: $?"
```
Expected: the version line; then a doctor report that names the missing tools and points at `install-tools`, exiting 1. **That failure is the correct answer** — the point is that it reports rather than crashes.

- [ ] **Step 5: Keep the build output out of git**

Append to `.gitignore`:

```
# PyInstaller output (tools/build-binary.py). The release workflow builds this
# per OS and archives it; it is never committed.
/dist/
/build/
```

- [ ] **Step 6: Document it**

Under `## Development` in `README.md`, add:

```markdown
### Building a binary

```bash
.venv/bin/pip install pyinstaller
PYTHONPATH=src .venv/bin/python tools/build-binary.py --version v0.1.0-local
```

Produces `dist/bin/yt-shorts/` — a directory, not a single file. The bundle
carries ctranslate2, onnxruntime, av and numpy, and a one-file build would
unpack all of it to a temp directory on every invocation.

The build is not finished until its own smoke test passes: the binary must
report its version, run `doctor` and `install-tools` without raising, and serve
the studio's SPA plus one API route. That last check is what catches a
`studio/static/` missing from the bundle — otherwise the studio serves a blank
page and says nothing about why.

`ffmpeg`, `yt-dlp` and the Whisper models are **not** bundled. Run
`yt-shorts install-tools` on the target machine.
```

- [ ] **Step 7: Run the whole suite and lint, then commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git add tools/build-binary.py README.md .gitignore
git commit -m "build: a PyInstaller binary whose smoke test proves the studio still serves its SPA"
```

- [ ] **Step 8: Clean up**

```bash
rm -rf dist build
```

---

## Follow-ups this branch deliberately did not do

Executed 2026-08-05. All nine tasks complete, final whole-branch review clean,
2544 tests passing. Four things were ruled out of scope rather than forgotten:

1. **The binary smoke test still performs a REAL install on an agent that is
   missing ffmpeg.** `install_tools.run()` installs missing tools even with no
   flags — `--update` only *additionally* upgrades present ones. The smoke step
   is now isolated to a throwaway `YT_SHORTS_DATA`, and its comment says so
   honestly, but on `macos-latest`/`ubuntu-latest` a release build will really
   run `brew install ffmpeg yt-dlp` / `apt-get install ffmpeg` at that step.
   **Block B's CI work must account for this**, or `run()` needs a dry-run mode.
2. **No test covers `run()`'s `sudo` decision** (`os.geteuid() != 0`). A CI
   runner executing as root silently takes the other branch, untested.
3. **`doctor` ignores the exit code of `ffmpeg -filters`.** A probe that failed
   to run reports "missing: overlay, boxblur, scale, setsar — this ffmpeg cannot
   composite the overlay", which is the wrong sentence for "the probe did not
   run".
4. **The flag-refusal test matrix is incomplete.** `studio --update`,
   `install-tools --no-browser` and `doctor --yes` are all refused correctly at
   runtime (verified by hand, exit 2) but no test pins them.

## Self-review

**Spec coverage.** A1 → Task 2. A2 → Tasks 1 and 3. A3 → Tasks 4-8 (decision helpers, the pinned yt-dlp, the managed dir and PATH, the command with its brew bootstrap and registry PATH, doctor). A4 → Task 9. A5 → the platform matrix belongs to block B's `release.yml`; Task 9 produces the artefact it archives, and the macOS quarantine note is spec'd into block C's wiki. The spec's "out of scope" items (studio button, notarisation, PyPI) have no task, correctly.

**Known deviation from the spec, deliberate:** the spec placed the MIT `LICENSE` in block B. It moved into Task 3, because `pyproject.toml` declares `license = "MIT"` and declaring a license with no file is a lie in the packaging metadata. Block B keeps the rest of the public-readiness work.

**Naming consistency:** `managed_tools_dir` (not `managed_bin_dir`, racecast's name — the directory is `.tools`, so the name follows), `ensure_tool_path`, `install_ytdlp_binary`, `ytdlp_asset_tag`, `run`, `checks`, `report`, `Check`, `resolve`. Each is defined in exactly one task and used with the same signature everywhere after it.

**Four defects found and fixed during this review**, recorded because each would have produced a test that passes for the wrong reason:

1. **`YT_SHORTS_DATA` does nothing in a test.** `tests/conftest.py`'s autouse `_isolated_resolved_workspace` patches `workspace.resolve()` **itself**, not the environment — so three tests in Tasks 6 and 7 that set that variable would have asserted against the fixture root while claiming to assert against their own `tmp_path`. They now use `workspace.resolve().root`, and say why.
2. **`run()` reached the real Homebrew.** With `which` injected but `find_brew()` called bare, `test_macos_does_not_download_yt_dlp` would have probed the actual machine — and on a Linux CI runner with no brew it would have fallen through to `bootstrap_brew`, which prompts on stdin. `find_brew` and `pick_manager` now both go through the injected `probe`.
3. **doctor's happy path asserted on the venv, not on doctor.** `all(c.ok for c in results)` includes the OPTIONAL imports, so the test would pass on a developer's `[all]` install and fail on CI's base one. It now checks only the required ones.
4. **Two unused imports** in the appended test blocks (`from pathlib import Path` in Task 6, a duplicate `import pytest` in Task 5) would have failed `tools/lint.py` on ruff's `F401` — after the tests themselves passed, which is the annoying order to discover it in.
