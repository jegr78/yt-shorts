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


def _write_entry_script(workdir):
    """PyInstaller's positional argument must be a runnable SCRIPT, not a
    `module:function` reference - and `yt_shorts.cli` (the entry point named
    in pyproject.toml's [project.scripts] and used by bin/yt-shorts) defines
    `main()` but never calls it at module scope; that call is left to
    console_scripts / bin/yt-shorts. Pointing PyInstaller at cli.py directly
    therefore builds a binary that imports the module and exits 0 having done
    nothing - proven by a real build: --version came back rc=0 with empty
    stdout AND empty stderr. This tiny generated launcher is bin/yt-shorts'
    own pattern (import main, call it, propagate its exit code), written into
    the same throwaway workdir as the VERSION file so it is never mistaken
    for a tracked source file."""
    entry_script = os.path.join(workdir, "entry.py")
    with open(entry_script, "w", encoding="utf-8") as handle:
        handle.write(
            "import sys\n"
            "from yt_shorts.cli import main\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )
    return entry_script


def build(launcher, workdir, version_file, sep):
    """Run PyInstaller. Returns the path to the built launcher."""
    entry_script = _write_entry_script(workdir)
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
    cmd.append(entry_script)
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
    def run(args, timeout=120, env=None):
        return subprocess.run([binary] + args, capture_output=True, text=True,
                              timeout=timeout, env=env)

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

    # 3. install-tools runs against a THROWAWAY workspace and does not crash.
    #    Same tolerance, same reason. This does NOT prove nothing is installed
    #    on the build agent: install_tools.run() calls install_commands()
    #    unconditionally, and with no flags it still installs whatever tool is
    #    MISSING (--update only additionally upgrades what is already
    #    present) - on a runner with a package manager but no ffmpeg, this
    #    step really does run e.g. `brew install ffmpeg yt-dlp`. What this
    #    check guarantees is narrower: the command runs to completion without
    #    raising, against a workspace this build owns rather than the
    #    operator's real one.
    with tempfile.TemporaryDirectory() as workspace:
        env = os.environ.copy()
        env["YT_SHORTS_DATA"] = workspace
        inst = run(["install-tools"], env=env)
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

    # TemporaryDirectory, not mkdtemp: this holds PyInstaller's --workpath, its
    # intermediate build tree (hundreds of MB), so a bare mkdtemp left one
    # behind in /tmp on every single build. dist/bin/ - the bundle this
    # produces - lives outside workdir and survives the cleanup untouched.
    with tempfile.TemporaryDirectory(prefix="yt-shorts-build-") as workdir:
        version_file = os.path.join(workdir, "VERSION")
        with open(version_file, "w", encoding="utf-8") as handle:
            handle.write(args.version + "\n")
        sep = ";" if os.name == "nt" else ":"

        binary = build(launcher, workdir, version_file, sep)
        if not args.skip_smoke:
            smoke(binary, args.version)


if __name__ == "__main__":
    main()
