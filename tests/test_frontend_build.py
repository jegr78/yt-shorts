"""Unit checks for the two places that build the studio frontend: the hatchling
hook in hatch_build.py (wheel and sdist) and ensure_frontend() in
tools/build-binary.py (the release binary).

Both do the same job and neither could share code with the other: the sdist
carries hatch_build.py but not tools/, so a wheel built FROM that sdist cannot
import a helper living there. Duplication that cannot be removed has to be
pinned instead, which is what this file is for - every test below is written
against BOTH implementations, so the day one grows a guard the other lacks,
the pair stops agreeing here rather than at a v* tag.

Neither module is on the package path, so both are loaded by path, the way
tests/test_lint.py loads tools/lint.py.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class _App:
    """Stands in for hatchling's build app - the hook only ever talks to it."""

    def __init__(self):
        self.info, self.warnings = [], []

    def display_info(self, message):
        self.info.append(message)

    def display_warning(self, message):
        self.warnings.append(message)


class _StubHookInterface:
    def __init__(self):
        self.app = _App()


def _load_hatch_build():
    """hatchling is a BUILD dependency, absent from the test venv - and even
    where it is present its real BuildHookInterface takes a constructor full of
    builder state this hook never touches. Both point the same way: stub the one
    base class, load the module by path, then take the stub back out of
    sys.modules so nothing else in the suite ever sees a fake hatchling."""
    names = [
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    ]
    saved = {name: sys.modules.get(name) for name in names}
    for name in names:
        sys.modules[name] = types.ModuleType(name)
    sys.modules[names[-1]].BuildHookInterface = _StubHookInterface
    try:
        spec = importlib.util.spec_from_file_location(
            "hatchbuildmod", ROOT / "hatch_build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                del sys.modules[name]
            else:
                sys.modules[name] = previous


def _load_build_binary():
    spec = importlib.util.spec_from_file_location(
        "binarybuildmod", ROOT / "tools" / "build-binary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hatch_build = _load_hatch_build()
build_binary = _load_build_binary()


def _tree(tmp_path, *, built):
    """A miniature checkout: web/src/ always, static/index.html only if `built`."""
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "src" / "App.tsx").write_text("export default null\n", encoding="utf-8")
    static = tmp_path / "static"
    static.mkdir()
    if built:
        (static / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    return web, static


@pytest.fixture
def hatch(tmp_path, monkeypatch):
    """The hook, pointed at a throwaway tree instead of this repository."""
    def _make(*, built):
        web, static = _tree(tmp_path, built=built)
        monkeypatch.setattr(hatch_build, "WEB", web)
        monkeypatch.setattr(hatch_build, "STATIC", static)
        return hatch_build.FrontendBuildHook(), web, static
    return _make


@pytest.fixture
def binary(tmp_path, monkeypatch):
    """ensure_frontend()'s module, pointed at a throwaway tree."""
    def _make(*, built):
        web, static = _tree(tmp_path, built=built)
        monkeypatch.setattr(build_binary, "WEB_SRC", str(web))
        monkeypatch.setattr(build_binary, "STATIC_SRC", str(static))
        return web, static
    return _make


def _npm(monkeypatch, module, *, found=True, writes_index=True, static=None):
    """Stub npm. Records every call so a test can assert what was run - and
    whether it was run at all, which is the point of the reuse tests."""
    calls = []
    monkeypatch.setattr(module.shutil, "which",
                        lambda name: "/usr/bin/npm" if found else None)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if writes_index and cmd[1:] == ["run", "build"]:
            (static / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return calls


class TestAnExistingBuildIsReused:
    """The load-bearing half: CI builds the SPA once and hands it to the other
    jobs, so a wheel built there must not spend a minute rebuilding its gift."""

    def test_the_hook_does_not_run_npm(self, hatch, monkeypatch):
        hook, _, static = hatch(built=True)
        calls = _npm(monkeypatch, hatch_build, static=static)
        hook.initialize("standard", {})
        assert calls == [], "the hook rebuilt a frontend it was handed"

    def test_ensure_frontend_does_not_run_npm(self, binary, monkeypatch):
        _, static = binary(built=True)
        calls = _npm(monkeypatch, build_binary, static=static)
        build_binary.ensure_frontend()
        assert calls == [], "the binary build rebuilt a frontend it was handed"


class TestAMissingNpmFailsLoudly:
    """A wheel without the studio installs cleanly and then 404s every page, so
    the failure has to happen at build time and has to say what to do."""

    def test_the_hook_raises_with_the_rebuild_command(self, hatch, monkeypatch):
        hook, web, static = hatch(built=False)
        _npm(monkeypatch, hatch_build, found=False, static=static)
        with pytest.raises(RuntimeError) as excinfo:
            hook.initialize("standard", {})
        assert "npm ci && npm run build" in str(excinfo.value)
        assert str(web) in str(excinfo.value)

    def test_ensure_frontend_exits_with_the_rebuild_command(self, binary, monkeypatch):
        web, static = binary(built=False)
        _npm(monkeypatch, build_binary, found=False, static=static)
        with pytest.raises(SystemExit) as excinfo:
            build_binary.ensure_frontend()
        assert "npm ci && npm run build" in str(excinfo.value)
        assert str(web) in str(excinfo.value)


class TestABuildThatProducesNothingIsAFailure:
    """npm exiting 0 without writing index.html is the quiet version of the same
    disaster: check=True is satisfied and the package ships empty."""

    def test_the_hook_raises(self, hatch, monkeypatch):
        hook, _, static = hatch(built=False)
        _npm(monkeypatch, hatch_build, writes_index=False, static=static)
        with pytest.raises(RuntimeError, match="index.html"):
            hook.initialize("standard", {})

    def test_ensure_frontend_exits(self, binary, monkeypatch):
        _, static = binary(built=False)
        _npm(monkeypatch, build_binary, writes_index=False, static=static)
        with pytest.raises(SystemExit, match="index.html"):
            build_binary.ensure_frontend()


class TestNpmIsBounded:
    """A hung npm must not hold a release job until the job timeout kills it."""

    def test_the_hook_passes_a_timeout(self, hatch, monkeypatch):
        hook, _, static = hatch(built=False)
        calls = _npm(monkeypatch, hatch_build, static=static)
        hook.initialize("standard", {})
        assert [cmd[1:] for cmd, _ in calls] == [["ci"], ["run", "build"]]
        assert all(kwargs.get("timeout") == hatch_build.TIMEOUT_SECONDS
                   for _, kwargs in calls)

    def test_ensure_frontend_passes_a_timeout(self, binary, monkeypatch):
        _, static = binary(built=False)
        calls = _npm(monkeypatch, build_binary, static=static)
        build_binary.ensure_frontend()
        assert [cmd[1:] for cmd, _ in calls] == [["ci"], ["run", "build"]]
        assert all(kwargs.get("timeout") == build_binary.FRONTEND_TIMEOUT_SECONDS
                   for _, kwargs in calls)


class TestAStaleBuildIsCalledOut:
    """What used to police this was a CI step that rebuilt the frontend and
    diffed it against the committed copy. Nothing does that now, so the reuse
    above would silently package the previous bundle. An mtime comparison is
    not that gate - it only makes the reuse audible."""

    @staticmethod
    def _age(static, web, *, static_newer):
        source, built = web / "src" / "App.tsx", static / "index.html"
        source.touch()
        built.touch()
        older, newer = (source, built) if static_newer else (built, source)
        import os
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

    def test_the_hook_warns_when_the_sources_are_newer(self, hatch, monkeypatch):
        hook, web, static = hatch(built=True)
        self._age(static, web, static_newer=False)
        _npm(monkeypatch, hatch_build, static=static)
        hook.initialize("standard", {})
        assert any("older than" in w for w in hook.app.warnings)

    def test_the_hook_stays_quiet_when_the_build_is_newer(self, hatch, monkeypatch):
        hook, web, static = hatch(built=True)
        self._age(static, web, static_newer=True)
        _npm(monkeypatch, hatch_build, static=static)
        hook.initialize("standard", {})
        assert hook.app.warnings == []

    def test_ensure_frontend_warns_when_the_sources_are_newer(
            self, binary, monkeypatch, capsys):
        web, static = binary(built=True)
        self._age(static, web, static_newer=False)
        _npm(monkeypatch, build_binary, static=static)
        build_binary.ensure_frontend()
        assert "older than" in capsys.readouterr().out

    def test_ensure_frontend_stays_quiet_when_the_build_is_newer(
            self, binary, monkeypatch, capsys):
        web, static = binary(built=True)
        self._age(static, web, static_newer=True)
        _npm(monkeypatch, build_binary, static=static)
        build_binary.ensure_frontend()
        assert "older than" not in capsys.readouterr().out

    def test_an_sdist_without_web_sources_is_not_stale(self, tmp_path, monkeypatch):
        # The normal case for the wheel built FROM an sdist: static/ arrived in
        # the archive, web/ was excluded from it. Nothing to compare, no warning.
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        monkeypatch.setattr(hatch_build, "WEB", tmp_path / "web")
        monkeypatch.setattr(hatch_build, "STATIC", static)
        monkeypatch.setattr(build_binary, "WEB_SRC", str(tmp_path / "web"))
        monkeypatch.setattr(build_binary, "STATIC_SRC", str(static))
        assert hatch_build.built_before_its_sources() is False
        assert build_binary._built_before_its_sources() is False
