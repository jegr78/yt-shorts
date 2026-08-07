"""End-to-end tests that drive the REAL built studio page in a real
Chromium browser (via Playwright), against a real HTTP server
(uvicorn + yt_shorts.studio.api.create_app) - not the FastAPI TestClient
the rest of the studio suite uses (test_studio_api.py, test_studio_jobs.py,
test_studio_static.py). Those never load a page or run any JavaScript, so a
wrong field name in the built bundle, a broken selector, or a request that
never fires could pass every one of them and still be broken for a real
operator. This file is what actually proves the built React/Vite/Mantine
page (src/yt_shorts/studio/web/, built into src/yt_shorts/studio/static/)
works against the real API.

The server, the seed data and the assertions are all Python: the event
seeded here is built with the same yt_shorts.clipstore/editorial helpers
tests/test_studio_api.py uses, against the ERF fixture channel
(tests/fixtures/channels/erf, pinned by tests/conftest.py so nothing here
ever touches ~/YT-Shorts-Data) - reusing @playwright/test's JS runner
would mean reimplementing that seeding in JavaScript, or shelling out to
Python helpers from it. pytest-playwright avoids that: one test command,
no duplicated data helpers.

Requires a Chromium browser installed for Playwright:
    .venv/bin/python -m playwright install chromium
The whole module SKIPS (not fails) at collection time with a clear reason
when that browser is not available - a fresh clone must not be blocked by
it (see CONTRIBUTING.md's own note on this, under "Running things").

Each assertion that a save "worked" reads the real edit.json on disk (or
goes through the plain HTTP API) rather than trusting only what the DOM
shows afterwards - a UI that shows "Saved" without having actually written
anything would still pass a DOM-only assertion.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import re
from playwright.sync_api import expect

from yt_shorts import clipstore, editorial, lexicon, tracks, workspace
from yt_shorts import glossary as glossary_module
from yt_shorts import profile as profile_module
from yt_shorts.job_queue import JobQueue
from yt_shorts.profile import load as profile_load
from yt_shorts.studio import jobs as studio_jobs
from yt_shorts.studio import worker as studio_worker
from yt_shorts.studio.api import create_app
from yt_shorts.studio import api as api_module
from yt_shorts.studio import api

CLIP_URL = "https://www.youtube.com/clip/UgkxSpeedy123"

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"

# The studio is workspace-level now (see api.py): every route is path-scoped
# and resolves its profile from CHANNELS_DIR, so - exactly like
# tests/test_studio_api.py - these tests operate on channel ``erf`` (the
# checked-in fixture) and event ``studio-test``, and reach the editor by
# deep-linking to /erf/studio-test rather than assuming it loads at /.
CHANNEL = "erf"
EVENT = "studio-test"


def editor_url(base_url: str) -> str:
    """The deep link straight into the editor screen for the seeded event -
    the seven-screen router (see web/src/Root.tsx) reads the path, and the
    SPA fallback (api.py) serves index.html for it, so a goto here lands in
    the editor exactly as the old ``goto(base_url)`` used to before the app
    became a router."""
    return f"{base_url}/{CHANNEL}/{EVENT}"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason=(
        "Chromium for Playwright is not installed - run "
        "`.venv/bin/python -m playwright install chromium` to enable the "
        "studio's end-to-end browser tests"
    ),
)


def clip_entry(url, hook, duration=6.0):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 10.0 + duration, "duration": duration,
            "error": None}


def _solid_video(path: Path, seconds: float = 2.0) -> None:
    """A tiny real video ffmpeg can extract a frame from - same technique
    tests/test_studio_api.py and tests/test_preview.py use, so preview.build
    has an actual raw.mp4 to draw on rather than a 409 (see below for a
    test that deliberately does NOT provide one)."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x336699:s=640x360:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    """Runs a real uvicorn server in a background thread - Playwright needs
    an actual HTTP server to navigate to, unlike fastapi.testclient.
    TestClient's in-process ASGI transport used elsewhere in this suite.

    One of these exists per MODULE now, not per test, and it serves the
    ``_AppSwitch`` below rather than any one test's app - see that class and
    ``studio_server`` for what that buys and what it does NOT share."""

    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        import uvicorn
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    def run(self) -> None:
        self._server.run()

    def stop(self) -> None:
        self._server.should_exit = True


class _AppSwitch:
    """The one ASGI application the module's uvicorn server ever sees: it
    forwards each request to whichever app the CURRENT test installed, and
    answers 503 when no test owns it.

    **Why this exists.** Every test in this file used to start and stop its
    own uvicorn server. Measured over the 116 tests here, that went from
    ~95s to ~66s - a difference explained almost entirely by per-test server
    overhead that was not real work: ~83ms per test waiting for a fresh
    server to answer its readiness probe, and ~150ms (over a second when the
    page had a poll in flight) per test inside ``Server.should_exit``'s
    graceful shutdown, whose main loop only notices the flag on a 0.1s tick.
    The tests' own browser work, roughly 56s, is untouched by this.

    **What is shared, exactly:** the listening socket, its port, the uvicorn
    server object, its event loop and the thread running it. Nothing else.

    **What is still per test:** the whole application. Every test gets a
    brand-new ``create_app()`` - its own ``JobStore``, its own ``JobQueue``
    and ``jobs.json``, its own ``Worker``, its own resolved workspace, its
    own ``tmp_path`` and its own ``profile.CHANNELS_DIR`` - installed here
    for the duration of that test and REMOVED at its teardown. A test
    therefore cannot reach another's jobs, clips or files through this
    switch: by the time the next app is installed, the previous one is
    unreachable and has been dropped.

    **A straggler cannot cross either.** Between two tests ``_app`` is
    ``None``, so a request from a page that has not finished dying gets a
    503 rather than the next test's app. It cannot arrive later than that:
    Playwright's ``context``/``page`` are function-scoped and closed before
    the next test's fixtures run, which is the same guarantee the old
    per-test server had by virtue of its socket going away.

    The lifespan protocol is answered HERE rather than forwarded. That is
    safe because ``create_app()`` registers no startup or shutdown handler
    of its own (it builds everything in the constructor), so there is
    nothing on a per-test app for a lifespan to run - ``_serving`` asserts
    exactly that on every install, because a real startup handler was proven
    (by execution) to silently never run through this switch otherwise.
    """

    def __init__(self) -> None:
        self._app = None

    def install(self, app) -> None:
        self._app = app

    def clear(self) -> None:
        self._app = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        app = self._app
        if app is None:
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body",
                        "body": b"no test owns this studio server"})
            return
        await app(scope, receive, send)


@pytest.fixture
def studio_profile(tmp_path, monkeypatch):
    """Copy the tiny checked-in ``erf`` fixture channel into a fresh tmp
    workspace, repoint ``profile.CHANNELS_DIR`` at that copy, and create an
    empty ``studio-test`` event under it - so ``create_app()`` (workspace
    level) resolves a real profile for /api/channels/erf/events/studio-test/…
    without ever touching the operator's real workspace. Same pattern as
    tests/test_studio_api.py's own ``studio_profile``."""
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    (channels / "erf" / "events" / EVENT).mkdir(parents=True)
    return profile_load(f"{CHANNEL}/{EVENT}")


@pytest.fixture
def event_dir(studio_profile):
    # The real event dir under the repointed CHANNELS_DIR; seed clips here,
    # and reach them via /erf/studio-test in the editor (or the scoped
    # /api/channels/erf/events/studio-test/... URLs).
    return studio_profile.event_dir


@pytest.fixture(scope="module")
def studio_server():
    """ONE real uvicorn server for this whole module, on a free localhost
    port, serving the ``_AppSwitch`` above.

    A free port rather than a fixed one, still: a fixed port would collide
    with a second run of this file, or with an operator's own ``bin/yt-shorts
    studio``. And it still waits until the server actually answers before
    handing the URL over - navigating before that would be a flaky test in
    disguise. The probe accepts the switch's own 503 as readiness, because
    an HTTP answer of any status is proof the socket is serving; only a
    connection error means "not up yet".

    Module-scoped, deliberately not session-scoped: nothing this file starts
    may outlive this file. The thread and its event loop are gone before the
    next test module is collected, which is what
    ``tests/test_studio_worker.py``'s own
    ``test_no_studio_e2e_server_thread_survives_into_this_module`` checks for
    directly (a stray ``_ServerThread`` or uvicorn-named thread still alive
    when that module starts) - not the same file's worker-thread checks,
    which filter by the worker's own thread name and would not notice a
    leaked uvicorn thread at all. A session-scoped server was proven, by
    execution, to slip past those worker-thread checks and let the full
    suite pass regardless.
    """
    switch = _AppSwitch()
    port = _free_port()
    thread = _ServerThread(switch, port)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + 10
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/channels", timeout=0.5)
            break
        except urllib.error.HTTPError as error:
            if error.code == 503:
                # A 503 from the switch with no app installed: the server
                # is up, which is all this probe is asking.
                break
            # Anything else (e.g. a 500) is not readiness - keep polling
            # rather than reading a broken switch as "started".
            last_error = error
            time.sleep(0.05)
        except Exception as error:  # noqa: BLE001 - just a readiness probe
            last_error = error
            time.sleep(0.05)
    else:
        raise RuntimeError(f"studio server did not start in time: {last_error}")

    try:
        yield SimpleNamespace(url=base_url, switch=switch)
    finally:
        thread.stop()
        thread.join(timeout=5)
        assert not thread.is_alive(), (
            "the module's uvicorn server thread outlived this file")


@contextlib.contextmanager
def _serving(app, server):
    """Hands `app` to the module's running server for the duration of one
    test and takes it away again afterwards, yielding the base URL.

    This used to start a uvicorn server per test; see ``_AppSwitch`` for the
    measurement that motivated the change and for exactly what is now shared
    (a socket, a loop and a thread) versus what is still built fresh for
    every test (the entire app, and everything hanging off it).

    Still a context manager taking the app, so a fixture that needs the APP
    OBJECT too (see ``live_queue_server``: the job queue is driven
    in-process, with no thread and no real work) does not carry a second
    copy of this.
    """
    # _AppSwitch answers the ASGI lifespan protocol ITSELF rather than
    # forwarding it to `app` (see its own docstring) - safe only because
    # `create_app()` registers no startup/shutdown handler of its own. A
    # real `@app.on_event("startup")` was proven (by execution) to run
    # under TestClient but silently NEVER run through this switch, with no
    # test in the repo noticing - so that assumption is checked here, on
    # every install, rather than left as a comment nothing enforces.
    assert not app.router.on_startup and not app.router.on_shutdown, (
        "this app has a startup/shutdown handler, but _AppSwitch answers "
        "the lifespan protocol itself and never forwards it - that handler "
        "would silently never run under this file's shared server")
    server.switch.install(app)
    try:
        yield server.url
    finally:
        server.switch.clear()


@pytest.fixture
def live_studio(studio_profile, tmp_path, studio_server):
    """The real studio app on a free localhost port, with its own plan and a
    RUNNING worker - the app object, its queue and its URL together.

    Two deliberate departures from ``create_app()``'s own defaults, and the
    second one is the one that needs justifying:

    - **its own ``jobs.json``.** ``create_app`` points the queue at the
      SESSION-scoped workspace root (tests/conftest.py's
      ``_isolated_resolved_workspace``), so an entry one test left behind
      would be claimed by whichever test collected next. Same reason
      ``live_queue_server`` below gives every test its own file.
    - **the worker is STARTED here.** ``create_app`` never starts it and must
      not - over two thousand tests construct an app - but the studio's Render,
      Detect and Trim buttons now ENQUEUE their work instead of starting it,
      so with no worker nothing in this file that clicks one would ever
      progress past "queued". Starting it in the fixture leaves that property
      exactly where it was (the app still starts nothing by itself; only
      ``bin/yt-shorts studio`` and this fixture do) and is safe for a
      structural reason rather than a habit: tests/conftest.py's autouse
      ``_no_real_job_starter`` replaces every ``studio.jobs.start_*_job`` with
      one that fails the test, so a click whose starter is not stubbed cannot
      quietly run a real render, a real Whisper decode or a paid model call -
      it fails loudly, at teardown, naming the starter.

    The alternative considered was driving ``drain_once()`` from each test
    after the click. It was rejected because the click is asynchronous in the
    BROWSER: the test would have to guess when the enqueue's POST had landed
    before draining, which is a sleep in disguise, and every one of these
    tests would have to carry it.
    """
    app = create_app()
    queue = JobQueue(tmp_path / "jobs.json", studio_jobs.KINDS,
                     dict(studio_worker.DEFAULT_LIMITS))
    app.state.job_queue = queue
    # A short interval: the worker's poll is the lag between "the operator
    # clicked" and "the entry starts", and every wait in this file pays it.
    worker = studio_worker.Worker(queue, app.state.job_store, interval=0.1)
    app.state.worker = worker
    worker.start()
    try:
        with _serving(app, studio_server) as base_url:
            yield SimpleNamespace(url=base_url, app=app, queue=queue, worker=worker)
    finally:
        # BOTH workers, and the second one is not defensive padding: a
        # workspace SWITCH rebuilds the queue and its worker
        # (api._build_queue_and_worker) and STARTS the new one when the old
        # was running, so after `TestWorkspaceManagementE2E` the live worker
        # is not the object this fixture started at all. Stopping only ours
        # leaked the replacement - measured, and it surfaced as
        # tests/test_studio_worker.py's `TestCreateAppWiring` failing
        # hundreds of tests later in another file.
        current = getattr(app.state, "worker", None)
        for candidate in (worker, current):
            if candidate is not None:
                candidate.stop()
        # The threads must really be gone, not merely asked to go. They are
        # DAEMON threads with a process-wide name, and `TestCreateAppWiring`
        # enumerates every thread in the process to prove `create_app` starts
        # none - so one leaked here fails a test whose own traceback names
        # nothing about this file. Failing the test that leaked it instead is
        # the whole point of asserting here.
        assert not worker.is_running() and not (
            current is not None and current.is_running()), (
            "a studio worker thread outlived its test - a pass is wedged "
            "inside a starter; stub whatever it is waiting on")


@pytest.fixture
def live_server(live_studio):
    """Just the base URL of ``live_studio``, for the tests that need nothing
    else. ``studio_profile`` has already repointed CHANNELS_DIR, so the
    workspace-level app lists and resolves against the tmp fixture copy."""
    return live_studio.url


# Strong references, deliberately: the check below compares app IDENTITY, and
# holding each app alive is what stops a freed one's address being reused by
# its successor and turning a real leak into a passing test.
_APPS_SEEN: list = []


class TestTheSharedServerSharesNothingButTheSocket:
    """The uvicorn server is now shared across this module (see
    ``_AppSwitch``); the application behind it is not. Every isolation claim
    in this file rests on that difference, so it is pinned here rather than
    left as a comment.

    Both of these would have been vacuously true when each test ran its own
    server, which is exactly why they are worth having now."""

    def test_each_test_is_handed_a_brand_new_app(self, live_studio):
        assert all(live_studio.app is not seen for seen in _APPS_SEEN), (
            "two tests were handed the SAME app object - a test could then "
            "see another's jobs, clips and job store through it")
        _APPS_SEEN.append(live_studio.app)

    def test_and_so_is_the_next_one(self, live_studio):
        assert all(live_studio.app is not seen for seen in _APPS_SEEN), (
            "two tests were handed the SAME app object - a test could then "
            "see another's jobs, clips and job store through it")
        _APPS_SEEN.append(live_studio.app)

    def test_between_two_tests_the_server_owns_no_app_at_all(self, studio_server):
        """A request arriving while no test owns the server is refused, not
        routed to whichever app happens to be installed next.

        This test asks for ``studio_server`` WITHOUT a ``live_studio``, which
        puts the switch in precisely the state it is in between two tests -
        the window a straggling request from a page that has not finished
        dying would have to arrive in.

        Deliberately self-contained: it installs an app and then clears it
        ITSELF rather than relying on this class's two earlier tests to have
        left one installed. Run only this test (``-k
        test_between_two_tests_the_server_owns_no_app_at_all``) and the old
        version still passed even with ``clear()`` neutered into a no-op -
        run alone, the switch's ``_app`` had never been set to begin with, so
        the 503 came from its untouched initial state, not from ``clear()``
        doing anything. Installing an app here means the assertion actually
        exercises ``clear()`` no matter which other tests ran."""
        app = create_app()
        studio_server.switch.install(app)
        studio_server.switch.clear()

        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{studio_server.url}/api/channels", timeout=5)
        assert caught.value.code == 503


class TestClipListAndSelection:
    def test_the_clip_list_loads_and_shows_each_clips_state(self, event_dir, live_server, page):
        untouched = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        kept_dir = clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456", "Barbie"))
        editorial.save(kept_dir, editorial.Edit(
            title="Jegr and the Barbie", status=editorial.KEPT, transcript=None))
        clipstore.short_path(kept_dir).write_bytes(b"fake mp4 bytes")

        page.goto(editor_url(live_server))

        speedy_row = page.get_by_role("button", name="Speedy!")
        barbie_row = page.get_by_role("button", name="Jegr and the Barbie")
        speedy_row.wait_for()
        barbie_row.wait_for()

        # The actual per-clip STATE, not just that a row with this name
        # exists - a list that showed every clip as "candidate" would still
        # pass a name-only assertion.
        assert "candidate" in speedy_row.inner_text().lower()
        assert "not rendered" in speedy_row.inner_text().lower()
        assert "kept" in barbie_row.inner_text().lower()
        assert "not rendered" not in barbie_row.inner_text().lower()
        assert untouched.name and kept_dir.name  # both clips were actually written

    def test_selecting_a_clip_shows_its_transcript_and_its_preview(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.1, "end": 0.5, "text": "very"},
                   {"start": 0.6, "end": 1.0, "text": "speedy"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))
        _solid_video(clipstore.raw_path(directory))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        # The transcript: both words actually present as editable text,
        # not just SOME table.
        page.get_by_role("textbox", name="Title").wait_for()
        assert page.get_by_role("cell", name="very").locator("input").input_value() == "very"
        assert page.get_by_role("cell", name="speedy").locator("input").input_value() == "speedy"

        # The preview: a real image loaded (not the "no preview" alert),
        # confirmed by actual pixel content, not just an <img> tag existing.
        assert page.get_by_role("alert").count() == 0
        preview_img = page.locator("img[alt^='Preview at']")
        preview_img.wait_for()
        natural_width = preview_img.evaluate("img => img.naturalWidth")
        assert natural_width > 0, "preview <img> has no actual decoded image data"


