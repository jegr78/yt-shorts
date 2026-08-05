# Short Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator cut seconds off the head and tail of an already-rendered `short.mp4` from the studio, previewed instantly and applied on demand, without re-downloading, re-transcribing or re-composing.

**Architecture:** The trim is an editorial value (`Edit.trim`) in `edit.json`. A new pure-ish module `trim.py` owns the one place a cut happens: it keeps the untrimmed render as `short.full.mp4`, cuts from that master into `short.mp4`, and records what it applied in `short.trim.json`. Three call sites use it — a studio job, `cmd_render`, and the studio's render job — so `short.mp4` always embodies the recorded trim. The studio previews the cut in the player with no encoding at all, and refuses delivery while a trim is pending.

**Tech Stack:** Python 3.14 (stdlib + subprocess), ffmpeg/ffprobe (the existing binary — no new codecs), FastAPI (studio routes only), React 19 + Mantine 9 + TypeScript, pytest, Vitest, Playwright-inside-pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-short-trim-design.md`

**One deliberate refinement of the spec.** The spec says `editorial.load` validates `head + tail + MIN_REMAINING_SECONDS <= duration`. `load` takes only a directory and has no access to `clip.json`'s duration, and making `editorial` read clip files would couple it to `clipstore`'s naming. So the split is: `load` validates the SHAPE (a pair of non-negative numbers), and a separate `editorial.validate_trim(trim, duration, path)` validates the duration relationship, called by the studio's PATCH route — exactly the pattern `validate_upload_override` already exists for, and for the same stated reason (reject before writing something `load` would only reject on the next read). Everything else follows the spec as written.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Tests: `PYTHONPATH=src .venv/bin/pytest -q`.
- Run the suite in the FOREGROUND with a generous timeout (400000 ms). Backgrounded runs do not reliably notify.
- `python3 tools/lint.py` must exit 0 (no `PYTHONPATH`).
- No test may hit the network, read a real API key, import `anthropic`, run a real Whisper decode, or spend money.
- The six SHA-256 hashes in `tests/test_event_layer_no_regression.py` must never be re-pinned. Nothing here touches `overlay.py`.
- Never reinstall or upgrade ffmpeg. It has no `libfreetype`/`libass`; nothing here needs them.
- `/Users/jegr/racecast/` is read-only.
- `setsar=1` stays at the end of the compose chain. Verify geometry with `ffprobe`, never with an extracted still.
- `render.py` must not learn about `clipstore` or editorial data.
- `trim.py` must not import FastAPI — `bin/yt-shorts` imports it in a venv that never installed FastAPI. Same rule as `subtitle_pipeline.py`.
- Frontend pure logic lives in its own module (`trim.ts`), never exported from a component file — Vite's fast-refresh boundary stays component-only.
- The built bundle is committed: after any frontend change run `npm run build` in `src/yt_shorts/studio/web/` and commit `src/yt_shorts/studio/static/`.
- `MIN_REMAINING_SECONDS = 3.0`. A floor on what remains, never a target length.
- Encoder settings are fixed: `-c:v libx264 -crf 18 -preset veryfast -c:a copy`.
- `-ss` and `-to` must BOTH precede `-i`. Measured: `-ss 5 -to 79 -i in.mp4` → 74.08 s (correct); `-ss 5 -i in.mp4 -to 79` → 79.10 s (wrong, exit code 0).

---

### Task 1: `Edit.trim` — the editorial value

**Files:**
- Modify: `src/yt_shorts/editorial.py`
- Test: `tests/test_editorial.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `editorial.Edit.trim: tuple[float, float] | None`; `editorial.MIN_REMAINING_SECONDS: float`; `editorial.effective_trim(edit) -> tuple[float, float]`; `editorial.validate_trim(trim: list | tuple | None, duration: float, path) -> None` raising `EditError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_editorial.py`:

```python
class TestTrim:
    def test_absent_trim_loads_as_none(self, tmp_path):
        (tmp_path / "edit.json").write_text('{"status": "candidate"}')
        assert editorial.load(tmp_path).trim is None

    def test_a_trim_round_trips(self, tmp_path):
        editorial.save(tmp_path, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(3.0, 2.0)))
        assert editorial.load(tmp_path).trim == (3.0, 2.0)

    def test_a_zero_trim_is_still_written_and_read_back(self, tmp_path):
        # (0, 0) and None mean the same thing to every consumer, but an
        # operator who typed two zeros has made a decision; save must not
        # quietly drop it and make the field flicker back to "unset".
        editorial.save(tmp_path, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(0.0, 0.0)))
        assert editorial.load(tmp_path).trim == (0.0, 0.0)

    def test_a_non_pair_is_reported(self, tmp_path):
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [1.0]}')
        with pytest.raises(editorial.EditError) as error:
            editorial.load(tmp_path)
        assert "trim" in str(error.value)

    def test_a_negative_value_is_reported(self, tmp_path):
        # Negative would mean EXTENDING the short, and there is no material
        # for that - the master is exactly as long as it is.
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [-1.0, 0.0]}')
        with pytest.raises(editorial.EditError):
            editorial.load(tmp_path)

    def test_a_boolean_is_not_a_number(self, tmp_path):
        # bool is a subclass of int in Python; without an explicit check
        # `true` would load as 1.0 second.
        (tmp_path / "edit.json").write_text('{"status": "candidate", "trim": [true, 0.0]}')
        with pytest.raises(editorial.EditError):
            editorial.load(tmp_path)

    def test_effective_trim_defaults_to_zeros(self):
        edit = editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None)
        assert editorial.effective_trim(edit) == (0.0, 0.0)

    def test_effective_trim_returns_the_override(self):
        edit = editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None,
                              trim=(1.5, 0.5))
        assert editorial.effective_trim(edit) == (1.5, 0.5)


class TestValidateTrim:
    def test_none_is_always_valid(self):
        editorial.validate_trim(None, 10.0, "trim")

    def test_a_trim_leaving_enough_is_valid(self):
        editorial.validate_trim([3.0, 2.0], 20.0, "trim")

    def test_a_trim_leaving_less_than_the_floor_is_reported(self):
        # 20 - 9 - 9 = 2.0, below MIN_REMAINING_SECONDS.
        with pytest.raises(editorial.EditError) as error:
            editorial.validate_trim([9.0, 9.0], 20.0, "trim")
        assert "3.0" in str(error.value)

    def test_the_floor_itself_is_allowed(self):
        editorial.validate_trim([8.5, 8.5], 20.0, "trim")

    def test_a_trim_longer_than_the_clip_is_reported(self):
        with pytest.raises(editorial.EditError):
            editorial.validate_trim([30.0, 0.0], 20.0, "trim")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q -k "Trim"`
