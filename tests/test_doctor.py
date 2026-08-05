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

    def test_a_lookalike_name_does_not_count_as_the_real_filter(self, tmp_path):
        """`scale` must match the FILTER named `scale`, not any text that
        merely contains those five letters. `scale2ref` and `scale_vt` are
        real, different ffmpeg filters, and `alphaextract`'s own description
        - shipped in essentially every ffmpeg build - contains the word
        "grayscale". A plain substring test against the whole `-filters`
        blob cannot tell any of these apart from `scale` itself, and so
        reports `scale` present on a build that never has it. This crafted
        output has neither `scale` nor `boxblur` nor `setsar` as a filter of
        their own, only look-alikes and mentions in prose."""
        def run(argv, **kwargs):
            if argv[1:] == ["-filters"]:
                return 0, (
                    " .. scale2ref        VV->VV     Scale the input video "
                    "size and/or convert the image format to the given "
                    "reference.\n"
                    " .. scale_vt          V->V       Scale Videotoolbox "
                    "frames\n"
                    " .. alphaextract      V->V       Extract an alpha "
                    "channel as a grayscale image component.\n"
                    " TS overlay           VV->V      Overlay a video "
                    "source on top of the input.\n"
                )
            return _run_ok(argv, **kwargs)

        check = next(c for c in doctor.checks(tmp_path, which=_found, run=run)
                     if c.name == "ffmpeg filters")

        assert not check.ok
        assert "scale" in check.detail
        assert "boxblur" in check.detail
        assert "setsar" in check.detail

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

    def test_a_namespaced_module_whose_parent_is_entirely_absent_is_reported_missing(
            self, monkeypatch):
        """`importlib.util.find_spec("google.genai")` raises
        ModuleNotFoundError, rather than returning None, when the PARENT
        package ("google") is not installed at all - the exact shape of a
        base install with none of the `[cloud]` extras. `_module_found` must
        swallow that and answer False, not let it propagate: every optional
        extra happens to be installed in THIS venv, so nothing else here
        would ever exercise that branch, and a future "simplification" back
        to a bare `find_spec(...) is not None` would pass the whole suite
        green and only crash on the one install where the check matters."""
        import importlib.util as importlib_util

        def raises(name):
            raise ModuleNotFoundError(f"No module named {name.split('.')[0]!r}")

        monkeypatch.setattr(importlib_util, "find_spec", raises)

        assert doctor._module_found("google.genai") is False


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