class TestPreviewRequestCount:
    """Regression test for a duplicate-request bug found in design review:
    selecting a clip fired the SAME GET /preview?at=0 request twice, back
    to back. The cause was two separate React commits for what is
    logically one selection - ClipEditor's reset-on-clip-change effect
    landed a moment after the commit that already delivered the new clip
    prop, and PreviewPane's fetch effect fired once for each commit (see
    ClipEditor.tsx and PreviewPane.tsx's own docstrings on the fix: state
    is now reset synchronously during render, and PreviewPane is keyed by
    clip name so a switch is a clean remount rather than a prop update
    racing a debounce timer).

    These tests COUNT preview requests rather than asserting one arrived -
    "one request arrived" was already true before the fix; a second,
    redundant one arriving right after it is exactly the bug."""

    def test_selecting_a_clip_fires_exactly_one_preview_request(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        _solid_video(clipstore.raw_path(directory))

        page.goto(editor_url(live_server))

        preview_requests: list[str] = []
        page.on(
            "request",
            lambda r: preview_requests.append(r.url) if "/preview" in r.url else None,
        )

        page.get_by_role("button", name="Speedy!").click()
        page.locator("img[alt^='Preview at']").wait_for()
        # The slider/word/title debounce windows are all <=300ms - give a
        # delayed, redundant second request time to arrive before counting.
        page.wait_for_timeout(500)

        assert len(preview_requests) == 1, preview_requests

    def test_switching_between_two_clips_fires_exactly_one_preview_request_each(
            self, event_dir, live_server, page):
        first = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        second = clipstore.write_clip(
            event_dir, clip_entry("https://www.youtube.com/clip/UgkxBarbie456", "Barbie"))
        _solid_video(clipstore.raw_path(first))
        _solid_video(clipstore.raw_path(second))

        page.goto(editor_url(live_server))

        preview_requests: list[str] = []
        page.on(
            "request",
            lambda r: preview_requests.append(r.url) if "/preview" in r.url else None,
        )

        page.get_by_role("button", name="Speedy!").click()
        page.locator("img[alt^='Preview at']").wait_for()
        page.wait_for_timeout(500)
        assert len(preview_requests) == 1, preview_requests

        preview_requests.clear()
        page.get_by_role("button", name="Barbie").click()
        page.locator("img[alt^='Preview at']").wait_for()
        page.wait_for_timeout(500)
        assert len(preview_requests) == 1, preview_requests


class TestSavingReachesEditJson:
    def test_editing_a_title_and_saving_reaches_edit_json(self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        assert not (directory / editorial.EDIT_FILENAME).exists()

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()
        title_box = page.get_by_role("textbox", name="Title")
        title_box.fill("Jegr Tunes")
        page.get_by_role("button", name="Save changes").click()

        # Wait for the save round-trip: the Save button disables again once
        # there is nothing left to save.
        page.get_by_role("button", name="Save changes").wait_for(state="visible")
        page.wait_for_function(
            "document.querySelector('button[disabled]') !== null "
            "|| Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

        saved = editorial.load(directory)
        assert saved.title == "Jegr Tunes"

    def test_editing_a_word_and_saving_records_based_on(self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.1, "end": 0.5, "text": "very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        word_box = page.get_by_role("cell", name="very").locator("input")
        word_box.fill("SUPER")
        page.get_by_role("button", name="Save changes").click()
        page.wait_for_timeout(50)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

        saved = editorial.load(directory)
        assert saved.transcript is not None
        # " SUPER", not "SUPER": the route restores the word boundary a text
        # field cannot express (editorial.normalise_word_boundaries), which is
        # what stops a hand-typed correction gluing itself to the word before
        # it. TestWordBoundariesThroughTheRoutes in tests/test_studio_api.py
        # owns that rule; this line records that the E2E save path goes
        # through it.
        assert saved.transcript["words"] == [{"start": 0.1, "end": 0.5, "text": " SUPER"}]
        # Anchored against what was ACTUALLY derived at save time, not some
        # placeholder - this is the mechanism a later conflict detection
        # relies on entirely.
        assert saved.transcript["based_on"] == editorial.checksum(derived)

    def test_a_hand_typed_word_keeps_its_boundary(self, event_dir, live_server, page):
        """The operator's actual bug, end to end: typing a correction lost the
        space and the caption rendered IT'SREIRACING. Covers all THREE halves
        of the fix in one pass - the field must DISPLAY the decoder's word
        without its leading space, loading it must not make the clip look
        edited, and the save must STORE the correction with the boundary.

        This is the ONLY automated guard on the first and second of those. A
        review proved it: reverting WordsEditor's display trim, and trimming
        into state instead (in its own onChange, or at the load site), each
        leaves tsc, oxlint and all 293 Vitest cases green - this project
        covers component behaviour by E2E, not by component tests.
        """
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.1, "end": 0.5, "text": " very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        word_box = page.get_by_role("cell", name="very").locator("input")
        # The display half: the decoder's own " very" is shown WITHOUT its
        # leading space, so no invisible character sits in front of the cursor.
        assert word_box.input_value() == "very"

        # The state half, and the reason the trim is display-ONLY: state keeps
        # the raw " very", so a clip nobody has touched is not dirty. Trimming
        # into state instead would make every word differ from its saved form
        # and every clip would open showing this badge.
        assert page.get_by_text("Unsaved changes").count() == 0

        word_box.fill("Rei")
        page.get_by_role("button", name="Save changes").click()
        page.wait_for_timeout(50)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

        saved = editorial.load(directory)
        assert saved.transcript is not None
        # The server half: stored WITH the boundary, which is what the renderer
        # burns in. A stored "Rei" here is the bug.
        assert saved.transcript["words"] == [{"start": 0.1, "end": 0.5, "text": " Rei"}]


class TestStatusAndConflict:
    def test_marking_a_clip_discarded_is_reflected_after_a_reload(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()
        # Mantine's SegmentedControl keeps the actual <input type=radio>
        # visually hidden and styles its <label> as the clickable segment,
        # so the radio itself is never "visible" to Playwright's actionability
        # check - click the label text instead, exactly as an operator would.
        page.get_by_role("radiogroup").get_by_text("Discarded", exact=True).click()

        # Confirm the write actually happened before reloading - a client
        # that reloaded server-authoritative state without ever writing
        # anything could still "look" reflected after reload by accident.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if editorial.load(directory).status == editorial.DISCARDED:
                break
            time.sleep(0.05)
        assert editorial.load(directory).status == editorial.DISCARDED

        page.goto(editor_url(live_server))
        # Discarded clips are hidden by default - the row must be gone...
        assert page.get_by_role("button", name="Speedy!").count() == 0
        # ...until the operator asks to see them, and then it must show the
        # status that survived the reload, read straight from disk.
        page.get_by_role("switch", name="Show discarded clips").click()
        row = page.get_by_role("button", name="Speedy!")
        row.wait_for()
        assert "discarded" in row.inner_text().lower()

    def test_the_conflict_banner_appears_and_names_what_happened(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        old_derived = [{"start": 0.0, "end": 0.5, "text": "very"}]
        old_correction = [{"start": 0.0, "end": 0.5, "text": "Speedy"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(old_derived), "words": old_correction}))
        changed = old_derived + [{"start": 0.5, "end": 1.0, "text": "overtakes"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": changed}))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        banner = page.get_by_role("alert").filter(has_text=re.compile("out of date", re.I))
        banner.wait_for()
        text = banner.inner_text().lower()
        # It must actually explain the mechanism, not just say "conflict":
        # what happened (transcript changed underneath it) and what the
        # tool did about it (kept the human's correction, not merged).
        assert "changed" in text
        assert "correction" in text
        assert "still being used" in text or "not" in text and "merged" in text

        # The human's correction still wins - it must be showing the
        # CORRECTED word, not the newly derived one.
        assert page.get_by_role("cell", name="Speedy").count() == 1


class TestLivePreviewOfUnsavedEdits:
    def test_editing_a_word_without_saving_updates_the_preview_before_save(
            self, event_dir, live_server, page):
        """The gap this whole feature closes: an operator fixes a
        misheard word and must see the preview change immediately, before
        clicking Save - not the other way around (edit, save blind, THEN
        find out if it looks right). Proven two ways: (1) the network
        traffic itself - editing without saving must fire a POST to the
        preview route (the live-preview path), not just repeat the GET
        that only ever reflects edit.json, and its response bytes must
        differ from the pre-edit GET; (2) the unsaved-changes badge must
        still be showing, since the preview updating must not be mistaken
        for a save happening. Also confirms the read did not leave a
        trace on disk - the studio's central invariant even when nothing
        is explicitly saved."""
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        # Covers t=0, the preview's default `at`, so the caption is
        # actually on screen for both the saved and the edited fetch.
        derived = [{"start": 0.0, "end": 3.0, "text": "very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))
        _solid_video(clipstore.raw_path(directory), seconds=4.0)
        edit_path = directory / editorial.EDIT_FILENAME
        assert not edit_path.exists()

        page.goto(editor_url(live_server))
        with page.expect_response(lambda r: "/preview" in r.url) as saved_info:
            page.get_by_role("button", name="Speedy!").click()
        saved_response = saved_info.value
        assert saved_response.request.method == "GET", (
            "selecting a clip with nothing edited must still use the GET "
            "preview route - the saved state"
        )
        saved_bytes = saved_response.body()

        word_box = page.get_by_role("cell", name="very").locator("input")
        with page.expect_response(lambda r: "/preview" in r.url) as edited_info:
            word_box.fill("SUPERDUPER")
        edited_response = edited_info.value
        assert edited_response.request.method == "POST", (
            "an unsaved transcript edit must hit the POST preview route "
            "carrying the client's own words, not the GET route that can "
            "only ever reflect edit.json"
        )
        assert edited_response.status == 200
        edited_bytes = edited_response.body()

        assert edited_bytes != saved_bytes, (
            "the preview did not change even though the caption text did "
            "- this is exactly the bug being fixed"
        )

        # The unsaved-changes indicator must still be honest: the preview
        # updating is not the same thing as a save happening.
        assert page.get_by_text("Unsaved changes").count() == 1

        # And the central invariant: previewing an unsaved edit is a
        # read. Nothing reached disk.
        assert not edit_path.exists()


def moment_entry(video_id, start, end, hook, source_title="ERF Race Part 1"):
    """A moment-shaped clip entry, path-encoded exactly the way
    yt_shorts.clip_from_moment.create_clip writes one (see CLAUDE.md's "A
    moment's clip identity is path-encoded" note) - built by hand here,
    like tests/test_studio_api.py's TestWindowEdit._moment, rather than
    imported, since detect_moments itself is what is being stubbed out in
    these tests (real detection is D2a/D2b's own suite's job, not this
    file's)."""
    return {"url": f"https://www.youtube.com/watch/{video_id}/{int(start)}-{int(end)}",
            "video_id": video_id, "hook": hook, "source_title": source_title,
            "start": start, "end": end, "duration": end - start, "error": None}


def _write_stream_transcript(root: Path, video_id: str, *, duration_seconds: float,
                              words: list[dict]) -> None:
    """Seed `streams/<video_id>/transcript.json` directly on disk - the same
    technique TestStreamScreenZoomWindow's own `_seed_transcript` uses, lifted
    to module level so the reachability/alerts/follow-a-pick tests below can
    share it without depending on that class."""
    directory = root / "streams" / video_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "transcript.json").write_text(json.dumps({
        "video_id": video_id, "duration_seconds": duration_seconds,
        "words": words, "missing_chunks": [],
    }), encoding="utf-8")


def _write_stream_analysis(root: Path, video_id: str, *, duration_seconds: float,
                            engine: str | None, moments: list[dict],
                            missing_windows: list[int] | None = None) -> None:
    """Seed `streams/<video_id>/moments.json` directly on disk, in the real
    shape `detect.detect_moments` writes (see that module's own docstring) -
    not through a detect job, since these tests are about the SCREEN reading
    an already-finished analysis, not about detection itself (that is
    TestStreamsAndDetection's own job, above)."""
    directory = root / "streams" / video_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "moments.json").write_text(json.dumps({
        "video_id": video_id, "engine": engine,
        "created_at": "2026-07-27T10:00:00+00:00",
        "duration_seconds": duration_seconds, "activity": [],
        "moments": moments, "missing_windows": missing_windows or [],
        "missing_chunks": [],
    }), encoding="utf-8")


def _long_word_list(n: int, *, step: float = 0.5) -> list[dict]:
    """`n` short, individually-searchable words half a second apart - each
    one's own text (`wordN`) doubles as a unique marker a test can search
    for or scroll to, the same role "moment 29" plays in a seeded moments
    list below."""
    words = []
    t = 0.0
    for i in range(n):
        words.append({"start": t, "end": t + 0.4, "text": f" word{i}"})
        t += step
    return words


def _wheel_scroll_until_visible(page, locator, hover_xy: tuple[float, float],
                                 *, max_steps: int = 40, dy: int = 350):
    """Scroll a REAL mouse wheel - not `scroll_into_view_if_needed()` - at a
    fixed point over the pane that should own the scroll, repeatedly, until
    `locator`'s own bounding box sits fully inside the current viewport (or
    `max_steps` is exhausted). Returns the final bounding box either way, so
    a caller asserts on it and gets a useful box in a failure message.

    This is deliberately NOT `scroll_into_view_if_needed()` plus a bounding
    box, despite that being this file's usual preference over `is_visible()`
    (see the module docstring's own convention). Measured directly against
    this screen's pre-fix layout: `scroll_into_view_if_needed()` still
    "succeeds" there, because it drives an element's `scrollIntoView`
    algorithm, which happily sets `scrollLeft`/`scrollTop` on ANY ancestor
    whose computed `overflow` is not `visible` - including one that is
    `overflow: hidden` with no scrollbar and no wheel handler, exactly what
    index.css's `body { overflow: hidden }` combined with the pre-fix
    `flexWrap: 'nowrap'` produces. That call can reach into a box a real
    operator's mouse and wheel never could, so it would report content as
    "reachable" that is not - and a reachability test built on it would not
    actually fail on the bug it exists to catch (confirmed by hand: reverting
    the layout fix while keeping `scroll_into_view_if_needed()` still left
    the search box's bounding box back on screen). A real
    `page.mouse.wheel()` only ever moves what a real wheel event would."""
    cx, cy = hover_xy
    page.mouse.move(cx, cy)
    viewport = page.viewport_size
    box = locator.bounding_box()
    for _ in range(max_steps):
        if (box is not None and 0 <= box["x"] and 0 <= box["y"]
                and box["x"] + box["width"] <= viewport["width"] + 1
                and box["y"] + box["height"] <= viewport["height"] + 1):
            break
        page.mouse.wheel(0, dy)
        page.wait_for_timeout(30)
        box = locator.bounding_box()
    return box


def _within_viewport(box: dict | None, viewport: dict) -> bool:
    return (box is not None and 0 <= box["x"] and 0 <= box["y"]
            and box["x"] + box["width"] <= viewport["width"] + 1
            and box["y"] + box["height"] <= viewport["height"] + 1)


class TestStreamsAndDetection:
    """Stage D2b's own addition: a channel's streams, listed and picked
    from in the studio, each with a "Detect moments" action that starts
    the same kind of background job a render already does (see
    hooks/useJobPolling.ts, reused rather than reimplemented for this).
    yt-dlp itself is never invoked here - `channel_catalogue` and
    `detect_moments` are monkeypatched at the `yt_shorts.studio.api`
    module level, exactly the pattern tests/test_studio_api.py's own
    TestStreamsRoute/TestDetectRoute already use for the same routes over
    plain HTTP; this file's job is proving the same routes work end to end
    through the real built page.

    `detect_moments` no longer creates clips (see detect.py's own module
    docstring and CLAUDE.md's "detection now only DISPLAYS"): it writes an
    ANALYSIS file and returns its Path, and a clip exists only once an
    operator picks a window (clip_from_moment.create_clip). The stream
    detail/moment picker DOES now exist (`StreamScreen.tsx`, reached at
    /{channel}/{event}/streams/{video_id}) and reads that analysis back -
    see `TestHitListBadNewsAlerts` and `TestStreamScreenNeverAnalysedJourney`
    further down in this file for that screen's own coverage. This class
    predates the stream screen and stays scoped to what it was written to
    prove: the streams TAB on the event editor and the detect job itself -
    the button's live "running" state, the job finishing, and the
    notification it reports. See the one test below for what that leaves in
    scope."""

    def test_stream_list_renders_and_a_detect_job_runs_to_completion_and_reports_its_result(
            self, event_dir, live_server, page, monkeypatch, real_job_starters):
        import threading
        from yt_shorts.detect import analysis_path
        from yt_shorts.youtube import Catalogue, Video

        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video("vid123", "ERF Race Part 1", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        gate = threading.Event()
        captured: dict = {}

        def fake_detect(video_id, workspace_dir, config, *, stream_title, **kwargs):
            # Held open by the gate below so the test has a reliable window
            # to observe the "running" state before the job completes -
            # without this, a fast fake could finish between one poll and
            # the next and the test would never actually see it (see
            # tests/test_studio_api.py's own TestDetectRoute for the same
            # technique against the plain HTTP route). Writes the real
            # moments.json SHAPE at the real path (see detect.py's
            # `analysis_path`/`detect_moments`) rather than a list of clip
            # names, which is the OLD contract this rewrite retired - a
            # stand-in shaped like that makes `_run_detect`'s
            # `Path(path).read_text(...)` raise `TypeError` in the
            # background thread instead of exercising anything real.
            gate.wait(5)
            captured["workspace_dir"] = workspace_dir
            path = analysis_path(workspace_dir, video_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "video_id": video_id,
                "engine": "lexicon",
                "created_at": "2026-07-27T10:00:00+00:00",
                "duration_seconds": 3661.0,
                "activity": [],
                "moments": [{"start": 92.0, "end": 104.0, "category": "incident",
                            "score": 3.0, "reason": "x", "hook_suggestion": None}],
                "missing_windows": [],
                "missing_chunks": [],
            }), encoding="utf-8")
            return path

        # Patched at `jobs._STUDIO_DETECT_FN` - the studio's own detect
        # policy (detect_moments bound to require_cached_transcript), which
        # is what the detect route reaches now that it no longer passes a
        # `detect_fn` of its own (Task 6). `api.detect_moments`, what this
        # used to patch, is not imported there any more.
        monkeypatch.setattr(studio_jobs, "_STUDIO_DETECT_FN", fake_detect)

        page.goto(editor_url(live_server))
        page.get_by_role("tab", name="Streams").click()

        stream_row = page.get_by_text("ERF Race Part 1")
        stream_row.wait_for()
        # The tower-style duration/views columns, in tabular figures - not
        # just that SOME row exists (see the module docstring on why every
        # other test in this file checks actual state, not presence).
        assert "1:01:01" in page.locator("body").inner_text()
        assert "12,345" in page.locator("body").inner_text()

        page.get_by_role("button", name="Detect moments").click()

        # A live "running" state, not a frozen button - the button itself
        # relabels while the job is in flight (see StreamPanel.tsx).
        detecting_button = page.get_by_role("button", name="Detecting…")
        detecting_button.wait_for(timeout=5000)

        gate.set()

        # The button reverting is the job leaving "running" (App.tsx's
        # `detecting` is derived from `detectJob?.status === 'running'`).
        page.get_by_role("button", name="Detect moments").wait_for(timeout=10000)

        # The one thing an operator sees today once a detect job finishes:
        # a notification naming the result (App.tsx's own effect on
        # `detectJob.status`), read from the job's single "detect" result
        # record - not per-candidate detail, because there is no per-clip
        # identity for a moment that created no clip.
        page.get_by_text(re.compile(r"Moment detection finished", re.I)).wait_for(timeout=5000)

        # The central invariant this whole rewrite exists to enforce:
        # detect created no clips. A studio that silently started writing
        # clips again would still pass every assertion above.
        assert list(clipstore.iter_clip_dirs(event_dir)) == []

        # And the analysis genuinely landed where detect_moments documents
        # it does, in the real shape - proving the stub exercised the same
        # contract the real function honours, not a shape of its own.
        written = analysis_path(captured["workspace_dir"], "vid123")
        data = json.loads(written.read_text(encoding="utf-8"))
        assert data["engine"] == "lexicon"
        assert len(data["moments"]) == 1

    def test_a_502_from_yt_dlp_shows_an_explanatory_state_not_a_broken_panel(
            self, event_dir, live_server, page, monkeypatch):
        from yt_shorts.youtube import YouTubeError

        def fail(url, **k):
            raise YouTubeError("yt-dlp is not installed")

        monkeypatch.setattr(api, "channel_catalogue", fail)

        page.goto(editor_url(live_server))
        page.get_by_role("tab", name="Streams").click()

        alert = page.get_by_role("alert").filter(
            has_text=re.compile("could not load streams", re.I))
        alert.wait_for()
        assert "yt-dlp" in alert.inner_text()
        # A retry action, not a dead end.
        alert.get_by_role("button", name="Retry").wait_for()


class TestStreamScreenZoomWindow:
    """A reviewer-found regression in StreamScreen.tsx's zoom effect: on
    mount `duration` is 0 (neither the transcript nor the analysis has
    resolved yet), the effect ran anyway, and `clampZoom({0,180}, 0)`
    collapsed the initial 180-second guess to `{0,0}` - a real stream is
    never 0 s long, so `clampZoom`'s own "shorter than the requested span is
    shown whole" rule did exactly what it is supposed to do for a length it
    was never meant to see. The NEXT clamp, once the real duration loads,
    then re-clamped that already-collapsed `{0,0}` up to `MIN_ZOOM_SECONDS`
    (10) rather than back to 180 - a permanent 10-second window on every
    stream, every time, with zero interaction.

    This is invisible to a `bounding_box()` check: the zoom lane's box is
    the same size whether it is showing 10 seconds or 180. Only the
    rendered time labels either side of "Drag to set a clip window" (see
    StreamTimeline.tsx) reveal it, which is what these two tests read.
    """

    def _seed_transcript(self, root: Path, video_id: str, duration_seconds: float) -> None:
        # No moments.json is written: the default empty analysis GET
        # …/moments returns has duration_seconds 0.0 (falsy), so
        # StreamScreen's `duration = analysis?.duration_seconds ||
        # transcript?.duration_seconds || 0` falls through to the
        # transcript's own duration exactly the way an un-detected stream
        # does in production.
        directory = root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text(json.dumps({
            "video_id": video_id,
            "duration_seconds": duration_seconds,
            "words": [
                {"start": 0.0, "end": 0.5, "text": " go"},
                {"start": max(duration_seconds - 0.5, 0.0), "end": duration_seconds,
                 "text": " done"},
            ],
            "missing_chunks": [],
        }), encoding="utf-8")

    def _zoom_label_group(self, page):
        # The zoom lane's own start/end labels sit in the Group either side
        # of this exact, page-unique caption (see StreamTimeline.tsx) - its
        # immediate parent is that Group, distinct from the OVERVIEW strip's
        # own total-duration label above it, which this must not be
        # confused with.
        return page.get_by_text("Drag to set a clip window").locator("xpath=..")

    def test_the_zoom_lane_opens_showing_the_intended_180_second_window(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        # 600 s so the overview strip's own total ("10:00") cannot be
        # mistaken for either zoom-lane label.
        self._seed_transcript(_fixed_workspace_root, video_id, 600.0)

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for()

        labels = self._zoom_label_group(page)
        expect(labels).to_contain_text("3:00")
        # The reviewer's reported permanent-collapse value must be gone, not
        # just "3:00" present alongside it.
        assert "0:10" not in labels.inner_text()

    def test_a_stream_shorter_than_180_seconds_shows_the_whole_stream(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._seed_transcript(_fixed_workspace_root, video_id, 90.0)

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for()

        labels = self._zoom_label_group(page)
        # The whole 90 s stream, not a 180 s guess clipped past its end and
        # not the collapsed-then-reclamped 10 s window either.
        expect(labels).to_contain_text("1:30")
        assert "0:10" not in labels.inner_text()


class TestStreamScreenReachability:
    """Task 7 review finding IMPORTANT 1: at a narrow width, the whole
    right-hand pane (player, both timeline lanes, the transcript and its
    search box) sat entirely outside the viewport with no scroll path -
    `flexWrap: 'nowrap'` (needed above the "md" breakpoint, see
    StreamScreen.tsx's own comment) combined with `body { overflow: hidden }`
    removed even the fallback horizontal scrollbar a plain page would have.
    The fix makes the two panes stack into one column below "md" and turns
    the screen's own bounded box into the scroll container.

    Both widths are seeded with the SAME stress case: a long hit list with
    BOTH bad-news alerts showing (the exact worst case HitList.tsx's own
    comment measures) and a transcript long enough to need its own scroll -
    proving reachability under the heaviest header/content load this screen
    can show, not just the empty case.

    See `_wheel_scroll_until_visible`'s own docstring for why this drives a
    real `page.mouse.wheel()` rather than this file's usual
    `scroll_into_view_if_needed()` - the latter would not have caught the
    bug this class exists to catch."""

    def _seed(self, root: Path, video_id: str) -> None:
        words = _long_word_list(200)
        duration = words[-1]["end"] + 60
        _write_stream_transcript(root, video_id, duration_seconds=duration, words=words)
        moments = [
            {"start": 20.0 * i, "end": 20.0 * i + 5, "category": "incident",
             "score": 3.0, "reason": f"moment {i}", "hook_suggestion": None}
            for i in range(30)
        ]
        _write_stream_analysis(
            root, video_id, duration_seconds=duration, engine="lexicon",
            moments=moments, missing_windows=[3, 7])

    def test_everything_is_reachable_at_1280x600(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._seed(_fixed_workspace_root, video_id)

        page.set_viewport_size({"width": 1280, "height": 600})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for(timeout=10000)
        viewport = page.viewport_size

        # Above "md" both alerts and the sort/filter controls are visible
        # immediately, and the overview strip and zoom lane sit at the TOP
        # of the right-hand pane - reachable with no scroll at all.
        overview = page.locator('[aria-label="Stream overview"]')
        zoom_lane = page.locator('[aria-label="Zoom lane"]')
        assert _within_viewport(overview.bounding_box(), viewport), overview.bounding_box()
        assert _within_viewport(zoom_lane.bounding_box(), viewport), zoom_lane.bounding_box()

        # The hit list's own ScrollArea (HitList.tsx's own 120px floor) -
        # hover over any of its own rows before wheeling, per this file's
        # "move the mouse into the pane first" rule.
        hit_list_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="moment 0")
        anchor_box = hit_list_area.bounding_box()
        last_moment = page.get_by_text("moment 29", exact=False).first
        box = _wheel_scroll_until_visible(
            page, last_moment,
            (anchor_box["x"] + anchor_box["width"] / 2, anchor_box["y"] + anchor_box["height"] / 2))
        assert _within_viewport(box, viewport), box

        # The right-hand column's own overflow fallback (StreamScreen.tsx's
        # `overflowY: 'auto'` on the player/timeline/transcript Stack) -
        # hover low in that column and scroll until the transcript's OWN
        # ScrollArea (search box included, it sits directly above the list)
        # is entirely on screen, not just its topmost pixel.
        right_col = page.locator(".mantine-Grid-col").nth(1)
        col_box = right_col.bounding_box()
        transcript_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="word0")
        t_box = _wheel_scroll_until_visible(
            page, transcript_area,
            (col_box["x"] + col_box["width"] / 2, min(col_box["y"] + col_box["height"] - 4, viewport["height"] - 2)))
        assert _within_viewport(t_box, viewport), t_box

        search_box = page.get_by_placeholder("Search the transcript")
        assert _within_viewport(search_box.bounding_box(), viewport), search_box.bounding_box()

        # Finally the transcript's OWN ScrollArea (TranscriptPane.tsx's own
        # 120px floor) - its own last line, scrolled to from the box just
        # brought fully into view above.
        last_word = page.get_by_text("word199", exact=False).first
        box = _wheel_scroll_until_visible(
            page, last_word,
            (t_box["x"] + t_box["width"] / 2, t_box["y"] + t_box["height"] / 2))
        assert _within_viewport(box, viewport), box

    def test_everything_is_reachable_at_900x700(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._seed(_fixed_workspace_root, video_id)

        page.set_viewport_size({"width": 900, "height": 700})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for(timeout=10000)
        viewport = page.viewport_size

        # No horizontal scrollbar exists here either way (matching the
        # reviewer's own measurement) - the fix is a vertical, single-column
        # stack inside NavScreen's fillHeight box, not a horizontal one.
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        client_width = page.evaluate("document.documentElement.clientWidth")
        assert scroll_width == client_width, (scroll_width, client_width)

        # Below "md" the two panes stack into ONE column and this screen's
        # own bounded box is the single scroll container - one real wheel
        # scroll, anywhere over the content, reaches everything in order.
        anchor = (viewport["width"] / 2, viewport["height"] / 2)

        last_moment = page.get_by_text("moment 29", exact=False).first
        box = _wheel_scroll_until_visible(page, last_moment, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        zoom_lane = page.locator('[aria-label="Zoom lane"]')
        box = _wheel_scroll_until_visible(page, zoom_lane, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        search_box = page.get_by_placeholder("Search the transcript")
        box = _wheel_scroll_until_visible(page, search_box, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        last_word = page.get_by_text("word199", exact=False).first
        box = _wheel_scroll_until_visible(page, last_word, anchor, dy=250)
        assert _within_viewport(box, viewport), box


class TestHitListBadNewsAlerts:
    """HitList.tsx's two alerts, against the real analysis shapes
    `stream_analysis.read_analysis` hands the screen - not just that some
    alert renders, but that each ENGINE/WINDOW combination shows exactly the
    alert(s) it should and none it should not (a false alarm on a healthy
    model run would train an operator to ignore the real one)."""

    def test_lexicon_engine_shows_the_weaker_engine_alert_alone(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        words = _long_word_list(4)
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=60.0, words=words)
        _write_stream_analysis(
            _fixed_workspace_root, video_id, duration_seconds=60.0, engine="lexicon",
            moments=[{"start": 5.0, "end": 8.0, "category": "incident", "score": 2.0,
                      "reason": "a lexicon hit", "hook_suggestion": None}])

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        alert = page.get_by_role("alert").filter(has_text=re.compile("without a model", re.I))
        alert.wait_for()
        assert page.get_by_role("alert").count() == 1

    def test_a_model_engine_shows_neither_alert(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        words = _long_word_list(4)
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=60.0, words=words)
        _write_stream_analysis(
            _fixed_workspace_root, video_id, duration_seconds=60.0, engine="model:gpt-5",
            moments=[{"start": 5.0, "end": 8.0, "category": "incident", "score": 2.0,
                      "reason": "a model hit", "hook_suggestion": None}])

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        # Wait for the real content to have actually loaded before trusting
        # an absence - otherwise "no alert yet" could just mean "not loaded
        # yet" and the assertion would be checking nothing.
        page.get_by_text("a model hit").wait_for()
        assert page.get_by_role("alert").count() == 0

    def test_missing_windows_shows_the_failed_window_alert_naming_a_count(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        words = _long_word_list(4)
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=60.0, words=words)
        _write_stream_analysis(
            _fixed_workspace_root, video_id, duration_seconds=60.0, engine="model:gpt-5",
            moments=[{"start": 5.0, "end": 8.0, "category": "incident", "score": 2.0,
                      "reason": "a model hit", "hook_suggestion": None}],
            missing_windows=[3, 7])

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        alert = page.get_by_role("alert").filter(has_text=re.compile("window", re.I))
        alert.wait_for()
        assert "2" in alert.inner_text()
        # Not the weaker-engine alert too - a model run with a couple of
        # failed windows is not the same problem as a lexicon-only run.
        assert page.get_by_role("alert").count() == 1

    def test_no_analysis_yet_shows_the_not_analysed_message_and_still_offers_the_zoom_lane(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        words = _long_word_list(4)
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=60.0, words=words)
        # No moments.json at all - GET …/moments answers its documented
        # empty analysis (engine: null, moments: []), the ordinary
        # never-detected state (see stream_analysis.py's own docstring).

        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.get_by_text(re.compile("has not been analysed yet", re.I)).wait_for()
        assert page.get_by_role("alert").count() == 0
        # A stream that has never been analysed still lets a window be
        # picked by hand - the zoom lane must still be there, not hidden
        # behind the "not analysed" state.
        page.locator('[aria-label="Zoom lane"]').wait_for()

    def test_lexicon_and_missing_windows_together_show_both_alerts_and_rows_stay_reachable(
            self, event_dir, live_server, page, _fixed_workspace_root):
        """The one engine/missing-window combination none of the three tests
        above covers: `engine: "lexicon"` AND non-empty `missing_windows` at
        once. This is not just the missing quarter of the 2x2 grid - it is
        the worst case an operator can hit (a weak engine AND hours nobody
        looked at), and it is the exact case that starved `HitList`'s own
        `ScrollArea` to 0px in an earlier task (see that component's 120px-
        floor comment). `TestStreamScreenReachability` seeds this same
        engine/missing_windows combination, but only proves the screen is
        reachable in general - it never asserts that BOTH alerts specifically
        are the ones showing. This test pins both together: both alerts
        render, and a row far down the hit list is still reachable underneath
        them by a real wheel scroll (`_wheel_scroll_until_visible`, not
        `scroll_into_view_if_needed()` - see that helper's own docstring for
        why)."""
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        words = _long_word_list(4)
        duration = 600.0
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=duration, words=words)
        moments = [
            {"start": 20.0 * i, "end": 20.0 * i + 5, "category": "incident",
             "score": 3.0, "reason": f"moment {i}", "hook_suggestion": None}
            for i in range(30)
        ]
        _write_stream_analysis(
            _fixed_workspace_root, video_id, duration_seconds=duration, engine="lexicon",
            moments=moments, missing_windows=[3, 7])

        # A short viewport, same as TestStreamScreenReachability's stress
        # case - a tall viewport never exercises the 120px floor at all.
        page.set_viewport_size({"width": 1280, "height": 600})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for(timeout=10000)
        viewport = page.viewport_size

        weaker_engine_alert = page.get_by_role("alert").filter(
            has_text=re.compile("without a model", re.I))
        weaker_engine_alert.wait_for()
        failed_window_alert = page.get_by_role("alert").filter(
            has_text=re.compile("window", re.I))
        failed_window_alert.wait_for()
        assert page.get_by_role("alert").count() == 2
        assert "2" in failed_window_alert.inner_text()

        # The rows underneath both alerts are still reachable - hover over
        # the hit list's own ScrollArea before wheeling, per this file's
        # "move the mouse into the pane first" rule, and scroll to its last
        # row.
        hit_list_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="moment 0")
        anchor_box = hit_list_area.bounding_box()
        # The whole clickable row, not just its "moment 29" text line - a
        # pane squeezed to a sliver can still coincidentally place one short
        # text node's coordinates inside the page's own rectangle while the
        # row itself has nowhere near enough room to actually show, which is
        # exactly the false pass a check against the PAGE viewport alone
        # would produce here (measured: with HitList's 120px floor removed,
        # the "moment 29" text alone still satisfied a page-viewport check
        # even though the pane itself had shrunk to ~20px, one text line).
        last_moment_row = page.get_by_text("moment 29", exact=False).first.locator("xpath=..")
        box = _wheel_scroll_until_visible(
            page, last_moment_row,
            (anchor_box["x"] + anchor_box["width"] / 2,
             anchor_box["y"] + anchor_box["height"] / 2))
        assert _within_viewport(box, viewport), box
        # Not merely inside the PAGE's bounds either - inside the hit list's
        # OWN scrollable pane, which is what "reachable underneath both
        # alerts" actually means. The pane's own frame does not move when
        # its content scrolls, so `anchor_box` still describes it here.
        assert (box is not None
                and anchor_box["y"] - 1 <= box["y"]
                and box["y"] + box["height"] <= anchor_box["y"] + anchor_box["height"] + 1), (
            box, anchor_box)


class TestTranscriptFollowsAPick:
    """StreamScreen's `handlePick`: picking a hit list row moves the
    transcript to it via `TranscriptPane`'s own `scrollToTime` ref method,
    not an effect on `currentTime` (see both modules' own docstrings on why
    a jump has to be imperative). This proves it actually moves the
    OPERATOR'S VIEW, not just component state - and that it is the pane's
    own container that scrolls, per this file's "container scrolled, not the
    page" convention, not `window`/`document`."""

    def test_picking_a_late_moment_scrolls_the_transcript_pane_not_the_page(
            self, event_dir, live_server, page, _fixed_workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        # 3000 words half a second apart is 25 minutes - long enough that a
        # moment near the end sits many transcript lines past what a single
        # screen shows without scrolling.
        words = _long_word_list(3000)
        duration = words[-1]["end"] + 10
        _write_stream_transcript(
            _fixed_workspace_root, video_id, duration_seconds=duration, words=words)
        # word2800 sits at t=1400.0 (2800 * 0.5s) - the moment below targets
        # that exact second, so "word2800" becoming visible IS the proof the
        # transcript followed the pick, not a coincidence of scroll amount.
        _write_stream_analysis(
            _fixed_workspace_root, video_id, duration_seconds=duration, engine="lexicon",
            moments=[{"start": 1400.0, "end": 1404.0, "category": "incident",
                      "score": 5.0, "reason": "late incident", "hook_suggestion": None}])

        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")
        page.locator('[aria-label="Zoom lane"]').wait_for(timeout=10000)

        transcript_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="word0")
        transcript_area.wait_for()

        late_word = page.get_by_text("word2800", exact=False).first
        # Before the pick: the transcript opens at its own top (word0's
        # neighbourhood), so a word 25 minutes in is not on screen.
        assert late_word.bounding_box() is None or not _within_viewport(
            late_word.bounding_box(), page.viewport_size)
        scroll_top_before = transcript_area.evaluate("el => el.scrollTop")
        page_scroll_before = page.evaluate("() => window.scrollY")

        page.get_by_text("late incident").click()

        # The pane's own ScrollArea moved…
        deadline = time.monotonic() + 5
        scroll_top_after = scroll_top_before
        while time.monotonic() < deadline:
            scroll_top_after = transcript_area.evaluate("el => el.scrollTop")
            if scroll_top_after > scroll_top_before:
                break
            time.sleep(0.05)
        assert scroll_top_after > scroll_top_before, (
            "TranscriptPane's own ScrollArea never scrolled")

        # …the picked word is now actually on screen…
        late_word.wait_for(timeout=5000)
        assert _within_viewport(late_word.bounding_box(), page.viewport_size), (
            late_word.bounding_box())

        # …and the PAGE itself never scrolled - NavScreen's fillHeight box
        # has no page-level scroll to fall back on (see index.css's
        # body { overflow: hidden }), and this is the one place a caller
        # could have wired the jump to `window.scrollTo` instead of the
        # ref-based, pane-scoped `scrollToTime` the docstrings insist on.
        assert page.evaluate("() => window.scrollY") == page_scroll_before == 0


class TestStreamScreenNeverAnalysedJourney:
    """The one flow no earlier test in this file strings together end to
    end: a stream that has NEVER been analysed (no moments.json at all,
    the ordinary starting state - see StreamScreen's own docstring) opens,
    its transcript is searched, a window is picked BY HAND on the zoom lane
    (there is no moment to click - there is no analysis), a clip is made
    from it, and that clip actually exists on disk afterwards. This is the
    "useful with no API key at all" promise proven as one journey rather
    than as separate parts.

    Deliberately NOT repeating what already exists elsewhere in this file:
    `TestHitListBadNewsAlerts.test_no_analysis_yet_shows_the_not_analysed_
    message_and_still_offers_the_zoom_lane` already pins the "not analysed"
    state and that the zoom lane still renders, and
    `TestStreamScreenReachability` already pins every pane's reachability at
    1280x600 and 900x700 with the wheel-based helper - this class's own
    viewport is a plain 1280x800 specifically so a real reachability
    assertion is not what this test is for. What is new here is that the
    search box actually FILTERS (no earlier test types into it and checks
    the result), that a window can be picked by DRAGGING the zoom lane
    rather than clicking a moment (no earlier test drags it at all), and
    that `POST …/streams/{video_id}/clips` reaches a real clip directory
    through the real button, not just through `test_studio_api.py`'s
    TestClient.
    """

    def test_no_analysis_search_pick_by_hand_and_create_a_clip_reaches_disk(
            self, event_dir, live_server, page, _fixed_workspace_root, monkeypatch):
        from yt_shorts.youtube import Catalogue, Video

        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        # Mocked the same way TestStreamsAndDetection mocks it, and for the
        # same reason M4 of the final branch review exists: with no mock at
        # all, `streamListTitle` stays null (channel_catalogue is never
        # called against real yt-dlp in a test) and `source_title` falls back
        # to `''` - which ALSO satisfies `!= video_id`, so the old assertion
        # passed on a build where the fix (`analysis?.stream_title ||
        # streamListTitle || ''`) was never exercised at all. Mocking a real
        # title here and asserting it lands verbatim in clip.json proves the
        # RESOLVED title actually reaches disk, not just that it isn't the id.
        real_title = "ERF Endurance Race - Full Stream"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, real_title, 600, 1000)],
                playlists=[], failed_playlists=[]))
        # Far enough apart (>12s, TranscriptPane's own GROUP_SECONDS) that
        # the two words land in DIFFERENT transcript lines - otherwise a
        # search for "contact" would still show the line containing
        # "Karussell" too, and the filter assertion below would prove
        # nothing.
        _write_stream_transcript(_fixed_workspace_root, video_id, duration_seconds=600.0, words=[
            {"start": 10.0, "end": 10.5, "text": " Karussell"},
            {"start": 200.0, "end": 200.5, "text": " contact"},
        ])
        # No moments.json is written at all - the load-bearing precondition
        # for this whole journey.

        page.set_viewport_size({"width": 1280, "height": 800})
        page.goto(f"{live_server}/{CHANNEL}/{EVENT}/streams/{video_id}")

        # The screen says plainly that nothing has been analysed yet, and
        # still offers the zoom lane to pick a window from by hand.
        page.get_by_text(re.compile("has not been analysed yet", re.I)).wait_for(timeout=5000)
        zoom_lane = page.locator('[aria-label="Zoom lane"]')
        zoom_lane.wait_for(timeout=5000)
        # Wait for the real 180s window, not the pre-duration {0,0} guess
        # TestStreamScreenZoomWindow's own class documents - dragging before
        # the re-clamp would drag a collapsed lane.
        page.get_by_text("3:00").wait_for(timeout=5000)

        # The transcript is genuinely searchable, not just present.
        page.get_by_text("Karussell").wait_for(timeout=5000)
        search = page.get_by_placeholder("Search the transcript")
        search.fill("contact")
        page.get_by_text("contact").wait_for(timeout=5000)
        assert page.get_by_text("Karussell").count() == 0
        search.fill("")
        page.get_by_text("Karussell").wait_for(timeout=5000)

        # A window picked BY HAND: drag across the zoom lane. There is no
        # moment to click - handlePick is not in play here at all.
        box = zoom_lane.bounding_box()
        y = box["y"] + box["height"] / 2
        x_start = box["x"] + box["width"] * 0.2
        x_mid = box["x"] + box["width"] * 0.3
        x_end = box["x"] + box["width"] * 0.4
        page.mouse.move(x_start, y)
        page.mouse.down()
        page.mouse.move(x_mid, y, steps=4)
        page.mouse.move(x_end, y, steps=4)
        page.mouse.up()

        make_clip = page.get_by_role("button", name=re.compile(r"^Make a clip"))
        expect(make_clip).to_be_enabled(timeout=5000)

        page.get_by_label("Hook").fill("PICKED BY HAND")

        # Nothing on disk yet - the create is the one write this screen
        # makes, and it has not happened until the button is clicked.
        assert list(clipstore.iter_clip_dirs(event_dir)) == []

        make_clip.click()
        page.get_by_text(re.compile(r"^Created ", re.I)).wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        created = []
        while time.monotonic() < deadline:
            created = list(clipstore.iter_clip_dirs(event_dir))
            if created:
                break
            time.sleep(0.05)
        assert len(created) == 1, created

        entry = clipstore.read_clip(created[0])
        assert entry["video_id"] == video_id
        assert entry["hook"] == "PICKED BY HAND"
        # Rounded (clip_from_moment.moment_url rounds to the nearest second)
        # but still inside the 180s zoom window the drag happened over.
        assert 0 <= entry["start"] < entry["end"] <= 180
        # IMPORTANT 3: on the never-analysed path `analysis` is null, so
        # `source_title` falls back to `streamListTitle` (the mocked
        # channel_catalogue above) rather than the video id. Asserting the
        # exact mocked title - not just `!= video_id` - is the point: `''`
        # also satisfies `!= video_id`, and with channel_catalogue unmocked
        # that is exactly what `source_title` used to be, which let a naive
        # over-fix (or no fix at all) pass this test undetected.
        assert entry["source_title"] == real_title


class TestWindowEdit:
    def test_selecting_a_candidate_and_saving_a_nudged_window_reaches_edit_json(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(
            event_dir, moment_entry("vid123", 92.0, 104.0, "CRASH AT TURN THREE"))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="CRASH AT TURN THREE").click()

        lead_in = page.get_by_label("Lead-in (s)")
        lead_out = page.get_by_label("Lead-out (s)")
        lead_in.wait_for()

        # Widen the window by 3s before the detected start and 2s after
        # the detected end - so effective_window becomes (89.0, 106.0).
        lead_in.fill("3")
        lead_in.blur()
        lead_out.fill("2")
        lead_out.blur()

        # The readout updates locally before any save - the same "shown
        # effective window updates, saving stays explicit" honesty
        # PreviewPane's own "Unsaved" marker already guarantees for
        # title/word edits (see PreviewPane.tsx's module docstring).
        page.get_by_text(re.compile(r"89\.0s\s*-\s*106\.0s", re.I)).wait_for()
        assert not (directory / editorial.EDIT_FILENAME).exists()

        page.get_by_role("button", name="Save changes").click()
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

        saved = editorial.load(directory)
        assert saved.window == (89.0, 106.0)

    def test_resetting_a_window_clears_the_override(self, event_dir, live_server, page):
        directory = clipstore.write_clip(
            event_dir, moment_entry("vid123", 92.0, 104.0, "CRASH AT TURN THREE"))
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE, transcript=None, window=(80.0, 110.0)))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="CRASH AT TURN THREE").click()
        page.get_by_text(re.compile(r"80\.0s\s*-\s*110\.0s", re.I)).wait_for()

        page.get_by_role("button", name="Reset to detected").click()
        # The readout falling back to the detected window is the UI's own
        # confirmation the round trip landed - wait for that before
        # checking disk, rather than racing the request.
        page.get_by_text(re.compile(r"92\.0s\s*-\s*104\.0s", re.I)).wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if editorial.load(directory).window is None:
                break
            time.sleep(0.05)
        assert editorial.load(directory).window is None


def _kept_rendered(event_dir, name_hook="CRASH", status="kept", url="https://www.youtube.com/watch/vid/0-12"):
    """A kept, already-rendered clip - the one shape UploadPanel actually
    offers an action for (see UploadPanel.tsx's own docstring: the backend
    409s every other case, so the button must not even appear for one).
    Mirrors tests/test_studio_api.py's TestUploadAndAuthRoutes._kept_rendered,
    built by hand here rather than imported since this file never imports
    from another test module."""
    directory = clipstore.write_clip(event_dir, {
        "url": url, "video_id": "vid", "hook": name_hook, "source_title": "ERF",
        "start": 0.0, "end": 12.0, "duration": 12.0, "error": None})
    clipstore.short_path(directory).write_bytes(b"pretend mp4")
    editorial.save(directory, editorial.Edit(title=None, status=status, transcript=None))
    return directory


class TestUploadAndAuth:
    """Stage E's own addition: the upload action and auth/quota status,
    built to the API contract in api.py (GET /api/auth, POST
    /api/clips/{name}/upload) - see UploadPanel.tsx and AuthStatusBar.tsx.

    The upload job itself is stubbed at the `yt_shorts.studio.jobs`
    module level (the same seam tests/test_studio_api.py's
    TestUploadAndAuthRoutes patches over plain HTTP), so nothing here ever
    touches google or the network - it fakes exactly what
    `start_upload_job`'s real uploader would do: write upload_record.json
    and log the resulting URL, in a background thread, under the event
    lock, the same shape a real upload job has (see studio/jobs.py's
    `_default_uploader` and `start_upload_job`)."""

    def _stub_upload_job(self, monkeypatch, event_dir, *, delay_event=None,
                        url="https://www.youtube.com/watch?v=FAKE123", captured=None):
        """Patches `yt_shorts.studio.api.jobs.start_upload_job` (the name
        the route actually calls through - see api.py's post_upload) with
        a fake that runs in a background thread like the real one, holds
        the event lock the same way, records an upload_record.json (so
        the effect can be asserted through the Python layer, per the
        brief), and finishes the job "done" with a log line the UI parses
        for the URL (see api.ts's extractUploadUrl -
        f"uploaded: {name} -> {url}", the exact shape studio/jobs.py's
        real uploader produces).

        `captured`, when given a dict, is filled in with the exact
        `name`/`force`/`visibility`/`publish_at` this fake was called with -
        letting a test assert what the confirm modal actually sent all the
        way down to the job layer (see api.py's post_upload, which threads
        `body.visibility`/`body.publish_at` straight through), without
        needing a real upload to complete first."""
        import threading

        from yt_shorts import upload_record
        from yt_shorts.lock import EventLock
        from yt_shorts.studio.jobs import JobStore

        def fake_start_upload_job(profile, job_store: JobStore, name, *, force=False, **kwargs):
            if captured is not None:
                captured["name"] = name
                captured["force"] = force
                captured["visibility"] = kwargs.get("visibility")
                captured["publish_at"] = kwargs.get("publish_at")
            event_lock = EventLock(profile.event_dir)
            event_lock.acquire()
            job = job_store.create()

            def run():
                try:
                    if delay_event is not None:
                        delay_event.wait(5)
                    directory = clipstore.clips_dir(event_dir) / name
                    upload_record.save(directory, "FAKE123", url, "private",
                                       when="2026-07-22T00:00:00+00:00")
                    job.record(name, "done", None, f"uploaded: {name} -> {url}")
                    job.finish("done")
                finally:
                    event_lock.release()

            threading.Thread(target=run, daemon=True).start()
            return job

        monkeypatch.setattr(api.jobs, "start_upload_job", fake_start_upload_job)

    def test_a_kept_rendered_clip_shows_upload_but_a_candidate_does_not(
            self, event_dir, live_server, page, monkeypatch):
        self._stub_upload_job(monkeypatch, event_dir)
        kept = _kept_rendered(event_dir, "CRASH AT TURN THREE", status="kept",
                              url="https://www.youtube.com/clip/UgkxCrash789")
        candidate = _kept_rendered(event_dir, "MAYBE LATER", status="candidate",
                                   url="https://www.youtube.com/clip/UgkxMaybe000")

        page.goto(editor_url(live_server))

        page.get_by_role("button", name="CRASH AT TURN THREE").click()
        page.get_by_role("button", name="Upload to YouTube").wait_for()

        page.get_by_role("button", name="MAYBE LATER").click()
        page.get_by_role("textbox", name="Title").wait_for()
        # No upload action at all for a clip the backend would 409 - not
        # just disabled, genuinely absent (see UploadPanel.tsx: it returns
        # null for anything that is not kept+rendered). Waited to "hidden"
        # rather than checked immediately, so this cannot pass merely
        # because the PREVIOUS clip's panel had not unmounted yet.
        page.get_by_role("button", name="Upload to YouTube").wait_for(state="hidden", timeout=3000)
        assert page.get_by_role("button", name="Upload again").count() == 0
        assert kept.name and candidate.name

    def test_editing_description_and_tags_and_saving_metadata_writes_the_override(
            self, event_dir, live_server, page, monkeypatch):
        """Task 8's own addition: "Save metadata" (UploadPanel.tsx's
        handleSaveMetadata) PATCHes description/tags/category_id/
        made_for_kids independently of starting an upload - this asserts
        the effect lands in TWO places: the per-clip `edit.json` override
        on disk (editorial.effective_upload's own source), and the
        upload-preview route, which recomputes `build_metadata` fresh from
        that same edit.json - so a save that only updated the DOM without
        actually reaching the server would fail both checks."""
        self._stub_upload_job(monkeypatch, event_dir)
        directory = _kept_rendered(event_dir, "METADATA EDIT CLIP",
                                   url="https://www.youtube.com/clip/UgkxMetadata777")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="METADATA EDIT CLIP").click()
        page.get_by_role("button", name="Upload to YouTube").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        # Only rendered once the upload-preview fetch has landed (see
        # UploadPanel.tsx: `{preview && !previewLoading && (...)}`), so
        # waiting on it is itself the wait for that fetch to complete.
        save_button = modal.get_by_role("button", name="Save metadata")
        save_button.wait_for()

        new_description = "A hand-edited description for this clip."
        # The description Textarea carries no accessible label (only a
        # character-count `description` sub-text) - it is the only
        # <textarea> INSIDE the modal's own DOM subtree; Mantine's autosize
        # measurement helper appends its one shared hidden mirror textarea
        # to document.body, outside the modal, so this stays unambiguous.
        modal.locator("textarea").fill(new_description)
        # The tags TextInput does carry a placeholder, unique in the modal.
        modal.get_by_placeholder("comma or newline separated").fill("turn1, overtake, crash")

        assert not save_button.is_disabled()
        save_button.click()

        page.get_by_text("Upload metadata saved.").wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        edit = editorial.load(directory)
        while time.monotonic() < deadline:
            edit = editorial.load(directory)
            if edit.upload and edit.upload.get("description") == new_description:
                break
            time.sleep(0.05)
        assert edit.upload is not None
        assert edit.upload["description"] == new_description
        assert edit.upload["tags"] == ["turn1", "overtake", "crash"]

        with urllib.request.urlopen(
                f"{live_server}/api/channels/{CHANNEL}/events/{EVENT}"
                f"/clips/{directory.name}/upload-preview") as response:
            preview = json.loads(response.read())
        assert preview["description"] == new_description
        assert preview["tags"] == ["turn1", "overtake", "crash"]

    def test_choosing_public_gates_confirm_on_the_checkbox_then_sends_visibility_public(
            self, event_dir, live_server, page, monkeypatch):
        """Pins the privacy-relaxation contract's client half (see
        CLAUDE.md's stage-E invariant): a non-private choice must not be
        confirmable until the operator ticks the checkbox naming the exact
        exposure, and only once ticked does "Confirm and upload" actually
        reach the job layer with that visibility - asserted against the
        stubbed `start_upload_job`'s own captured kwargs, the same seam
        `test_confirming_an_upload_starts_a_job_and_shows_the_uploaded_url`
        above uses, so this never depends on a real YouTube call."""
        captured: dict = {}
        self._stub_upload_job(monkeypatch, event_dir, captured=captured)
        directory = _kept_rendered(event_dir, "PUBLIC CHOICE CLIP",
                                   url="https://www.youtube.com/clip/UgkxPublic555")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="PUBLIC CHOICE CLIP").click()
        page.get_by_role("button", name="Upload to YouTube").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("radiogroup").get_by_text("Public", exact=True).click()

        confirm_button = modal.get_by_role("button", name="Confirm and upload")
        confirm_button.wait_for()
        # Gated: picking Public alone must not be enough to confirm.
        assert confirm_button.is_disabled()

        checkbox = modal.get_by_role("checkbox", name=re.compile("this upload will be public", re.I))
        checkbox.wait_for()
        checkbox.check()
        assert not confirm_button.is_disabled()

        confirm_button.click()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if captured.get("visibility") is not None:
                break
            time.sleep(0.05)
        assert captured["name"] == directory.name
        assert captured["visibility"] == "public"
        assert captured["publish_at"] is None

    def test_scheduling_an_upload_sends_publish_at_and_keeps_visibility_private(
            self, event_dir, live_server, page, monkeypatch):
        """The "Scheduled" choice is UI sugar over `visibility: "private"`
        plus a `publishAt` (see UploadPanel.tsx's own docstring on
        VisibilityChoice and youtube_upload.build_metadata's guard: a
        scheduled publish is only ever valid alongside private visibility,
        since YouTube itself flips it to public once that time arrives) -
        this pins that the job layer actually receives BOTH halves of that:
        a real `publish_at` and `visibility="private"`, not "scheduled"."""
        captured: dict = {}
        self._stub_upload_job(monkeypatch, event_dir, captured=captured)
        directory = _kept_rendered(event_dir, "SCHEDULED CHOICE CLIP",
                                   url="https://www.youtube.com/clip/UgkxSched666")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="SCHEDULED CHOICE CLIP").click()
        page.get_by_role("button", name="Upload to YouTube").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("radiogroup").get_by_text("Scheduled", exact=True).click()

        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        page.get_by_label("Publish at").fill(future)

        confirm_button = modal.get_by_role("button", name="Confirm and upload")
        confirm_button.wait_for()
        # Gated on the schedule-specific checkbox even though a future date
        # is already filled in - a valid schedule alone is not a confirm.
        assert confirm_button.is_disabled()

        checkbox = modal.get_by_role("checkbox", name=re.compile("scheduled to go public", re.I))
        checkbox.wait_for()
        checkbox.check()
        assert not confirm_button.is_disabled()

        confirm_button.click()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if captured.get("publish_at") is not None:
                break
            time.sleep(0.05)
        assert captured["name"] == directory.name
        assert captured["visibility"] == "private"
        assert captured["publish_at"] is not None

    def test_confirming_an_upload_starts_a_job_and_shows_the_uploaded_url(
            self, event_dir, live_server, page, monkeypatch):
        url = "https://www.youtube.com/watch?v=E2EFAKE"
        self._stub_upload_job(monkeypatch, event_dir, url=url)
        directory = _kept_rendered(event_dir, "OVERTAKE INTO THE HAIRPIN",
                                   url="https://www.youtube.com/clip/UgkxOvertake111")
        from yt_shorts import upload_record
        assert not upload_record.is_uploaded(directory)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="OVERTAKE INTO THE HAIRPIN").click()

        page.get_by_role("button", name="Upload to YouTube").click()
        # The confirmation shows the exact title being sent and the
        # privacy invariant, before anything is posted.
        modal = page.get_by_role("dialog")
        modal.wait_for()
        assert "OVERTAKE INTO THE HAIRPIN" in modal.inner_text()
        assert "private" in modal.inner_text().lower()

        modal.get_by_role("button", name="Confirm and upload").click()

        # The job's own effect actually reached disk - asserted through
        # the Python layer, as the other E2E tests in this file do (see
        # the module docstring).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if upload_record.is_uploaded(directory):
                break
            time.sleep(0.05)
        record = upload_record.load(directory)
        assert record is not None
        assert record["url"] == url

        # And the UI reflects it: the uploaded URL, as a real link, inside
        # the panel's own "Uploaded" state - scoped to that specific alert
        # (identified by its "manage or change it...YouTube Studio" copy,
        # unique to it - see UploadPanel.tsx's own Alert, worded this way
        # since Task 8's privacy relaxation: an upload's visibility/schedule
        # is now the operator's own per-upload choice, not always "Private",
        # so the old hard-coded "make it public" copy no longer applies)
        # rather than a page-wide search, since Mantine's toast
        # notifications ALSO carry role="alert" and one of them ("Upload
        # started."/"Uploaded - <url>") would otherwise be an ambiguous
        # second match for anything less specific.
        uploaded_alert = page.get_by_role("alert").filter(
            has_text=re.compile("manage or change it"))
        uploaded_alert.wait_for(timeout=5000)
        uploaded_link = uploaded_alert.get_by_role("link", name=url)
        assert uploaded_link.get_attribute("href") == url

        # The panel itself now shows the uploaded state rather than
        # reverting to the plain upload button.
        assert page.get_by_role("button", name="Upload again").count() == 1

    def test_the_confirmation_modal_shows_the_fetched_description_and_title(
            self, event_dir, live_server, page, studio_profile):
        """Stage E's own addition (see api.ts's `getUploadPreview` and
        UploadPanel.tsx's own docstring): the modal fetches
        GET /api/clips/{name}/upload-preview when it opens and shows the
        REAL description/title rather than the old "generated from this
        channel's config" placeholder. Seeds a clip whose effective
        title/description are known via the Python layer - the same
        `build_metadata` the route itself calls - so this asserts against
        an independently computed expectation, not just "some text
        appeared"."""
        from yt_shorts import clipstore
        from yt_shorts.youtube_upload import build_metadata

        directory = _kept_rendered(
            event_dir, "PILE UP AT THE CHICANE",
            url="https://www.youtube.com/clip/UgkxPileup444")
        clip = clipstore.read_clip(directory)
        edit = editorial.load(directory)
        meta = build_metadata(clip, edit, studio_profile.config)
        expected_description = meta["snippet"]["description"]
        assert expected_description  # sanity: the fixture template renders real text

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="PILE UP AT THE CHICANE").click()
        page.get_by_role("button", name="Upload to YouTube").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        assert "PILE UP AT THE CHICANE" in modal.inner_text()

        # The fetched description replaces the old placeholder - waited
        # for, since the fetch happens after the modal itself is shown
        # (the brief requires the modal not block on it).
        page.get_by_text(expected_description).wait_for(timeout=5000)
        assert "generated from this channel" not in modal.inner_text().lower()

    def test_an_uploaded_clip_still_shows_uploaded_after_a_fresh_page_load(
            self, event_dir, live_server, page):
        """Upload state is driven by the clip summary's `has_upload`/
        `upload_url` (see api.py's `_summary`), not just what a browser
        session watched a job finish for - so a clip that already has an
        upload.json record BEFORE the page is ever loaded (seeded here in
        Python, never via a job this session ran) must still show
        "uploaded" on the very first load, and again after an explicit
        reload."""
        from yt_shorts import upload_record

        url = "https://www.youtube.com/watch?v=PERSISTED1"
        directory = _kept_rendered(
            event_dir, "ALREADY UPLOADED EARLIER",
            url="https://www.youtube.com/clip/UgkxAlready222")
        upload_record.save(directory, "PERSISTED1", url, "private",
                           when="2026-07-22T00:00:00+00:00")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="ALREADY UPLOADED EARLIER").click()

        uploaded_alert = page.get_by_role("alert").filter(
            has_text=re.compile("manage or change it"))
        uploaded_alert.wait_for(timeout=5000)
        uploaded_link = uploaded_alert.get_by_role("link", name=url)
        assert uploaded_link.get_attribute("href") == url
        assert page.get_by_role("button", name="Upload again").count() == 1

        # And again after a genuine reload, discarding all client-side
        # state - this is the case a session-only "uploadedRecord" could
        # never pass.
        page.reload()
        page.get_by_role("button", name="ALREADY UPLOADED EARLIER").click()
        uploaded_alert = page.get_by_role("alert").filter(
            has_text=re.compile("manage or change it"))
        uploaded_alert.wait_for(timeout=5000)
        assert page.get_by_role("button", name="Upload again").count() == 1

    def test_disconnected_bar_opens_the_connect_dialog_with_the_channel_id_prefilled(
            self, event_dir, live_server, page, monkeypatch):

        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)

        page.goto(editor_url(live_server))

        connect_button = page.get_by_role("button", name="Connect channel")
        connect_button.wait_for()
        assert page.get_by_text("Not connected").count() >= 1

        connect_button.click()
        modal = page.get_by_role("dialog")
        modal.wait_for()

        # Pre-filled from GET /api/auth's channel_id (the ERF fixture's own
        # id), but a real, editable textbox - not a read-only display.
        channel_input = page.get_by_role("textbox", name="YouTube channel ID")
        channel_input.wait_for()
        assert channel_input.input_value() == "UCb3S2oA7lANdg5IS0QtF46w"

        text = modal.inner_text()
        # A plain statement that consent happens in the operator's own
        # browser, never the studio's (see CLAUDE.md's stage E boundaries) -
        # no terminal/CLI instruction any more.
        assert "browser" in text.lower()
        assert "password" in text.lower()
        assert "bin/yt-shorts" not in text
        # Never a real credential rendered anywhere on the page.
        assert "client_secret" not in page.locator("body").inner_text().lower()

        # Dismissable with Escape and with the Cancel control (hard constraint).
        page.keyboard.press("Escape")
        modal.wait_for(state="hidden")

    def _stub_connect_job(self, monkeypatch, connected: dict, *, fail_reason: str | None = None,
                          delay_event=None):
        """Patches `yt_shorts.studio.api.jobs.start_connect_job` (the name
        the route calls through - see api.py's post_connect) with a fake
        that runs in a background thread like the real one and flips
        `connected["value"]` to True on success, so a subsequent GET
        /api/auth (via the patched `load_credentials` below) reports this
        channel as connected - without ever touching google or opening a
        real browser (see studio/jobs.py's `_default_connector`, which this
        replaces). Also stubs `google_require` the same way
        tests/test_studio_api.py's TestConnectRoute does, so a venv without
        the optional Google libraries still exercises this path.
        `delay_event`, when given, blocks the fake job until the test sets
        it, deterministically holding the job in "running" long enough to
        assert the dialog's own running state (see the upload stub's own
        identical `delay_event`). Also records the `force` flag the route
        received into `connected["force"]`, so a test can assert a forced
        re-connect (see AuthStatusBar's "Switch account") actually sent
        `force: true` all the way from the click through POST
        /api/auth/connect to start_connect_job, without needing a real
        token to force past."""
        import threading

        from yt_shorts.studio.jobs import JobStore

        monkeypatch.setattr(api, "google_require", lambda feature: None)

        # Make GET /api/auth hermetic: reflect connected["value"] rather than
        # reading the real workspace's auth dir, which on a machine where the
        # operator has actually run `auth` would already hold a token and wrongly
        # report this channel connected before the test even starts.
        def fake_load_credentials(channel_id, *, auth_dir, oauth, store=None):
            return {"token": "fake"} if connected["value"] else None

        monkeypatch.setattr(api, "load_credentials", fake_load_credentials)

        def fake_start_connect_job(profile, job_store: JobStore, channel_id, *,
                                   force=False):
            connected["force"] = force
            job = job_store.create()

            def run():
                if delay_event is not None:
                    delay_event.wait(5)
                if fail_reason is not None:
                    job.record(channel_id, "failed", fail_reason, f"ERROR: {fail_reason}")
                    job.finish("failed")
                    return
                connected["value"] = True
                job.record(channel_id, "done", None, f"connected: {channel_id}")
                job.finish("done")

            threading.Thread(target=run, daemon=True).start()
            return job

        monkeypatch.setattr(api.jobs, "start_connect_job", fake_start_connect_job)

    def test_confirming_the_connect_dialog_shows_the_waiting_for_consent_state(
            self, event_dir, live_server, page, monkeypatch):
        import threading

        connected = {"value": False}
        delay_event = threading.Event()
        self._stub_connect_job(monkeypatch, connected, delay_event=delay_event)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Connect channel").click()
        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("button", name="Confirm and connect").click()

        # Held "running" by delay_event, so this is deterministic: the job
        # cannot have finished yet.
        page.get_by_text("Waiting for consent in your browser").wait_for(timeout=3000)
        delay_event.set()
        modal.wait_for(state="hidden", timeout=5000)

    def test_a_failed_connect_job_shows_its_reason_and_keeps_the_dialog_open(
            self, event_dir, live_server, page, monkeypatch):
        connected = {"value": False}
        self._stub_connect_job(monkeypatch, connected, fail_reason="consent was denied")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Connect channel").click()
        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("button", name="Confirm and connect").click()

        failure = page.get_by_text("consent was denied")
        failure.wait_for(timeout=5000)
        # The dialog stays open (not silently closed) so the operator can
        # retry, and never claims success.
        assert modal.is_visible()
        assert page.get_by_text("Not connected").count() >= 1

    def test_confirming_the_connect_dialog_starts_a_job_and_the_bar_shows_connected(
            self, event_dir, live_server, page, monkeypatch):

        connected = {"value": False}

        def fake_load_credentials(channel_id, *, auth_dir, oauth, store=None):
            return {"token": "fake"} if connected["value"] else None

        monkeypatch.setattr(api, "load_credentials", fake_load_credentials)
        self._stub_connect_job(monkeypatch, connected)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Connect channel").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("button", name="Confirm and connect").click()

        # On completion the dialog closes itself and the bar re-fetches
        # GET /api/auth, which now reports connected (see fake_load_credentials).
        modal.wait_for(state="hidden", timeout=5000)
        page.get_by_text("Connected", exact=True).wait_for(timeout=5000)
        assert page.get_by_role("button", name="Connect channel").count() == 0

    def test_switch_account_is_available_while_connected_and_sends_force_true(
            self, event_dir, live_server, page, monkeypatch):
        # Seeded already-connected, unlike every other connect test above -
        # this is the case the disconnected-only "Connect channel" button
        # could never reach: an operator who wants to re-run consent past a
        # VALID token (e.g. they picked their personal account, not the
        # brand channel) has to be able to do this even though the bar
        # already shows "Connected" (see auth.authorize's `force`).
        connected = {"value": True}
        self._stub_connect_job(monkeypatch, connected)

        page.goto(editor_url(live_server))
        page.get_by_text("Connected", exact=True).wait_for()
        assert page.get_by_role("button", name="Connect channel").count() == 0

        switch_button = page.get_by_role("button", name="Switch account")
        switch_button.wait_for()
        switch_button.click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        # Force mode says plainly that this replaces the existing connection
        # - not the same copy the first-time connect dialog shows.
        assert "replaces the current connection" in modal.inner_text().lower()

        modal.get_by_role("button", name="Confirm and switch account").click()

        # Completes exactly like a first-time connect: dialog closes, GET
        # /api/auth is re-fetched, the bar still (or again) shows connected.
        modal.wait_for(state="hidden", timeout=5000)
        page.get_by_text("Connected", exact=True).wait_for(timeout=5000)

        # The whole point of "Switch account" over reusing "Connect channel"
        # - the job the route actually started was asked to force past the
        # existing valid token, not silently reuse it.
        assert connected["force"] is True

    def test_a_forced_switch_that_fails_with_a_mismatch_shows_the_reason_and_stays_open(
            self, event_dir, live_server, page, monkeypatch):
        connected = {"value": True}
        mismatch = (
            "You authorized Jens' personal channel (UCabc123personal), but "
            "connecting this channel needs UCb3S2oA7lANdg5IS0QtF46w (ERF). "
            "Nothing was stored."
        )
        self._stub_connect_job(monkeypatch, connected, fail_reason=mismatch)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Switch account").click()

        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("button", name="Confirm and switch account").click()

        failure = page.get_by_text(mismatch)
        failure.wait_for(timeout=5000)
        # Kept open (not silently closed) so the operator can retry with the
        # right account, and the job was still asked to force re-consent.
        assert modal.is_visible()
        assert connected["force"] is True
        # The mismatch means nothing was stored - still connected as before,
        # never flips to "Not connected" just because a switch attempt failed.
        assert page.get_by_text("Connected", exact=True).count() >= 1