Expected: FAIL — `AttributeError: module 'yt_shorts.editorial' has no attribute 'effective_trim'` and `TypeError: Edit.__init__() got an unexpected keyword argument 'trim'`.

- [ ] **Step 3: Add the field, the constant and the two functions**

In `src/yt_shorts/editorial.py`, add to the `Edit` dataclass AFTER `upload` (appending keeps every existing positional construction working):

```python
    trim: tuple[float, float] | None = None
```

Add beside the other module constants:

```python
# A floor on what must REMAIN after a trim, never a target length: below three
# seconds there is no short left, only a mistake.
MIN_REMAINING_SECONDS = 3.0
```

In `load`, directly after the `window` block and before the `upload` block:

```python
    trim = payload.get("trim")
    if trim is not None:
        if (not isinstance(trim, list) or len(trim) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and v >= 0 for v in trim)):
            raise EditError(
                f"'trim' must be a [head, tail] pair of non-negative numbers "
                f"or absent: {path}")
        trim = (float(trim[0]), float(trim[1]))
```

Change `load`'s return to carry it:

```python
    return Edit(title=title, status=status, transcript=transcript, window=window,
                upload=upload, trim=trim)
```

In `save`, after the `window` line:

```python
    if edit.trim is not None:
        payload["trim"] = [edit.trim[0], edit.trim[1]]
```

Add beside `effective_window`:

```python
def effective_trim(edit: Edit) -> tuple[float, float]:
    """The operator's head/tail cut, or no cut at all.

    An absent trim and (0.0, 0.0) are the same request; this is the one place
    that flattens the distinction, so no consumer has to test for None.
    """
    return edit.trim if edit.trim is not None else (0.0, 0.0)


def validate_trim(trim, duration: float, path: str | Path = "trim") -> None:
    """Validates a trim AGAINST A DURATION - the half `load` cannot do.

    `load` sees only a directory, so it validates the shape (a pair of
    non-negative numbers) and nothing else. Whether a trim leaves a short
    behind depends on the clip's length, which only a caller holding
    clip.json knows. Same division `validate_upload_override` already draws,
    and for the same reason: the studio's PATCH route rejects a bad value
    BEFORE writing it, rather than writing something `load` will refuse on
    every future read of that clip.
    """
    if trim is None:
        return
    head, tail = float(trim[0]), float(trim[1])
    if head + tail + MIN_REMAINING_SECONDS > duration + 1e-9:
        raise EditError(
            f"a trim of {head}s + {tail}s leaves less than "
            f"{MIN_REMAINING_SECONDS}s of a {duration}s clip: {path}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q`
Expected: PASS, no failures.

- [ ] **Step 5: Run the whole suite (nothing else may move)**

Run: `PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 400000)
Expected: all pass. `Edit` gained an optional trailing field, so every existing construction still works.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/editorial.py tests/test_editorial.py
git commit -m "feat(editorial): a clip's head/tail trim is an editorial value"
```

---

### Task 2: `trim.py` — the one place a cut happens

**Files:**
- Create: `src/yt_shorts/trim.py`
- Modify: `src/yt_shorts/clipstore.py`
- Test: `tests/test_trim.py`

**Interfaces:**
- Consumes: `editorial.Edit.trim`, `editorial.effective_trim` (Task 1).
- Produces: `clipstore.short_master_path(dir) -> Path`; `clipstore.short_trim_state_path(dir) -> Path`; `trim.applied(directory) -> tuple[float, float] | None`; `trim.is_pending(directory, edit) -> bool`; `trim.forget_applied(directory) -> None`; `trim.ensure_applied(directory, edit, *, runner=..., ffmpeg="ffmpeg", ffprobe="ffprobe") -> bool`; `trim.TrimError`.

- [ ] **Step 1: Add the two path helpers**

In `src/yt_shorts/clipstore.py`, beside `short_path`:

```python
def short_master_path(clip_dir_: str | Path) -> Path:
    """The untrimmed render, kept only while a trim is applied."""
    return Path(clip_dir_) / "short.full.mp4"


def short_trim_state_path(clip_dir_: str | Path) -> Path:
    """Which trim short.mp4 currently embodies. Absent means none."""
    return Path(clip_dir_) / "short.trim.json"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_trim.py`:

