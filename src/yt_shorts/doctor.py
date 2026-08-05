"""`yt-shorts doctor` - what is installed, what works, and what to run when it
does not. It reports; `install_tools` repairs. Keeping the two apart is why
this module runs nothing but probes.

STDLIB ONLY, and no FastAPI: reachable from the CLI, which runs in a venv that
may never have installed it.
"""

import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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


# An identifier-shaped token: starts with a letter, then letters/digits/
# underscore. Deliberately not a parse of ffmpeg's column layout (flags,
# name, in->out, description) - see _filter_names for why.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _filter_names(filters_output: str) -> set[str]:
    """The distinct identifier-shaped tokens in `ffmpeg -filters` output, to
    be compared against REQUIRED_FILTERS by EXACT match.

    A plain `"scale" in output` substring test (an earlier version of this
    check) matches inside the word "grayscale", which appears in the
    DESCRIPTION of unrelated filters (`alphaextract`, `extractplanes`) that
    ship in essentially every ffmpeg build - so it reported `scale` present
    regardless of whether the `scale` filter itself existed. It also could
    not tell `scale` from `scale2ref` or `scale_vt`. Splitting the whole
    output into identifier tokens and requiring an EXACT match on one of
    them fixes both: `scale2ref` and `scale_vt` are each one token, neither
    equal to `scale`, and `grayscale` is one token, not `scale` plus a
    prefix.

    This does not parse the table's literal column structure (flags / name
    / in->out / description) - a real filter's NAME never collides with a
    standalone token elsewhere in this machine's actual `-filters` output
    (verified directly; see task-8-report.md), and a strict column parser
    would be unable to read the plain space-separated filter lists the
    injected `run` seam uses in tests, since those carry no table structure
    to parse."""
    return set(_TOKEN.findall(filters_output or ""))


def _module_found(module: str) -> bool:
    """Whether `module` can be imported, without actually importing it.

    `importlib.util.find_spec` raises ModuleNotFoundError instead of
    returning None for a dotted name whose PARENT package is entirely
    absent (measured: find_spec("google.genai") raises when "google" itself
    is not installed, rather than returning None as it does for every other
    shape of "not installed" here). A doctor that crashed while reporting an
    optional dependency missing would be worse than one that just reports it
    missing, so that case is treated the same as "not found"."""
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def checks(workspace_root=None, *, which=None, run=None, version=None) -> list[Check]:
    """Every check, in report order. `which`, `run` and `version` are the
    injectable seams. Every probe but one is read-only; the exception is the
    workspace check, which creates `workspace_root` (mkdir) and writes a
    small probe file to confirm it is writable, removing the file again
    afterwards (see `_workspace_check`).

    Tool resolution here is a bare `which(tool)` (default `shutil.which`),
    which only sees a managed install under `<workspace>/.tools` because
    `cli.py`'s `main()` calls `install_tools.ensure_tool_path()` before ever
    reaching this function. A caller that invokes `checks()` directly -
    a future studio route, say - without that call first would report a
    perfectly good managed yt-dlp as missing."""
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
        present = _filter_names(output)
        absent = [f for f in REQUIRED_FILTERS if f not in present]
        results.append(Check(
            "ffmpeg filters", not absent,
            "all present" if not absent
            else f"missing: {', '.join(absent)} - this ffmpeg cannot composite the overlay"))

    for label, module, why in OPTIONAL_IMPORTS:
        found = _module_found(module)
        results.append(Check(label, found,
                             "installed" if found else f"not installed - needed for {why}",
                             required=False))

    results.append(_workspace_check(workspace_root))
    return results


def _workspace_check(workspace_root) -> Check:
    """The workspace must resolve and be writable, or nothing this tool does
    can be saved."""
    if workspace_root is None:
        return Check("workspace", False,
                     "could not be resolved - set YT_SHORTS_DATA or create ~/YT-Shorts-Data")
    root = Path(workspace_root)
    probe = root / ".doctor-write-probe"
    # Split deliberately: mkdir/touch is what "writable" actually means, and
    # its failure is what makes the check FAIL. A failed unlink afterwards
    # means the write already succeeded - the workspace IS writable, it just
    # has one leftover probe file - so it must not be folded into the same
    # except and reported as "not writable", which would be untrue.
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.touch()
    except OSError as error:
        return Check("workspace", False, f"{root} is not writable: {error}")
    try:
        probe.unlink()
    except OSError as error:
        return Check("workspace", True,
                     f"{root} (writable; could not remove probe file "
                     f"{probe.name}: {error})")
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