class TestPreviewUnavailable:
    def test_a_preview_409_shows_the_explanatory_message_not_a_broken_image(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        assert not clipstore.raw_path(directory).exists()

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        alert = page.get_by_role("alert").filter(has_text=re.compile("no preview available", re.I))
        alert.wait_for()
        text = alert.inner_text().lower()
        assert "raw" in text or "render" in text
        # No <img> ever appears in place of the alert - a broken image icon
        # would be the failure mode this test exists to catch.
        assert page.locator("img[alt^='Preview at']").count() == 0


class TestStaleShortRefresh:
    """The operator's bug: after a re-render the studio kept playing the old
    short until a hard browser reload. Two layers caused it - a player URL
    that never changed, so the mounted <video> had no reason to reload, and a
    response with no Cache-Control, so the browser could answer from cache
    without asking. This is the only automated guard on the first layer.
    """

    def test_a_re_rendered_short_reaches_the_player_without_a_reload(
            self, event_dir, live_server, page):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        clipstore.short_path(directory).write_bytes(b"the first render")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        player = page.locator("video")
        player.wait_for(timeout=5000)
        before = player.get_attribute("src")
        assert "?v=" in before, f"the player URL carries no version: {before}"

        # A CLI render, as far as the browser is concerned: the bytes change
        # with nothing in the page having asked for it.
        clipstore.short_path(directory).write_bytes(
            b"a second render, of a different length entirely")

        # The focus path. Dispatched rather than driven through the real
        # window manager: page.bring_to_front() is unreliable headless, and
        # what is under test is our handler, not the browser's event
        # delivery.
        page.evaluate("window.dispatchEvent(new Event('focus'))")

        page.wait_for_function(
            "(previous) => document.querySelector('video')?.getAttribute('src') !== previous",
            arg=before,
            timeout=5000,
        )
        after = player.get_attribute("src")
        assert "?v=" in after
        assert after != before, "the player is still pointing at the old render"

    def test_the_tab_switch_path_refreshes_it_too(
            self, event_dir, live_server, page):
        """The OTHER half of the trigger, and it needs its own test: the app
        listens for `focus` AND `visibilitychange` because neither covers
        every case - switching browser TABS fires visibilitychange reliably,
        while alt-tabbing to another APPLICATION does not do so dependably
        across platforms. Without this, removing just the visibilitychange
        listener would leave the focus test above green and the commoner of
        the two cases silently broken.
        """
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        clipstore.short_path(directory).write_bytes(b"the first render")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        player = page.locator("video")
        player.wait_for(timeout=5000)
        before = player.get_attribute("src")
        assert "?v=" in before, f"the player URL carries no version: {before}"

        clipstore.short_path(directory).write_bytes(
            b"a second render, of a different length entirely")

        # The page is visible throughout, so the handler's own
        # visibilityState guard passes and the real refetch runs - what is
        # substituted here is only the browser's event delivery, exactly as
        # in the focus test above.
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

        page.wait_for_function(
            "(previous) => document.querySelector('video')?.getAttribute('src') !== previous",
            arg=before,
            timeout=5000,
        )
        after = player.get_attribute("src")
        assert "?v=" in after
        assert after != before, "the player is still pointing at the old render"


class TestTranscriptAddRemove:
    """Whisper drops words it cannot hear - on sung audio, whole phrases.
    The editor could only ever update a row in place, so there was no way to
    type a missing line back in. This is the only automated guard on the
    buttons: the pure rules are unit-tested in words.test.ts, but nothing
    except an E2E proves they are wired to anything.
    """

    def _seed(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        derived = [{"start": 0.0, "end": 1.0, "text": " here"},
                   {"start": 1.0, "end": 9.0, "text": " stretched"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}))
        return directory

    def _save(self, page):
        page.get_by_role("button", name="Save changes").click()
        page.wait_for_timeout(50)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some("
            "b => b.textContent.includes('Save changes') && b.disabled)"
        )

    def _row_for_word(self, page, text):
        # LOCATOR FIX (plan's `filter(has_text=...)` did not resolve): a
        # word's text lives in a TextInput's `value` attribute, which
        # `filter(has_text=...)` cannot see - it matches on textContent, and
        # an <input>'s value is not text content, so every `has_text` variant
        # of this locator matched zero rows (confirmed: `.count()` was 0).
        # `filter(has=...)` instead asks whether a descendant matches a given
        # LOCATOR, which is evaluated against the live DOM's `value` attribute
        # rather than a cached text snapshot, and does resolve.
        return page.get_by_role("row").filter(has=page.locator(f'input[value="{text}"]'))

    def test_inserting_a_word_splits_the_row_and_reaches_edit_json(
            self, event_dir, live_server, page):
        """The operator's case: a word Whisper stretched across the words it
        missed, split so the missing one can be typed in."""
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        row = self._row_for_word(page, "stretched")
        row.get_by_title("Insert a word after this one (splits its time in half)").click()

        # The new row is the third body row (header is row 0). `.last` is
        # scoped to that single row, where the text field is the last input -
        # not a bare page-wide .last, which is how this file was caught before.
        page.get_by_role("row").nth(3).get_by_role("textbox").last.fill("missing")
        self._save(page)

        saved = editorial.load(directory)
        assert saved.transcript["words"] == [
            {"start": 0.0, "end": 1.0, "text": " here"},
            {"start": 1.0, "end": 5.0, "text": " stretched"},
            {"start": 5.0, "end": 9.0, "text": " missing"},
        ], saved.transcript["words"]

    def test_removing_a_word_reaches_edit_json(self, event_dir, live_server, page):
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        self._row_for_word(page, "stretched").get_by_title(
            "Remove this word").click()
        self._save(page)

        saved = editorial.load(directory)
        assert [word["text"] for word in saved.transcript["words"]] == [" here"]

    def test_an_overlapping_timing_warns_but_still_saves(
            self, event_dir, live_server, page):
        """Decision 5 as a test rather than a comment: the warning appears AND
        the save still goes through. Refusing to save would trap an operator
        in a state the tool itself let them reach."""
        directory = self._seed(event_dir)

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        # Drag the second row's start back into the first row's span.
        row = self._row_for_word(page, "stretched")
        row.get_by_role("textbox").first.fill("0.5")

        warning = page.get_by_text("overlaps the previous word")
        warning.wait_for(timeout=5000)

        self._save(page)
        saved = editorial.load(directory)
        assert saved.transcript["words"][1]["start"] == 0.5

    def test_inserting_does_not_strand_focus_in_another_words_field(
            self, event_dir, live_server, page):
        """Rows are keyed by array index. That was harmless while the list
        could only be edited in place; an insert now shifts every later row's
        key, so React reuses the DOM nodes by POSITION and a focused input can
        end up showing a different word than the one the operator was editing.

        A review demonstrated exactly that with React Testing Library - but
        RTL's synthetic click does not move focus the way a real click on a
        button does, so the demonstration may be an artifact. This settles it
        in a real browser, which is the only place the answer counts.

        Passing means focus went to the button (the normal browser behaviour)
        or stayed on the same word. Failing means the operator's next
        keystrokes would land in a different word's box with nothing but the
        changed content to warn them.
        """
        self._seed(event_dir)
        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        # Put the caret in the LATER row, then insert above it.
        self._row_for_word(page, "stretched").get_by_role("textbox").last.click()
        self._row_for_word(page, "here").get_by_title(
            "Insert a word after this one (splits its time in half)").click()

        focused = page.evaluate(
            "() => { const el = document.activeElement;"
            " return el && el.tagName === 'INPUT' ? el.value : null }"
        )
        assert focused in (None, "stretched"), (
            f"focus was left in a text field showing {focused!r} - the "
            "operator's next keystrokes would edit the wrong word"
        )


class TestEventAdmin:
    """Stage G2's own addition: the event-list screen can now create, rename
    and delete an event (see web/src/components/EventsScreen.tsx and the
    POST/PATCH/DELETE routes in api.py, over yt_shorts.event_admin). Every
    assertion checks the real directory on disk under the fixture's tmp
    CHANNELS_DIR - not just the DOM - exactly as the rest of this file proves
    an effect through the Python layer, since a list that showed a new/renamed
    row without the directory actually changing would still pass a DOM-only
    check."""

    def test_create_rename_and_delete_an_event_from_the_start_screen(
            self, studio_profile, live_server, page):
        # The events live under the repointed CHANNELS_DIR the studio_profile
        # fixture set (see tests/conftest.py / this file's fixtures) - the
        # same dir create_app() lists and mutates.
        events = profile_module.CHANNELS_DIR / "erf" / "events"

        # From the start screen: open the erf channel to reach its event list.
        page.goto(live_server)
        page.get_by_text("Endurance Racing Federation").click()
        page.wait_for_url(re.compile(r"/erf$"))
        page.get_by_text(EVENT, exact=True).wait_for()

        # Create a new event via the dialog.
        assert not (events / "round-9").exists()
        page.get_by_role("button", name="New event").click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        dialog.get_by_role("textbox", name="Event name").fill("round-9")
        dialog.get_by_role("button", name="Create").click()
        dialog.wait_for(state="hidden")

        # It appears in the list AND its directory now exists on disk.
        page.get_by_text("round-9", exact=True).wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (events / "round-9").is_dir():
                break
            time.sleep(0.05)
        assert (events / "round-9").is_dir()

        # Rename it via the row's ⋯ menu.
        page.get_by_role("button", name="Actions for round-9").click()
        page.get_by_role("menuitem", name="Rename").click()
        rename_dialog = page.get_by_role("dialog")
        rename_dialog.wait_for()
        name_field = rename_dialog.get_by_role("textbox", name="Event name")
        name_field.fill("round-10")
        rename_dialog.get_by_role("button", name="Rename").click()
        rename_dialog.wait_for(state="hidden")

        # The new name shows and the directory actually moved.
        page.get_by_text("round-10", exact=True).wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (events / "round-10").is_dir() and not (events / "round-9").exists():
                break
            time.sleep(0.05)
        assert (events / "round-10").is_dir()
        assert not (events / "round-9").exists()

        # Delete it via the typed-name confirmation dialog.
        page.get_by_role("button", name="Actions for round-10").click()
        page.get_by_role("menuitem", name="Delete").click()
        delete_dialog = page.get_by_role("dialog")
        delete_dialog.wait_for()
        # The destructive button stays disabled until the exact name is typed.
        delete_button = delete_dialog.get_by_role("button", name="Delete")
        assert delete_button.is_disabled()
        delete_dialog.get_by_role("textbox", name="Type the event name to confirm").fill("round-10")
        delete_button.click()
        delete_dialog.wait_for(state="hidden")

        # The row is gone AND the directory is removed from disk.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not (events / "round-10").exists():
                break
            time.sleep(0.05)
        assert not (events / "round-10").exists()
        assert page.get_by_text("round-10", exact=True).count() == 0


class TestChannelAdmin:
    """Stage G3a's own addition: the start screen (channel list) can now
    create, edit, rename and delete a channel (see
    web/src/components/ChannelsScreen.tsx and the POST/PATCH/DELETE routes in
    api.py, over yt_shorts.channel_admin). Every assertion checks the real
    directory / channel.json on disk under the fixture's tmp CHANNELS_DIR - not
    just the DOM - exactly as the rest of this file proves an effect through the
    Python layer, since a list that showed a new/renamed row without the
    directory actually changing would still pass a DOM-only check."""

    def test_create_edit_rename_and_delete_a_channel_from_the_start_screen(
            self, studio_profile, live_server, page):
        # The channels live under the repointed CHANNELS_DIR the studio_profile
        # fixture set - the same dir create_app() lists and mutates.
        channels = profile_module.CHANNELS_DIR

        page.goto(live_server)
        # The seeded ERF fixture channel is already listed.
        page.get_by_text("Endurance Racing Federation").wait_for()

        # Create a new channel via the dialog: a slug plus the six identity
        # fields (channel_url auto-fills from the id).
        assert not (channels / "newleague").exists()
        page.get_by_role("button", name="New channel").click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        dialog.get_by_role("textbox", name="Slug").fill("newleague")
        dialog.get_by_role("textbox", name="YouTube channel ID").fill("UCnewleague00000000000")
        dialog.get_by_role("textbox", name="Handle").fill("@newleague")
        dialog.get_by_role("textbox", name="Display name").fill("New League")
        dialog.get_by_role("textbox", name="Language").fill("en")
        dialog.get_by_role("textbox", name="Footer").fill("NEW | @newleague")
        dialog.get_by_role("button", name="Create").click()
        dialog.wait_for(state="hidden")

        # It appears in the list AND its channel.json now exists on disk with
        # the identity we sent (channel_url auto-filled from the id).
        page.get_by_text("New League").wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (channels / "newleague" / "channel.json").is_file():
                break
            time.sleep(0.05)
        created = json.loads((channels / "newleague" / "channel.json").read_text())
        assert created["display_name"] == "New League"
        assert created["handle"] == "@newleague"
        assert created["channel_url"] == "https://www.youtube.com/channel/UCnewleague00000000000"

        # Edit its display name via the row's ⋯ menu (only the changed field is
        # sent; the rest are left unchanged by the backend's partial merge).
        page.get_by_role("button", name="Actions for newleague").click()
        page.get_by_role("menuitem", name="Edit").click()
        edit_dialog = page.get_by_role("dialog")
        edit_dialog.wait_for()
        display_field = edit_dialog.get_by_role("textbox", name="Display name")
        display_field.fill("Renamed League")
        edit_dialog.get_by_role("button", name="Save changes").click()
        edit_dialog.wait_for(state="hidden")

        page.get_by_text("Renamed League").wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            data = json.loads((channels / "newleague" / "channel.json").read_text())
            if data["display_name"] == "Renamed League":
                break
            time.sleep(0.05)
        data = json.loads((channels / "newleague" / "channel.json").read_text())
        assert data["display_name"] == "Renamed League"
        assert data["handle"] == "@newleague"  # untouched field kept

        # Rename its slug via the ⋯ menu - the directory moves.
        page.get_by_role("button", name="Actions for newleague").click()
        page.get_by_role("menuitem", name="Rename").click()
        rename_dialog = page.get_by_role("dialog")
        rename_dialog.wait_for()
        rename_dialog.get_by_role("textbox", name="New slug").fill("newleague2")
        rename_dialog.get_by_role("button", name="Rename").click()
        rename_dialog.wait_for(state="hidden")

        # The new-slug row appears and the directory actually moved.
        page.get_by_role("button", name="Actions for newleague2").wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (channels / "newleague2").is_dir() and not (channels / "newleague").exists():
                break
            time.sleep(0.05)
        assert (channels / "newleague2").is_dir()
        assert not (channels / "newleague").exists()

        # Delete it via the typed-slug confirmation dialog.
        page.get_by_role("button", name="Actions for newleague2").click()
        page.get_by_role("menuitem", name="Delete").click()
        delete_dialog = page.get_by_role("dialog")
        delete_dialog.wait_for()
        # The destructive button stays disabled until the exact slug is typed.
        delete_button = delete_dialog.get_by_role("button", name="Delete")
        assert delete_button.is_disabled()
        delete_dialog.get_by_role(
            "textbox", name="Type the channel slug to confirm").fill("newleague2")
        delete_button.click()
        delete_dialog.wait_for(state="hidden")

        # The row is gone AND the directory is removed from disk.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not (channels / "newleague2").exists():
                break
            time.sleep(0.05)
        assert not (channels / "newleague2").exists()
        assert page.get_by_role("button", name="Actions for newleague2").count() == 0


class TestWorkspaceNavigation:
    """Stage G1's own addition: the studio is now a six-screen router
    (see web/src/Root.tsx) over the workspace's channels -> events -> the
    existing editor. Every other test in this file deep-links straight to
    the editor (editor_url); this class proves the navigation SHELL itself -
    that the start screen lists the seeded ERF fixture channel, opening it
    lists its events, opening an event reaches the editor, and a reload on
    the event URL stays in the editor rather than falling back to the start
    screen (the SPA fallback in api.py serves index.html and the router
    reads the path)."""

    def test_start_screen_lists_the_channel_then_its_events_then_the_editor(
            self, event_dir, live_server, page):
        # Seed one clip so the editor's clip list has a row to prove we
        # actually landed in the editor at the end.
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        # / -> the channel list: the seeded ERF fixture channel, shown by its
        # display name and handle (see workspace_listing.list_channels and the
        # fixture's channel.json), not just "some row".
        page.goto(live_server)
        page.get_by_text("Endurance Racing Federation").wait_for()
        assert "@ERFofficial" in page.locator("body").inner_text()

        # Opening it -> /erf, the event list, including the seeded studio-test
        # event.
        page.get_by_text("Endurance Racing Federation").click()
        page.wait_for_url(re.compile(r"/erf$"))
        page.get_by_text(EVENT, exact=True).wait_for()

        # Opening the event -> the editor at /erf/studio-test, showing the
        # clip list (the seeded clip's own row).
        page.get_by_text(EVENT, exact=True).click()
        page.wait_for_url(re.compile(r"/erf/studio-test$"))
        page.get_by_role("button", name="Speedy!").wait_for()

    def test_a_reload_on_the_event_url_stays_in_the_editor(
            self, event_dir, live_server, page):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").wait_for()

        # A hard reload discards all client state. The SPA fallback serves
        # index.html for /erf/studio-test and the router reads the path, so
        # the editor - not the channel list - must come back, still scoped to
        # the right event (proven by both the clip row and the breadcrumb).
        page.reload()
        page.get_by_role("button", name="Speedy!").wait_for()
        assert page.get_by_text(EVENT, exact=True).count() >= 1
        # The start screen would show "Pick a channel to edit its events." -
        # it must NOT be what came back.
        assert page.get_by_text("Pick a channel").count() == 0


class TestWorkspaceManagementE2E:
    """Task 11's own addition: proves the workspace-manager dialog (Settings
    screen, see SettingsScreen.tsx's WorkspaceManagerModal) actually re-roots
    the real built page, not just the API - tests/test_studio_api.py's own
    TestWorkspaces.test_switch_to_a_created_workspace already proves the
    re-root mechanism itself (the closed-over channels_dir + profile.CHANNELS_DIR
    pair-write) at the TestClient/API level; this is the same assertion driven
    through the real browser instead: create a workspace from the dialog, land
    back on an EMPTY channel list (proving the whole app really re-rooted, not
    just one API call), then create a channel and confirm its channel.json
    lands under the NEW workspace on disk - not the seeded fixture's.

    FsBrowser (see FsBrowser.tsx) has no free-text path field - it only ever
    browses from GET /api/fs's default, which is ``Path.home()`` - so this
    monkeypatches ``Path.home`` to ``tmp_path`` and ``api._config_home`` to
    match, exactly the ``_use_tmp_home`` idiom tests/test_studio_api.py's
    TestWorkspaces already uses for the same routes over plain HTTP, so a
    subfolder created under ``tmp_path`` is something the real dialog can
    actually navigate to and select - never the operator's real home or
    ``~/.config``."""

    def test_creating_a_workspace_from_settings_reroots_the_whole_app(
            self, studio_profile, live_server, page, tmp_path, monkeypatch):

        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / ".config")
        (tmp_path / "parent").mkdir()

        page.goto(f"{live_server}/settings")
        page.get_by_text("Workspace", exact=True).wait_for()
        page.get_by_role("button", name="Manage workspaces…").click()

        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        dialog.get_by_role("radiogroup").get_by_text("New", exact=True).click()

        # FsBrowser's default listing (no path given) is Path.home() == tmp_path,
        # which - besides studio_profile's own "channels" - now also holds the
        # "parent" folder created above; select it as the new workspace's parent.
        dialog.get_by_text("parent").click()
        dialog.get_by_role("textbox", name="New workspace name").fill("ws-new")
        dialog.get_by_role("button", name="Create").click()

        # A successful create reloads the whole app onto the new (empty)
        # workspace (see WorkspaceManagerModal's own `applied`) - the "No
        # channels yet" alert is proof the reload actually landed on a
        # workspace with nothing in it yet, not just that the dialog closed.
        page.get_by_text("No channels yet").wait_for(timeout=5000)

        # GET /api/workspaces (the exact assertion the brief calls for)
        # reports the new workspace as current, from a fresh HTTP call - not
        # trusting only what the DOM shows.
        workspaces = json.loads(
            urllib.request.urlopen(f"{live_server}/api/workspaces").read())
        new_root = tmp_path / "parent" / "ws-new"
        assert workspaces["current"]["path"] == str(new_root)

        # A channel created now must land under the NEW workspace, proving
        # the re-root reached every route - not just GET /api/workspaces.
        page.get_by_role("button", name="New channel").click()
        create_dialog = page.get_by_role("dialog")
        create_dialog.wait_for()
        create_dialog.get_by_role("textbox", name="Slug").fill("demo")
        create_dialog.get_by_role("textbox", name="YouTube channel ID").fill("UCx000000000000000000")
        create_dialog.get_by_role("textbox", name="Handle").fill("@demo")
        create_dialog.get_by_role("textbox", name="Display name").fill("Demo League")
        create_dialog.get_by_role("textbox", name="Language").fill("en")
        create_dialog.get_by_role("textbox", name="Footer").fill("DEMO | @demo")
        create_dialog.get_by_role("button", name="Create").click()
        create_dialog.wait_for(state="hidden")

        page.get_by_text("Demo League").wait_for()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (new_root / "channels" / "demo" / "channel.json").is_file():
                break
            time.sleep(0.05)
        assert (new_root / "channels" / "demo" / "channel.json").is_file()
        # The old workspace's fixture channel must not have moved or been
        # touched by any of this.
        assert (tmp_path / "channels" / "erf" / "channel.json").is_file()