```python
"""The one place a rendered short is cut.

Every branch runs with an INJECTED subprocess runner - no ffmpeg, no ffprobe,
no video - except the single real-ffmpeg test at the end, which is what
catches the argument-order trap ffmpeg reports no error for.
"""

import json

import pytest

from yt_shorts import clipstore, editorial, trim


def _edit(**over):
    return editorial.Edit(title=None, status=editorial.CANDIDATE,
                          transcript=None, **over)


def _clip_dir(tmp_path, *, short_bytes=b"SHORT"):
    directory = tmp_path / "clip"
    directory.mkdir()
    if short_bytes is not None:
        clipstore.short_path(directory).write_bytes(short_bytes)
    return directory


class FakeRunner:
    """Stands in for the ffmpeg/ffprobe boundary. Records every command and
    writes the output file the real ffmpeg would have written."""

    def __init__(self, duration=84.0, fail=False):
        self.duration = duration
        self.fail = fail
        self.commands = []

    def __call__(self, command):
        self.commands.append(list(command))
        if command[0].endswith("ffprobe"):
            return f"{self.duration}\n"
        if self.fail:
            raise RuntimeError("ffmpeg exploded")
        from pathlib import Path
        Path(command[-1]).write_bytes(b"CUT")
        return ""


class TestNothingToDo:
    def test_no_trim_and_no_state_changes_nothing(self, tmp_path):
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        assert trim.ensure_applied(directory, _edit(), runner=runner) is False
        assert runner.commands == []
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"

    def test_no_short_at_all_is_not_an_error(self, tmp_path):
        # The values may be set before anything is rendered; the render
        # applies them later.
        directory = _clip_dir(tmp_path, short_bytes=None)
        runner = FakeRunner()
        assert trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                   runner=runner) is False
        assert runner.commands == []

    def test_applying_the_same_trim_twice_cuts_once(self, tmp_path):
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        assert trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                   runner=runner) is True
        first = len(runner.commands)
        assert trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                   runner=runner) is False
        assert len(runner.commands) == first


class TestCuttingFromTheMaster:
    def test_the_first_cut_promotes_the_short_to_master(self, tmp_path):
        directory = _clip_dir(tmp_path)
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        assert clipstore.short_master_path(directory).read_bytes() == b"SHORT"
        assert clipstore.short_path(directory).read_bytes() == b"CUT"
        assert trim.applied(directory) == (3.0, 2.0)

    def test_a_second_different_trim_measures_from_the_original(self, tmp_path):
        # The whole reason a master is kept: corrections must not compound.
        directory = _clip_dir(tmp_path)
        runner = FakeRunner(duration=84.0)
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=runner)
        trim.ensure_applied(directory, _edit(trim=(5.0, 1.0)), runner=runner)
        cuts = [c for c in runner.commands if not c[0].endswith("ffprobe")]
        assert len(cuts) == 2
        # Both cut the master, and both -to values are measured against the
        # master's full 84 s, never against an already-shortened file.
        for command in cuts:
            assert str(clipstore.short_master_path(directory)) in command
        assert cuts[0][cuts[0].index("-to") + 1] == "82.0"
        assert cuts[1][cuts[1].index("-to") + 1] == "83.0"

    def test_ss_and_to_both_precede_the_input(self, tmp_path):
        # Measured, not stylistic: with -to AFTER -i, ffmpeg reads it as a
        # LENGTH rather than a position, the tail cut silently does not
        # happen, and ffmpeg still exits 0.
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=runner)
        command = [c for c in runner.commands if not c[0].endswith("ffprobe")][0]
        assert command.index("-ss") < command.index("-i")
        assert command.index("-to") < command.index("-i")

    def test_reverting_to_zero_restores_the_master_and_cleans_up(self, tmp_path):
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=runner)
        assert trim.ensure_applied(directory, _edit(trim=(0.0, 0.0)),
                                   runner=runner) is True
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()
        assert trim.applied(directory) is None


class TestFailureLeavesEverythingConsistent:
    def test_a_failed_cut_keeps_the_previous_pair_and_removes_the_scratch(self, tmp_path):
        directory = _clip_dir(tmp_path)
        with pytest.raises(trim.TrimError):
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                runner=FakeRunner(fail=True))
        # The state file is written only AFTER a successful replace, so a
        # failure leaves "no trim applied" - which is true.
        assert trim.applied(directory) is None
        assert not list(directory.glob("*part*"))

    def test_an_unreadable_probe_reports_rather_than_guessing(self, tmp_path):
        directory = _clip_dir(tmp_path)

        class BadProbe(FakeRunner):
            def __call__(self, command):
                if command[0].endswith("ffprobe"):
                    return "not-a-number\n"
                return super().__call__(command)

        with pytest.raises(trim.TrimError) as error:
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=BadProbe())
        assert "duration" in str(error.value).lower()

    def test_a_missing_master_with_a_state_file_re_promotes(self, tmp_path):
        # A half-deleted directory. Re-applying from the current short loses
        # the already-cut seconds, so it must be reported, not silent.
        directory = _clip_dir(tmp_path)
        clipstore.short_trim_state_path(directory).write_text(
            json.dumps({"head": 3.0, "tail": 2.0}))
        notes = []
        trim.ensure_applied(directory, _edit(trim=(4.0, 1.0)),
                            runner=FakeRunner(), on_note=notes.append)
        assert any("master" in note for note in notes), notes
        assert clipstore.short_master_path(directory).read_bytes() == b"SHORT"


class TestPendingAndForget:
    def test_pending_is_a_comparison_of_two_recorded_values(self, tmp_path):
        directory = _clip_dir(tmp_path)
        assert trim.is_pending(directory, _edit()) is False
        assert trim.is_pending(directory, _edit(trim=(3.0, 2.0))) is True
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        assert trim.is_pending(directory, _edit(trim=(3.0, 2.0))) is False
        assert trim.is_pending(directory, _edit(trim=(4.0, 2.0))) is True

    def test_forget_applied_removes_the_master_as_well_as_the_state(self, tmp_path):
        # The subtle one: after a re-render, short.mp4 IS the new master. A
        # leftover short.full.mp4 is the OLD composition, and cutting from it
        # would build the deliverable out of stale material.
        directory = _clip_dir(tmp_path)
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        trim.forget_applied(directory)
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()

    def test_after_forget_a_re_render_becomes_the_new_master(self, tmp_path):
        directory = _clip_dir(tmp_path)
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        trim.forget_applied(directory)
        clipstore.short_path(directory).write_bytes(b"RERENDERED")
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        assert clipstore.short_master_path(directory).read_bytes() == b"RERENDERED"


class TestWithRealFfmpeg:
    def test_the_cut_lands_on_the_requested_duration_and_keeps_geometry(self, tmp_path):
        """The one test that runs ffmpeg for real.

        It asserts the DURATION, not the exit code: with -to after -i the
        tail cut silently does not happen and ffmpeg still exits 0, so an
        exit-code assertion passes on the broken command.
        """
        import subprocess

        directory = tmp_path / "clip"
        directory.mkdir()
        source = clipstore.short_path(directory)
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=180x320:rate=10:duration=10",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(source)], check=True)

        def probe(path, entries):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", entries,
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, check=True)
            return out.stdout.strip()

        before = probe(source, "stream=width,height,sample_aspect_ratio")
        assert trim.ensure_applied(directory, _edit(trim=(2.0, 3.0))) is True

        duration = float(probe(clipstore.short_path(directory), "format=duration"))
        assert 4.8 < duration < 5.2, duration      # 10 - 2 - 3
        after = probe(clipstore.short_path(directory), "stream=width,height,sample_aspect_ratio")
        assert after.splitlines()[0] == before.splitlines()[0]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_trim.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.trim'`.

- [ ] **Step 4: Write the module**

Create `src/yt_shorts/trim.py`:

