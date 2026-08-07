"""Session-wide test isolation: never resolve through the operator's workspace.

yt_shorts.profile.CHANNELS_DIR is resolved once at import from
workspace.resolve() - YT_SHORTS_DATA, then ~/YT-Shorts-Data, then the
repository's own channels/ (which does not exist in this repository; the
data lives in a workspace instead - see the wiki's "Where the data lives").
Left alone, tests that load a channel by name (e.g.
profile.load("erf/community-clips-back-catalogue")) would silently depend
on whichever of those the machine running the suite happens to have -
different results (or failures) on a machine without that workspace, or
once it is deleted.

tests/fixtures/channels/ is the suite's own copy of the ERF channel, owned
by the repository: channel.json, brand.json, glossary.json, layout.py,
fonts/, and the source list for community-clips-back-catalogue - nothing
derived (no rendered output, transcripts, clips.json or index.html). This
fixture points every test at that copy instead, autouse, so no test can
reach the machine's real workspace by omission.

A second, independent leak exists below it: ``studio.api.create_app()``
now calls ``workspace.resolve()`` directly (it opens the studio's central
log before the first request - see api.py's own comment there), and
``resolve()`` reads ``os.environ`` fresh on every call rather than caching
at import like ``profile.CHANNELS_DIR`` does. Patching CHANNELS_DIR above
does nothing for that call, and most studio tests build
``TestClient(create_app())`` with no per-test override. Left alone, that
resolves to the SAME places CHANNELS_DIR would have: ``~/YT-Shorts-Data``
if the operator has one (writing real log records into it), or - on a
fresh checkout with neither ``~/YT-Shorts-Data`` nor ``YT_SHORTS_DATA``
set - the repository root itself, creating and writing ``<repo>/logs/``.

An earlier attempt at this fixture set ``YT_SHORTS_DATA`` for the whole
session instead of what follows below. That does stop the real-directory
write, but it changes what the resolved workspace's ``origin`` IS, not
just where it points: ``studio/api.py``'s workspace-switch/create/copy
routes deliberately refuse (409) while
``workspace.resolve().origin == "YT_SHORTS_DATA"`` (switching is
meaningless while an env var pins the root - see ``_guard_reroot`` and
``copy_workspace_route``), so pinning the env var session-wide trips a
guard several tests exist specifically to exercise, turning their expected
400/200 into an unrelated 409. The fixture below patches the RESOLVER
instead of the environment, returning a Workspace whose ``origin`` is the
literal ``"test"`` - a value ``resolve()`` itself never produces (its own
origins are ``"YT_SHORTS_DATA"``, ``"config"``, ``"default"`` and
``"repository"``), so it trips none of those guards while still being
obviously a fixture value if it ever surfaces in output.

Patching ``workspace.resolve`` alone is not enough: ``studio/api.py`` does
``from ..workspace import resolve as _resolve_workspace``, a from-import
that copies the function object into its own namespace at import time, so
reassigning the attribute on the ``workspace`` module afterwards does not
touch the name already bound inside ``api``. Both names have to be patched.
``tests/conftest.py`` itself must not import ``yt_shorts.studio.api`` to do
that - importing it at module scope would pull FastAPI into every test
run, including a venv that never installed it (see api.py's own docstring
on why FastAPI stays confined to that package). Instead this reaches it via
``sys.modules.get(...)``, which finds it only once some studio test module
has already imported it at collection time, and is a no-op otherwise - the
same reasoning ``pytest.ini`` and this project apply everywhere else: no
test file may be the thing that first drags in an optional dependency.

``yt_shorts/studio/jobs.py`` (background render/detect/upload/connect/copy
jobs) does the SAME from-import, for the SAME reason (it needs the
workspace root to place each job's own log file under
``<root>/logs/jobs/`` - see jobs.py's ``_open_job_log``), and is patched the
same way below, via ``sys.modules.get("yt_shorts.studio.jobs")``. Left
unpatched, every existing studio test that starts a job would create real
job-log files under the operator's actual workspace the moment a job is
created - the exact regression this fixture exists to prevent, just one
call deeper than ``api.py``'s own central-log setup.

One more wrinkle: a handful of tests (tests/test_studio_api.py's
``TestWorkspaces._use_tmp_home`` and tests/test_studio_e2e.py's workspace-
manager E2E test) exercise the actual switch/create machinery end to end -
they monkeypatch ``Path.home`` to their own ``tmp_path`` and
``api._config_home`` to match, then expect a *later* call to
``_resolve_workspace()`` (after ``POST /api/workspaces/create`` writes the
new "current" into that tmp config) to reflect the switch. A flat, always-
the-same-value stub breaks exactly that: ``GET /api/workspaces`` would keep
reporting the fixture's own root forever, no matter what the test just
switched to. So the patched resolver below is not a pure constant - it
calls through to the REAL ``resolve()`` whenever ``Path.home()`` no longer
matches the machine's actual home (i.e. some test has repointed it, taking
responsibility for its own isolation), and only substitutes the fixed
workspace when ``Path.home()`` is untouched, which is the common case
and the one that must never reach the real ``~/YT-Shorts-Data`` or the
repository fallback by omission.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from yt_shorts import profile, workspace

FIXTURE_CHANNELS_DIR = Path(__file__).parent / "fixtures" / "channels"

# Captured before anything below ever patches workspace.resolve, so the
# fixture can still call through to the real resolution logic for a test
# that has deliberately repointed Path.home() (see _isolated_resolved_workspace).
_REAL_RESOLVE = workspace.resolve
_REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _isolated_channels_dir(monkeypatch):
    monkeypatch.setattr(profile, "CHANNELS_DIR", FIXTURE_CHANNELS_DIR)


@pytest.fixture(scope="session")
def _fixed_workspace_root(tmp_path_factory):
    """One workspace root (with its ``channels/`` subdirectory already
    present), fixed for the whole session.

    Session-scoped, not the usual per-test ``tmp_path``, for the same
    reason the previous attempt at this fixture was: ``logsetup.
    configure_logging`` is idempotent by LOGGER NAME only, not by path (see
    its own docstring) - the FIRST ``create_app()`` call in this process
    fixes the ``"ytshorts"`` logger's destination for the rest of the
    session, no matter which test triggers it. A per-test root would mean
    every ``create_app()`` after the first silently no-ops against a
    directory a prior test's teardown may already have removed. Keeping the
    root constant for the whole session makes that one-time idempotence
    harmless: every call, first test to last, resolves to the same
    still-live directory.
    """
    root = tmp_path_factory.mktemp("yt_shorts_workspace")
    (root / "channels").mkdir()
    return root


@pytest.fixture(autouse=True)
def _isolated_resolved_workspace(monkeypatch, _fixed_workspace_root):
    """Make every workspace resolution - ``workspace.resolve()``, and
    ``studio.api``'s and ``studio.jobs``'s already-bound
    ``_resolve_workspace`` - return the same fixture-owned root, instead of
    walking the real ``YT_SHORTS_DATA``/``~/YT-Shorts-Data``/repository
    chain. See the module docstring above for why this patches the
    resolver rather than the environment, and why ``origin="test"`` in
    particular.

    A test that needs different resolver behaviour (e.g.
    tests/test_workspace.py, which imports ``resolve`` by name and calls it
    directly, or tests/test_studio_api.py/test_studio_e2e.py cases that
    monkeypatch ``api._resolve_workspace`` themselves to exercise the
    workspace-switch machinery, or tests/test_job_logging.py, which
    monkeypatches ``jobs._resolve_workspace`` itself to point job logs at
    its own ``tmp_path``) is unaffected or wins outright: a direct
    ``from ... import resolve`` binds its own reference before this fixture
    ever runs, and a per-test ``monkeypatch.setattr`` call happens inside
    the test body, after this autouse fixture has already run, and reuses
    the SAME ``monkeypatch`` instance - so it simply overwrites this
    fixture's patch for that one test and both are undone together at
    teardown.

    The remaining case - a test that repoints ``Path.home`` but leaves
    ``_resolve_workspace`` alone (``_use_tmp_home`` and the workspace-manager
    E2E test) - is handled by ``_resolve`` below falling through to the REAL
    resolver, scoped to that repointed home, rather than by this fixture
    special-casing those tests by name. When falling through to the real
    resolver, ``repo_channels`` is pinned to the fixture's own tmp tree so
    that tier-4 repository-fallback resolution cannot escape the fixture's
    isolation and create directories like ``<repo>/logs/`` in the real
    repository.
    """
    fixed = workspace.Workspace(
        root=_fixed_workspace_root,
        channels_dir=_fixed_workspace_root / "channels",
        origin="test",
    )

    def _resolve(*_args, **_kwargs):
        if Path.home() != _REAL_HOME:
            # Some test has repointed Path.home() (and, in practice,
            # api._config_home() to match) - trust it and use the real
            # resolve() logic, ignoring the process's real environment
            # (env={}) so a YT_SHORTS_DATA the operator happens to have
            # exported can never change the result. This is what lets
            # those tests observe a workspace switch's effect on a later
            # resolve() call, the one thing a flat stub cannot do.
            return _REAL_RESOLVE(
                env={}, repo_channels=_fixed_workspace_root / "unused" / "channels"
            )
        return fixed

    monkeypatch.setattr(workspace, "resolve", _resolve)
    studio_api = sys.modules.get("yt_shorts.studio.api")
    if studio_api is not None:
        monkeypatch.setattr(studio_api, "_resolve_workspace", _resolve)
    studio_jobs = sys.modules.get("yt_shorts.studio.jobs")
    if studio_jobs is not None:
        monkeypatch.setattr(studio_jobs, "_resolve_workspace", _resolve)


# What `TestClient` is pointed at unless a test says otherwise. Starlette's
# own default is `http://testserver`, a host the studio does not serve and
# now refuses (see studio/api.py's `_local_origin_guard`) - so without the
# fixture below every request in this suite would answer 403.
LOOPBACK_BASE_URL = "http://127.0.0.1"


@pytest.fixture(autouse=True)
def _test_client_speaks_from_loopback(monkeypatch):
    """Make every `TestClient` in this suite address the studio the way a real
    browser does: at 127.0.0.1.

    `studio/api.py`'s host guard refuses any request whose `Host` header names
    something other than a loopback address, because that is what a
    DNS-rebound page's Host looks like and it is the only header such a page
    cannot change. Starlette's `TestClient` defaults to `http://testserver`,
    which is precisely such a host.

    The alternative - teaching the guard to accept `testserver` - was rejected:
    it would put a test artifact in the production allowlist AND leave the
    guard unexercised by every one of the ~2300 tests that go through
    `TestClient`. Pointing the client at loopback instead means the guard runs,
    and passes, on every single one of them, which is the stronger check.

    This patches the CLASS rather than each of the ~20 construction sites so a
    test written next cannot forget - the same reasoning `_no_real_job_starter`
    below states for its own blanket patch. A test that deliberately wants a
    different host still wins: an explicit `base_url=` (or a per-request
    `headers={"host": ...}`, which is how the guard's own tests drive it)
    overrides this default rather than fighting it.

    `tests/conftest.py` must not be the thing that first drags starlette into a
    run that never INSTALLED it - but "not yet imported" is not "not installed",
    and reading `sys.modules` alone conflated the two. A test that imports
    `TestClient` inside its own body imports it AFTER this fixture, so the
    fixture found nothing, patched nothing and returned silently, and the test
    got the unpatched class and a 403. Measured: `pytest
    tests/test_studio_worker.py` alone failed on exactly that, while the full
    suite passed because `tests/test_studio_api.py` imports at module level and
    sorts first. `find_spec` answers the question actually meant, and imports
    nothing when the answer is no.
    """
    testclient = sys.modules.get("starlette.testclient")
    if testclient is None:
        if importlib.util.find_spec("starlette") is None:
            return
        import starlette.testclient as testclient
    original = testclient.TestClient.__init__

    def __init__(self, app, *args, **kwargs):
        # Only when the caller passed no base_url of its own - positionally
        # (args[0] is TestClient's own second parameter) or by keyword.
        if not args and "base_url" not in kwargs:
            kwargs["base_url"] = LOOPBACK_BASE_URL
        original(self, app, *args, **kwargs)

    monkeypatch.setattr(testclient.TestClient, "__init__", __init__)


# The five kinds `studio/worker.py`'s STARTERS can start off a queue entry.
# `connect` and `copy` are deliberately NOT here: neither is queueable, so
# no `drain_once()` can reach them by accident, and every test that drives
# one already stubs it by name.
_JOB_STARTERS = (
    "start_transcribe_job",
    "start_detect_job",
    "start_render_job",
    "start_trim_job",
    "start_upload_job",
)


@pytest.fixture
def real_job_starters():
    """Opt out of `_no_real_job_starter` below: this test drives a REAL
    `studio.jobs.start_*_job`, with the expensive thing INSIDE it stubbed.

    Requesting this fixture (directly, or via
    ``@pytest.mark.usefixtures("real_job_starters")`` on a class or module)
    is how a test says so out loud. It is not a way to run real work: the
    hard rule still holds - no test may reach the network, spend money, or
    run a real Whisper decode, render or ffmpeg encode - and a test that
    opts in still has to stub whatever inside the starter would do any of
    those (`render.build_short`, `trim.ensure_applied`, the upload service,
    `_STUDIO_DETECT_FN`, …). What it opts out of is only the blanket
    refusal, so the starter's own parameter translation and its real
    `EventLock` acquisition can be exercised.
    """
    return _JOB_STARTERS


@pytest.fixture(autouse=True)
def _no_real_job_starter(request, monkeypatch):
    """No test starts real background work unless it says it means to.

    `Worker.drain_once()` is called at around forty sites across four test
    files, and its whole job is to turn a queue entry into a
    `studio.jobs.start_*_job` call - a real `EventLock`, a real `Job`, a
    real thread, and for `transcribe`/`detect` a real yt-dlp download, a
    real Whisper decode or a real paid model call. A bare
    ``queue.enqueue("transcribe", …)`` followed by ``drain_once()`` reaches
    all of that, and until this fixture existed the only thing standing in
    the way was a comment and the habit of stubbing the right name. That is
    not a guard: it fails open, silently, for every test anyone writes next.

    So every starter is replaced, for every test, by one that FAILS the test
    loudly. A test that genuinely wants a real starter opts in by requesting
    the `real_job_starters` fixture above.

    **The patch has to reach the name the WORKER calls**, which is why it is
    set on the `yt_shorts.studio.jobs` MODULE and not on some imported copy:
    `worker._start_transcribe` and friends call `jobs.start_transcribe_job`
    through the module at call time, and so does `studio/api.py`. This
    repository has been bitten twice by the opposite - a `from x import y`
    binding that a later `setattr` on `x` never reached - and
    `_isolated_resolved_workspace` above patches three names for exactly
    that reason. `tests/test_studio_worker.py`'s
    `TestNoRealStarterRunsByAccident` proves this one lands.

    The refusal is `pytest.fail`, whose `Failed` is a **BaseException** -
    deliberately, because `Worker._start` catches `Exception` broadly and
    would otherwise turn the refusal into that entry's own failure reason
    and carry on, leaving a green test that had in fact called the starter.
    `ran` is the belt to that braces: anything that still manages to swallow
    the exception is caught at teardown instead - which is not a hypothetical
    case, it is how the E2E suite is covered at all, since there the starter
    runs on the live server's own thread where no raised exception can reach
    the test body.

    Like the workspace patches above, this reaches `studio.jobs` through
    `sys.modules` rather than importing it - `tests/conftest.py` must not be
    the thing that first drags FastAPI's neighbourhood into a run.

    It YIELDS the list of starter names that were refused, so the one test
    that drives this fixture on purpose
    (`tests/test_studio_worker.py`'s `TestNoRealStarterRunsByAccident`) can
    assert on it and then clear it - for that test a refusal is the expected
    outcome, not a leak the teardown below should also report.
    """
    studio_jobs = sys.modules.get("yt_shorts.studio.jobs")
    if studio_jobs is None or "real_job_starters" in request.fixturenames:
        yield []
        return

    ran: list[str] = []

    def refuse(name):
        def starter(*_args, **_kwargs):
            ran.append(name)
            pytest.fail(
                f"a real job starter ran - stub it: this test called "
                f"yt_shorts.studio.jobs.{name}, which starts real background "
                f"work (an EventLock, a thread, and possibly yt-dlp, a "
                f"Whisper decode, ffmpeg or a paid model call). Replace it "
                f"with monkeypatch.setattr(jobs, {name!r}, …), or request the "
                f"`real_job_starters` fixture if this test really does mean "
                f"to drive the starter itself.")
        return starter

    for name in _JOB_STARTERS:
        monkeypatch.setattr(studio_jobs, name, refuse(name))
    yield ran
    assert not ran, (
        f"a real job starter ran and something swallowed the refusal: "
        f"{', '.join(sorted(set(ran)))}")