class TestBrandEditor:
    """Stage G3b's own addition: a channel's screen (ChannelScreen.tsx) now
    has a Brand tab alongside Events (BrandEditor.tsx), over the
    GET/PUT .../brand and POST/DELETE .../fonts/{filename} routes in api.py
    (brand_admin.py / font_admin.py). Every assertion checks the real
    brand.json and fonts/ directory on disk under the fixture's tmp
    CHANNELS_DIR - not just what the DOM shows - same as
    TestChannelAdmin/TestEventAdmin above: a "Saved" toast with nothing
    actually written would still pass a DOM-only check."""

    def test_upload_a_font_assign_it_change_a_color_and_save(
            self, studio_profile, live_server, page, tmp_path):
        channels = profile_module.CHANNELS_DIR

        # A filename that is NOT already on disk, so the upload assertion
        # below is unambiguous - the erf fixture channel already ships
        # BarlowCondensed-Bold.ttf itself (see brand.json), so re-uploading
        # it under its own name would prove nothing about whether the
        # upload actually happened. Reuses that font's REAL bytes (not a
        # fake/empty file) under a new name, so font_admin.save_font's PIL
        # load-check exercises a real font, same as the fixture's own.
        font_bytes = (FIXTURE_CHANNELS / "erf" / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()
        upload_source = tmp_path / "Brand-Upload.ttf"
        upload_source.write_bytes(font_bytes)

        fonts_dir = channels / "erf" / "fonts"
        assert not (fonts_dir / "Brand-Upload.ttf").exists()

        # / erf -> the channel screen; the Brand tab (see ChannelScreen.tsx)
        # sits next to Events, which stays the default so nothing about the
        # existing event flow changes.
        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name="Brand").click()
        page.get_by_role("button", name="Upload font").wait_for()

        # Upload via the (hidden) file input FileButton renders - Playwright
        # can set it directly without a real file picker dialog. Wait on the
        # font list row's own delete button (unique) rather than the plain
        # filename text - Mantine keeps each Select's option list mounted
        # (hidden) in the DOM even while closed, so a bare get_by_text match
        # on the filename is ambiguous once the fonts list includes it.
        page.locator('input[type="file"]').set_input_files(str(upload_source))
        page.get_by_role("button", name="Delete Brand-Upload.ttf").wait_for()
        assert (fonts_dir / "Brand-Upload.ttf").is_file()

        # Assign the newly uploaded font to both roles. Scoped to `:visible`
        # so only the currently-OPEN dropdown's option is matched - the
        # other Select's own (closed, hidden) option list also carries a
        # same-named option node.
        def select_font(field_label: str, font_name: str) -> None:
            page.get_by_role("combobox", name=field_label).click()
            page.locator(
                '[role="option"]:visible', has_text=re.compile(rf"^{re.escape(font_name)}$"),
            ).click()

        select_font("Hook font", "Brand-Upload.ttf")
        select_font("Small font", "Brand-Upload.ttf")

        # Change a color.
        text_color = page.get_by_role("textbox", name="Text color")
        text_color.fill("#112233")
        text_color.blur()

        # The live preview (POST .../brand/preview, debounced ~300ms) lands
        # on a real object URL - never a broken/empty image.
        preview = page.get_by_alt_text("Brand preview")
        deadline = time.monotonic() + 5
        src = None
        while time.monotonic() < deadline:
            src = preview.get_attribute("src")
            if src:
                break
            time.sleep(0.1)
        assert src

        # Save - the button only enables once the form is both complete
        # (brandReadyToSave) and actually changed (see BrandEditor.tsx).
        save_button = page.get_by_role("button", name="Save brand")
        save_button.wait_for()
        assert not save_button.is_disabled()
        save_button.click()

        # brand.json on disk reflects the new color AND the new font
        # assignment - the real PUT .../brand effect, not just a DOM claim.
        brand_path = channels / "erf" / "brand.json"
        deadline = time.monotonic() + 5
        data: dict = {}
        while time.monotonic() < deadline:
            data = json.loads(brand_path.read_text())
            if data.get("colors", {}).get("text") == "#112233":
                break
            time.sleep(0.05)
        assert data["colors"]["text"] == "#112233"
        assert data["fonts"]["hook"] == "fonts/Brand-Upload.ttf"
        assert data["fonts"]["small"] == "fonts/Brand-Upload.ttf"

    def test_setting_upload_defaults_saves_them_without_touching_mode(
            self, studio_profile, live_server, page):
        """Task 7's own addition, closed by Task 8: the Brand tab's
        "Upload defaults" card (description template/tags/category/made-for-
        kids) - see BrandEditor.tsx's uploadDefaultsFromBrand/formToPatch.
        The fixture channel's brand.json ships with NO `upload` section at
        all (see tests/fixtures/channels/erf/brand.json), so `uploadMode` is
        `undefined` and formToPatch's own guard omits `mode` from the PUT
        entirely - this pins that saving the description/tags/category below
        never spuriously invents a `mode` the operator never touched (that
        stays the Settings screen's own api/manual toggle - see
        EditorForm.uploadMode's docstring)."""
        channels = profile_module.CHANNELS_DIR
        brand_path = channels / "erf" / "brand.json"
        assert "upload" not in json.loads(brand_path.read_text())

        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name="Brand").click()

        description_field = page.get_by_role("textbox", name="Description template")
        description_field.wait_for()
        description_field.fill("Highlights from {source_title}!")

        page.get_by_role("textbox", name="Tags").fill("erf, sim racing, highlights")

        page.get_by_role("combobox", name="Category").click()
        page.locator('[role="option"]:visible', has_text=re.compile("^Sports$")).click()

        save_button = page.get_by_role("button", name="Save brand")
        save_button.wait_for()
        assert not save_button.is_disabled()
        save_button.click()

        deadline = time.monotonic() + 5
        data: dict = {}
        while time.monotonic() < deadline:
            data = json.loads(brand_path.read_text())
            if data.get("upload", {}).get("description") == "Highlights from {source_title}!":
                break
            time.sleep(0.05)
        assert data["upload"]["description"] == "Highlights from {source_title}!"
        assert data["upload"]["tags"] == ["erf", "sim racing", "highlights"]
        assert data["upload"]["category_id"] == "17"  # Sports
        # No prior mode existed, and this editor never offers to set one.
        assert "mode" not in data["upload"]

    def test_a_channel_band_slider_reaches_the_preview(
            self, studio_profile, live_server, page):
        """The Task 6 review proved `bands` can be dropped from the editor's
        patch builder (BrandEditor.tsx's `formToPatch`) with every unit gate
        still green - `tsc`, oxlint and all 291 Vitest tests. This is the
        only guard: the picture itself must change when the slider moves.

        It asserts on the preview REQUEST, not on the rendered image's src.
        The first version of this test compared `img.src` before and after,
        which is VACUOUS: the src is an object URL minted fresh for every
        preview response, so it differs on any refire whether or not the
        dragged value reached the server. Proven - with `bands` deleted from
        formToPatch, rebuilt, that version still passed. The payload is the
        constraint itself and cannot pass by accident."""
        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name="Brand").click()
        preview = page.get_by_role("img", name="Brand preview")
        preview.wait_for(timeout=5000)

        previews: list[dict] = []

        def capture(request):
            if request.method == "POST" and request.url.endswith("/brand/preview"):
                body = request.post_data
                if body:
                    previews.append(json.loads(body))

        page.on("request", capture)

        # "Band opacity" is a plain heading Text directly inside the Colors
        # card's own Stack (BrandEditor.tsx keeps it in the same card as the
        # four color fields, below a Divider) - its immediate parent is that
        # Stack, which also holds the two BAND_FIELDS sliders as descendants,
        # so xpath=".." reaches a card scope that actually contains them.
        card = page.get_by_text("Band opacity", exact=True).locator("xpath=..")
        upper = card.get_by_role("slider").first
        upper.click()
        # 25 presses for a 20-step range: a keypress lost to the re-render
        # after the click leaves the slider at 0.05, which failed this
        # assertion one run in three. Mantine clamps at `min`, so the extra
        # presses are provably harmless while the margin is not.
        for _ in range(25):
            upper.press("ArrowLeft")

        # The preview is debounced, so poll for the request rather than
        # asserting immediately.
        def dragged_value_sent():
            return any(body.get("bands", {}).get("top") == 0 for body in previews)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not dragged_value_sent():
            page.wait_for_timeout(50)
        assert dragged_value_sent(), (
            "no preview request carried the dragged band value - the slider "
            f"moved but the picture would not. Bodies seen: {previews}")


class TestEventBrandEditor:
    """The event-brand stage's own addition: the editor header (App.tsx) has
    an "Event branding" button that opens a right-hand Drawer hosting
    EventBrandEditor.tsx - per-section (colors/fonts/logo/output/subtitles)
    inherit-vs-override of the event's brand.json over the channel's, via
    GET/PUT .../events/{event}/brand (event_brand_admin.py). Unlike
    TestBrandEditor above (whole channel brand, always present), an event's
    brand.json is a PARTIAL override that may not exist at all - overriding
    ONE section must write ONLY that section, never the other four, and
    "Inherit" must leave the section out of the file entirely (see
    event_brand_admin.update_event_brand). This test proves that by reading
    the real file on disk, not just the DOM, exactly like TestBrandEditor -
    plus the effective (merged) view the GET route returns, and the Drawer's
    own mandatory scroll-container requirement (see App.tsx's Drawer,
    scrollAreaComponent=ScrollArea.Autosize) at a short viewport where the
    five stacked section cards plus the live preview would not otherwise
    fit."""

    def test_overriding_colors_writes_only_that_section_and_effective_reflects_it(
            self, event_dir, live_server, page):
        new_accent = "#AA33CC"

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Event branding").click()

        drawer = page.get_by_role("dialog")
        drawer.wait_for()

        # Scope to the Colors section's own Card rather than a bare
        # get_by_role("radio", name="Override") - EVERY section (Fonts,
        # Colors, Subtitles, Logo, Output) has its own identically-labelled
        # Inherit/Override SegmentedControl, so an unscoped lookup would be
        # ambiguous. "Colors" (mixed case, matched case-insensitively) is the
        # one section header unique to this card; the input itself is
        # visually hidden by Mantine's SegmentedControl (a real click on it
        # is refused as "not visible"), so click its own label text instead -
        # exactly what an operator's mouse actually lands on.
        colors_card = drawer.locator(".mantine-Card-root").filter(has_text="Colors")
        colors_card.get_by_text("Override", exact=True).click()

        accent_field = page.get_by_role("textbox", name="Accent color")
        accent_field.wait_for()
        assert not accent_field.is_disabled()
        accent_field.fill(new_accent)
        accent_field.blur()

        save_button = page.get_by_role("button", name="Save brand")
        save_button.wait_for()
        assert not save_button.is_disabled()
        save_button.click()

        # (a) The event's brand.json on disk holds ONLY the overridden
        # section - colors - never fonts/logo/output/subtitles, which stay
        # purely inherited (absent from the file).
        brand_path = event_dir / "brand.json"
        deadline = time.monotonic() + 5
        data: dict = {}
        while time.monotonic() < deadline:
            if brand_path.is_file():
                data = json.loads(brand_path.read_text())
                if data.get("colors", {}).get("accent") == new_accent:
                    break
            time.sleep(0.05)
        assert set(data.keys()) == {"colors"}
        assert data["colors"]["accent"] == new_accent

        # (b) GET .../brand's effective (merged) view reflects the new
        # accent - the same merge profile.load itself applies at render time.
        with urllib.request.urlopen(
                f"{live_server}/api/channels/{CHANNEL}/events/{EVENT}/brand") as response:
            body = json.loads(response.read())
        assert body["effective"]["colors"]["accent"] == new_accent

        # (c) The Drawer owns its own scroll at a short viewport - the
        # mandatory visual-acceptance criterion (see App.tsx's own comment on
        # scrollAreaComponent): five stacked section cards plus the live
        # preview do not fit in 500px, so the ScrollArea's viewport (not the
        # page) must be what scrolls.
        page.set_viewport_size({"width": 1280, "height": 500})
        scroll_info = drawer.evaluate("""el => {
            const viewport = el.querySelector('.mantine-ScrollArea-viewport');
            return viewport && {scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight};
        }""")
        assert scroll_info is not None
        assert scroll_info["scrollHeight"] > scroll_info["clientHeight"]

    def test_an_event_overrides_band_opacity(
            self, studio_profile, event_dir, live_server, page):
        """The round trip this feature can most easily lose: a slider that
        does not reach the file, or a save that drops the section.

        The brief's own draft opened this editor via a "Brand" tab - that
        exists only on the CHANNEL screen (see TestBrandEditor above). The
        event-level editor is this class's own "Event branding" button plus
        Drawer (see the module docstring above and
        test_overriding_colors_writes_only_that_section_and_effective_reflects_it),
        so this test reaches it the same way. It also drives the section's
        own Inherit/Override SegmentedControl by clicking its "Override" text
        (a `role="switch"` element does not exist here - a bare `get_by_text`
        on "Override" is what the existing colors test above already proved
        works) rather than a `role="switch"` lookup, and scopes to the "Band
        opacity" card the same way that test scopes to "Colors" - each
        section has its own identically-labelled Inherit/Override control, so
        an unscoped lookup would be ambiguous, exactly the trap the module
        docstring and CLAUDE.md both warn about."""
        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Event branding").click()

        drawer = page.get_by_role("dialog")
        drawer.wait_for()

        card = drawer.locator(".mantine-Card-root").filter(has_text="Band opacity")
        card.get_by_text("Override", exact=True).click()

        # Mantine renders a Slider as a role="slider" element; drive it with
        # the keyboard rather than a drag, which is both flaky and
        # resolution-dependent. 25 presses rather than the exact 20 the step
        # (0.05) needs to cross the full [0, 1] range: Mantine's own
        # ArrowLeft handler clamps at the slider's min (see Slider.mjs's
        # handleTrackKeydownCapture - Math.max(..., domainMin)), so the extra
        # presses are harmless, and the margin absorbs a keypress landing
        # while the Override toggle's own re-render (disabled -> enabled) is
        # still settling - a real, observed flake (one lost ArrowLeft left
        # the saved value at 0.05 instead of 0) that a bare `== 0` assertion
        # must not paper over by weakening to "less than before".
        upper = card.get_by_role("slider").first
        upper.click()
        for _ in range(25):
            upper.press("ArrowLeft")

        save_button = page.get_by_role("button", name="Save brand", exact=True)
        save_button.wait_for()
        assert not save_button.is_disabled()
        save_button.click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        written = json.loads((event_dir / "brand.json").read_text(encoding="utf-8"))
        assert written["bands"]["top"] == 0