```python
"""Cutting seconds off the head and tail of an already-rendered short.

The ONE place a rendered short is cut. It imports `clipstore` and `editorial`
but never FastAPI - `bin/yt-shorts` must import it in a venv that never
installed FastAPI, the same rule `subtitle_pipeline.py` follows.

THE INVARIANT: `short.mp4` embodies the trim recorded in `short.trim.json`
(no file means no trim). Everything here exists to keep that true.

Three files, and each has one job:

    short.mp4         the deliverable - what the player, the download link
                      and the upload all read, always already cut
    short.full.mp4    the untrimmed master, present only while a trim is
                      applied. Derived, not an original: a re-render
                      recreates it.
    short.trim.json   which trim short.mp4 currently embodies

Cutting ALWAYS reads the master, never the current `short.mp4`. That is what
stops a second correction from compounding on the first - trim 3 s, then
change your mind and trim 5 s, and the result is 5 s off the original rather
than 8.

The cut RE-ENCODES. A stream copy was measured and rejected: cuts land on
keyframes, which sit 4.18 s apart in this project's own output, so a cut
requested at 5.0 s landed at 4.18 s. `-crf 18 -preset veryfast` takes 15 s
for an 84-second 1080x1920 short and is visually indistinguishable at one
generation - and because every cut comes from the master, there is never a
second generation.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import clipstore, editorial

# Measured on this project's own 84-second short: 15 s, geometry preserved.
ENCODE_ARGS = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "copy"]

# Tolerance for comparing a recorded trim with a requested one. JSON
# round-trips floats exactly, but an operator's UI may send 3 where 3.0 was
# stored; this keeps that from looking like a change.
TOLERANCE_SECONDS = 0.001


class TrimError(RuntimeError):
    """Cutting failed. The previous short and its state are untouched."""


def _default_runner(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["failed"])[-1]
        raise RuntimeError(message)
    return result.stdout


def _print_note(message: str) -> None:
    import sys
    print(f"NOTE: {message}", file=sys.stderr)


def applied(directory: str | Path) -> tuple[float, float] | None:
    """The trim `short.mp4` currently embodies, or None."""
    path = clipstore.short_trim_state_path(directory)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (float(payload["head"]), float(payload["tail"]))
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable state file means "we do not know what this file is",
        # which is the same starting position as "nothing applied": the next
        # ensure_applied re-derives from whatever short.mp4 is now. Never
        # raises - a corrupt sidecar must not make a clip unopenable.
        return None


def is_pending(directory: str | Path, edit: editorial.Edit) -> bool:
    """Whether the operator's trim has NOT been applied to short.mp4 yet.

    A comparison of two recorded values - never a probe of the video. This
    runs per clip on every studio clip-list request.
    """
    if not clipstore.short_path(directory).exists():
        return False
    desired = editorial.effective_trim(edit)
    current = applied(directory) or (0.0, 0.0)
    return not _same(desired, current)


def forget_applied(directory: str | Path) -> None:
    """Drops the master AND the state file.

    Called by a render BEFORE ensure_applied. After a render, `short.mp4` is
    a freshly composed untrimmed file and IS the new master; a
    `short.full.mp4` left over from before that render is the OLD
    composition, and cutting from it would build the deliverable out of stale
    material while the fresh short sat beside it unused. Removing only the
    state file leaves exactly that stale master in place for the promote step
    to skip - which is why both go.
    """
    for path in (clipstore.short_master_path(directory),
                 clipstore.short_trim_state_path(directory)):
        path.unlink(missing_ok=True)


def ensure_applied(directory: str | Path, edit: editorial.Edit, *,
                   runner=_default_runner, ffmpeg: str = "ffmpeg",
                   ffprobe: str = "ffprobe", on_note=_print_note) -> bool:
    """Makes `short.mp4` embody `edit.trim`. Idempotent. Returns whether it
    changed anything.

    `runner` is the injected subprocess boundary, so every branch tests with
    no ffmpeg, no ffprobe and no video - the same treatment
    `stream_transcribe.ytdlp_downloader` gives its own tool calls.
    """
    directory = Path(directory)
    short = clipstore.short_path(directory)
    if not short.exists():
        return False                      # nothing rendered yet; a render will apply it

    desired = editorial.effective_trim(edit)
    current = applied(directory) or (0.0, 0.0)
    if _same(desired, current):
        return False

    master = clipstore.short_master_path(directory)
    if not master.exists():
        if clipstore.short_trim_state_path(directory).exists():
            on_note(f"{directory.name}: the untrimmed master is gone; re-cutting "
                    f"from the current short, which is already {current[0]}s + "
                    f"{current[1]}s shorter than the render")
        short.replace(master)             # a rename, not a copy

    if _same(desired, (0.0, 0.0)):
        master.replace(short)
        clipstore.short_trim_state_path(directory).unlink(missing_ok=True)
        return True

    duration = _probe_duration(master, runner=runner, ffprobe=ffprobe)
    head, tail = desired
    # The scratch name keeps the .mp4 EXTENSION: ffmpeg picks its muxer from
    # it and refuses to write to an unknown one. Same rule (and same hard
    # lesson) as render.compose's own scratch file.
    scratch = directory / "short.trim-part.mp4"
    try:
        # -ss AND -to both BEFORE -i. Measured on this project's own short:
        # `-ss 5 -to 79 -i in.mp4` yields 74.08 s; moving -to after -i yields
        # 79.10 s, because the seek has reset timestamps and -to is then read
        # as a LENGTH. The tail cut silently does not happen and ffmpeg exits
        # 0 either way.
        runner([ffmpeg, "-v", "error", "-y",
                "-ss", _seconds(head), "-to", _seconds(duration - tail),
                "-i", str(master), *ENCODE_ARGS, str(scratch)])
        # Written aside and MOVED into place, never straight to the target:
        # from the moment a naive in-place cut starts, short.mp4 exists but
        # is incomplete, its (mtime, size) version token matches, and the
        # studio would hand out `immutable` for bytes still being written.
        scratch.replace(short)
    except Exception as error:
        raise TrimError(f"could not cut {directory.name}: "
                        f"{type(error).__name__}: {error}") from error
    finally:
        scratch.unlink(missing_ok=True)

    # AFTER the replace, never before: a failed cut must leave the previous
    # file and its state agreeing with each other.
    clipstore.short_trim_state_path(directory).write_text(
        json.dumps({"head": head, "tail": tail}, indent=2) + "\n", encoding="utf-8")
    return True


def _same(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return (abs(left[0] - right[0]) <= TOLERANCE_SECONDS
            and abs(left[1] - right[1]) <= TOLERANCE_SECONDS)


def _seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _probe_duration(path: Path, *, runner, ffprobe: str) -> float:
    """The master's real length. Never guessed: `-to` is a position, and a
    wrong one silently produces a wrongly-sized short."""
    try:
        output = runner([ffprobe, "-v", "error", "-show_entries",
                         "format=duration", "-of", "csv=p=0", str(path)])
        return float(output.strip())
    except Exception as error:
        raise TrimError(
            f"could not read the duration of {path.name}: "
            f"{type(error).__name__}: {error}") from error
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_trim.py -q`
Expected: PASS, including `TestWithRealFfmpeg`.

