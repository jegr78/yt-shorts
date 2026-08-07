Most people never need this. Every release carries a wheel, an sdist and a
per-OS binary, and `pipx install .` / `pip install ".[all]"` from a checkout
builds what it needs on the way in. This page is for producing those artefacts
yourself.

Running the test suite, the linter and the frontend's own checks is a
different subject and lives in
[CONTRIBUTING.md](https://github.com/jegr78/yt-shorts/blob/main/CONTRIBUTING.md).

## What each build needs

| | Python | Node | PyInstaller |
|---|---|---|---|
| wheel / sdist | 3.12 or newer | yes | no |
| binary | 3.12 or newer | yes | yes |

**Node**, because the studio's frontend is built rather than committed —
`src/yt_shorts/studio/static/` is git-ignored, and both builds below produce it
so that nobody installing the result ever needs Node. Which versions will do,
and why a Node outside that range stops `npm ci` instead of warning, is in
[CONTRIBUTING.md](https://github.com/jegr78/yt-shorts/blob/main/CONTRIBUTING.md#the-frontend);
the range itself is declared in `src/yt_shorts/studio/web/package.json`'s
`engines`.

**PyInstaller** only for the binary, and only in the environment you build
from; nothing installs it as a dependency.

## A wheel and an sdist

```bash
python3 -m pip install --upgrade build
python3 -m build              # writes dist/*.whl and dist/*.tar.gz
```

`hatch_build.py` is what makes the frontend part of the package: an existing
`src/yt_shorts/studio/static/` is reused as is (with a warning if it is older
than `web/src/`, i.e. if it looks stale), and otherwise it runs `npm ci && npm
run build` there. A missing `npm` with nothing to reuse fails the build with
that instruction rather than producing a wheel that installs cleanly and then
404s every studio page.

Installing from a checkout (`pipx install .`, `pip install ".[all]"`) runs the
same hook, which is why those need Node too.

## A binary for one OS

```bash
.venv/bin/pip install -e ".[all]"
.venv/bin/pip install pyinstaller
.venv/bin/python tools/build-binary.py --version v0.1.0-local
```

The first line is not optional: the build passes `faster_whisper`, `av` and
`onnxruntime` to PyInstaller's `--collect-data`, which needs them importable,
and the smoke test below starts a real server, which needs FastAPI and uvicorn.
Both workflows install the package the same way before PyInstaller.

Produces `dist/bin/yt-shorts/` — a **directory, not a single file**. The bundle
carries ctranslate2, onnxruntime, av and numpy, and a one-file build would
unpack all of it to a temp directory on every invocation, so even `yt-shorts
doctor` would take seconds.

**One binary per OS, built on that OS.** There is no cross-build; the release
workflow runs a four-runner matrix and attaches
`yt-shorts-windows-x64.zip`, `yt-shorts-macos-arm64.tar.gz`,
`yt-shorts-linux-x64.tar.gz` and `yt-shorts-linux-arm64.tar.gz`. Download the
archive for your OS, unpack it, and run the `yt-shorts` (`yt-shorts.exe` on
Windows) inside it. On macOS the binaries are unsigned and arrive quarantined —
see [If something goes wrong](If-something-goes-wrong#macos-refuses-to-run-the-binary).

It builds the frontend first if `studio/static/` is empty, so this needs Node
as well; an existing build is reused as is — which is how CI's test jobs share
one bundle; the binary jobs deliberately build their own, so the npm path is
exercised before a tag.

The build is not finished until its own smoke test passes: the binary must
report its version, run `doctor` and `install-tools` without raising, and serve
the studio's SPA plus one API route (`GET /api/channels`). That last check is
what catches a `studio/static/` missing from the bundle — otherwise the studio
serves a blank page and says nothing about why. `--skip-smoke` skips it.

## What is deliberately not bundled

Neither the wheel nor the binary carries:

- **`ffmpeg` and `ffprobe`** — every render composites with them.
- **`yt-dlp`** — every download and all stream discovery.
- **the Whisper models** — fetched at first use, hundreds of MB and dependent
  on which model is configured (see [Subtitles](Subtitles) for the default
  model's size on disk).

The first two are what `install-tools` is for. On the target machine:

```bash
yt-shorts install-tools     # winget / brew / apt / pacman, whichever it has
yt-shorts doctor            # what is installed, what works, what to fix
```