class TestSettingsScreen:
    """Stage G4's own addition: the workspace-level Settings screen (see
    web/src/components/SettingsScreen.tsx and GET /api/settings, DELETE
    .../auth in api.py) - one page listing every channel's connection state,
    with a Disconnect action that forgets the locally stored OAuth token
    (auth.forget_credentials). Connect/Switch is a real OAuth browser flow
    and is deliberately NOT covered here (see TestUploadAndAuth's own
    connect tests, which already stub the job rather than drive a real
    consent screen) - this class only proves Disconnect, a plain local file
    delete with no OAuth involved.

    Hermetic per the G4 brief's isolation note: GET /api/settings and
    DELETE .../auth both resolve the auth directory via
    api._resolve_workspace(), which by default reads the REAL machine
    workspace (YT_SHORTS_DATA / ~/YT-Shorts-Data) - NOT the tmp
    CHANNELS_DIR the studio_profile fixture repoints. So _resolve_workspace
    is monkeypatched here to a tmp workspace root under the SAME tmp_path
    studio_profile already uses, and load_credentials is monkeypatched to a
    plain token-file-existence check (rather than a real oauth.valid/
    ensure_fresh call) - "connected" reflects only the token file seeded
    and later deleted by this test, never anything under the operator's
    real ~/YT-Shorts-Data."""

    def test_settings_disconnects_a_channel(
            self, studio_profile, live_server, page, monkeypatch):
        from yt_shorts.workspace import Workspace

        # The tmp workspace root studio_profile already set CHANNELS_DIR
        # under (channels = tmp_path / "channels", see that fixture) - reuse
        # it rather than introduce a second tmp root, so "the auth dir the
        # studio resolves" and "the channels dir it lists" agree.
        root = profile_module.CHANNELS_DIR.parent
        auth_dir = root / "auth"
        auth_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels",
                              origin="YT_SHORTS_DATA"))
        monkeypatch.setattr(api, "google_require", lambda feature: None)

        channel_id = json.loads(
            (profile_module.CHANNELS_DIR / "erf" / "channel.json").read_text())["id"]
        token = auth_dir / f"token-{channel_id}.json"
        token.write_text("{}", encoding="utf-8")

        # "connected" iff the token file is still on disk - the real
        # DELETE .../auth route removes exactly this file
        # (auth.forget_credentials), so this reflects that effect
        # hermetically without a real oauth.valid/ensure_fresh call.
        def fake_load_credentials(channel_id, *, auth_dir, oauth, store=None):
            return object() if (auth_dir / f"token-{channel_id}.json").exists() else None

        monkeypatch.setattr(api, "load_credentials", fake_load_credentials)

        # The erf fixture is the only channel under CHANNELS_DIR, so a
        # page-level text query is unambiguous - no need to scope into its
        # own row/card first.
        page.goto(f"{live_server}/settings")
        page.get_by_text(channel_id).wait_for()
        page.get_by_text("connected", exact=True).wait_for()

        page.get_by_role("button", name="Disconnect").click()
        modal = page.get_by_role("dialog")
        modal.wait_for()
        # The destructive button stays disabled until the exact channel id
        # is typed (same typed-confirmation gate as the channel/event
        # delete dialogs - deleteConfirmed in eventAdmin.ts).
        disconnect_button = modal.get_by_role("button", name="Disconnect")
        assert disconnect_button.is_disabled()
        modal.get_by_role("textbox", name="Type the channel ID to confirm").fill(channel_id)
        assert not disconnect_button.is_disabled()
        disconnect_button.click()
        modal.wait_for(state="hidden")

        # The token file is actually gone on disk - not just a DOM claim -
        # and the row's own state flips because the page refetched
        # GET /api/settings after the DELETE.
        assert not token.exists()
        page.get_by_text("not connected", exact=True).wait_for(timeout=5000)
        assert page.get_by_role("button", name="Connect").count() == 1
        assert page.get_by_role("button", name="Disconnect").count() == 0


class TestQueueLimitsControl:
    """Task 11: the Settings screen's control for `PUT /api/settings/limits`
    (see api.py's `_queue_pools`/`_validated_limits` and
    `QueueLimitsPanel`/`PoolLimitField` in SettingsScreen.tsx). Before this
    task the route existed and was already tested at the HTTP level
    (test_studio_api.py's `TestJobQueueRoutes`), but nothing in the browser
    called it - the only way to change a pool's limit was to hand-edit
    `<workspace>/settings.json` and restart the studio.

    Uses `live_queue_server` (a REAL app, a REAL running-worker-free queue
    this test drives by hand with `drain_once()`), the same fixture
    `TestJobsScreen` uses, rather than `live_server`'s auto-polling worker -
    the point of every test below is to observe a precise before/after state
    around one Save click, which a background thread claiming things on its
    own schedule would make nondeterministic.

    Every test cleans up `<workspace>/settings.json` in a `finally`: the
    route writes it into the SESSION-scoped `_fixed_workspace_root` (see
    conftest.py's `_isolated_resolved_workspace`), which every other test in
    the session shares - a value left behind here would silently change the
    default limits `GET /api/settings` reports to whichever test collects
    next, the same reason `test_studio_api.py`'s
    `test_the_pool_limits_round_trip_through_settings` unlinks it too.
    """

    HERE = {"channel": CHANNEL, "event": EVENT}

    @staticmethod
    def _state(queue, entry_id: str) -> str:
        return next(e.state for e in queue.list() if e.id == entry_id)

    @staticmethod
    def _stub_transcribe(monkeypatch):
        made = []

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job(f"fake-transcribe-{len(made)}", kind="transcribe")
            job.cancel = kwargs.get("cancel")
            made.append(job)
            return job

        monkeypatch.setattr(studio_jobs, "start_transcribe_job", fake_start)
        return made

    @staticmethod
    def _cleanup_settings(root):
        workspace.settings_path(root).unlink(missing_ok=True)

    def _wait_for_live_limit(self, queue, pool, value):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if queue.limits().get(pool) == value:
                return
            time.sleep(0.05)
        assert queue.limits().get(pool) == value, (
            "the browser's Save never reached the live queue")

    def test_the_panel_says_what_the_number_does_before_its_changed(
            self, live_queue_server, page, _fixed_workspace_root):
        # Rule 1: a limit is per WORKSPACE, and raising it past the
        # machine's own cores makes things slower - said where the control
        # is, not tucked into a tooltip nobody opens.
        try:
            page.goto(f"{live_queue_server.url}/settings")
            page.get_by_text("Job queue limits").wait_for()
            body = page.locator("body").inner_text().lower()
            assert "workspace" in body
            assert "not per event" in body
            assert "not per channel" in body
            assert "slower" in body
            # The current values, pre-filled - not blank fields the operator
            # has to go discover elsewhere first.
            expect(page.get_by_role("textbox", name="cpu pool")).to_have_value("1")
            expect(page.get_by_role("textbox", name="net pool")).to_have_value("3")
        finally:
            self._cleanup_settings(_fixed_workspace_root)

    def test_the_panel_says_a_lower_limit_will_not_touch_running_work(
            self, live_queue_server, page, _fixed_workspace_root):
        # Rule 4, the half that belongs on the screen rather than only in
        # the server's behaviour: an operator watching running work when
        # they lower a limit could otherwise reasonably worry it just got
        # killed.
        try:
            page.goto(f"{live_queue_server.url}/settings")
            expect(page.get_by_text(re.compile(
                r"never touches work already running", re.I))).to_be_visible()
        finally:
            self._cleanup_settings(_fixed_workspace_root)

    def test_saving_reaches_the_live_queue_not_just_the_file(
            self, live_queue_server, page, monkeypatch, _fixed_workspace_root):
        """Rule 3, end to end: the route already re-points the live queue -
        proven here through the real browser control rather than assumed.

        Two entries queued under the default cpu limit of 1: only the first
        claims the pool. Saving cpu=2 from the Settings screen must free the
        SECOND to start too - a fix that only wrote settings.json (and left
        the running app's own queue object untouched) would still pass a
        test that only re-read the file.
        """
        server = live_queue_server
        try:
            self._stub_transcribe(monkeypatch)
            first = server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-a"))
            second = server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-b"))
            server.app.state.worker.drain_once()
            assert self._state(server.queue, first.id) == "running"
            assert self._state(server.queue, second.id) == "queued", (
                "the default cpu limit of 1 must hold the second entry back")

            page.goto(f"{server.url}/settings")
            page.get_by_role("textbox", name="cpu pool").fill("2")
            page.get_by_role("button", name="Save the cpu pool limit").click()

            # The LIVE queue object this server is actually running - not a
            # fresh read of the file - so this is proof the PUT re-pointed
            # the in-memory queue, not only settings.json.
            self._wait_for_live_limit(server.queue, "cpu", 2)

            # And the file too, so a restart keeps the operator's choice.
            on_disk = workspace.read_settings(_fixed_workspace_root)
            assert on_disk["limits"]["cpu"] == 2

            server.app.state.worker.drain_once()
            assert self._state(server.queue, second.id) == "running", (
                "the second entry should now be claimable under the raised limit")
        finally:
            self._cleanup_settings(_fixed_workspace_root)

    def test_lowering_a_limit_below_running_work_kills_and_double_counts_nothing(
            self, live_queue_server, page, monkeypatch, _fixed_workspace_root):
        """Rule 4, proven end to end: three transcriptions running, the
        operator sets cpu: 1 from the Settings screen - nothing is killed,
        and nothing is claimed twice. The honest behaviour, and the one this
        pins: running work finishes normally, and the pool claims no new
        work until it is back under the new limit."""
        server = live_queue_server
        try:
            self._stub_transcribe(monkeypatch)
            # Room for three at once, simulating three already in flight -
            # set directly on the queue rather than through a prior Save, so
            # this test's own Save (cpu: 3 -> 1) is the only one exercised
            # through the browser.
            server.queue.set_limits({"cpu": 3})
            entries = [server.queue.enqueue(
                "transcribe", dict(self.HERE, video_id=f"vid-{i}")) for i in range(3)]
            server.app.state.worker.drain_once()
            for entry in entries:
                assert self._state(server.queue, entry.id) == "running"

            page.goto(f"{server.url}/settings")
            page.get_by_role("textbox", name="cpu pool").fill("1")
            page.get_by_role("button", name="Save the cpu pool limit").click()
            self._wait_for_live_limit(server.queue, "cpu", 1)

            # Nothing running was touched by the lower limit.
            for entry in entries:
                assert self._state(server.queue, entry.id) == "running", (
                    "lowering the limit must never stop work already running")

            # A fourth entry must NOT be claimed: the pool already holds
            # three, three over the new limit of one.
            fourth = server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-3"))
            server.app.state.worker.drain_once()
            assert self._state(server.queue, fourth.id) == "queued", (
                "a lowered limit must not be exceeded by a new claim")

            # Finish two of the three: one is still running, which alone
            # already fills the new limit of one, so the fourth still may
            # not start - this is the "nothing double-counted" half.
            server.queue.mark_finished(entries[0].id, "done")
            server.queue.mark_finished(entries[1].id, "done")
            server.app.state.worker.drain_once()
            assert self._state(server.queue, fourth.id) == "queued", (
                "one entry still running already fills the new limit of 1")

            # Finish the last one: the pool is empty, under the limit again,
            # and the fourth is claimable.
            server.queue.mark_finished(entries[2].id, "done")
            server.app.state.worker.drain_once()
            assert self._state(server.queue, fourth.id) == "running", (
                "the pool should claim new work again once back under the limit")
        finally:
            self._cleanup_settings(_fixed_workspace_root)

    def test_the_servers_refusal_is_shown_verbatim_not_reimplemented_client_side(
            self, live_queue_server, page, monkeypatch, _fixed_workspace_root):
        """Rule 5: the server is the sole authority on what counts as a
        usable limit - `parsePoolLimit` (settings.ts) is a client-side HINT
        only. It already agrees with the server on every value reachable
        through the rendered field (both require a positive whole number),
        by design, so the only way to see the server refuse something the
        UI let through is to force the server's OWN validator to refuse a
        value the client considers perfectly fine - proving the failure
        path this rule requires is real, not merely assumed because the two
        sides usually agree."""

        def always_refuses(current, patch):
            raise api.HTTPException(
                status_code=400,
                detail="this test's own reason the server refused it")

        monkeypatch.setattr(api, "_validated_limits", always_refuses)
        try:
            page.goto(f"{live_queue_server.url}/settings")
            page.get_by_role("textbox", name="cpu pool").fill("5")
            page.get_by_role("button", name="Save the cpu pool limit").click()
            expect(page.get_by_text(
                "this test's own reason the server refused it")).to_be_visible()
            # Nothing was applied - the live queue kept its default, and
            # nothing was silently retried into a different, "acceptable"
            # value on the client's own initiative.
            assert live_queue_server.queue.limits()["cpu"] == 1
        finally:
            self._cleanup_settings(_fixed_workspace_root)

    def test_the_panel_is_reachable_at_a_short_viewport(
            self, live_queue_server, page, _fixed_workspace_root):
        """Scrolling is an acceptance criterion, not a nicety - driven with a
        REAL mouse wheel (see `_wheel_scroll_until_visible`), never
        `scroll_into_view_if_needed()`."""
        try:
            page.set_viewport_size({"width": 1280, "height": 420})
            page.goto(f"{live_queue_server.url}/settings")
            target = page.get_by_role("textbox", name="net pool")
            target.wait_for()
            viewport = page.viewport_size
            assert not _within_viewport(target.bounding_box(), viewport), (
                "the field was already on screen; this viewport does not "
                "exercise scrolling at all")
            box = _wheel_scroll_until_visible(
                page, target, (viewport["width"] / 2, viewport["height"] / 2))
            assert _within_viewport(box, viewport), box
        finally:
            self._cleanup_settings(_fixed_workspace_root)


class TestRenderFreezesEditor:
    """A running render freezes the clip editor's inputs (Option A). The
    render reads edit.json LIVE per clip (jobs._render_one), so for a batch
    render an edit to a not-yet-rendered clip would silently leak into the
    in-flight run; freezing the editor while a render is in flight makes the
    behaviour predictable. The render job is stubbed at the same jobs seam
    the upload/detect tests use, so nothing re-encodes or hits the network -
    a threading.Event holds the job "running" exactly as long as the
    assertions need, then releases it so the editor re-enables."""

    def _stub_render_job(self, monkeypatch, hold: threading.Event):
        from yt_shorts.lock import EventLock
        from yt_shorts.studio.jobs import JobStore

        # `cancel` AND `progress` are both keywords the WORKER passes (see
        # worker._start_render, which forwards both unconditionally): the
        # render is enqueued now, not started on the click, so the stub has
        # to accept the same call the real starter does.
        #
        # `progress` was MISSING here, and the cost was a test that looked
        # green while asserting nothing it claimed to. The TypeError was
        # caught by Worker._start's blanket handler, the entry went
        # queued -> running -> failed in microseconds, and this stub never
        # ran at all - so `renderWork.running` was never true and the three
        # in-flight assertions below were satisfied by `renderStarting`
        # alone, the optimistic flag handleRender sets for the duration of
        # the enqueue POST. Measured before the fix: with
        # `disabled={rendering}` alone the test failed 3 of 3 runs, and with
        # `disabled={renderStarting}` alone it passed 4 of 5 - which is the
        # ~87% pass rate this test was quietly flaking at, the failures
        # being Playwright's first assertion poll racing the POST.
        #
        # A stub that cannot be called is worse than no stub: the rule
        # CLAUDE.md states here - freeze on a RUNNING render only, never on
        # a merely queued one - had no test anywhere while this one appeared
        # to be it.
        def fake_start_render_job(profile, job_store: JobStore, clip_names, *,
                                  cancel=None, progress=None):
            event_lock = EventLock(profile.event_dir)
            event_lock.acquire()
            job = job_store.create()

            def run():
                try:
                    hold.wait(10)  # stay "running" until the test releases it
                    job.record("clip", "done", None, "done: clip")
                    job.finish("done")
                finally:
                    event_lock.release()

            threading.Thread(target=run, daemon=True).start()
            return job

        monkeypatch.setattr(api.jobs, "start_render_job", fake_start_render_job)

    def test_editor_inputs_freeze_while_a_render_runs(
            self, event_dir, live_server, page, monkeypatch):
        hold = threading.Event()
        self._stub_render_job(monkeypatch, hold)
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()
        title = page.get_by_role("textbox", name="Title")
        title.wait_for()
        expect(title).to_be_enabled()  # editable before any render

        try:
            page.get_by_role("button", name="This clip").click()
            # WAIT for the entry to actually be RUNNING before asserting the
            # freeze, and do not skip this step. `handleRender` sets an
            # optimistic `renderStarting` for the duration of the enqueue
            # POST, and App freezes on `rendering || renderStarting` - so an
            # assertion made straight after the click is satisfied by the
            # optimistic flag alone and says NOTHING about a running render.
            # Measured: with the click-then-assert shape, the test passed
            # under `disabled={rendering}` alone AND under
            # `disabled={renderStarting}` alone, so it pinned neither.
            # RenderPanel's badge carries the entry's own state, and by the
            # time it reads `running` the POST has long resolved and
            # `renderStarting` is false - which is what makes the three
            # assertions below about the RUNNING render they claim to be
            # about. This is the rule CLAUDE.md states here: freeze on a
            # RUNNING render only, never on a merely queued one, which with
            # a stopped worker would freeze forever.
            expect(page.get_by_text("running", exact=True)).to_be_visible()
            # While the render is running, the whole editor is frozen: no edit
            # can be staged or committed that a batch render would pick up.
            expect(title).to_be_disabled()
            expect(page.get_by_role("button", name="Save changes")).to_be_disabled()
            expect(
                page.get_by_text("Editing is locked while the render is running", exact=False)
            ).to_be_visible()
        finally:
            hold.set()  # let the stubbed render finish even if an assert fails

        # Once the job completes, the editor is editable again.
        expect(title).to_be_enabled()


class TestLogsScreen:
    """Task 9's own addition: the workspace-level Logs screen (LogsScreen.tsx,
    over GET /api/logs* - see api.py's "Logs: read-only views over
    <workspace>/logs/" section and logsetup.py), proven against the REAL
    built page: seeded central-log lines actually render and the file is
    listed, a job log seeded straight to disk is listed by its decoded kind
    and shows its content once selected, and a detect job that really ran
    through this session's live_server (reusing TestStreamsAndDetection's
    own stub pattern) is reachable via its own "View log" link, showing that
    job's own written content - not a placeholder.

    Seeds directly onto `_fixed_workspace_root` (see tests/conftest.py's
    `_isolated_resolved_workspace`, autouse for the whole session) rather
    than a fresh tmp_path, because that IS the root `_resolve_workspace()`
    resolves to for every studio route in this session - the actual
    directory the Logs screen's routes read from (a separate concern from
    `profile.CHANNELS_DIR`/`studio_profile`, which is what the channel/event
    routes read - see that fixture's own module docstring on the two
    independent roots). The central log is APPENDED to, never overwritten:
    a `write_text` truncation could race the "ytshorts" logger's own
    already-open file handle from an earlier test in this session and
    corrupt it (see logsetup.configure_logging's idempotence-by-name) -
    the same reason test_studio_logs_api.py's own `test_tails_from_a_position`
    appends rather than rewrites."""

    def test_seeded_central_log_lines_render_and_the_file_is_listed(
            self, event_dir, live_server, page, _fixed_workspace_root):
        logs_dir = workspace.logs_dir(_fixed_workspace_root)
        marker = uuid.uuid4().hex[:8]
        with (logs_dir / workspace.CENTRAL_LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(f"2026-07-24 00:00:00 INFO seed-a-{marker}\n")
            handle.write(f"2026-07-24 00:00:01 INFO seed-b-{marker}\n")

        page.goto(f"{live_server}/logs")
        # The central log is listed by its own name (its list row, not just
        # the viewer header that also echoes the selected file's name).
        page.get_by_role("button").filter(has_text="Central log").wait_for()

        body_text = page.locator("body").inner_text()
        assert f"seed-a-{marker}" in body_text
        assert f"seed-b-{marker}" in body_text

    def test_a_seeded_job_log_is_listed_by_kind_and_shows_its_content_when_selected(
            self, event_dir, live_server, page, _fixed_workspace_root):
        job_id = uuid.uuid4().hex[:8]
        name = f"detect-{job_id}.log"
        jobs_dir = workspace.job_logs_dir(_fixed_workspace_root)
        (jobs_dir / name).write_text(
            f"2026-07-24 00:00:00 INFO seeded job line {job_id}\n", encoding="utf-8")

        page.goto(f"{live_server}/logs")
        job_row = page.locator("button").filter(has_text=name)
        job_row.wait_for()
        # Listed with its kind, decoded from the filename (see logs.ts's
        # jobKindFromLogName) - the Badge sitting alongside the filename.
        assert "detect" in job_row.inner_text().lower()

        job_row.click()
        page.get_by_text(f"seeded job line {job_id}").wait_for()

    def test_a_real_detect_jobs_view_log_link_shows_its_own_log_content(
            self, event_dir, live_server, page, monkeypatch, real_job_starters):
        from yt_shorts.youtube import Catalogue, Video

        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "Log E2E Stream", 60, 1)],
                playlists=[], failed_playlists=[]))

        gate = threading.Event()

        def fake_detect(video_id, workspace_dir, config, *, stream_title, **kwargs):
            # Held open exactly like TestStreamsAndDetection's own stub, so
            # the test has a reliable window to observe "running" before
            # the job (and its own log lines) complete. detect_moments no
            # longer creates a clip - it writes an analysis file and returns
            # its path (see detect.py) - so the stand-in does the same,
            # instead of the pre-rewrite behaviour of writing a clip and
            # returning a list of clip names.
            gate.wait(5)
            analysis = Path(workspace_dir) / "streams" / video_id / "moments.json"
            analysis.parent.mkdir(parents=True, exist_ok=True)
            analysis.write_text(json.dumps({
                "engine": "lexicon",
                "moments": [{"start": 10.0, "end": 20.0}],
                "missing_windows": [],
            }), encoding="utf-8")
            return analysis

        # Patched at `jobs._STUDIO_DETECT_FN` - the studio's own detect
        # policy (detect_moments bound to require_cached_transcript), which
        # is what the detect route reaches now that it no longer passes a
        # `detect_fn` of its own (Task 6). `api.detect_moments`, what this
        # used to patch, is not imported there any more.
        monkeypatch.setattr(studio_jobs, "_STUDIO_DETECT_FN", fake_detect)

        page.goto(editor_url(live_server))
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("Log E2E Stream").wait_for()

        page.get_by_role("button", name="Detect moments").click()
        page.get_by_role("button", name="Detecting…").wait_for(timeout=5000)

        # log_name is known as soon as the job is created (see jobs.py's
        # Job.__init__/JobStore.create) - the link appears before the job
        # even finishes.
        view_log = page.get_by_text("View log")
        view_log.wait_for()

        gate.set()
        page.get_by_text("Detect: done").wait_for(timeout=10000)

        view_log.click()
        page.wait_for_url(re.compile(r"/logs\?file=detect-"))

        deadline = time.monotonic() + 5
        content = ""
        while time.monotonic() < deadline:
            content = page.locator("body").inner_text()
            if f"start: detect {video_id}" in content:
                break
            time.sleep(0.1)
        # The job's OWN written narrative - not a placeholder page: the
        # start line, this job's own "detect" result recording the engine
        # and moment count (see jobs.py's _run_detect/Job.record), and its
        # terminal summary.
        assert f"start: detect {video_id}" in content
        assert "engine=lexicon" in content
        assert "summary: done (1 moments)" in content


def marker_row(container, marker: str):
    """The one row-level Mantine Group for `marker`'s row inside `container`
    (a MomentsEditor instance, scoped to a Card/Tabs.Panel/Drawer dialog) -
    see MarkerRowView in MomentsEditor.tsx: the marker's own text sits TWO
    Mantine Group ancestors below the row itself (the first only wraps the
    marker name and its source badge together), so climbing two ".mantine-
    Group-root" ancestors reaches the row that also carries the weight, the
    badge and the Override/Disable/Remove actions - everything a test needs
    to read or click for one marker."""
    return container.get_by_text(marker, exact=True).locator(
        "xpath=ancestor::div[contains(@class, 'mantine-Group-root')][2]")


class TestMomentsEditor:
    """Task 8's own addition: the moments-lexicon editor (MomentsEditor.tsx,
    over the GET/PUT .../moments and POST /api/moments/adopt-default routes
    in api.py, lexicon_admin.py) mounted at all three scopes CLAUDE.md's
    lexicon paragraph describes - workspace (a Card in Settings), channel (a
    Tabs.Tab) and event (a Drawer) - proving the additive-layers contract end
    to end: a marker added at a less specific scope shows up INHERITED at a
    more specific one, a save writes ONLY the scope's own layer (never a
    layer it merely inherits from), and disabling an inherited marker (weight
    0) is itself an own entry at the disabling scope.

    Workspace-scope routes resolve through `_resolve_workspace()`, which by
    default reads the operator's REAL machine workspace - not the tmp
    CHANNELS_DIR `studio_profile` repoints - so this monkeypatches it to the
    same tmp workspace root `studio_profile` already used, exactly the
    isolation `TestSettingsScreen.test_settings_disconnects_a_channel` above
    already established for the same seam. Channel/event-scope routes read
    `channels_dir` under that same root, which is where `studio_profile`
    already put the copied `erf` fixture - so all three scopes agree on one
    workspace throughout.

    One flowing test, not four independent ones: steps 2-4 depend on state
    the earlier steps actually wrote (the channel step needs a REAL
    workspace-layer marker to show as inherited; adopting the default at the
    end deliberately clobbers the workspace marker the first step added, so
    it has to run last) - splitting them would mean re-deriving that state
    with no browser involved, which is what the plain-HTTP suite
    (test_studio_moments_api.py) already covers."""

    def test_workspace_channel_event_layers_and_adopt_default(
            self, studio_profile, event_dir, live_server, page, monkeypatch):
        from yt_shorts.workspace import Workspace

        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA"))

        workspace_moments_path = root / "moments.json"
        channel_moments_path = profile_module.CHANNELS_DIR / "erf" / "moments.json"
        event_moments_path = event_dir / "moments.json"
        assert not workspace_moments_path.exists()
        # The channel fixture ships its OWN moments.json (see tests/fixtures/
        # channels/erf/moments.json) - it is the ten-marker weighted lexicon
        # this task itself converted, not empty.
        assert channel_moments_path.exists()
        assert not event_moments_path.exists()

        # 1. Settings: add a workspace-layer marker with a weight, save, and
        # confirm it actually reached <workspace>/moments.json - nothing
        # merged in from the channel/default layers, since a PUT overwrites
        # only own rows (lexicon_admin.update's own contract).
        page.goto(f"{live_server}/settings")
        page.get_by_text("Moments lexicon", exact=True).wait_for()
        page.get_by_role("textbox", name="New marker").fill("chicane bump")
        page.get_by_role("textbox", name="Weight").fill("2.5")
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_text("chicane bump").wait_for()
        # .first: Settings also renders the Glossary card's own "Save"
        # button below this one (task 8's addition) - Moments' Card comes
        # first in SettingsScreen, so .first is this editor's own Save.
        page.get_by_role("button", name="Save", exact=True).first.click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        workspace_data: dict = {}
        while time.monotonic() < deadline:
            if workspace_moments_path.is_file():
                workspace_data = json.loads(workspace_moments_path.read_text())
                if "chicane bump" in workspace_data.get("markers", {}):
                    break
            time.sleep(0.05)
        assert workspace_data["markers"] == {"chicane bump": 2.5}

        # 2. Channel: open erf's Moments tab, confirm the workspace marker
        # from step 1 shows as INHERITED (not editable in place, badged with
        # its source layer), add a channel-only marker, save, and confirm
        # only the channel file changed - the workspace file this step never
        # touched stays byte-identical to what step 1 wrote.
        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name="Moments").click()
        page.get_by_text("chicane bump").wait_for()
        inherited_row = marker_row(page, "chicane bump")
        assert "workspace" in inherited_row.inner_text().lower()
        # An inherited row has no editable weight box - only Override/Disable.
        assert inherited_row.get_by_role("button", name="Override").count() == 1

        page.get_by_role("textbox", name="New marker").fill("pit lane speeding")
        page.get_by_role("textbox", name="Weight").fill("1.5")
        page.get_by_role("button", name="Add", exact=True).click()
        page.get_by_text("pit lane speeding").wait_for()
        # .first: same reason as the Settings step above - Mantine keeps the
        # Glossary tab's panel (and its own "Save" button) mounted even while
        # the Moments tab is active, and Moments precedes Glossary in tab
        # order.
        page.get_by_role("button", name="Save", exact=True).first.click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        channel_data: dict = {}
        while time.monotonic() < deadline:
            channel_data = json.loads(channel_moments_path.read_text())
            if "pit lane speeding" in channel_data.get("markers", {}):
                break
            time.sleep(0.05)
        assert channel_data["markers"]["pit lane speeding"] == 1.5
        # The channel's PRE-EXISTING own markers (the fixture's ten) survive
        # the save - a PUT re-sends every own row, not only the new one.
        assert channel_data["markers"]["crash"] == 3.0
        # The workspace-only marker never leaked into the channel's own file.
        assert "chicane bump" not in channel_data["markers"]
        assert json.loads(workspace_moments_path.read_text()) == workspace_data

        # 3. Event: open the event drawer, disable a marker this event only
        # INHERITS (from the channel layer, not its own), save, and confirm
        # the event file records the disable as its own weight-0 entry, and
        # the row itself renders struck through with a "disabled" badge.
        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Event moments").click()
        drawer = page.get_by_role("dialog")
        drawer.wait_for()
        crash_row = marker_row(drawer, "crash")
        assert "channel" in crash_row.inner_text().lower()
        crash_row.get_by_role("button", name="Disable").click()
        drawer.get_by_role("button", name="Save", exact=True).click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        event_data: dict = {}
        while time.monotonic() < deadline:
            if event_moments_path.is_file():
                event_data = json.loads(event_moments_path.read_text())
                if "crash" in event_data.get("markers", {}):
                    break
            time.sleep(0.05)
        assert event_data["markers"] == {"crash": 0}
        assert "disabled" in crash_row.inner_text().lower()

        # 4. Back in Settings, adopt the built-in default into the workspace
        # layer: confirm the modal, and confirm the workspace file gained the
        # default's own entries ADDITIVELY - the "chicane bump" marker step 1
        # wrote SURVIVES, exactly as lexicon_admin.adopt_default's own
        # docstring requires (adopt is never a whole-layer overwrite of only
        # the default; that would silently destroy a saved custom marker).
        page.goto(f"{live_server}/settings")
        page.get_by_text("Moments lexicon", exact=True).wait_for()
        # .first: Settings also has the Glossary card's own identically-named
        # adopt button (task 8's addition) below this one.
        page.get_by_role("button", name="Adopt the built-in default").first.click()
        modal = page.get_by_role("dialog")
        modal.wait_for()
        modal.get_by_role("button", name="Adopt as my own").click()
        modal.wait_for(state="hidden")
        page.get_by_text("Adopted the built-in default as your own.").wait_for(timeout=5000)

        deadline = time.monotonic() + 5
        final_data: dict = {}
        while time.monotonic() < deadline:
            final_data = json.loads(workspace_moments_path.read_text())
            if "pole" in final_data.get("markers", {}):
                break
            time.sleep(0.05)
        assert final_data["markers"]["crash"] == 3.0
        assert final_data["markers"]["pole"] == 0.3
        # The default's ~39 markers PLUS the "chicane bump" marker step 1
        # already saved here - adopt is additive, so nothing step 1 wrote is
        # lost.
        assert len(final_data["markers"]) == len(lexicon.DEFAULT_MARKERS) + 1
        assert final_data["markers"]["chicane bump"] == 2.5