`_seconds(82.0)` returns `"82"`, not `"82.0"` — it strips trailing zeros so the command line stays readable. The two assertions in `test_a_second_different_trim_measures_from_the_original` are therefore:

```python
        assert cuts[0][cuts[0].index("-to") + 1] == "82"
        assert cuts[1][cuts[1].index("-to") + 1] == "83"
```

Fix them to read exactly that before running; do not change `_seconds` instead.

- [ ] **Step 6: Run lint and the whole suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 400000)
Expected: lint exit 0, all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/trim.py src/yt_shorts/clipstore.py tests/test_trim.py
git commit -m "feat(trim): cut a rendered short from an untrimmed master"
```

---

### Task 3: Every render leaves the invariant true

**Files:**
- Modify: `bin/yt-shorts` (the `build_short` call in `cmd_render`, around line 220)
- Modify: `src/yt_shorts/studio/jobs.py` (`_render_one`, around line 279)
- Test: `tests/test_cli.py`, `tests/test_studio_jobs.py`

**Interfaces:**
- Consumes: `trim.ensure_applied`, `trim.forget_applied` (Task 2); `editorial.Edit.trim` (Task 1).
- Produces: nothing new — both render paths now end with the trim applied.

- [ ] **Step 1: Write the failing tests**

In `tests/test_studio_jobs.py`, add:

Append to `tests/test_studio_jobs.py`, modelled on `TestALostCaptionTrackIsReported` (same fixtures, same stubbing technique):

```python
class TestRenderAppliesTheTrim:
    """A render writes an UNTRIMMED short.mp4. Without re-applying, the
    operator's trim silently vanishes on every re-render - and the stale
    master from before the render must go first, or the next cut would read
    the old composition instead of this one."""

    def test_the_render_forgets_the_old_master_then_re_applies(
            self, event_dir, studio_profile, monkeypatch):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, trim=(3.0, 2.0)))

        def fake_build_short(source, hook, footer, target, config, work_dir, **kwargs):
            Path(target).write_bytes(b"stub short")
            return target

        calls = []
        monkeypatch.setattr("yt_shorts.studio.jobs.render.build_short", fake_build_short)
        monkeypatch.setattr("yt_shorts.studio.jobs.trim.forget_applied",
                            lambda d: calls.append(("forget", Path(d).name)))
        monkeypatch.setattr(
            "yt_shorts.studio.jobs.trim.ensure_applied",
            lambda d, e, **kw: calls.append(("apply", Path(d).name, e.trim)) or True)

        job = jobs_module.JobStore().create(kind="render")
        jobs_module._render_one(studio_profile, directory, directory.name,
                                skip_discarded=False, job=job)

        assert [c[0] for c in calls] == ["forget", "apply"], calls
        assert calls[1][2] == (3.0, 2.0)
        assert job.snapshot()["results"][directory.name]["status"] == "done"
```

Add `editorial` to that file's imports if it is not already there.

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_jobs.py -q -k Trim`
Expected: FAIL — `AttributeError: module 'yt_shorts.studio.jobs' has no attribute 'trim'`.

- [ ] **Step 3: Wire both render paths**

In `src/yt_shorts/studio/jobs.py`, add `trim` to the existing `yt_shorts` import group, and directly after the `render.build_short(...)` call in `_render_one`:

```python
        # The freshly composed short IS the new master, so any master left
        # from before this render is stale - forget_applied drops it AND the
        # state file, then ensure_applied promotes this composition and cuts.
        # Without this pair a render silently discards the operator's trim.
        trim.forget_applied(directory)
        trim.ensure_applied(directory, edit, on_note=notes.append)
```

In `bin/yt-shorts`, add `from yt_shorts import trim  # noqa: E402` beside the other `yt_shorts` imports, and directly after the `build_short(...)` call in `cmd_render`:

```python
            trim.forget_applied(directory)
            trim.ensure_applied(directory, edit)
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_jobs.py tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Run lint and the whole suite**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 400000)
Expected: lint exit 0, all tests pass.

- [ ] **Step 6: Commit**

```bash
git add bin/yt-shorts src/yt_shorts/studio/jobs.py tests/
git commit -m "fix(render): a render re-applies the operator's trim instead of dropping it"
```

---

### Task 4: The studio's routes — apply, report, refuse

**Files:**
- Modify: `src/yt_shorts/studio/api.py` (`_summary` ~line 428, `patch_clip` ~line 1590, `post_upload` ~line 1526, `get_short` ~line 1710, the route docstring block ~line 44)
- Modify: `src/yt_shorts/studio/jobs.py` (add `start_trim_job`)
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: `trim.ensure_applied`, `trim.is_pending`, `trim.applied` (Task 2); `editorial.validate_trim`, `editorial.Edit.trim` (Task 1).
- Produces: `POST …/clips/{name}/trim` returning `{"job_id": str}`; `GET …/clips/{name}/short?as=download`; `_summary` fields `trim` and `trim_applied`; `PatchClipBody.trim`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_studio_api.py`, add:

