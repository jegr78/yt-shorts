"""The one place a rendered short is cut.

Every branch runs with an INJECTED subprocess runner - no ffmpeg, no ffprobe,
no video - except the single real-ffmpeg test at the end, which is what
catches the argument-order trap ffmpeg reports no error for.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from yt_shorts import clipstore, editorial, trim
from yt_shorts.cancel import CancelToken, Stopped, run_cancellable


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


class FailAfterWritingRunner(FakeRunner):
    """Fails AFTER writing its output file - the shape a real ffmpeg failure
    takes (a crash partway through an encode). `FakeRunner(fail=True)` raises
    BEFORE writing anything, so a test built on it proves nothing about
    atomicity: no scratch file ever exists, so asserting one is gone is
    vacuous. This is what a reviewer used to prove that: deleting the
    scratch-then-replace dance AND deleting the `finally` cleanup both still
    left all 14 pre-existing tests green."""

    def __call__(self, command):
        self.commands.append(list(command))
        if command[0].endswith("ffprobe"):
            return f"{self.duration}\n"
        from pathlib import Path
        Path(command[-1]).write_bytes(b"PARTIAL")
        raise RuntimeError("ffmpeg exploded mid-encode")


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

    def test_the_first_cut_is_silent(self, tmp_path):
        # No short.trim.json exists yet, so this is an ordinary first cut,
        # not the half-deleted-directory case the missing-master note is
        # for. A regression that fired the note on every first cut would
        # pass every other test in this file - nothing else pins silence.
        directory = _clip_dir(tmp_path)
        notes = []
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                            runner=FakeRunner(), on_note=notes.append)
        assert notes == []

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
        assert cuts[0][cuts[0].index("-to") + 1] == "82"
        assert cuts[1][cuts[1].index("-to") + 1] == "83"

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

    def test_a_failed_zero_revert_raises_trimerror_not_a_raw_oserror(
            self, tmp_path, monkeypatch):
        # The half-deleted-directory case: no master, but a state file, so
        # ensure_applied re-promotes the current short to master before
        # noticing the desired trim is (0, 0) and immediately trying to
        # rename it straight back. That second rename sat outside every
        # try/except - a real OS failure here (a full disk, a permission
        # error) used to escape as a bare OSError instead of the module's
        # one error type, and would have left short.mp4 stranded under the
        # master's name with nothing having tried to name what happened.
        directory = _clip_dir(tmp_path)
        clipstore.short_trim_state_path(directory).write_text(
            json.dumps({"head": 3.0, "tail": 2.0}))
        master = clipstore.short_master_path(directory)
        real_replace = Path.replace

        def flaky_replace(self, target):
            if self == master:
                raise OSError("disk full")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", flaky_replace)
        with pytest.raises(trim.TrimError) as error:
            trim.ensure_applied(directory, _edit(trim=(0.0, 0.0)), runner=FakeRunner())
        # The message must name the recovery, not just the cause: a complete
        # copy is sitting right there under short.full.mp4, and an operator
        # reading only "could not restore ... disk full" has no way to learn
        # that without reading this module's own source.
        message = str(error.value)
        assert "short.full.mp4" in message
        assert "short.mp4" in message
        assert "re-render" in message


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
        # The deliverable itself, not just the state file: short.mp4 was
        # RENAMED to short.full.mp4 before the cut was even attempted, so a
        # failed cut used to leave short.mp4 gone for good - only
        # short.full.mp4 remained, applied()/is_pending() both then read
        # "nothing to do" from its absence, and the studio showed a perfectly
        # good short as "not rendered". The promotion must be undone.
        assert clipstore.short_path(directory).exists()
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"
        assert not clipstore.short_master_path(directory).exists()

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
        # Same restore as its sibling failure test: the probe fails AFTER
        # the promotion rename, so short.mp4 must come back rather than
        # staying gone under the master's name.
        assert clipstore.short_path(directory).exists()
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"

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

    def test_a_corrupt_state_file_does_not_claim_a_known_zero_loss(self, tmp_path):
        # applied() returns None for a state file it cannot parse - the note
        # used to fall back to (0.0, 0.0) and say "already 0.0s + 0.0s
        # shorter than the render" in the same breath as claiming a trim IS
        # recorded. Self-contradictory, and simply wrong whenever the real
        # numbers were anything else.
        directory = _clip_dir(tmp_path)
        clipstore.short_trim_state_path(directory).write_text("not json")
        notes = []
        trim.ensure_applied(directory, _edit(trim=(4.0, 1.0)),
                            runner=FakeRunner(), on_note=notes.append)
        assert notes, notes
        assert "0.0s" not in notes[0]
        assert "unreadable" in notes[0] or "unknown" in notes[0]


class TestUnknownStateWithAPresentMaster:
    """Master present, state file missing or unreadable, desired trim (0,
    0): the early `_same(desired, current)` return used to fire on the
    (0.0, 0.0) fallback without ever looking at the master sitting right
    beside short.mp4 - so a genuinely cut short.mp4 (state file lost in the
    window between scratch.replace(short) and the state-file write) was
    handed out as if it were the full render, with is_pending and every
    delivery guard agreeing nothing was pending."""

    def test_a_missing_state_file_restores_from_the_master(self, tmp_path):
        directory = _clip_dir(tmp_path, short_bytes=b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"MASTER")
        # No short.trim.json at all.
        notes = []
        changed = trim.ensure_applied(directory, _edit(trim=(0.0, 0.0)),
                                      runner=FakeRunner(), on_note=notes.append)
        assert changed is True
        assert clipstore.short_path(directory).read_bytes() == b"MASTER"
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()
        assert notes, "an operator must be told the file was rebuilt"
        assert "master" in notes[0]

    def test_a_corrupt_state_file_restores_from_the_master(self, tmp_path):
        directory = _clip_dir(tmp_path, short_bytes=b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"MASTER")
        clipstore.short_trim_state_path(directory).write_text("not json")
        notes = []
        changed = trim.ensure_applied(directory, _edit(trim=(0.0, 0.0)),
                                      runner=FakeRunner(), on_note=notes.append)
        assert changed is True
        assert clipstore.short_path(directory).read_bytes() == b"MASTER"
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()
        assert notes, "an operator must be told the file was rebuilt"

    def test_a_readable_state_file_agreeing_with_zero_is_unaffected(self, tmp_path):
        # Control: a master present ALONGSIDE a state file that legitimately
        # and READABLY says (0.0, 0.0) is `known`, not `unknown_but_mastered`
        # - the ordinary "already reverted" case, not the one this fix
        # targets. It must still take the cheap early return and touch
        # neither file.
        directory = _clip_dir(tmp_path, short_bytes=b"SHORT")
        clipstore.short_master_path(directory).write_bytes(b"MASTER")
        clipstore.short_trim_state_path(directory).write_text(
            json.dumps({"head": 0.0, "tail": 0.0}))
        runner = FakeRunner()
        changed = trim.ensure_applied(directory, _edit(trim=(0.0, 0.0)), runner=runner)
        assert changed is False
        assert runner.commands == []
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"
        assert clipstore.short_master_path(directory).exists()


class TestNotesReachBothChannels:
    """Mirrors subtitle_pipeline.make_subtitle_provider's own note() - "two
    consumers, two mechanisms, on purpose". A studio trim job (Task 4) will
    pass its own `on_note` to collect the clip's `reason`; the durable log
    record must not vanish because a caller supplied one. `_log_note` used
    to be the DEFAULT `on_note`, so passing a caller's own callback silently
    dropped the logger entirely - this pins that both fire, always."""

    def test_on_note_does_not_silence_the_logger(self, tmp_path, caplog):
        directory = _clip_dir(tmp_path)
        clipstore.short_trim_state_path(directory).write_text(
            json.dumps({"head": 3.0, "tail": 2.0}))
        notes = []
        with caplog.at_level("WARNING", logger="ytshorts.trim"):
            trim.ensure_applied(directory, _edit(trim=(4.0, 1.0)),
                                runner=FakeRunner(), on_note=notes.append)
        assert notes
        assert any("master" in record.message for record in caplog.records)

    def test_an_injected_logger_receives_the_note_instead_of_the_default(self, tmp_path):
        import logging

        directory = _clip_dir(tmp_path)
        clipstore.short_trim_state_path(directory).write_text(
            json.dumps({"head": 3.0, "tail": 2.0}))

        class Collector(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())

        job_logger = logging.getLogger("test.trim.job_log")
        collector = Collector()
        job_logger.addHandler(collector)
        job_logger.setLevel(logging.WARNING)
        try:
            trim.ensure_applied(directory, _edit(trim=(4.0, 1.0)),
                                runner=FakeRunner(), logger=job_logger)
        finally:
            job_logger.removeHandler(collector)
        assert any("master" in message for message in collector.messages)


class TestAtomicity:
    """CRITICAL 2: nothing in the suite above notices a refactor that skips
    the scratch-file dance and writes straight to short.mp4 - a reviewer
    proved this by deleting `scratch.replace(short)`, deleting the `finally`
    cleanup, and even deleting both together, and all 14 pre-existing tests
    stayed green. `FakeRunner(fail=True)` is why: it raises BEFORE writing
    anything, so no scratch file ever exists and `not
    directory.glob("*part*")` is true regardless of whether the code is
    atomic. These tests use `FailAfterWritingRunner`, which writes its output
    and THEN raises - the shape a real ffmpeg crash mid-encode takes.

    The first version of this test still did not pin the mechanism: it ran
    `FailAfterWritingRunner` on a FIRST cut, where `promoted` is True and the
    Critical-1 restore (`master.replace(short)`) puts the previous short back
    regardless of whether the scratch dance exists - so the assertion passed
    whether or not `scratch.replace(short)`/`finally` were even there. The
    case that mechanism does NOT cover is a failed SECOND cut, where
    `promoted` is False (the master already existed from the first, GOOD cut)
    and there is nothing left to restore FROM - the only thing standing
    between a truncated `short.mp4` and the previous good one is the
    scratch/`finally` dance itself. Mutating `scratch = short` (writing
    straight to the target, no `.replace()`, no `finally`) makes this
    version fail: the runner's `b"PARTIAL"` bytes land directly in
    `short.mp4` and stay there when the raise propagates."""

    def test_a_cut_that_fails_after_writing_leaves_no_scratch_and_the_old_short_intact(
            self, tmp_path):
        directory = _clip_dir(tmp_path)
        good_runner = FakeRunner()
        assert trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                   runner=good_runner) is True
        assert clipstore.short_path(directory).read_bytes() == b"CUT"
        assert trim.applied(directory) == (3.0, 2.0)

        bad_runner = FailAfterWritingRunner()
        with pytest.raises(trim.TrimError):
            trim.ensure_applied(directory, _edit(trim=(5.0, 1.0)), runner=bad_runner)
        assert not list(directory.glob("*part*"))
        # promoted is False here - the master already existed from the first
        # cut - so this is NOT the Critical-1 restore path; the only thing
        # that can be keeping short.mp4 the FIRST cut's bytes rather than the
        # second, failed cut's b"PARTIAL" is the scratch-then-replace dance
        # never having reached its replace.
        assert clipstore.short_path(directory).read_bytes() == b"CUT"
        assert trim.applied(directory) == (3.0, 2.0)

    def test_the_cut_writes_to_a_scratch_path_ending_in_dot_mp4(self, tmp_path):
        # ffmpeg picks its muxer from the OUTPUT extension and refuses to
        # write at all to an unrecognised one - a scratch name like
        # short.mp4.part would make ffmpeg itself fail, not a tidy
        # assertion. Same rule, same hard lesson, as render.compose's own
        # scratch file (see CLAUDE.md, TestComposeIsAtomic).
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=runner)
        cut = [c for c in runner.commands if not c[0].endswith("ffprobe")][0]
        assert cut[-1].endswith(".mp4")


class TestDurationValidation:
    def test_an_over_trim_is_rejected_before_any_cut(self, tmp_path):
        # editorial.validate_trim already produces the right message; this
        # only pins that trim.py actually calls it. Before this, an
        # over-trim reached ffmpeg, which failed with its own opaque
        # "Error opening input files: Invalid argument" instead.
        directory = _clip_dir(tmp_path)
        runner = FakeRunner(duration=4.0)
        with pytest.raises(trim.TrimError) as error:
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=runner)
        assert f"{editorial.MIN_REMAINING_SECONDS}s" in str(error.value)
        # Names the clip directory an operator recognises, not the absolute
        # path of short.full.mp4 - a file this message's audience has never
        # heard of and which the restore just below removes anyway.
        assert directory.name in str(error.value)
        assert str(clipstore.short_master_path(directory)) not in str(error.value)
        cuts = [c for c in runner.commands if not c[0].endswith("ffprobe")]
        assert cuts == []
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"


class TestPendingAndForget:
    def test_pending_is_a_comparison_of_two_recorded_values(self, tmp_path):
        directory = _clip_dir(tmp_path)
        assert trim.is_pending(directory, _edit()) is False
        assert trim.is_pending(directory, _edit(trim=(3.0, 2.0))) is True
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        assert trim.is_pending(directory, _edit(trim=(3.0, 2.0))) is False
        assert trim.is_pending(directory, _edit(trim=(4.0, 2.0))) is True

    def test_a_clip_that_was_never_trimmed_is_not_unknown(self, tmp_path):
        # No master at all - is_unknown must not fire on the ordinary case,
        # or every untouched clip would report unknown.
        directory = _clip_dir(tmp_path)
        assert trim.is_unknown(directory) is False
        assert trim.is_pending(directory, _edit()) is False

    def test_a_normally_applied_trim_is_not_unknown(self, tmp_path):
        # A readable state file agreeing with the master's presence is the
        # ordinary "trim applied" case, not the crash/corruption window
        # is_unknown exists to catch.
        directory = _clip_dir(tmp_path)
        trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)), runner=FakeRunner())
        assert trim.is_unknown(directory) is False
        assert trim.is_pending(directory, _edit(trim=(3.0, 2.0))) is False

    def test_a_master_with_no_state_file_is_unknown_and_blocks_delivery(self, tmp_path):
        # THE BLOCKER: a crash between scratch.replace(short) and the state
        # write (or a deleted/corrupted sidecar) leaves a cut short.mp4
        # sitting next to an untrimmed master with nothing recorded. Before
        # is_unknown existed, is_pending's own (0, 0)-vs-(0, 0) comparison
        # never looked at the master and reported False here - the exact
        # state the reviewer measured shipping a 5s cut as a 10s "full
        # render" on all three delivery paths.
        directory = _clip_dir(tmp_path, short_bytes=b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"MASTER")
        assert trim.is_unknown(directory) is True
        assert trim.is_pending(directory, _edit()) is True
        # Reverting the is_unknown check inside is_pending (i.e. going back
        # to the plain _same(desired, current) comparison) makes this
        # False again - this assertion is what pins the fix, not merely
        # that is_unknown itself returns True.

    def test_a_master_with_a_corrupt_state_file_is_also_unknown(self, tmp_path):
        directory = _clip_dir(tmp_path, short_bytes=b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"MASTER")
        clipstore.short_trim_state_path(directory).write_text("not json")
        assert trim.is_unknown(directory) is True
        assert trim.is_pending(directory, _edit()) is True

    def test_is_unknown_is_false_with_no_short_at_all(self, tmp_path):
        # A directory with neither short.mp4 nor a master: nothing to
        # repair, nothing to trust or distrust.
        directory = tmp_path / "clip"
        directory.mkdir()
        assert trim.is_unknown(directory) is False

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
        # `-ss`-before-`-i` combined with `-c:a copy` is exactly the
        # combination that can drop the audio stream entirely if the copy
        # cannot start cleanly at the seek point - and a still frame or a
        # geometry assertion would never notice a silent short. Assert the
        # audio stream is actually still there, not just that the command
        # exited 0.
        audio = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(clipstore.short_path(directory))],
            capture_output=True, text=True, check=True).stdout.strip()
        assert audio == "audio", audio


class StubChild:
    """A child process that writes its output file and then hangs until it is
    signalled - the shape a real ffmpeg takes partway through an encode.

    It has to WRITE before hanging: a stub that never produced a scratch file
    would make "the scratch file is gone afterwards" vacuously true, which is
    the same trap FailAfterWritingRunner above exists to avoid.
    """

    def __init__(self, on_start=None):
        self.terminated = 0
        self.killed = 0
        self.returncode = None
        if on_start is not None:
            on_start()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        # Mirrors real subprocess.Popen.__exit__, which waits for the child
        # on the way out - so a mutation reverting the fix to a literal
        # subprocess.run(...) (always driven as a context manager) exercises
        # this stub's own TimeoutExpired/kill/wait path instead of just
        # failing the `with` statement before the mutation is even reached.
        self.wait()
        return False

    def communicate(self, input=None, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return ("", "")

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("stub", timeout or 0)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def poll(self):
        return self.returncode


class KilledCutRunner:
    """ffprobe answers normally; the ffmpeg CUT goes through the real
    cancellable subprocess boundary with a stubbed child, so the termination
    can be OBSERVED rather than inferred from how fast the call returned."""

    def __init__(self, token, duration=84.0):
        self.token = token
        self.duration = duration
        self.commands = []
        self.children = []

    def __call__(self, command, *, cancel=None):
        self.commands.append(list(command))
        if command[0].endswith("ffprobe"):
            return f"{self.duration}\n"

        def popen(cmd, **kwargs):
            def on_start():
                # ffmpeg has begun writing its output - and THIS is when the
                # operator clicks "cancel now". A kill requested BEFORE the
                # call would never reach a subprocess at all: ensure_applied's
                # own entry check refuses before promoting anything.
                Path(cmd[-1]).write_bytes(b"HALF")
                self.token.request_kill()

            child = StubChild(on_start=on_start)
            self.children.append(child)
            return child

        return run_cancellable(command, timeout=900, cancel=cancel,
                               popen=popen).stdout


class TestStoppingATrim:
    def test_a_kill_terminates_the_subprocess_but_the_finally_still_runs(self, tmp_path):
        # The design's central safety property: a hard stop never kills the
        # Python thread, only the child it is waiting on - so the cleanup that
        # keeps a half-written artifact from surviving still runs.
        directory = _clip_dir(tmp_path)
        token = CancelToken()
        runner = KilledCutRunner(token)

        with pytest.raises(Stopped):
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                runner=runner, cancel=token)

        assert runner.children and runner.children[0].terminated == 1
        scratch = directory / "short.trim-part.mp4"
        assert not scratch.exists(), "the half-written cut survived the kill"
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()

    def test_a_stop_requested_before_the_cut_touches_nothing(self, tmp_path):
        directory = _clip_dir(tmp_path)
        token = CancelToken()
        token.request_stop()
        runner = FakeRunner()
        with pytest.raises(Stopped):
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                runner=runner, cancel=token)
        assert runner.commands == []
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"

    def test_a_stop_is_not_reported_as_a_trim_failure(self, tmp_path):
        # Stopped must reach the job runner intact: wrapped in TrimError it
        # would mark a job the operator stopped as `failed`.
        directory = _clip_dir(tmp_path)
        token = CancelToken()

        def stopping_runner(command, *, cancel=None):
            # A kill that lands while the probe is running: run_cancellable
            # terminates the child and raises Stopped from inside the runner.
            if command[0].endswith("ffprobe"):
                raise Stopped("the subprocess was terminated on a hard stop")
            raise AssertionError("the cut should never have been reached")

        with pytest.raises(Stopped):
            trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                runner=stopping_runner, cancel=token)
        # ... and the deliverable is put back, exactly as for a TrimError.
        assert clipstore.short_path(directory).read_bytes() == b"SHORT"
        assert not clipstore.short_master_path(directory).exists()

    def test_without_a_token_the_runner_is_called_exactly_as_before(self, tmp_path):
        # Every injected runner written before this parameter existed keeps
        # working: the kwarg is only sent when a caller asked for cancellation.
        # FakeRunner.__call__ takes a single positional `command` and no
        # `cancel` keyword at all - if ensure_applied ever wrapped it in
        # functools.partial(runner, cancel=...) unconditionally (even with
        # cancel=None), THAT alone would already TypeError, which is all the
        # old `is True` assertion actually proved. This additionally pins
        # the unchanged SHAPE of what gets called - a probe then a cut, in
        # that order, with nothing extra - the same pair
        # test_ss_and_to_both_precede_the_input inspects for the cancel-aware
        # path, so the two stay comparable.
        directory = _clip_dir(tmp_path)
        runner = FakeRunner()
        assert trim.ensure_applied(directory, _edit(trim=(3.0, 2.0)),
                                   runner=runner) is True
        assert len(runner.commands) == 2, runner.commands
        probe, cut = runner.commands
        assert probe[0].endswith("ffprobe")
        assert cut[0].endswith("ffmpeg")
        assert cut.index("-ss") < cut.index("-i")
        assert cut.index("-to") < cut.index("-i")


class TestDefaultRunnerForwardsCancel:
    """trim._default_runner is the ONE runner production actually reaches -
    every test above (KilledCutRunner, FakeRunner, stopping_runner) injects
    its own `runner=` and never touches this function at all. A code review
    found _default_runner's own `run_cancellable(command, timeout=900,
    cancel=cancel)` call could be mutated to `cancel=None` and the whole
    suite stayed green, because nothing exercises _default_runner directly.
    Monkeypatches run_cancellable itself - the same technique
    tests/test_render.py's TestBuildShortForwardsCancelToBothBoundaries uses
    one layer up - and drives _default_runner directly rather than through
    ensure_applied, so it stays fast with no real subprocess."""

    def test_the_token_reaches_run_cancellable(self, monkeypatch):
        token = CancelToken()
        seen: list = []

        def fake_run_cancellable(command, *, timeout, cancel=None):
            seen.append(cancel)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        monkeypatch.setattr(trim, "run_cancellable", fake_run_cancellable)

        trim._default_runner(["ffmpeg", "-i", "in.mp4"], cancel=token)

        assert seen == [token], (
            "_default_runner did not forward its cancel token into run_cancellable"
        )


class TestTheTrimStateIsWrittenAtomically:
    """`short.trim.json` says what the file on disk has ALREADY had cut from
    it. An unreadable one reads as "nothing cut yet", about a short that HAS
    been cut - so a reader landing inside a truncating write can cut an
    already-trimmed short a second time. The state is replaced whole now: it
    is either the previous cut or the new one.
    """

    def test_a_failed_state_write_leaves_the_previous_state_intact(
            self, tmp_path, monkeypatch):
        directory = _clip_dir(tmp_path)
        assert trim.ensure_applied(directory, _edit(trim=(2.0, 1.0)),
                                   runner=FakeRunner()) is True
        state_path = clipstore.short_trim_state_path(directory)
        before = state_path.read_bytes()

        # Only the STATE write fails. A blanket os.replace patch would take
        # the cut down with it - trim promotes its own scratch through
        # os.replace too - and the test would pass on the wrong exception.
        real_replace = os.replace

        def _boom_on_the_state(src, dst):
            if str(dst).endswith(clipstore.short_trim_state_path(directory).name):
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _boom_on_the_state)
        with pytest.raises(OSError):
            trim.ensure_applied(directory, _edit(trim=(5.0, 4.0)),
                                runner=FakeRunner())

        # The cut itself already happened - that is trim's own design (the
        # state is written AFTER the replace, see ensure_applied), not
        # something this change alters. What must hold is that the state file
        # is a WHOLE state and not an empty file: a reader gets the previous
        # cut, never "nothing cut yet".
        assert state_path.read_bytes() == before
        assert json.loads(state_path.read_text(encoding="utf-8")) == {
            "head": 2.0, "tail": 1.0}