def glossary_row(container, label: str):
    """The one row-level Mantine Group for the term/correction spelled
    `label`, inside `container` (a GlossaryEditor instance scoped to a
    Card/Tabs.Panel/Drawer dialog). GlossaryEditor's EntryRow nests its label
    Text exactly the way MomentsEditor's MarkerRowView nests its marker Text -
    one Group wrapping the label with its badge, itself inside the row's own
    outer Group that also carries the Override/Disable/Enable/Remove actions
    - so the same "climb two .mantine-Group-root ancestors" idiom `marker_row`
    already uses reaches the whole row here too."""
    return container.get_by_text(label, exact=True).first.locator(
        "xpath=ancestor::div[contains(@class, 'mantine-Group-root')][2]")


class TestGlossaryEditor:
    """The glossary editor mounted at all three scopes - workspace (a Card in
    Settings), channel (a Tabs.Tab) and event (a Drawer) - proving the
    additive-layers contract end to end: an entry added at a less specific
    scope shows up INHERITED at a more specific one, a save writes ONLY the
    scope's own layer, and disabling an inherited entry is itself an own
    entry at the disabling scope.

    One flowing test, not four: each step depends on state the earlier steps
    wrote (the channel step needs a real workspace entry to inherit; the
    final event step needs the event's own layer already populated by the
    earlier event step, so an unrelated save afterwards has a real own row to
    accidentally clobber, not an empty one), and splitting them would mean
    re-deriving that state with no browser - which
    tests/test_studio_glossary_api.py already covers over plain HTTP.

    The channel and event steps also carry this task's own required coverage
    of EntryRow's action matrix (GlossaryEditor.tsx): the channel step
    disables a term (Karussell) this scope only INHERITS from the built-in
    default, and the event step - the next scope down - then proves that a
    row a LESS specific layer disabled offers Enable (not just Override), and
    that clicking it produces a row this scope owns and can save. EntryRow's
    matrix has no unit test (this repo's Vitest suite covers pure modules
    only, no component-render harness), so this is the only place a rendered
    action set is ever asserted.

    glossary.DEFAULT_LAYER is EMPTY by design now (see glossary.py's module
    docstring) - the shipped Nordschleife vocabulary this class exercises as
    "the built-in default" moved to tracks.PACKS. Every test below
    monkeypatches DEFAULT_LAYER to that pack's own layer (byte-identical to
    what used to live in glossary.DEFAULT_TERMS/DEFAULT_REPLACEMENTS) rather
    than dropping this coverage: it is what proves an inherited, disableable
    "built-in" row still renders correctly through the editor, and it is a
    real, in-process monkeypatch (live_server runs create_app() on a thread
    in THIS process), so glossary_admin's calls see it exactly as they would
    see the real default.

    There is no adopt-default step any more - glossary_admin lost that route
    when the shipped default went empty (see CLAUDE.md's "Shipped vocabulary
    is scoped to a circuit, not global"). The final step below instead picks
    a DIFFERENT venue's circuit (Monza, not the Nordschleife this class
    monkeypatches as the built-in default) at event scope, and proves the
    round trip that replaces adopt-default as this class's data-loss check:
    the file records the selected id, the pack's own rows arrive as
    inherited, and a later, unrelated save does not clear the selection - the
    round trip this feature can most easily lose."""

    def test_workspace_channel_event_layers_and_circuit_selection(
            self, studio_profile, event_dir, live_server, page, monkeypatch):
        from yt_shorts.workspace import Workspace

        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA"))
        monkeypatch.setattr(
            glossary_module, "DEFAULT_LAYER",
            tracks.as_layer(tracks.get("nurburgring-nordschleife")))

        workspace_path = root / "glossary.json"
        channel_path = profile_module.CHANNELS_DIR / "erf" / "glossary.json"

        # 1. Workspace scope: add a correction in Settings and assert the file.
        page.goto(f"{live_server}/settings")
        page.get_by_label("Heard as").last.fill("kessichen")
        page.get_by_label("Should say").last.fill("Kesselchen")
        page.get_by_role("button", name="Add correction").last.click()
        page.get_by_role("button", name="Save").last.click()
        page.wait_for_function(
            "() => document.body.innerText.includes('Saved.')")
        written = json.loads(workspace_path.read_text(encoding="utf-8"))
        assert written["replacements"]["kessichen"] == "Kesselchen"

        # 2. Channel scope: the workspace entry shows as inherited; add a
        #    channel term, disable a term inherited from the built-in
        #    default (Karussell - the event step below needs it as an
        #    inherited-and-disabled row to prove Enable is reachable), and
        #    assert ONLY the channel file changed.
        #
        #    Deviation from the brief here: the brief's own snippet adds
        #    "Rei Racing" and asserts the channel's terms end up equal to
        #    exactly {"Rei Racing": True}. The checked-in fixture (tests/
        #    fixtures/channels/erf/glossary.json) already owns "Rei Racing"
        #    (plus "Team Fullsend" and "Nordschleife") at the channel layer,
        #    so addOwnTermRow rejects the duplicate outright ("this term is
        #    already one of your own entries" - glossaryLayers.ts) and no
        #    Save ever fires; and even a genuinely new term would still
        #    round-trip the fixture's other three own terms, because
        #    glossary_admin.update overwrites the WHOLE own layer, never a
        #    merge. Adding a term that is not already own, and asserting
        #    inclusion rather than exact equality, is what actually exercises
        #    the "add" step against this fixture's real, pre-existing state.
        before = workspace_path.read_text(encoding="utf-8")
        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name="Glossary").click()
        page.wait_for_function(
            "() => document.body.innerText.includes('kessichen')")
        assert page.get_by_text("workspace", exact=True).count() >= 1

        page.get_by_label("New term").fill("Boxengasse")
        page.get_by_role("button", name="Add term").click()

        karussell_term = glossary_row(page, "Karussell")
        assert "built-in" in karussell_term.inner_text().lower()
        karussell_term.get_by_role("button", name="Disable").click()

        # .last: Mantine's Tabs.Panel keeps every panel mounted (just hidden)
        # rather than unmounting the inactive ones, so the Moments tab's own
        # "Save" button is ALSO in the DOM here - Moments precedes Glossary
        # in ChannelScreen's tab declaration order, so .last is Glossary's.
        page.get_by_role("button", name="Save").last.click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")

        channel_written = json.loads(channel_path.read_text(encoding="utf-8"))
        assert channel_written["terms"]["Boxengasse"] is True
        assert channel_written["terms"]["Karussell"] is False
        # The channel's PRE-EXISTING own terms (the fixture's own three)
        # survive the save - a PUT re-sends every own row, not only the ones
        # this step touched.
        assert channel_written["terms"]["Rei Racing"] is True
        assert channel_written["terms"]["Team Fullsend"] is True
        assert channel_written["terms"]["Nordschleife"] is True
        assert workspace_path.read_text(encoding="utf-8") == before

        # 3. Event scope: disable an inherited correction (kessichen, from
        #    workspace) AND enable a different inherited-and-disabled term
        #    (Karussell, disabled one scope up in step 2) - together these
        #    are the task's required proof that Enable and Disable are both
        #    reachable from a single inherited row, and that clicking Enable
        #    produces a row this scope owns and can save.
        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Event glossary").click()
        drawer = page.get_by_role("dialog")
        drawer.wait_for()
        drawer.get_by_text("kessichen", exact=True).wait_for()

        kessichen_row = glossary_row(drawer, "kessichen")
        assert "workspace" in kessichen_row.inner_text().lower()
        kessichen_row.get_by_role("button", name="Disable").click()

        karussell_row = glossary_row(drawer, "Karussell")
        assert "channel" in karussell_row.inner_text().lower()
        assert "disabled" in karussell_row.inner_text().lower()
        # Override alone is a dead end for a row a less-specific layer
        # disabled (it would copy the CURRENT disabled state forward) -
        # Enable must be offered directly.
        assert karussell_row.get_by_role("button", name="Enable").count() == 1
        karussell_row.get_by_role("button", name="Enable").click()

        drawer.get_by_role("button", name="Save", exact=True).click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        event_written = json.loads(
            (event_dir / "glossary.json").read_text(encoding="utf-8"))
        assert None in event_written["replacements"].values()
        assert event_written["terms"]["Karussell"] is True
        assert page.get_by_text("disabled", exact=False).count() >= 1

        # 4. Event scope: pick a circuit, save, and assert BOTH that the file
        #    records it and that the pack's rows arrive as inherited - then
        #    save an unrelated row and assert the circuit SURVIVES, which is
        #    the round-trip this feature can most easily lose.
        page.goto(f"{live_server}/erf/{event_dir.name}")
        page.get_by_role("button", name="Event glossary").click()
        drawer = page.get_by_role("dialog")
        drawer.wait_for()
        drawer.get_by_label("Circuit").click()
        page.get_by_role("option", name="Autodromo Nazionale Monza").click()
        drawer.get_by_role("button", name="Save").click()
        page.wait_for_function("() => document.body.innerText.includes('Saved.')")

        written = json.loads((event_dir / "glossary.json").read_text(encoding="utf-8"))
        assert written["track"] == "monza"
        drawer.get_by_text("Lesmo", exact=True).wait_for()

        drawer.get_by_label("New term").fill("Boxengasse")
        drawer.get_by_role("button", name="Add term").click()
        drawer.get_by_role("button", name="Save").click()
        # NOT another wait on "Saved.": the first save's notification is still
        # on screen at this point (Mantine keeps it for seconds, and these two
        # saves are a fraction of a second apart), so that text is already
        # true before this save has been sent - the wait would return at once
        # and the read below would race the PUT. Poll for the state this step
        # is actually about instead, the way the moments editor's own adopt
        # step in this file does.
        deadline = time.monotonic() + 5
        after: dict = {}
        while time.monotonic() < deadline:
            after = json.loads((event_dir / "glossary.json").read_text(encoding="utf-8"))
            if "Boxengasse" in after.get("terms", {}):
                break
            time.sleep(0.05)
        assert after["track"] == "monza", "an unrelated save cleared the circuit"
        assert after["terms"]["Boxengasse"] is True

    def test_the_editor_scrolls_at_a_short_viewport(
            self, studio_profile, event_dir, live_server, page, monkeypatch):
        """Standing acceptance criterion (CLAUDE.md): every pane must scroll
        to all its elements. The default alone is 32 terms and 10
        corrections, and this test's channel scope also carries its own
        three own terms plus one own correction, so Save must stay reachable
        at a laptop-short viewport without the page scrolling.

        glossary.DEFAULT_LAYER is EMPTY now (see TestGlossaryEditor's own
        docstring) - this test monkeypatches it to tracks.PACKS's Nordschleife
        pack, byte-identical to the old shipped default, so the 32-term/
        10-correction stress case this test exists to exercise is unchanged.

        Deviation from the brief here: the brief names "boyacht" as the last
        correction row. GlossaryEditor sorts each list own-first, then
        enabled-before-disabled, then alphabetically by key
        (glossaryLayers.sortRows) - and among the ten built-in corrections,
        "boyacht" sorts FIRST (a lower-case 'b' beats every other initial
        letter in that set), not last. The row that is actually alphabetically
        last is "shriver schwanz" - confirmed against the Nordschleife pack's
        own replacements directly rather than assumed."""
        from yt_shorts.workspace import Workspace

        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA"))
        monkeypatch.setattr(
            glossary_module, "DEFAULT_LAYER",
            tracks.as_layer(tracks.get("nurburgring-nordschleife")))

        page.set_viewport_size({"width": 1280, "height": 620})
        page.goto(f"{live_server}/erf")
        page.get_by_role("tab", name="Glossary").click()
        # Not "Corrections" (mixed case, as written in GlossaryEditor.tsx's
        # JSX): that heading carries `tt="uppercase"`, and a real browser's
        # innerText reflects the CSS-applied text-transform, rendering it as
        # "CORRECTIONS" - a case-sensitive .includes('Corrections') here
        # waits forever on a string that is genuinely never on the page.
        page.wait_for_function(
            "() => document.body.innerText.toLowerCase().includes('corrections')")

        # .last for the same reason step 2 above needs it: Moments' Save
        # button is also mounted (Mantine keeps inactive Tabs.Panel content
        # in the DOM), and Moments precedes Glossary in tab order.
        save = page.get_by_role("button", name="Save").last
        save.scroll_into_view_if_needed()
        assert save.is_visible()
        # The last correction row is reachable through the list's OWN scroll
        # container, not the page's.
        last = page.get_by_text("shriver schwanz", exact=True).first
        last.scroll_into_view_if_needed()
        assert last.is_visible()


class TestTrimJourney:
    """Set head and tail, watch the resulting duration update with no
    encoding at all, apply, and see the button flip. The preview being free
    is the whole point of the feature - if this test ever needs to wait for
    an encode before the duration text changes, the preview has regressed
    into a render."""

    def test_set_values_see_the_duration_and_apply(self, event_dir, live_server, page,
                                                   real_job_starters):
        # duration=10.0 to match the synthetic short below: the PATCH route's
        # trim validation checks the requested cut against clip.json's own
        # `duration` field (the harvested moment's span, see api.py's
        # patch_clip - `editorial.validate_trim(body.trim,
        # float(clip.get("duration", 0.0)), "trim")`), not the rendered
        # file's real length. The default fixture duration (6.0s) is too
        # short for a 2s+3s cut to pass that check at all.
        directory = clipstore.write_clip(
            event_dir, clip_entry(CLIP_URL, "Speedy!", duration=10.0))
        # A real 10-second video, so the player reports a real duration.
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=180x320:rate=10:duration=10",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=10",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(clipstore.short_path(directory))], check=True)

        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(editor_url(live_server))
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

        # The step a branch-level review caught: the player has now loaded
        # the ALREADY-CUT ~5.04s short.mp4, while the staged head/tail
        # (reset to the just-applied 2/3 by the apply flow) are still
        # ABSOLUTE cuts from the 10s master. Without reconstructing the
        # master's length from shortDuration + appliedTrim, the readout
        # double-counts the cut already made: 5.04 - 2 - 3 reads "0:00
        # after the cut" for a clip that in fact still has 5s remaining -
        # this must keep reading "0:05", exactly as it did before Apply
        # was ever clicked.
        page.get_by_text("0:05 after the cut").wait_for(timeout=5000)

        # The branch's own motivating scenario: "trim 3s, then change your
        # mind and trim 5s" - a LARGER second trim, entirely within what
        # the 10s master can still accommodate, must be OFFERED, not
        # blocked by a false floor error computed against the smaller,
        # already-cut file.
        page.get_by_label("Head (s)").fill("3")
        page.get_by_label("Tail (s)").fill("3")
        page.get_by_text("0:04 after the cut").wait_for(timeout=5000)
        second_apply = page.get_by_role("button", name="Apply trim")
        expect(second_apply).to_be_enabled(timeout=5000)
        assert page.get_by_text("Less than 3s would remain").count() == 0

    def test_upload_stays_disabled_for_as_long_as_the_apply_job_runs(
            self, event_dir, live_server, page, monkeypatch):
        """Regression test for a review finding: `handleApplyTrim` used to
        `await patchClip(...)` and throw the response away, only handing the
        new `clip.trim` up to App.tsx once the encode job finished (see
        ClipEditor.tsx's own comment on this at the call site). For the whole
        encode window - and forever, on a failed job, since `onUpdated` was
        never called at all in that case - `selectedClip.trim` stayed at its
        old value, so UploadPanel's `trimPending` guard (`isPending` against
        `clip.trim`/`clip.trim_applied`) read false and "Upload to YouTube"
        stayed enabled while the server would correctly 409 it.

        Sampling that window against a REAL encode would be exactly the kind
        of flaky test a reviewer would (rightly) reject: a real apply job
        takes several seconds (see the previous test in this class), and
        catching the disabled-but-should-be-enabled staleness would mean
        racing a poll against it. Instead this test controls the timing
        itself: the trim job is held "running" for as long as the test cares
        to ask - not a narrow window to be sampled, but a state held open on
        command. The PATCH that saves the trim is NOT faked - it hits the real
        backend, and is confirmed on disk - so what's actually exercised is
        whether the frontend updates `selectedClip` the moment that real PATCH
        resolves, which is the one thing the fix changes.

        The hold used to be two `page.route` fakes (`POST …/clips/*/trim`
        answering with a job id, `GET /api/jobs/*` answering "running"
        forever). Apply now ENQUEUES the cut instead (`POST /api/jobs`), so
        those two URLs are no longer the ones the click uses, and intercepting
        them would leave a real entry in the plan for the fixture's running
        worker to start for real. The hold moved to the starter itself, which
        is both the same technique the other job tests in this file use and
        one interception fewer."""
        directory = clipstore.write_clip(
            event_dir, clip_entry(CLIP_URL, "Speedy!", duration=10.0))
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.KEPT, transcript=None))
        # No real encode needed for this test: has_short is a plain stat()
        # on this path (see api.py's _short_version) and the apply job
        # itself is faked below, so these bytes are never actually read by
        # anything real.
        clipstore.short_path(directory).write_bytes(b"not a real video")

        page.goto(editor_url(live_server))
        page.get_by_role("button", name="Speedy!").click()

        upload_button = page.get_by_role("button", name="Upload to YouTube")
        upload_button.wait_for()
        assert upload_button.is_enabled(), "no trim staged yet - must start enabled"

        page.get_by_label("Head (s)").fill("2")
        page.get_by_label("Tail (s)").fill("3")

        # Hold the queued trim at "running" for as long as this test wants -
        # no ffmpeg, no re-encode, nothing touched on disk. The worker really
        # does claim the entry and really does call this; only the work
        # inside it is a stand-in.
        hold = threading.Event()

        def fake_start_trim_job(profile, job_store, name, *, cancel=None):
            job = job_store.create(kind="trim")

            def run():
                hold.wait(20)
                job.record(name, "done", None, "done: trim")
                job.finish("done")

            threading.Thread(target=run, daemon=True).start()
            return job

        monkeypatch.setattr(studio_jobs, "start_trim_job", fake_start_trim_job)

        try:
            page.get_by_role("button", name="Apply trim").click()
            self._assert_upload_stays_disabled(page, directory, upload_button)
        finally:
            hold.set()  # let the stubbed job finish even if an assert fails

    @staticmethod
    def _assert_upload_stays_disabled(page, directory, upload_button):
        """The assertions themselves, in their own method only so the caller
        can hold the stubbed job open in a `try`/`finally` around them."""
        # Confirm the REAL PATCH actually landed - the job is faked, but the
        # save that precedes it is not.
        deadline = time.monotonic() + 5
        edit: dict = {}
        while time.monotonic() < deadline:
            edit = json.loads((directory / editorial.EDIT_FILENAME).read_text())
            if edit.get("trim") == [2.0, 3.0]:
                break
            time.sleep(0.05)
        assert edit.get("trim") == [2.0, 3.0], edit

        # The fake job never leaves "running", so this is not a narrow
        # instant to be caught mid-flight - it holds for as long as the test
        # keeps checking. On the unfixed code this fails outright:
        # selectedClip.trim never left its old value, so the button never
        # disables and this text never appears.
        expect(upload_button).to_be_disabled(timeout=5000)
        page.get_by_text(
            "A trim is pending for this clip - apply it above before uploading."
        ).wait_for(timeout=5000)

    def test_the_apply_button_reaches_the_repair_when_the_trim_state_is_unknown(
            self, event_dir, live_server, page, real_job_starters):
        """THE BLOCKER, in a real browser: short.full.mp4 (an untrimmed
        master) survives beside a cut-looking short.mp4 with no
        short.trim.json at all - the crash-between-cut-and-state-write
        window, or a deleted/corrupted sidecar. Before this fix the
        reviewer measured `BUTTON: Trim applied  enabled: False` and no
        pending warning here - `isPending(null, null)` reads false, so the
        one control that could reach the repair was disabled. Confirms the
        button is reachable, labelled for what it does, and that clicking
        it actually repairs the file: short.mp4 gets the master's bytes
        back and short.trim.json/short.full.mp4 are cleaned up - exactly
        `ensure_applied`'s own `unknown_but_mastered` -> desired-(0, 0)
        branch, which needs no real ffmpeg encode (see test_trim.py's
        TestUnknownStateWithAPresentMaster for the same branch unit-tested)."""
        directory = clipstore.write_clip(
            event_dir, clip_entry(CLIP_URL, "Speedy!", duration=10.0))
        clipstore.short_path(directory).write_bytes(b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"FULL-MASTER-BYTES")
        # No short.trim.json - the state this whole test targets.

        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(editor_url(live_server))
        page.get_by_text("Speedy!").first.click()

        apply_button = page.get_by_role("button", name="Repair trim")
        expect(apply_button).to_be_visible(timeout=5000)
        expect(apply_button).to_be_enabled()
        page.get_by_text(
            "This clip's applied trim state is unknown"
        ).wait_for(timeout=5000)

        apply_button.click()
        page.get_by_role("button", name="Trim applied").wait_for(timeout=15000)

        assert clipstore.short_path(directory).read_bytes() == b"FULL-MASTER-BYTES"
        assert not clipstore.short_master_path(directory).exists()
        assert not clipstore.short_trim_state_path(directory).exists()



@pytest.fixture
def provider_workspace(tmp_path, monkeypatch):
    """A per-test workspace root for the Settings screen's provider block.

    Deliberately NOT the session-scoped ``_fixed_workspace_root`` every other
    test in this file uses: this one writes a real key file into
    ``<root>/auth/``, and a key left behind in the shared root would make some
    later test's ``key_present is False`` assertion pass or fail depending on
    collection order. Same reasoning, and the same patch target, as
    tests/test_studio_api.py's own ``workspace`` fixture - ``studio/api.py``
    from-imports ``resolve``, so it is ``api._resolve_workspace`` that has to
    move, not ``workspace.resolve``.
    """
    from yt_shorts.workspace import Workspace
    root = tmp_path / "provider-workspace"
    (root / "channels").mkdir(parents=True)
    monkeypatch.setattr(
        api_module, "_resolve_workspace",
        lambda: Workspace(root=root, channels_dir=root / "channels", origin="test"))
    return root