```python
class TestTrimRoutes:
    def test_summary_reports_desired_and_applied(self, client, event_dir):
        # A clip with a rendered short and no trim: both null.
        body = client.get(f"{EV}/clips/{NAME}").json()
        assert body["trim"] is None and body["trim_applied"] is None

    def test_patching_a_trim_stores_it(self, client, event_dir):
        body = client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]}).json()
        assert body["trim"] == [3.0, 2.0]
        assert body["trim_applied"] is None          # set, not applied

    def test_a_trim_leaving_less_than_the_floor_is_422(self, client, event_dir):
        response = client.patch(f"{EV}/clips/{NAME}", json={"trim": [999.0, 999.0]})
        assert response.status_code == 422

    def test_null_clears_the_trim(self, client, event_dir):
        client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]})
        assert client.patch(f"{EV}/clips/{NAME}", json={"trim": None}).json()["trim"] is None

    def test_upload_is_refused_while_a_trim_is_pending(self, client, event_dir):
        client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]})
        response = client.post(f"{EV}/clips/{NAME}/upload")
        assert response.status_code == 409
        assert "trim" in response.json()["detail"].lower()

    def test_the_download_form_is_refused_while_pending(self, client, event_dir):
        client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]})
        assert client.get(f"{EV}/clips/{NAME}/short?as=download").status_code == 409

    def test_the_plain_short_url_still_streams_while_pending(self, client, event_dir):
        # The player previews the trim; blocking this would kill the very
        # thing the operator needs in order to choose the values.
        client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]})
        assert client.get(f"{EV}/clips/{NAME}/short").status_code == 200

    def test_applying_starts_a_job(self, client, event_dir, monkeypatch):
        client.patch(f"{EV}/clips/{NAME}", json={"trim": [3.0, 2.0]})
        response = client.post(f"{EV}/clips/{NAME}/trim")
        assert response.status_code == 200 and "job_id" in response.json()

    def test_applying_without_a_short_is_409(self, client, event_dir):
        # NAME_WITHOUT_SHORT: use the same fixture the other "no short"
        # tests in this file already rely on.
        response = client.post(f"{EV}/clips/{NAME_WITHOUT_SHORT}/trim")
        assert response.status_code == 409
```

Use this file's existing fixtures (`client`, `event_dir`, the `EV` scope constant and the clip-name constants) exactly as the neighbouring tests do.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k Trim`
Expected: FAIL — `KeyError: 'trim'` and `404` for the new route.

- [ ] **Step 3: Add the job starter**

In `src/yt_shorts/studio/jobs.py`:

```python
def _run_trim(profile: Profile, job: Job, name: str, event_lock: EventLock) -> None:
    log = job_logger(job)
    log.info("start: trim %s", name)
    try:
        directory = profile.event_dir / clipstore.CLIPS_DIRNAME / name
        edit = editorial.load(directory)
        notes: list[str] = []
        changed = trim.ensure_applied(directory, edit, on_note=notes.append)
        summary = "; ".join(notes)
        job.record(name, "done", summary or None,
                   f"done: {name}" + ("" if changed else " (already applied)"))
        job.finish("done")
    except Exception as error:  # noqa: BLE001 - a failed trim must not kill the studio
        reason = f"{type(error).__name__}: {logsetup.shorten_urls(str(error))}"
        job.record(name, "failed", reason, f"ERROR: {name}: {reason}")
        job.finish("failed")
    finally:
        _log_terminal(job)
        finish_job_log(job)
        event_lock.release()


def start_trim_job(profile: Profile, job_store: JobStore, name: str) -> Job:
    """Applies a clip's trim in the background, under the SAME event lock a
    render takes: applying writes short.mp4, and so does a render."""
    event_lock = EventLock(profile.event_dir)
    event_lock.acquire()
    job = job_store.create("trim")
    thread = threading.Thread(target=_run_trim,
                              args=(profile, job, name, event_lock), daemon=True)
    thread.start()
    return job
```

- [ ] **Step 4: Add the routes and the summary fields**

In `_summary`, beside `short_version`:

```python
        # Two RECORDED values, never a probe: this runs per clip on every
        # list request. `trim` is what the operator wants, `trim_applied` is
        # what short.mp4 currently embodies; unequal means pending.
        "trim": list(edit.trim) if edit.trim is not None else None,
        "trim_applied": (list(_applied) if (_applied := trim.applied(directory))
                         else None),
```

In `PatchClipBody`:

```python
    # Head/tail seconds to cut off the rendered short; null clears it back to
    # no trim. Told apart from "omitted" by model_fields_set, like window.
    trim: list[float] | None = None
```

In `patch_clip`, beside the `window` block:

```python
        if "trim" in fields_set:
            if body.trim is None:
                trim_value = None
            elif len(body.trim) != 2:
                raise HTTPException(status_code=422, detail="trim must be [head, tail]")
            else:
                trim_value = (float(body.trim[0]), float(body.trim[1]))
                try:
                    editorial.validate_trim(trim_value, float(clip.get("duration", 0.0)),
                                            "trim")
                except editorial.EditError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
```

Add `trim_value` to the `Edit(...)` construction (initialise it as `trim_value = edit.trim` beside the other locals at the top of the function).

The new route, beside `post_render`:

```python
    @app.post(EV + "/clips/{name}/trim")
    def post_trim(channel: str, event: str, name: str) -> dict:
        profile = _load_profile(channel, event)
        directory, _clip = _load_clip_or_404(profile, name)
        if not clipstore.short_path(directory).exists():
            raise HTTPException(
                status_code=409,
                detail=f"{name} has no rendered short yet; the trim will be "
                       f"applied by the next render")
        try:
            job = jobs.start_trim_job(profile, app.state.job_store, name)
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}
```

In `get_short`, add the parameter and the guard:

```python
    def get_short(channel: str, event: str, name: str, v: str | None = None,
                  as_: str | None = Query(None, alias="as")):
        ...
        # The guard belongs to LEAVING the studio, not to previewing. The
        # player reads the plain URL and must keep working while a trim is
        # pending - previewing the cut is exactly how an operator chooses the
        # values. `as=download` is what the download link sets, and only that
        # form is refused. Stated plainly because it has a limit: a hand-made
        # request without the parameter still gets the untrimmed file. What is
        # hard-guarded is the path that reaches the channel.
        if as_ == "download" and trim.is_pending(directory, _load_edit_or_500(directory)):
            raise HTTPException(
                status_code=409,
                detail=f"{name} has a trim that has not been applied yet - "
                       f"apply it before downloading")
```

Import `Query` from fastapi if it is not already imported.

In `post_upload`, after the existing "no rendered short" check:

```python
        if trim.is_pending(directory, edit):
            raise HTTPException(
                status_code=409,
                detail=f"{name} has a trim that has not been applied yet - "
                       f"apply it before uploading")
```

Add the two new routes to the docstring route list at the top of `api.py`.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q`
Expected: PASS.

- [ ] **Step 6: Verify create_app() still pulls no heavy imports**

Run:
```bash
PYTHONPATH=src .venv/bin/python -c "
import sys
class Block:
    def find_module(self, name, path=None):
        if name in ('anthropic', 'googleapiclient'): raise ImportError('blocked')