class TestSettingsModelProvidersReachability:
    """The Settings screen's "Model providers" block, at a viewport too short
    to show the page whole.

    Scrolling is a mandatory acceptance criterion in this project, and this
    block is the newest thing on an already-long screen: every provider row,
    its Save button and the Remove button that only exists once a key is
    stored have to be reachable, and adding it must not have cut off what was
    already below it (the moments lexicon and glossary editors, which sit
    underneath).

    Driven by a REAL mouse wheel (``_wheel_scroll_until_visible``), never
    ``scroll_into_view_if_needed()`` - see that helper's own docstring for the
    measured reason: that call was proven here to "pass" on a build whose
    container no real wheel could move.
    """

    def _open(self, page, live_server):
        # Short on purpose - a tall viewport shows the whole screen and
        # exercises nothing.
        page.set_viewport_size({"width": 1280, "height": 600})
        page.goto(f"{live_server}/settings")
        page.get_by_text("Model providers").wait_for(timeout=10000)

    def test_every_provider_row_and_both_buttons_are_reachable(
            self, live_server, page, provider_workspace):
        from yt_shorts import providers as providers_module
        from yt_shorts.providers import anthropic_api
        # A stored key is what makes the Remove button exist at all.
        providers_module.save_api_key(
            provider_workspace / "auth", anthropic_api.KEY_FILENAME, "sk-test-key")

        self._open(page, live_server)
        viewport = page.viewport_size
        anchor = (viewport["width"] / 2, viewport["height"] / 2)

        # One row per registered provider, in the server's own order.
        save_buttons = page.get_by_role("button", name="Save key")
        assert save_buttons.count() == len(providers_module.ordered())

        # Remove FIRST, then the Save buttons top to bottom - the helper only
        # ever wheels DOWNWARD, and Remove sits in the first provider's row
        # (the only one holding a key here), so checking it after scrolling
        # past would test whether the wheel can go backwards rather than
        # whether the button is reachable.
        remove = page.get_by_role("button", name="Remove", exact=True)
        assert remove.count() == 1        # only the provider that has a key
        box = _wheel_scroll_until_visible(page, remove, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        for index in range(save_buttons.count()):
            box = _wheel_scroll_until_visible(page, save_buttons.nth(index), anchor, dy=250)
            assert _within_viewport(box, viewport), (index, box)

        # The field is never pre-filled: the server never returns a key, and
        # an input that looked populated would imply the studio holds
        # something it does not. Assert the COUNT first (M3) - a loop over
        # `.all()` passes trivially on zero elements, which is exactly what a
        # `PasswordInput` silently swapped for a `TextInput` would produce.
        password_inputs = page.locator('input[type="password"]').all()
        assert len(password_inputs) == len(providers_module.ordered())
        for value in password_inputs:
            assert value.input_value() == ""

    def test_the_editors_below_the_new_block_are_still_reachable(
            self, live_server, page, provider_workspace):
        # The other half of the acceptance criterion: adding a card must not
        # have starved or cut off what was already underneath it. These two
        # inputs are the last interactive things on the screen, inside the
        # moments-lexicon and glossary editors respectively.
        self._open(page, live_server)
        viewport = page.viewport_size
        anchor = (viewport["width"] / 2, viewport["height"] / 2)

        for label in ("New marker", "New term"):
            target = page.get_by_label(label, exact=True).first
            target.wait_for(timeout=10000)
            box = _wheel_scroll_until_visible(page, target, anchor, dy=250)
            assert _within_viewport(box, viewport), (label, box)
            assert box["height"] > 0, label


class TestUnverifiedProviderIsDisclosedInBothSurfaces:
    """An operator choosing a provider must be told it was never measured
    WHERE they choose it - the channel Brand editor - not only on the
    Settings screen they may never have opened. Same move `VERIFIED` already
    makes for correctness: a hidden risk turned into a disclosed one.

    The price disclosure rides along in the same section for the same reason
    (see providers.priceSentence): openai_api.DEFAULT_MODEL is deliberately
    not the cheapest entry in its own PRICES table, and an operator should
    learn what the choice costs at the moment of choosing rather than on a
    bill.

    SINCE 2026-07-31 NO SHIPPED PROVIDER IS UNVERIFIED - all three have been
    measured against their real service. So the unverified state is
    SYNTHESISED here, by patching one module's VERIFIED to False, rather than
    borrowing whichever provider happened to lag. That is deliberate and must
    stay: the disclosure is live code a fourth provider will arrive needing,
    and dropping these tests because nothing ships unverified today would
    leave it unpinned until the day it is wrong. `openai_api` is the module
    patched only because it was the last real instance; nothing here depends
    on that.
    """

    def _open_brand(self, page, live_server):
        page.set_viewport_size({"width": 1400, "height": 900})
        page.goto(f"{live_server}/{CHANNEL}")
        page.get_by_role("tab", name=re.compile("brand", re.I)).click()
        page.get_by_text("Moment detection").wait_for(timeout=10000)

    def test_choosing_an_unverified_provider_says_so_and_names_the_price(
            self, live_server, page, studio_profile, monkeypatch):
        # Patched BEFORE the page is opened: the route reads module.VERIFIED
        # per request, and the brand editor fetches /api/settings on load.
        from yt_shorts.providers import openai_api
        monkeypatch.setattr(openai_api, "VERIFIED", False)
        self._open_brand(page, live_server)

        # The select itself marks it, before anything is chosen…
        page.get_by_role("combobox", name="Provider").click()
        expect(page.get_by_role("option", name=re.compile("OpenAI"))).to_contain_text(
            "unverified")
        page.get_by_role("option", name=re.compile("OpenAI")).click()

        # …and the caveat is spelled out in full once it IS chosen.
        expect(page.get_by_text("Never measured")).to_be_visible()
        expect(page.get_by_text(re.compile("never been measured"))).to_contain_text("OpenAI")

        # Switching provider CLEARS the model field rather than pinning the
        # new provider's default into it (M2) - a save with the field left
        # blank must keep tracking that provider's own default rather than
        # freezing today's value into brand.json. The placeholder discloses
        # what would actually be used.
        model_field = page.get_by_role("textbox", name="Model")
        expect(model_field).to_have_value("")
        expect(model_field).to_have_attribute(
            "placeholder", "gpt-5.6-terra (the provider's default)")

        # The price, at the moment of choosing: the selected model's rate and
        # this provider's cheapest one beside it - as a FLOOR, never a total.
        price = page.get_by_text(re.compile(r"per 1M input / output tokens"))
        expect(price).to_contain_text("$2.00")
        expect(price).to_contain_text("$12.00")
        expect(price).to_contain_text("gpt-5.6-luna")
        expect(price).to_contain_text("$0.20")
        expect(price).to_contain_text("floor")

    def test_a_verified_provider_carries_no_caveat(self, live_server, page, studio_profile):
        # The other half of the mutation this test exists to catch: an
        # unconditional caveat would be as wrong as a missing one - it would
        # make "unverified" meaningless by attaching it to everything.
        self._open_brand(page, live_server)
        page.get_by_role("combobox", name="Provider").click()
        page.get_by_role("option", name=re.compile("Anthropic")).click()
        model_field = page.get_by_role("textbox", name="Model")
        expect(model_field).to_have_value("")
        expect(model_field).to_have_attribute(
            "placeholder", "claude-opus-5 (the provider's default)")
        expect(page.get_by_text("Never measured")).to_have_count(0)

    def test_settings_marks_the_unverified_providers_too(
            self, live_server, page, provider_workspace, monkeypatch):
        # No registered provider ships unverified since 2026-07-31, so one is
        # synthesised (see the class docstring) - otherwise the count assertion
        # below would read 0 against 0 and pass on a Settings page that had
        # stopped rendering the badge at all. What is pinned is still that the
        # count MATCHES the registry, never a literal number.
        from yt_shorts.providers import openai_api
        monkeypatch.setattr(openai_api, "VERIFIED", False)
        page.set_viewport_size({"width": 1280, "height": 2400})
        page.goto(f"{live_server}/settings")
        page.get_by_text("Model providers").wait_for(timeout=10000)
        from yt_shorts import providers as providers_module
        unverified = [m for m in providers_module.ordered() if not m.VERIFIED]
        assert unverified                      # the synthesis actually took
        expect(page.get_by_text("Never measured")).to_have_count(len(unverified))
        for module in unverified:
            expect(page.get_by_text(
                re.compile(f"{re.escape(module.PROVIDER_ID)}|"
                           "never been measured"))).not_to_have_count(0)

    def test_an_unrecognised_stored_provider_is_shown_not_blank(
            self, live_server, page, studio_profile):
        """M1 regression. brand.json can carry a `detect.provider` this build
        does not register at all - an old choice for a since-removed
        provider, or a hand edit - and Mantine's Select used to resolve that
        unmatched value to undefined, rendering the "Anthropic (the built-in
        default)" placeholder as if nothing had been chosen, while the model
        field beside it kept showing the real stored value. Worse, the
        caveat/price disclosure below silently vanished too, because it reads
        off the same lookup that just came back empty.

        `providerOptions` must give the select a synthetic entry for the
        unrecognised id so it shows the REAL stored value, and
        `unrecognizedProviderNote` must fill the disclosure spot with a
        plain "not recognised" note rather than leaving it blank.
        """
        brand_path = studio_profile.channel_dir / "brand.json"
        brand = json.loads(brand_path.read_text())
        brand["detect"] = {"provider": "mistral", "model": "mistral-large"}
        brand_path.write_text(json.dumps(brand))

        self._open_brand(page, live_server)

        provider_field = page.get_by_role("combobox", name="Provider")
        expect(provider_field).to_have_value("mistral")
        model_field = page.get_by_role("textbox", name="Model")
        expect(model_field).to_have_value("mistral-large")

        expect(page.get_by_text("Not recognised")).to_be_visible()
        expect(page.get_by_text(
            re.compile(r'"mistral" is not a provider this build recognises'))).to_be_visible()

        # The vanished-disclosure half: neither a caveat nor a price is
        # claimed for a provider this build knows nothing about - showing
        # either would be a guess dressed up as a fact.
        expect(page.get_by_text("Never measured")).to_have_count(0)
        expect(page.get_by_text(re.compile(r"per 1M input / output tokens"))).to_have_count(0)


@pytest.fixture
def live_queue_server(studio_profile, tmp_path, studio_server):
    """A live studio whose job queue is a fresh file this test owns, handed
    over together with the app object itself.

    Two things `live_server` cannot give the Jobs screen:

    - **a per-test plan.** `create_app()` points the queue at the
      SESSION-scoped workspace root (see tests/conftest.py's
      `_isolated_resolved_workspace`), so entries left behind would leak
      into whichever test collected next - the same reason
      tests/test_studio_api.py's `TestJobQueueRoutes` gives every test its
      own `jobs.json`.
    - **a worker nothing starts by itself.** `create_app` builds one and
      leaves it stopped, and this suite must keep it that way: a running
      worker would perform REAL whole-stream transcription, detection and
      rendering. Every test below that needs a RUNNING entry drives
      `drain_once()` by hand with the one relevant `studio.jobs.start_*_job`
      replaced by a stub - no network, no ffmpeg, no Whisper decode, no key
      and no money, exactly like the plain-HTTP queue tests. That is a
      structural guarantee rather than a habit now: `tests/conftest.py`'s
      autouse `_no_real_job_starter` refuses an unstubbed starter outright,
      and here - where the call happens on the live server's own thread -
      the refusal surfaces as that fixture's teardown check rather than as
      an exception in the test body.
    """
    app = create_app()
    queue = JobQueue(tmp_path / "jobs.json", studio_jobs.KINDS,
                     dict(studio_worker.DEFAULT_LIMITS))
    app.state.job_queue = queue
    app.state.worker = studio_worker.Worker(queue, app.state.job_store)
    with _serving(app, studio_server) as base_url:
        yield SimpleNamespace(url=base_url, app=app, queue=queue)


class TestJobsScreen:
    """The workspace-level Jobs screen (JobsScreen.tsx over GET /api/jobs and
    the seven queue routes), proven against the REAL built page.

    Four of the assertions here exist because the payload is easy to render
    wrongly in ways no type-check catches, and every one of them is recorded
    in `list_queue`'s own docstring and in web/src/jobs.ts:

    - `worker_running: false` is not "idle": `create_app` never starts a
      worker, so a plan can sit motionless with nothing wrong, and the
      screen has to SAY so.
    - a stop is ACCEPTED (202), not completed - the entry goes to
      `stopping`, and the warning must not promise otherwise.
    - `finished` arrives oldest-first despite the heading; the client
      reverses it.
    - nothing writes `Entry.progress`, so no progress reading may be
      invented on screen.

    Plus the one this task exists to close: before it, a stream with no
    cached transcript was a dead end - detection refuses it and the screen
    told the operator to send a POST by hand.
    """

    HERE = {"channel": CHANNEL, "event": EVENT}

    @staticmethod
    def _state(queue, entry_id: str) -> str:
        return next(e.state for e in queue.list() if e.id == entry_id)

    def _running(self, server, monkeypatch, kind, params=None):
        """One entry of `kind`, actually RUNNING, with its starter stubbed.

        The stub records the cancel token on the Job exactly as the real
        starters do - that is what a stop has to reach - and performs no
        work at all.
        """
        made = {}

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job(f"fake-{kind}", kind=kind)
            job.cancel = kwargs.get("cancel")
            made["job"] = job
            return job

        monkeypatch.setattr(studio_jobs, f"start_{kind}_job", fake_start)
        entry = server.queue.enqueue(kind, dict(self.HERE, **(params or {})))
        server.app.state.worker.drain_once()
        assert self._state(server.queue, entry.id) == "running", "the entry never started"
        return entry, made["job"]

    def test_a_queued_entry_is_listed_and_a_stopped_worker_is_said_plainly(
            self, live_queue_server, page):
        server = live_queue_server
        server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-a"))

        page.goto(f"{server.url}/jobs")

        expect(page.get_by_text("erf/studio-test · video_id=vid-a")).to_be_visible()
        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-a")
        expect(row.get_by_text("transcribe", exact=True)).to_be_visible()
        expect(row.get_by_text("queued", exact=True)).to_be_visible()

        # The worker was never started (create_app does not), so this plan
        # will not move. That has to be stated as a fact about the WORKER -
        # never as "idle", which is what an operator would otherwise read
        # into a queue that never advances.
        alert = page.get_by_text(re.compile(r"worker is not running", re.I)).first
        expect(alert).to_be_visible()
        body = page.locator("body").inner_text().lower()
        assert "idle" not in body
        assert "nothing to do" not in body
        # And it names what is stuck rather than merely that something is.
        expect(page.get_by_text(re.compile(r"1 queued entry will not start"))).to_be_visible()

    def test_a_row_with_nothing_to_report_renders_no_progress_at_all(
            self, live_queue_server, page, monkeypatch):
        server = live_queue_server
        # The running entry first, THEN the queued one: `drain_once` starts
        # everything it can, so an entry enqueued before it would be started
        # with its real starter - a genuine render, which this suite must
        # never perform.
        self._running(server, monkeypatch, "detect", {"video_id": "vid-p"})
        server.queue.enqueue("render", dict(self.HERE))

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · video_id=vid-p")).to_be_visible()

        # The detect above is RUNNING and has reported nothing yet (its
        # starter is a stub that never calls the callback), and the render
        # is merely queued. For both, the honest rendering is NOTHING - not
        # "0 of 0", and not an empty bar that reads as a broken column.
        assert "0 of 0" not in page.locator("body").inner_text()
        expect(page.locator(".mantine-Progress-root")).to_have_count(0)

    def test_a_trim_reports_nothing_however_far_along_it_is(
            self, live_queue_server, page, monkeypatch):
        # `jobs.KINDS["trim"].progress_unit` is None, so the worker hands a
        # trim no callback at all and its row can never carry a reading. A
        # cut is one ffmpeg invocation; "1 of 1" for it would be a
        # decoration, which is worse than silence.
        server = live_queue_server
        self._running(server, monkeypatch, "trim", {"clip": "clip-a"})

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · clip=clip-a")).to_be_visible()

        expect(page.locator(".mantine-Progress-root")).to_have_count(0)
        assert " of 1" not in page.locator("body").inner_text()

    def test_what_the_work_reports_is_what_the_row_says(
            self, live_queue_server, page, monkeypatch):
        """The whole route, end to end: the work counts, the worker adds the
        unit, the queue file holds it, `GET /api/jobs` serves it and the row
        renders it.

        The stub starter here calls the progress callback the worker handed
        it - which is exactly what `stream_transcribe` does per chunk - so
        nothing is decoded, downloaded or paid for.
        """
        server = live_queue_server
        made = {}

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job("fake-transcribe", kind="transcribe")
            job.cancel = kwargs.get("cancel")
            kwargs["progress"](20, 50)
            made["job"] = job
            return job

        monkeypatch.setattr(studio_jobs, "start_transcribe_job", fake_start)
        server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-prog"))
        server.app.state.worker.drain_once()

        page.goto(f"{server.url}/jobs")
        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-prog")
        expect(row.get_by_text("chunk 20 of 50")).to_be_visible()
        expect(row.locator(".mantine-Progress-root")).to_have_count(1)

    def test_a_finished_row_keeps_no_stale_reading(
            self, live_queue_server, page, monkeypatch):
        # "chunk 20 of 50" on a row that says `done` is a claim about a job
        # that is over - and this survives a restart, since it is read back
        # out of jobs.json.
        server = live_queue_server
        made = {}

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job("fake-transcribe-2", kind="transcribe")
            job.cancel = kwargs.get("cancel")
            kwargs["progress"](20, 50)
            made["job"] = job
            return job

        monkeypatch.setattr(studio_jobs, "start_transcribe_job", fake_start)
        server.queue.enqueue("transcribe", dict(self.HERE, video_id="vid-done"))
        server.app.state.worker.drain_once()
        made["job"].finish("done")
        server.app.state.worker.drain_once()   # reaps it

        page.goto(f"{server.url}/jobs")
        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-done")
        expect(row.get_by_text("done", exact=True)).to_be_visible()
        expect(row.get_by_text("chunk 20 of 50")).to_have_count(0)
        expect(row.locator(".mantine-Progress-root")).to_have_count(0)

    def test_the_progress_line_is_reachable_at_a_short_viewport(
            self, live_queue_server, page, monkeypatch):
        """Scrolling is an acceptance criterion, not a nicety.

        A reading that renders below the fold of a plan with a few rows in
        it is a readout nobody can read. Driven with a REAL mouse wheel (see
        `_wheel_scroll_until_visible`), never `scroll_into_view_if_needed()`.
        """
        server = live_queue_server
        started = []

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job(f"fake-detect-{len(started)}", kind="detect")
            job.cancel = kwargs.get("cancel")
            started.append(job)
            kwargs["progress"](7, 9)
            return job

        monkeypatch.setattr(studio_jobs, "start_detect_job", fake_start)
        # Every one of them RUNNING, so every row carries a reading: the
        # default `net` pool allows three at a time, and a queued row would
        # (rightly) show nothing at all.
        server.queue.set_limits({"cpu": 6, "net": 6})
        # More rows than a 600px-high window can show, the last one carrying
        # the reading this has to reach.
        for index in range(6):
            server.queue.enqueue("detect", dict(self.HERE, video_id=f"vid-s{index}"))
        server.app.state.worker.drain_once()

        page.set_viewport_size({"width": 1280, "height": 600})
        page.goto(f"{server.url}/jobs")
        target = page.locator(".mantine-Card-root").filter(
            has_text="video_id=vid-s5").get_by_text("window 7 of 9")
        expect(target).to_have_count(1)

        viewport = page.viewport_size
        # The test is only worth anything if the row really is below the
        # fold to start with - otherwise it would pass on a screen that
        # cannot scroll at all.
        assert not _within_viewport(target.bounding_box(), viewport), (
            "the last row was already on screen; this viewport no longer "
            "exercises scrolling at all")
        box = _wheel_scroll_until_visible(
            page, target, (viewport["width"] / 2, viewport["height"] / 2))
        assert _within_viewport(box, viewport), box

    def test_a_stop_is_warned_about_honestly_and_moves_the_entry_to_stopping(
            self, live_queue_server, page, monkeypatch):
        server = live_queue_server
        entry, job = self._running(server, monkeypatch, "detect",
                                   {"video_id": "vid-stop"})

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · video_id=vid-stop")).to_be_visible()
        page.get_by_role("button", name="Stop", exact=True).click()

        # The warning names WHEN it lands and WHAT has already been spent,
        # and promises neither speed nor a refund.
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("after the current window")
        expect(dialog).to_contain_text(re.compile(r"paid for", re.I))
        expect(dialog).to_contain_text(re.compile(r"refunds nothing", re.I))
        warning = dialog.inner_text().lower()
        assert "immediat" not in warning, warning

        page.get_by_role("button", name="Ask it to stop").click()

        # 202: ACCEPTED. The entry goes to `stopping` (not `stopped`) and
        # the token the work is checking really was reached.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._state(server.queue, entry.id) == "stopping":
                break
            time.sleep(0.05)
        assert self._state(server.queue, entry.id) == "stopping"
        assert job.cancel is not None and job.cancel.stop_requested is True
        assert job.cancel.kill_requested is False, "a plain stop must not kill"

        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-stop")
        expect(row.get_by_text("stopping", exact=True)).to_be_visible()
        # A stopping entry offers ONE thing: the escalation to a hard stop.
        # A second graceful stop 409s (it asks for nothing that was not
        # already asked for), a resume is not a transition the queue has,
        # and a remove is refused outright.
        expect(row.get_by_role("button", name="Stop", exact=True)).to_have_count(0)
        expect(row.get_by_role("button", name="Resume")).to_have_count(0)
        expect(row.get_by_role("button", name="Drop")).to_have_count(0)
        expect(row.get_by_role("button", name="Hard stop")).to_be_visible()

    def test_a_stopping_entry_can_still_be_escalated_to_a_hard_stop(
            self, live_queue_server, page, monkeypatch):
        """A graceful stop waits for the work's own safe point, which for a
        stream chunk or a rendered clip is minutes away - so escalating is a
        real thing an operator needs, and this screen used to withhold it.

        Not because it was the wrong control, but because the SERVER
        half-applied it: `Worker.request_stop(force=True)` requested the kill
        and only then called `mark_stopping`, which refuses any state but
        `running` - so the click performed the kill and reported a 409. That
        is fixed on the server (see tests/test_studio_worker.py and
        TestJobQueueRoutes), and this is the operator's end of it.
        """
        server = live_queue_server
        entry, job = self._running(server, monkeypatch, "detect",
                                   {"video_id": "vid-escalate"})
        server.app.state.worker.request_stop(entry.id)
        assert self._state(server.queue, entry.id) == "stopping"
        assert job.cancel.kill_requested is False

        page.goto(f"{server.url}/jobs")
        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-escalate")
        expect(row.get_by_text("stopping", exact=True)).to_be_visible()
        row.get_by_role("button", name="Hard stop").click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text(re.compile(r"terminates the subprocess", re.I))
        dialog.get_by_role("button", name="Hard stop").click()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if job.cancel.kill_requested:
                break
            time.sleep(0.05)
        assert job.cancel.kill_requested is True, (
            "the escalation never reached the token the work is checking")
        # And it was not reported as a refusal for something it performed.
        assert "Could not stop this job" not in page.locator("body").inner_text()
        assert self._state(server.queue, entry.id) == "stopping"

    def test_a_running_entry_cannot_be_dropped_but_a_queued_one_can(
            self, live_queue_server, page, monkeypatch):
        server = live_queue_server
        self._running(server, monkeypatch, "render")
        queued = server.queue.enqueue("trim", dict(self.HERE, clip="clip-a"))

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · clip=clip-a")).to_be_visible()

        running_row = page.locator(".mantine-Card-root").filter(has_text="after the current clip")
        expect(running_row.get_by_role("button", name="Drop")).to_have_count(0)
        expect(running_row.get_by_role("button", name="Stop", exact=True)).to_be_visible()

        # The queued one really can be dropped, and the drop reaches disk -
        # so the absence above is a rule about the state, not a screen that
        # simply never draws the button.
        queued_row = page.locator(".mantine-Card-root").filter(has_text="clip=clip-a")
        queued_row.get_by_role("button", name="Drop").click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(e.id != queued.id for e in server.queue.list()):
                break
            time.sleep(0.05)
        assert all(e.id != queued.id for e in server.queue.list())

    def test_recently_finished_is_shown_newest_first(self, live_queue_server, page):
        server = live_queue_server
        for index in (1, 2, 3):
            entry = server.queue.enqueue("trim", dict(self.HERE, clip=f"clip-{index}"))
            server.queue.claim_next()
            server.queue.mark_finished(entry.id, "done")

        # The payload's own order is queue order - oldest first - for this
        # section too; the heading says "recently finished", so the client
        # reverses it rather than the heading being reworded to match.
        with urllib.request.urlopen(f"{server.url}/api/jobs", timeout=5) as response:
            payload = json.loads(response.read())
        assert [row["params"]["clip"] for row in payload["finished"]] == [
            "clip-1", "clip-2", "clip-3"]

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · clip=clip-3")).to_be_visible()
        shown = page.get_by_text(re.compile(r"· clip=clip-\d")).all_text_contents()
        assert [text.split("clip=")[1] for text in shown] == ["clip-3", "clip-2", "clip-1"]

    def test_a_queued_entry_can_be_reordered_past_its_neighbour(
            self, live_queue_server, page):
        server = live_queue_server
        first = server.queue.enqueue("trim", dict(self.HERE, clip="clip-first"))
        second = server.queue.enqueue("trim", dict(self.HERE, clip="clip-second"))

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · clip=clip-second")).to_be_visible()
        row = page.locator(".mantine-Card-root").filter(has_text="clip=clip-second")
        row.get_by_role("button", name="Move up").click()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if [e.id for e in server.queue.list()] == [second.id, first.id]:
                break
            time.sleep(0.05)
        assert [e.id for e in server.queue.list()] == [second.id, first.id]

        # The row that is already first has nowhere to go, so its Move up is
        # disabled rather than sending a call that would reorder nothing.
        top = page.locator(".mantine-Card-root").filter(has_text="clip=clip-second")
        expect(top.get_by_role("button", name="Move up")).to_be_disabled()

    def test_every_row_and_control_is_reachable_at_a_short_viewport(
            self, live_queue_server, page):
        """Scrolling is an acceptance criterion, not a nicety - and this has
        to drive a REAL mouse wheel (see `_wheel_scroll_until_visible`'s own
        docstring): `scroll_into_view_if_needed()` was measured passing on a
        broken build in this repo, because it sets scrollLeft/scrollTop on
        any ancestor whose computed overflow is not `visible`, including one
        a real wheel could never move."""
        server = live_queue_server
        for index in range(14):
            server.queue.enqueue("trim", dict(self.HERE, clip=f"clip-{index:02d}"))

        page.set_viewport_size({"width": 900, "height": 500})
        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · clip=clip-00")).to_be_visible()
        viewport = page.viewport_size

        # No horizontal overflow: index.css's `body { overflow: hidden }`
        # removes the fallback scrollbar a plain page would have, so a row
        # pushed sideways would be unreachable outright.
        assert (page.evaluate("document.documentElement.scrollWidth")
                == page.evaluate("document.documentElement.clientWidth"))

        anchor = (viewport["width"] / 2, viewport["height"] / 2)
        last_row = page.locator(".mantine-Card-root").filter(has_text="clip=clip-13")
        box = _wheel_scroll_until_visible(page, last_row, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        # Its controls too, not just the card's top edge - a row whose
        # buttons sit below the fold is not actually usable.
        drop = last_row.get_by_role("button", name="Drop")
        assert _within_viewport(drop.bounding_box(), viewport), drop.bounding_box()

    def test_a_rows_log_link_is_built_from_the_job_id_not_the_entry_id(
            self, live_queue_server, page, monkeypatch):
        """`/api/jobs/{id}` addresses TWO disjoint id spaces - a
        `studio.jobs.Job` for `GET` and `…/log`, a queue `Entry` for
        `DELETE` and every `POST …/{id}/…` - minted by different code and
        never equal. Every mutating control on this screen uses the ENTRY's
        id; the log link is the one thing that must use `job_id`, and a link
        built from the wrong one 404s.

        `jobs.test.ts` pins `jobLogFile` itself, but nothing pinned the
        WIRING: rewiring `JobsScreen.tsx` to build the name from `entry.id`
        left all eight E2E cases green, which is the exact trap this screen's
        own docstring names. So this asserts on the URL the click produces.
        """
        server = live_queue_server
        entry, job = self._running(server, monkeypatch, "detect",
                                   {"video_id": "vid-log"})

        page.goto(f"{server.url}/jobs")
        expect(page.get_by_text("erf/studio-test · video_id=vid-log")).to_be_visible()
        row = page.locator(".mantine-Card-root").filter(has_text="video_id=vid-log")
        row.get_by_role("button", name="View log").click()

        # `<kind>-<job id>.log` - the name studio/jobs.py's `_open_job_log`
        # writes and the Logs screen lists.
        expect(page).to_have_url(re.compile(rf"/logs\?file=detect-{re.escape(job.id)}\.log$"))
        assert entry.id not in page.url, (
            f"the log link was built from the entry id {entry.id!r}, which "
            f"names no log file at all")

    def test_the_streams_tab_transcribe_button_queues_a_transcribe(
            self, live_queue_server, page, monkeypatch):
        """The per-row Transcribe button in the event screen's Streams tab -
        the one an operator working through a channel's streams actually
        reaches for.

        The committed E2E covered only `StreamScreen`'s alert button (below).
        Changing this button's `enqueueJob('transcribe', …)` to `'detect'`
        left the whole suite green, and a detect against a stream with no
        transcript is precisely what this branch made the route REFUSE - so
        the kind and the params are what this asserts, not that a click was
        possible.
        """
        from yt_shorts.youtube import Catalogue, Video

        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "ERF Race Part 9", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Race Part 9").wait_for()

        page.get_by_role("button", name="Transcribe", exact=True).click()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.queue.list():
                break
            time.sleep(0.05)
        entries = server.queue.list()
        assert [e.kind for e in entries] == ["transcribe"], (
            "the Streams tab queued the wrong kind of job")
        assert entries[0].params == {"channel": CHANNEL, "event": EVENT,
                                     "video_id": video_id}
        assert entries[0].state == "queued"
        # And the operator is told where it went, rather than left guessing
        # whether the click did anything.
        expect(page.get_by_text(re.compile(r"Transcription queued", re.I))).to_be_visible()

    def test_the_stream_screen_dead_end_now_queues_a_transcribe(
            self, live_queue_server, page):
        """The one thing this task must not leave undone. Detection refuses a
        stream with no cached transcript, the transcript route 404s, and the
        alert used to tell the operator to send `POST /api/jobs` by hand - a
        GUI instructing its operator to use an HTTP client. The button is the
        fix, and this proves it writes a real entry into the plan."""
        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}/streams/{video_id}")
        alert = page.get_by_text(re.compile(r"has a transcript only after"))
        expect(alert).to_be_visible()
        # The advice is a button now, not a curl command.
        assert "POST /api/jobs" not in page.locator("body").inner_text()

        page.get_by_role("button", name="Transcribe this stream").click()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            entries = server.queue.list()
            if entries:
                break
            time.sleep(0.05)
        entries = server.queue.list()
        assert [e.kind for e in entries] == ["transcribe"]
        assert entries[0].params == {"channel": CHANNEL, "event": EVENT,
                                     "video_id": video_id}
        assert entries[0].state == "queued"

        # …and this screen FOLLOWS that entry rather than forgetting it the
        # moment the POST answered, which is what it used to do. The badge,
        # the button's own label and the reason it has not started are the
        # three things a discarded entry could show none of - on the kind
        # whose job runs for hours.
        expect(page.get_by_text(re.compile(r"^Transcribe: queued$"))).to_be_visible()
        expect(page.get_by_role("button", name="Queued…")).to_be_visible()
        expect(page.get_by_text(re.compile(r"queued - not \w+ yet", re.I)).first
               ).to_be_visible()
        expect(page.get_by_text(re.compile(r"worker is not running", re.I)).first
               ).to_be_visible()
        # The claim the old notification made without ever reading
        # `worker_running` - false against this server, where nothing drains.
        body = page.locator("body").inner_text()
        assert "starts as soon as the worker is free" not in body
        assert "idle" not in body.lower()

        # And the operator is pointed at where it can be watched, which is
        # the screen this task added.
        page.get_by_role("button", name="Jobs", exact=True).click()
        expect(page.get_by_text(f"erf/studio-test · video_id={video_id}")).to_be_visible()

    def test_the_stream_screens_queued_state_is_reachable_at_a_short_viewport(
            self, live_queue_server, page):
        """Scrolling is an acceptance criterion, and this added new content
        ABOVE an alert an operator has to read and act on. Drives a real
        mouse wheel: this repo has measured `scroll_into_view_if_needed()`
        passing on a build where no wheel could reach the element at all."""
        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"

        page.set_viewport_size({"width": 900, "height": 420})
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}/streams/{video_id}")
        expect(page.get_by_role("button", name="Transcribe this stream")).to_be_visible()
        page.get_by_role("button", name="Transcribe this stream").click()
        expect(page.get_by_text(re.compile(r"^Transcribe: queued$"))).to_be_visible()

        viewport = page.viewport_size
        # Nothing may scroll SIDEWAYS - the badge row and its links are the
        # newest thing that could have widened this column.
        assert (page.evaluate("document.documentElement.scrollWidth")
                == page.evaluate("document.documentElement.clientWidth"))
        anchor = (viewport["width"] / 2, viewport["height"] / 2)
        note = page.get_by_text(re.compile(r"worker is not running", re.I)).first
        box = _wheel_scroll_until_visible(page, note, anchor, dy=200)
        assert _within_viewport(box, viewport), box
        # …and the Retry button under the alert is still reachable beneath it.
        retry = page.get_by_role("button", name="Retry")
        box = _wheel_scroll_until_visible(page, retry, anchor, dy=200)
        assert _within_viewport(box, viewport), box