sys.meta_path.insert(0, Block())
from yt_shorts.studio.api import create_app
create_app(); print('ok')"
```
Expected: `ok`.

- [ ] **Step 7: Run lint and the whole suite, then commit**

Run: `python3 tools/lint.py && PYTHONPATH=src .venv/bin/pytest -q` (foreground, timeout 400000)

```bash
git add src/yt_shorts/studio/ tests/test_studio_api.py
git commit -m "feat(studio): apply a trim as a job, and refuse delivery while one is pending"
```

---

### Task 5: `trim.ts` — the frontend's arithmetic

**Files:**
- Create: `src/yt_shorts/studio/web/src/trim.ts`
- Create: `src/yt_shorts/studio/web/src/trim.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `type Trim = { head: number; tail: number }`; `remainingSeconds(duration, trim)`; `isPending(desired, applied)`; `trimProblems(duration, trim): string[]`; `MIN_REMAINING_SECONDS = 3`.

- [ ] **Step 1: Write the failing tests**

Create `src/yt_shorts/studio/web/src/trim.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { MIN_REMAINING_SECONDS, isPending, remainingSeconds, trimProblems } from './trim'

describe('remainingSeconds', () => {
  it('subtracts both ends', () => {
    expect(remainingSeconds(84, { head: 3, tail: 2 })).toBeCloseTo(79)
  })

  it('never goes below zero', () => {
    expect(remainingSeconds(10, { head: 9, tail: 9 })).toBe(0)
  })
})

describe('isPending', () => {
  it('is false when desired equals applied', () => {
    expect(isPending({ head: 3, tail: 2 }, { head: 3, tail: 2 })).toBe(false)
  })

  it('treats no trim and a zero trim as the same request', () => {
    expect(isPending({ head: 0, tail: 0 }, null)).toBe(false)
    expect(isPending(null, { head: 0, tail: 0 })).toBe(false)
  })

  it('is true once either end differs', () => {
    expect(isPending({ head: 3, tail: 2 }, { head: 3, tail: 1 })).toBe(true)
    expect(isPending({ head: 1, tail: 0 }, null)).toBe(true)
  })

  it('tolerates float noise from a JSON round trip', () => {
    expect(isPending({ head: 3.0000001, tail: 2 }, { head: 3, tail: 2 })).toBe(false)
  })
})

describe('trimProblems', () => {
  it('is empty for a trim that leaves enough', () => {
    expect(trimProblems(84, { head: 3, tail: 2 })).toEqual([])
  })

  it('flags a negative value', () => {
    expect(trimProblems(84, { head: -1, tail: 0 })).toHaveLength(1)
  })

  it('flags a trim that leaves less than the floor', () => {
    // Same floor the server enforces; the client says so before the 422.
    expect(trimProblems(10, { head: 4, tail: 4 })).toHaveLength(1)
    expect(MIN_REMAINING_SECONDS).toBe(3)
  })
})
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd src/yt_shorts/studio/web && npm test`
Expected: FAIL — cannot resolve `./trim`.

- [ ] **Step 3: Write the module**

Create `src/yt_shorts/studio/web/src/trim.ts`:

```ts
/**
 * The arithmetic behind trimming a rendered short.
 *
 * Pure, and in its own module rather than inside a component, so Vite's
 * fast-refresh boundary stays component-only and these rules are unit-tested
 * without rendering anything - same arrangement as words.ts and window.ts.
 */

export type Trim = { head: number; tail: number }

/** The server's own floor (editorial.MIN_REMAINING_SECONDS). Kept in step so
 * the operator is told before the 422, not by it. */
export const MIN_REMAINING_SECONDS = 3

/** Float noise from a JSON round trip must not read as a change. */
const TOLERANCE_SECONDS = 0.001

export function remainingSeconds(duration: number, trim: Trim | null): number {
  if (!trim) return duration
  return Math.max(0, duration - trim.head - trim.tail)
}

export function isPending(desired: Trim | null, applied: Trim | null): boolean {
  const a = desired ?? { head: 0, tail: 0 }
  const b = applied ?? { head: 0, tail: 0 }
  return (Math.abs(a.head - b.head) > TOLERANCE_SECONDS
    || Math.abs(a.tail - b.tail) > TOLERANCE_SECONDS)
}

export function trimProblems(duration: number, trim: Trim | null): string[] {
  if (!trim) return []
  const problems: string[] = []
  if (trim.head < 0 || trim.tail < 0) {
    problems.push('A trim cannot be negative - there is no material to add back.')
  }
  if (remainingSeconds(duration, trim) < MIN_REMAINING_SECONDS) {
    problems.push(`Less than ${MIN_REMAINING_SECONDS}s would remain.`)
  }
  return problems
}
```

- [ ] **Step 4: Run the tests**

Run: `cd src/yt_shorts/studio/web && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/trim.ts src/yt_shorts/studio/web/src/trim.test.ts
git commit -m "feat(studio-web): the trim arithmetic, as its own tested module"
```

---

### Task 6: The controls, the free preview, and the guarded links

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts` (`ClipSummary`, `shortUrl`, a new `applyTrim`)
- Modify: `src/yt_shorts/studio/web/src/components/ClipEditor.tsx` (the player block at line 332)
- Modify: `src/yt_shorts/studio/web/src/components/ManualUploadPanel.tsx` (the download link at line 80)
- Modify: `src/yt_shorts/studio/web/src/components/UploadPanel.tsx`
- Rebuild: `src/yt_shorts/studio/static/`
- Test: `tests/test_studio_e2e.py`

**Interfaces:**
- Consumes: `trim.ts` (Task 5); `_summary`'s `trim`/`trim_applied` and `POST …/clips/{name}/trim` (Task 4).
- Produces: nothing further.

- [ ] **Step 1: Extend the API layer**

In `api.ts`, add to `ClipSummary`:

```ts
  /** The operator's head/tail cut in seconds, or null for none. */
  trim: [number, number] | null
  /** The cut short.mp4 currently embodies, or null. Unequal to `trim` means
   * the cut has not been applied yet - see trim.ts's isPending. */
  trim_applied: [number, number] | null
```

Change `shortUrl` so a caller must state its purpose:

```ts
/** `purpose` is required so tsc fails if a call site forgets it: the
 * download form is refused by the server while a trim is pending, the
 * player's is not - previewing the cut is how an operator chooses it. */
export function shortUrl(name: string, version: string | null,
                         purpose: 'play' | 'download' = 'play'): string {
  const base = `${eventScope()}/clips/${encodeURIComponent(name)}/short`
  const params = new URLSearchParams()
  if (version !== null) params.set('v', version)
  if (purpose === 'download') params.set('as', 'download')
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

export function applyTrim(name: string): Promise<{ job_id: string }> {
  return fetch(`${eventScope()}/clips/${encodeURIComponent(name)}/trim`,
               { method: 'POST' }).then(asJson<{ job_id: string }>)
}
```

- [ ] **Step 2: Add the controls and the preview to ClipEditor**

Replace the `<video>` at `ClipEditor.tsx:334` with a player that honours the trim, and add the controls below it:

```tsx
<video
  ref={videoRef}
  controls
  src={shortUrl(clip.name, clip.short_version)}
  style={{ width: '100%', maxHeight: 360 }}
  onLoadedMetadata={(event) => { event.currentTarget.currentTime = trimHead }}
  onTimeUpdate={(event) => {
    // The preview costs nothing: no encoding, no request, no new file. That
    // is what makes nudging the values until they look right free.
    const video = event.currentTarget
    if (video.duration && video.currentTime >= video.duration - trimTail) {
      video.pause()
    }
  }}
/>
<Group gap="xs" mt="xs" align="end">
  <NumberInput label="Head (s)" min={0} step={0.5} value={trimHead}
               onChange={(v) => setTrimHead(Number(v) || 0)} w={110} />
  <NumberInput label="Tail (s)" min={0} step={0.5} value={trimTail}
               onChange={(v) => setTrimTail(Number(v) || 0)} w={110} />
  <Text size="sm" c="dimmed">
    {formatStreamDuration(remainingSeconds(shortDuration, { head: trimHead, tail: trimTail }))} after the cut
  </Text>
  <Button size="xs" onClick={handleApplyTrim}
          disabled={!trimIsPending || trimProblems(shortDuration, { head: trimHead, tail: trimTail }).length > 0}>
    {trimIsPending ? 'Apply trim' : 'Trim applied'}
  </Button>
</Group>
{trimProblems(shortDuration, { head: trimHead, tail: trimTail }).map((problem) => (
  <Text key={problem} size="xs" c="red">{problem}</Text>
))}
```

`shortDuration` comes from the video element's own `duration` on `loadedmetadata` (state, defaulting to `clip.detected_window[1] - clip.detected_window[0]` until it loads). The two values are saved with the rest of the editorial correction, through the existing PATCH — no separate save path. `handleApplyTrim` calls `applyTrim(clip.name)` and then refetches the clip, exactly as the render button already does.

- [ ] **Step 3: Guard the two delivery affordances**

In `ManualUploadPanel.tsx`, change the link to `shortUrl(clip.name, clip.short_version, 'download')` and disable it while `isPending(clip.trim, clip.trim_applied)`, with the reason named in the same place. Do the same for the upload button in `UploadPanel.tsx`.

- [ ] **Step 4: Type-check, lint, unit-test, build**

```bash
cd src/yt_shorts/studio/web
npx tsc --noEmit && npm run lint && npm test && npm run build
```
Expected: all exit 0. `tsc` will flag every `shortUrl` call site that has not been given a purpose — that is the point of the required parameter.

- [ ] **Step 5: Write the E2E**

In `tests/test_studio_e2e.py`, add one journey following this file's existing conventions (its `live_server`/`page` fixtures, and `_wheel_scroll_until_visible` for any reachability assertion — never `scroll_into_view_if_needed()`):

```python
class TestTrimJourney:
    """Set head and tail, watch the resulting duration update with no
    encoding at all, apply, and see the button flip. The preview being free
    is the whole point of the feature - if this test ever needs to wait for
    an encode before the duration text changes, the preview has regressed
    into a render."""

    def test_set_values_see_the_duration_and_apply(self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, _clip_entry(hook="Speedy!"))
        # A real 10-second video, so the player reports a real duration.
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=180x320:rate=10:duration=10",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(clipstore.short_path(directory))], check=True)

        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}")
        page.get_by_text("Speedy!").first.click()

        page.get_by_label("Head (s)").fill("2")
        page.get_by_label("Tail (s)").fill("3")
        # 10 - 2 - 3, shown before anything is cut.
        page.get_by_text("0:05 after the cut").wait_for(timeout=5000)

        apply_button = page.get_by_role("button", name="Apply trim")
        expect(apply_button).to_be_enabled(timeout=5000)
        apply_button.click()

        page.get_by_role("button", name="Trim applied").wait_for(timeout=15000)
        state = json.loads((directory / "short.trim.json").read_text())
        assert (state["head"], state["tail"]) == (2.0, 3.0)
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(clipstore.short_path(directory))],
            capture_output=True, text=True, check=True).stdout.strip())
        assert 4.8 < duration < 5.2, duration
```

`_clip_entry`, `CHANNEL` and `EVENT` are this file's existing helpers; use them rather than inventing new ones. This is the one E2E in the plan and it runs real ffmpeg on a 10-second synthetic clip — no network, no Whisper, no cost.

- [ ] **Step 6: Run lint, the whole suite, and confirm the bundle is committed**

```bash
python3 tools/lint.py
PYTHONPATH=src .venv/bin/pytest -q          # foreground, timeout 400000
cd src/yt_shorts/studio/web && npm run build && cd - && git status --short
```
Expected: lint exit 0, all tests pass, `git status` clean after the build (the committed bundle matches source).

- [ ] **Step 7: Document it in CLAUDE.md**

Add a short section beside "A rendered short is served by a VERSIONED url": the three files and the invariant, that cutting always reads the master so corrections never compound, the `-ss`/`-to` argument-order trap with its two measured numbers, why the download form is guarded but the player's URL is not, and that `forget_applied` drops the master as well as the state file.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/studio/ tests/test_studio_e2e.py CLAUDE.md
git commit -m "feat(studio-web): trim a rendered short from the editor"
```

---

## Self-review notes

- **Spec coverage:** data model → Task 1; files, invariant and the cut → Task 2; render call sites and `forget_applied` ordering → Task 3; routes, guards and `_summary` → Task 4; frontend arithmetic → Task 5; controls, preview, guarded links, E2E and docs → Task 6. The spec's "out of scope" items (cutting from the middle, `Edit.window`, configurable encoder settings) appear in no task, deliberately.
- **The one thing an implementer must not "simplify":** `-ss` and `-to` both before `-i`, and the real-ffmpeg test asserting the resulting DURATION. Every other assertion in this plan would pass on the broken command.
- **The second:** `forget_applied` removes the master AND the state file. Removing only the state file leaves a stale master that later cuts would read.