class TestTheOtherButtonsGoThroughTheQueueToo:
    """Detect, Render and Trim are queue entries now, not jobs started on the
    click - and every one of them says so while it waits.

    The machinery for this shipped whole and unused: `enqueueJob` had exactly
    two call sites, both Transcribe buttons, so a detection run could still be
    neither scheduled nor paused and never appeared on the Jobs screen at all.

    Every test here runs against `live_queue_server`, whose worker is NOT
    started - which is the point rather than a convenience. It is the state an
    operator hits for real (any way of running the studio other than
    `bin/yt-shorts studio` leaves the worker stopped), and it is the one where
    a panel that flipped to a spinner on click would lie forever. So each case
    asserts three things: the right KIND with the right params reached the
    plan, the panel does not claim the work started, and it names WHY it has
    not. Two of those three survive a mutation the type-checker cannot see -
    swapping a kind, or dropping the explanation.

    `upload` is deliberately absent: it still starts directly (see
    api.ts's note where startDetect used to be, and `_validate_enqueue`).
    """

    @staticmethod
    def _first_entry(server, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entries = server.queue.list()
            if entries:
                return entries[0]
            time.sleep(0.05)
        raise AssertionError("nothing reached the plan - the click queued nothing")

    @staticmethod
    def _expect_the_stalled_worker_is_named(page):
        """The panel says it is queued and names the reason it has not moved.

        The wording is `jobs.ts`'s `stallNote`/`waitNote` and the two are held
        to the same rule: state it as a fact about the WORKER. "Idle" and
        "nothing to do" are precisely the wrong words for a plan that will
        never move, and this is the surface an operator meets first - they may
        never open the Jobs screen at all.
        """
        expect(page.get_by_text(
            re.compile(r"queued - not \w+ yet", re.I)).first).to_be_visible()
        expect(page.get_by_text(re.compile(r"worker is not running", re.I)).first).to_be_visible()
        body = page.locator("body").inner_text().lower()
        assert "idle" not in body
        assert "nothing to do" not in body

    def test_render_this_clip_queues_a_render_and_says_it_has_not_started(
            self, event_dir, live_queue_server, page):
        server = live_queue_server
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name="Speedy!").click()
        page.get_by_role("button", name="This clip").click()

        entry = self._first_entry(server)
        assert entry.kind == "render", "the render button queued the wrong kind"
        assert entry.params["channel"] == CHANNEL and entry.params["event"] == EVENT
        assert entry.params["clips"] == [
            next(d.name for d in clipstore.iter_clip_dirs(event_dir))]
        assert entry.state == "queued"

        # Nothing is rendering: with no worker, nothing ever will be.
        self._expect_the_stalled_worker_is_named(page)
        assert "Render started" not in page.locator("body").inner_text()
        # And the editor is NOT frozen for a render that has not begun -
        # freezing exists to keep edit.json fixed while a render READS it,
        # and a queued entry has read nothing.
        expect(page.get_by_role("textbox", name="Title")).to_be_enabled()

    def test_render_all_kept_queues_every_kept_clip(
            self, event_dir, live_queue_server, page):
        server = live_queue_server
        for hook in ("Speedy!", "Jegr Tunes"):
            directory = clipstore.write_clip(
                event_dir, clip_entry(f"{CLIP_URL}-{hook}", hook))
            editorial.save(directory, editorial.Edit(
                title=None, status=editorial.KEPT, transcript=None))

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name=re.compile(r"All kept \(2\)")).click()

        entry = self._first_entry(server)
        assert entry.kind == "render"
        assert sorted(entry.params["clips"]) == sorted(
            d.name for d in clipstore.iter_clip_dirs(event_dir))

    def test_detect_moments_queues_a_detect_and_says_it_has_not_started(
            self, event_dir, live_queue_server, page, monkeypatch):
        from yt_shorts.youtube import Catalogue, Video

        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "ERF Race Part 12", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Race Part 12").wait_for()
        page.get_by_role("button", name="Detect moments").click()

        entry = self._first_entry(server)
        assert entry.kind == "detect", "the Detect moments button queued the wrong kind"
        # `stream_title` is sent by the CLIENT: the direct route read it from a
        # process-lifetime cache an entry running hours later cannot rely on.
        assert entry.params == {"channel": CHANNEL, "event": EVENT,
                                "video_id": video_id,
                                "stream_title": "ERF Race Part 12"}
        assert entry.state == "queued"

        # The button says "Queued…", never "Detecting…" - nothing is
        # detecting, and with no worker nothing will be.
        expect(page.get_by_role("button", name="Queued…")).to_be_visible()
        expect(page.get_by_role("button", name="Detecting…")).to_have_count(0)
        self._expect_the_stalled_worker_is_named(page)

    def test_transcribe_queues_a_transcribe_and_says_it_has_not_started(
            self, event_dir, live_queue_server, page, monkeypatch):
        """The kind that runs LONGEST was the one with no feedback at all.

        Both Transcribe buttons enqueued and threw the entry away, so the
        panel showed no badge, no reason for the wait, no failure and no
        completion - and the notification asserted "It starts as soon as the
        worker is free" without ever consulting `worker_running`, which is
        precisely the sentence `waitNote` exists to prevent. Against this
        server that claim is false: the worker is not running, and nothing in
        this plan will start at all.
        """
        from yt_shorts.youtube import Catalogue, Video

        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "ERF Race Part 17", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Race Part 17").wait_for()
        page.get_by_role("button", name="Transcribe").click()

        entry = self._first_entry(server)
        assert entry.kind == "transcribe", "the Transcribe button queued the wrong kind"
        assert entry.params == {"channel": CHANNEL, "event": EVENT,
                                "video_id": video_id}
        assert entry.state == "queued"

        # The button says "Queued…", never "Transcribing…" - nothing is
        # decoding, and with no worker nothing will be.
        expect(page.get_by_role("button", name="Queued…")).to_be_visible()
        expect(page.get_by_role("button", name="Transcribing…")).to_have_count(0)
        expect(page.get_by_text(re.compile(r"^Transcribe: queued$"))).to_be_visible()
        self._expect_the_stalled_worker_is_named(page)
        # And the promise the old notification made is nowhere on the page.
        assert "starts as soon as the worker is free" not in \
            page.locator("body").inner_text()

    def _queue_a_detect(self, server, page, monkeypatch, title="ERF Race Part 13"):
        """Clicks Detect moments on a single fake stream. Returns its badge."""
        from yt_shorts.youtube import Catalogue, Video

        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue", lambda url, **k: Catalogue(
                videos=[Video(video_id, title, 60, 1)], playlists=[], failed_playlists=[]))
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text(title).wait_for()
        page.get_by_role("button", name="Detect moments").click()
        return re.compile(r"^Detect: queued$")

    def _queue_a_transcribe(self, server, page, monkeypatch, title="ERF Race Part 16"):
        """Clicks Transcribe on a single fake stream. Returns its badge."""
        from yt_shorts.youtube import Catalogue, Video

        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue", lambda url, **k: Catalogue(
                videos=[Video(video_id, title, 60, 1)], playlists=[], failed_playlists=[]))
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text(title).wait_for()
        page.get_by_role("button", name="Transcribe").click()
        return re.compile(r"^Transcribe: queued$")

    def _queue_a_render(self, server, event_dir, page):
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name="Speedy!").click()
        page.get_by_role("button", name="This clip").click()
        return re.compile(r"^queued$")

    def _queue_a_trim(self, server, event_dir, page):
        directory = clipstore.write_clip(
            event_dir, clip_entry(CLIP_URL, "Speedy!", duration=10.0))
        # `has_short` is a stat() on this path - these bytes are never read.
        clipstore.short_path(directory).write_bytes(b"not a real video")
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name="Speedy!").click()
        page.get_by_label("Head (s)").fill("2")
        page.get_by_label("Tail (s)").fill("3")
        page.get_by_role("button", name="Apply trim").click()
        # The trim panel draws no state badge of its own - the Alert is what
        # tells the operator the cut is queued.
        return None

    # Every panel that draws a "View log" link off a queue entry it is
    # following. It is an enumeration rather than one example on purpose: the
    # single detect case this used to be was justified by "that is the one
    # that already had a link", and that reason expired in the very commit
    # that gave RenderPanel one too - after which replacing `jobLogFile(entry)`
    # there with a link built from `entry.id` left the whole file green.
    # ClipEditor's trim panel is deliberately absent: it draws no log link at
    # all, so there is nothing here for it to get wrong. `transcribe` joined
    # the list when its two buttons finally started KEEPING their entry -
    # before that they threw it away on the spot, so the longest-running kind
    # in the tool had no badge, no link and no explanation to get wrong.
    LOG_LINK_PANELS = ["detect", "render", "transcribe"]

    @pytest.mark.parametrize("panel", LOG_LINK_PANELS)
    def test_a_queued_entry_offers_no_log_link_until_the_worker_claims_it(
            self, panel, event_dir, live_queue_server, page, monkeypatch):
        """`Entry.job_id` is null until the worker claims the entry, and
        `/api/jobs/{id}` addresses two disjoint id spaces - so a "View log"
        link built from anything else names no file at all and 404s. The
        honest rendering while an entry waits is NO link.

        Run once per panel that draws such a link, so a rewiring that reaches
        for `entry.id` instead of `job_id` fails in whichever one it happens
        in. Each case also pins that the panel is really SHOWING the entry
        (its state badge), because "no View log anywhere" would otherwise pass
        just as well on a panel that rendered nothing at all."""
        server = live_queue_server
        if panel == "detect":
            badge = self._queue_a_detect(server, page, monkeypatch)
        elif panel == "transcribe":
            badge = self._queue_a_transcribe(server, page, monkeypatch)
        else:
            badge = self._queue_a_render(server, event_dir, page)

        entry = self._first_entry(server)
        assert entry.job_id is None, "the worker is stopped; nothing can have claimed this"
        expect(page.get_by_text(badge)).to_be_visible()
        # No link, rather than a link to a file that does not exist.
        expect(page.get_by_text("View log")).to_have_count(0)
        # The way to it is the Jobs screen, which does exist.
        expect(page.get_by_text("Jobs").first).to_be_visible()

    @pytest.mark.parametrize("panel", ["detect", "render", "trim"])
    def test_a_held_event_lock_is_named_in_the_panel_that_asked_for_the_work(
            self, panel, event_dir, live_queue_server, page, monkeypatch):
        """The OTHER honest reason, and the one this class did not cover.

        `waitNote` has two branches worth a browser: the worker is not running
        (a dead end - every other test here), and another job holds this
        event's lock (normal and temporary, so the SERVER's own wording is
        passed through rather than reworded). Only the first was pinned end to
        end, which left the lock branch provable by unit test and by hand and
        by nothing that runs on its own.

        The lock is not really taken here: the worker records the reason on
        the entry while leaving it `queued`, and that round trip
        (`claim_next` -> `defer`) is driven directly, so nothing is started
        and no lock file is touched. `is_running` is faked True for the same
        reason - a stopped worker wins over a lock reason in `waitNote`, by
        design, and it is the lock's turn to be shown here."""
        server = live_queue_server
        # A stopped worker is checked FIRST and would hide this branch.
        monkeypatch.setattr(server.app.state.worker, "is_running", lambda: True)
        if panel == "detect":
            self._queue_a_detect(server, page, monkeypatch, title="ERF Race Part 14")
        elif panel == "render":
            self._queue_a_render(server, event_dir, page)
        else:
            self._queue_a_trim(server, event_dir, page)

        entry = self._first_entry(server)
        # Exactly what the worker does when `lock.EventLock` refuses: claim
        # it, fail to take the lock, put it back QUEUED with the reason.
        reason = (
            f"Event '{EVENT}' is locked by process 4242 - another render is "
            f"already running against this event")
        claimed = server.queue.claim_next()
        assert claimed is not None and claimed.id == entry.id
        server.queue.defer(entry.id, reason=reason)
        assert next(e for e in server.queue.list() if e.id == entry.id).state == "queued"

        # The panel names the lock, in the server's own words.
        expect(page.get_by_text(re.compile(r"queued - not \w+ yet", re.I)).first
               ).to_be_visible()
        expect(page.get_by_text("locked by process 4242")).to_be_visible()
        # And does NOT blame the worker, which is running.
        body = page.locator("body").inner_text().lower()
        assert "worker is not running" not in body
        assert "idle" not in body
        assert "nothing to do" not in body

    @pytest.mark.parametrize("panel", ["detect", "render", "trim", "transcribe"])
    def test_removing_a_queued_entry_hands_the_panel_back_to_the_operator(
            self, panel, event_dir, live_queue_server, page, monkeypatch):
        """A queued entry dropped on the Jobs screen must not leave the panel
        that queued it disabled for good.

        `allowedActions` offers `remove` on a `queued` entry, so this is an
        ordinary supported flow - queue a trim, change your mind, drop it -
        and `useQueuedJob` used to answer `pending: true` forever afterwards:
        the controls stayed disabled and the panel went on claiming work was
        queued, until a page reload. See that hook's own docstring and
        `useJobPolling`'s, which states the same rule for the job it follows.

        The removal is done through the QUEUE rather than by clicking the Jobs
        screen's own Remove button, so the panel under test never unmounts -
        navigating away and back would reset its state and hide the wedge
        entirely."""
        server = live_queue_server
        if panel == "detect":
            self._queue_a_detect(server, page, monkeypatch, title="ERF Race Part 15")
            # This one does not merely disable its control, it relabels it -
            # so "busy" is the absence of the button an operator can click.
            control = page.get_by_role("button", name="Detect moments")
            expect(page.get_by_role("button", name="Queued…")).to_be_visible()
            expect(control).to_have_count(0)
        elif panel == "transcribe":
            # Relabelled like the detect button beside it, for the same
            # reason - "busy" is the absence of the button that can be clicked.
            self._queue_a_transcribe(server, page, monkeypatch,
                                     title="ERF Race Part 18")
            control = page.get_by_role("button", name="Transcribe")
            expect(page.get_by_role("button", name="Queued…")).to_be_visible()
            expect(control).to_have_count(0)
        elif panel == "render":
            self._queue_a_render(server, event_dir, page)
            control = page.get_by_role("button", name="This clip")
            expect(control).to_be_disabled()
        else:
            self._queue_a_trim(server, event_dir, page)
            control = page.get_by_role("button", name="Apply trim")
            expect(control).to_be_disabled()

        entry = self._first_entry(server)
        expect(page.get_by_text(re.compile(r"queued - not \w+ yet", re.I)).first
               ).to_be_visible()

        server.queue.remove(entry.id)

        # The control comes back, and the panel says what became of the entry
        # rather than silently pretending it was never queued.
        expect(control).to_be_enabled()
        expect(page.get_by_text(re.compile(r"no longer in the plan", re.I))).to_be_visible()
        # And it stops claiming something is queued.
        expect(page.get_by_text(re.compile(r"queued - not \w+ yet", re.I))).to_have_count(0)

    def test_apply_trim_queues_a_trim_and_says_it_has_not_started(
            self, event_dir, live_queue_server, page):
        server = live_queue_server
        directory = clipstore.write_clip(
            event_dir, clip_entry(CLIP_URL, "Speedy!", duration=10.0))
        # No real encode is possible here (the worker is stopped) and none is
        # wanted: `has_short` is a stat() on this path, so these bytes are
        # never read by anything.
        clipstore.short_path(directory).write_bytes(b"not a real video")

        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name="Speedy!").click()
        page.get_by_label("Head (s)").fill("2")
        page.get_by_label("Tail (s)").fill("3")
        page.get_by_role("button", name="Apply trim").click()

        entry = self._first_entry(server)
        assert entry.kind == "trim", "the Apply trim button queued the wrong kind"
        assert entry.params == {"channel": CHANNEL, "event": EVENT,
                                "clip": directory.name}
        assert entry.state == "queued"

        # The cut has NOT been made - short.mp4 is untouched - and the panel
        # says so rather than spinning on a job that does not exist.
        assert clipstore.short_path(directory).read_bytes() == b"not a real video"
        self._expect_the_stalled_worker_is_named(page)

    def test_a_queued_render_is_reachable_and_readable_at_a_short_viewport(
            self, event_dir, live_queue_server, page):
        """Scrolling is an acceptance criterion, and the queued explanation is
        new content in an already-tall column - so it must not be what pushes
        the render controls off the bottom. Drives a REAL mouse wheel: this
        repo has measured `scroll_into_view_if_needed()` passing on a build
        where no wheel could reach the element at all."""
        server = live_queue_server
        clipstore.write_clip(event_dir, clip_entry(CLIP_URL, "Speedy!"))

        page.set_viewport_size({"width": 900, "height": 500})
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("button", name="Speedy!").click()
        page.get_by_role("button", name="This clip").click()
        self._first_entry(server)

        viewport = page.viewport_size
        assert (page.evaluate("document.documentElement.scrollWidth")
                == page.evaluate("document.documentElement.clientWidth"))
        anchor = (viewport["width"] / 2, viewport["height"] / 2)
        note = page.get_by_text(re.compile(r"worker is not running", re.I)).first
        box = _wheel_scroll_until_visible(page, note, anchor, dy=250)
        assert _within_viewport(box, viewport), box
        # And the buttons that put it there are still reachable underneath it.
        button = page.get_by_role("button", name="This clip")
        box = _wheel_scroll_until_visible(page, button, anchor, dy=250)
        assert _within_viewport(box, viewport), box

    @pytest.mark.parametrize("kind,button_name,unit,done,total", [
        ("detect", "Detect moments", "window", 7, 9),
        ("transcribe", "Transcribe", "chunk", 3, 8),
    ])
    def test_a_running_streams_tab_jobs_progress_is_shown(
            self, kind, button_name, unit, done, total, live_queue_server,
            page, monkeypatch):
        """StreamPanel's two longest-running kinds - detect (an hour of
        stream decode) and transcribe (up to eight hours) - used to render
        only a state badge and a log link, even though `Entry.progress` has
        carried a reading for both since the job queue grew a producer for
        it. `TrackedEntry` already held `work.entry` and already imported
        from jobs.ts, so `progressLabel(entry)` was a one-line addition -
        this pins that it is actually RENDERED here, the same "chunk N of M"
        / "window N of M" text JobsScreen already shows for the identical
        entry (see `test_the_progress_line_is_reachable_at_a_short_viewport`
        above)."""
        from yt_shorts.youtube import Catalogue, Video

        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "ERF Progress Stream", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job(f"fake-{kind}", kind=kind)
            job.cancel = kwargs.get("cancel")
            # Mirrors this file's own test_the_progress_line_is_reachable_
            # at_a_short_viewport: report a reading synchronously, before
            # the worker marks the entry running under the hood, exactly as
            # a real starter's own progress callback would from its thread.
            kwargs["progress"](done, total)
            return job

        monkeypatch.setattr(studio_jobs, f"start_{kind}_job", fake_start)

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Progress Stream").wait_for()
        page.get_by_role("button", name=button_name, exact=True).click()

        self._first_entry(server)
        server.app.state.worker.drain_once()

        expect(page.get_by_text(f"{unit} {done} of {total}")).to_be_visible()

    def test_the_streams_tab_progress_line_is_reachable_at_a_short_viewport(
            self, live_queue_server, page, monkeypatch):
        """Scrolling is a mandatory acceptance criterion for anything added
        to a screen. The progress line above is new content in an already-
        tall Streams tab, so it must not render out of reach on a short
        window. Real mouse wheel, never `scroll_into_view_if_needed()` - see
        `_wheel_scroll_until_visible`'s own docstring for why."""
        from yt_shorts.youtube import Catalogue, Video

        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: Catalogue(
                videos=[Video(video_id, "ERF Short Viewport Stream", 3661, 12345)],
                playlists=[], failed_playlists=[]))

        def fake_start(profile, job_store, *args, **kwargs):
            job = studio_jobs.Job("fake-detect", kind="detect")
            job.cancel = kwargs.get("cancel")
            kwargs["progress"](7, 9)
            return job

        monkeypatch.setattr(studio_jobs, "start_detect_job", fake_start)

        page.set_viewport_size({"width": 900, "height": 500})
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Short Viewport Stream").wait_for()
        page.get_by_role("button", name="Detect moments").click()

        self._first_entry(server)
        server.app.state.worker.drain_once()

        viewport = page.viewport_size
        anchor = (viewport["width"] / 2, viewport["height"] / 2)
        reading = page.get_by_text("window 7 of 9")
        box = _wheel_scroll_until_visible(page, reading, anchor, dy=250)
        assert _within_viewport(box, viewport), box


class TestPlaylistFilterAndBulkQueueing:
    """The Streams tab with a channel's back catalogue in it.

    Every test here runs against a studio whose worker is deliberately NOT
    running, like TestTheOtherButtonsGoThroughTheQueueToo: a click must
    leave an honest, explained queue entry, not a spinner.
    """

    def _catalogue(self):
        from yt_shorts.youtube import Catalogue, Playlist, Video
        return Catalogue(
            videos=[
                Video("vid-a", "ERF Race Part 1", 29975, 2200, ["PL1"]),
                Video("vid-b", "ERF Race Part 2", 29478, 1300, ["PL1"]),
                # In a playlist and NOT in the Streams tab - the case that
                # made two multi-hour ERF broadcasts unreachable.
                Video("vid-c", "ERF Special Catalunya 6H", 8983, 400, ["PL2"]),
                Video("vid-d", "ERF Loose Stream", 3600, 100, []),
            ],
            playlists=[Playlist("PL1", "2026 Season", 2, 0),
                       Playlist("PL2", "ERF Specials", 1, 2)],
            failed_playlists=[])

    def _serve(self, monkeypatch):
        catalogue = self._catalogue()
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: catalogue)

    def _wait_for_entries(self, server, count, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(server.queue.list()) >= count:
                break
            time.sleep(0.05)
        return server.queue.list()

    def test_the_filter_narrows_the_list_to_a_playlist(
            self, live_queue_server, page, monkeypatch):
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("ERF Race Part 1").wait_for()
        # The union, not the Streams tab's own total. Mantine's Select shows
        # its current option as an <input readonly>'s VALUE, not as a text
        # node, so this reads the value rather than get_by_text. The role is
        # needed too - the closed listbox shares the same aria-label.
        expect(page.get_by_role("combobox", name="Filter by playlist")).to_have_value(
            "All streams (4)")

        # Same ambiguity as above - the closed listbox shares the label, so
        # this needs the role too. "ERF Specials" carries 2 unavailable
        # members in this fixture (Playlist("PL2", "ERF Specials", 1, 2)
        # above), so its option label folds that count in too - a displayed
        # "(1)" is not silently a 1 that came from 3 because of those two
        # dropped, unavailable members.
        page.get_by_role("combobox", name="Filter by playlist").click()
        page.get_by_role("option", name="ERF Specials (1 + 2 unavailable)").click()

        expect(page.get_by_text("ERF Special Catalunya 6H")).to_be_visible()
        expect(page.get_by_text("ERF Race Part 1")).not_to_be_visible()

    def test_a_bulk_transcribe_queues_one_entry_per_stream_in_list_order(
            self, live_queue_server, page, monkeypatch):
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 2").check()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_role("button", name="Queue transcription for selected").click()

        entries = self._wait_for_entries(server, 2)
        assert [e.kind for e in entries] == ["transcribe", "transcribe"]
        # Catalogue order, NOT the order the boxes were ticked: the plan
        # should match the list the operator was looking at.
        assert [e.params["video_id"] for e in entries] == ["vid-a", "vid-b"]

    def test_transcribe_and_detect_chains_each_detect_behind_its_own_transcribe(
            self, live_queue_server, page, monkeypatch):
        """The whole point of the chained action, and the reason it needed
        no new queue mechanism: `Entry.after` already holds a dependent back
        until its dependency is done, and FAILS it if that never succeeds.
        A detect whose `after` pointed at the wrong transcribe - or at
        nothing - would run on an untranscribed stream."""
        self._serve(monkeypatch)
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_label("Select ERF Race Part 2").check()
        page.get_by_role("button", name="Queue transcription and detection for selected").click()

        entries = self._wait_for_entries(server, 4)
        by_id = {e.id: e for e in entries}
        detects = [e for e in entries if e.kind == "detect"]
        assert len(detects) == 2
        for detect_entry in detects:
            dependency = by_id[detect_entry.after]
            assert dependency.kind == "transcribe"
            assert dependency.params["video_id"] == detect_entry.params["video_id"], (
                "a detect was chained behind another stream's transcription")

    def test_a_queued_dependent_says_what_it_waits_for(
            self, live_queue_server, page, monkeypatch):
        """`waitNote`'s dependency branch, in a browser. Before it, a detect
        waiting on a transcription was told it was "next in line, and starts
        as soon as the worker has a free slot" - which a free slot does not
        do.

        `waitNote` checks a stopped worker FIRST (see its own docstring), so
        the whole class's default of a worker that never runs would only
        ever show that dead-end note and never reach the dependency branch
        this test exists to pin. `is_running` is faked True the same way
        `test_a_held_event_lock_is_named_in_the_panel_that_asked_for_the_work`
        already does for the identical reason - the queue itself stays
        undrained, so nothing actually starts."""
        self._serve(monkeypatch)
        server = live_queue_server
        monkeypatch.setattr(server.app.state.worker, "is_running", lambda: True)
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 1").check()
        page.get_by_role("button", name="Queue transcription and detection for selected").click()
        self._wait_for_entries(server, 2)

        expect(page.get_by_text(re.compile(
            r"waits for the transcribe job it depends on", re.I))).to_be_visible()

    def test_a_stream_that_already_has_a_transcript_is_skipped_and_said_so(
            self, live_queue_server, page, monkeypatch, _fixed_workspace_root):
        """A re-transcription re-downloads the stream's audio before it can
        even count its chunks - gigabytes for an 8-hour race. The bar says
        so before the click rather than after the download.

        `_fixed_workspace_root` (tests/conftest.py) is the root every app in
        this suite resolves to, and it is SESSION-scoped - so this seeds a
        UNIQUE video id rather than a fixed one, the same reason the other
        tests in this file mint ids with uuid4. A fixed `vid-a` here would
        leave a transcript on disk for every test that collected after it.
        """
        from yt_shorts.youtube import Catalogue, Playlist, Video
        server = live_queue_server
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video(video_id, "ERF Race Part 1", 29975, 2200, ["PL1"])],
            playlists=[Playlist("PL1", "2026 Season", 1, 0)],
            failed_playlists=[]))
        directory = _fixed_workspace_root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")

        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        # `get_by_text` is a case-insensitive SUBSTRING match by default, so
        # this would also match "transcription" - which the bulk bar renders
        # a few lines below, once a row is ticked. Exact, so a later addition
        # cannot turn this into a strict-mode error in a test that is about
        # the row's badge.
        expect(page.get_by_text("Transcript", exact=True)).to_be_visible()
        page.get_by_label("Select ERF Race Part 1").check()

        # The note renders beside BOTH action rows that carry the transcribe
        # leg - the transcribe-only one and the transcribe+detect one - so
        # `.first` would have kept passing if it stopped rendering beside
        # either. Counting alone is not enough either: `to_have_count(2)`
        # pins how MANY rows carry the note, never WHICH, so it passes just
        # as happily on a build that drops it from the combined row and
        # grows a spurious one beside detect-only - and that is precisely
        # the confusion this note exists to prevent, since the combined row
        # is the one where a PAID detection is queued for a video whose
        # transcription is skipped. So each row is asserted through the
        # button that names it, and the count then says "and nowhere else".
        pattern = re.compile(r"1 transcription skipped: already transcribed", re.I)
        for button_name in ("Queue transcription for selected",
                            "Queue transcription and detection for selected"):
            row = page.get_by_role(
                "button", name=button_name, exact=True).locator("xpath=..")
            expect(row.get_by_text(pattern)).to_be_visible()
        expect(page.get_by_text(pattern)).to_have_count(2)
        # And the button refuses rather than clicking into silence.
        transcribe = page.get_by_role("button", name="Queue transcription for selected")
        expect(transcribe).to_be_disabled()

    def test_a_failed_playlist_is_named_rather_than_silently_missing(
            self, live_queue_server, page, monkeypatch):
        from yt_shorts.youtube import Catalogue, FailedPlaylist, Video
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video("vid-a", "ERF Race Part 1", 29975, 2200, [])],
            playlists=[],
            failed_playlists=[FailedPlaylist("Bathurst 12 Hour", "HTTP 404")]))
        server = live_queue_server
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        expect(page.get_by_text("Bathurst 12 Hour")).to_be_visible()

    def test_the_bulk_bar_never_hides_the_last_row(
            self, live_queue_server, page, monkeypatch):
        """Reachability at a short viewport is an acceptance criterion here,
        not a nicety: the bar is a footer inside the panel, and a footer
        that grows can push the list's last rows out of reach.

        Driven with a real mouse WHEEL, never scroll_into_view_if_needed():
        that call was proven on this branch to pass on a broken build, by
        setting scrollLeft/scrollTop on an overflow:hidden ancestor no real
        wheel could ever move.

        The anchor is the stream list's OWN ScrollArea, not the viewport's
        centre: StreamPanel lives inside AppShell.Navbar, a fixed 400px-wide
        column on the left, so a wheel hovered over the middle of a
        1280px-wide viewport lands over AppShell.Main and moves nothing in
        the list at all.
        """
        from yt_shorts.youtube import Catalogue, Video
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video(f"vid-{n}", f"ERF Race Part {n}", 3600, 10, [])
                    for n in range(30)],
            playlists=[], failed_playlists=[]))
        server = live_queue_server
        viewport = {"width": 1280, "height": 520}
        page.set_viewport_size(viewport)
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_label("Select ERF Race Part 0").check()   # the bar appears

        last = page.get_by_text("ERF Race Part 29")
        stream_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="ERF Race Part 0")
        area_box = stream_area.bounding_box()
        anchor = (area_box["x"] + area_box["width"] / 2,
                  area_box["y"] + area_box["height"] / 2)
        box = _wheel_scroll_until_visible(page, last, anchor, dy=250)
        assert _within_viewport(box, viewport), box

    def test_a_dozen_failed_playlists_do_not_starve_the_list_or_the_alert(
            self, live_queue_server, page, monkeypatch):
        """A reviewer's real Playwright probe measured commit 221b022's
        one-line-per-failed-playlist Alert starving the stream list's
        ScrollArea to 12px at this suite's own short viewport (1280x520,
        same as test_the_bulk_bar_never_hides_the_last_row above) once 12
        playlists had failed - 40 real mouse-wheel steps moved the target
        row's bounding box by ZERO. 5 failed playlists did not reproduce it;
        12 is the count that did, hence 12 here.

        The fix is two-part - StreamPanel.tsx floors the stream list's
        ScrollArea (same 120px, same reasoning, as HitList.tsx's own fix for
        the sibling starvation this project already hit once) AND bounds the
        failed-playlists Alert itself in its own scrollable region, so a
        long failure list scrolls internally instead of growing without
        limit and pushing its own later entries off-screen. Both halves are
        pinned: the last stream row and the alert's own last failure entry
        must each be reachable by a real wheel, not merely present in the
        DOM.

        Driven with a real mouse WHEEL, never scroll_into_view_if_needed() -
        see _wheel_scroll_until_visible's own docstring for why that call
        was proven, on this branch, to pass against a broken build.
        """
        from yt_shorts.youtube import Catalogue, FailedPlaylist, Video
        monkeypatch.setattr(api, "channel_catalogue", lambda url, **k: Catalogue(
            videos=[Video(f"vid-{n}", f"ERF Race Part {n}", 3600, 10, [])
                    for n in range(30)],
            playlists=[],
            failed_playlists=[
                FailedPlaylist(f"Failed Playlist {n}", "HTTP 429") for n in range(12)
            ]))
        server = live_queue_server
        viewport = {"width": 1280, "height": 520}
        page.set_viewport_size(viewport)
        page.goto(f"{server.url}/{CHANNEL}/{EVENT}")
        page.get_by_role("tab", name="Streams").click()
        page.get_by_text("Failed Playlist 0").wait_for()

        # The stream list - same anchor/helper as
        # test_the_bulk_bar_never_hides_the_last_row above.
        last = page.get_by_text("ERF Race Part 29")
        stream_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="ERF Race Part 0")
        area_box = stream_area.bounding_box()
        anchor = (area_box["x"] + area_box["width"] / 2,
                  area_box["y"] + area_box["height"] / 2)
        box = _wheel_scroll_until_visible(page, last, anchor, dy=250)
        assert _within_viewport(box, viewport), box

        # The alert's own last failure entry - a second, independent scroll
        # region. "Failed Playlist 0" and "Failed Playlist 11" are each
        # unambiguous substrings (no other entry's text contains either as a
        # contiguous run), so no .first is needed.
        last_failure = page.get_by_text("Failed Playlist 11")
        alert_area = page.locator(".mantine-ScrollArea-viewport").filter(
            has_text="Failed Playlist 0")
        alert_box = alert_area.bounding_box()
        alert_anchor = (alert_box["x"] + alert_box["width"] / 2,
                        alert_box["y"] + alert_box["height"] / 2)
        box2 = _wheel_scroll_until_visible(page, last_failure, alert_anchor, dy=250)
        assert _within_viewport(box2, viewport), box2
