"""Command line: yt-shorts {harvest|render|gallery|migrate|upload} <channel>/<event>
                yt-shorts auth <channel>
                yt-shorts studio [--no-browser] [<channel>[/<event>]]
                yt-shorts detect <channel>/<event> <video-id>   scan a stream for moments
                yt-shorts install-tools [--update] [--yes]
                yt-shorts doctor
                yt-shorts --version
"""

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from yt_shorts import atomicwrite
from yt_shorts import clipstore
from yt_shorts import doctor
from yt_shorts import editorial
from yt_shorts import install_tools
from yt_shorts import logsetup
from yt_shorts import version
from yt_shorts import workspace
from yt_shorts.gallery import build_page
from yt_shorts.harvest import ClipEntry, harvest
from yt_shorts.lock import EventLock, LockError, StudioLock
from yt_shorts.migrate import (
    MigrationError, migrate_event, sync_channel_file, sync_channel_tree)
# yt_shorts.profile is deliberately NOT imported here: it resolves the
# workspace at import time (CHANNELS_DIR = workspace.resolve().channels_dir),
# and this file's own __main__ block below is what is supposed to turn a
# WorkspaceError into the understandable message + exit 2 __main__ already
# catches for it. Importing profile at module scope ran that resolution
# before __main__'s try/except ever got a chance to run, so a bad
# YT_SHORTS_DATA shipped as a raw traceback with exit code 1 - the
# carefully worded WorkspaceError message never reached the operator, and
# the try/except below was dead code. See main()'s own import of profile.
from yt_shorts.render import build_short, source_for_clip
from yt_shorts.subtitle_pipeline import make_subtitle_provider
from yt_shorts import trim
from yt_shorts import upload_record
from yt_shorts import upload_policy
from yt_shorts._google import GoogleUnavailable, require as google_require


def cmd_harvest(dir_: Path, ytdlp: str = "yt-dlp") -> int:
    """Queries the event's source list and writes one directory per clip.

    The source list is ``sources.json`` - the name the new (workspace)
    layout uses, and the one ``migrate`` writes. An event that has not been
    migrated yet (repository-fallback mode) may still only have the old
    layout's ``clip_urls.json``; that is read instead, so an un-migrated
    event keeps working. Neither existing is reported as an understandable
    error, exit code 2, rather than a raw FileNotFoundError - the first
    command of the documented workflow must not crash with a traceback on
    an event nothing has written a source list into.

    An entry already resolved without error is NOT queried again, but kept
    as it stands. Reason: a temporary disruption (rate limiting, network
    down) must never replace good timecodes with a failure - the subsequent
    render needs exactly this data, and a second harvest run should only
    ever IMPROVE the state, never make it worse. Only missing or previously
    failed entries are (re-)queried.

    Only clip.json is ever written here. edit.json belongs to the operator
    and is never touched by a derivation step, so a hand-edited title
    survives any number of harvest runs.

    A human can force a re-resolve for one clip by deleting its clip.json.

    write_clip() is called per entry inside its own try/except: an entry
    with no usable url (e.g. a source-list row missing "url" entirely)
    has no identity to be filed under and write_clip() refuses it with
    ClipStoreError. That must fail only THIS entry, the same as every
    other per-entry failure in this tool - not take the whole run down
    before entries after it ever get a chance to be written.
    """
    sources_path = dir_ / "sources.json"
    legacy_path = dir_ / "clip_urls.json"
    if sources_path.exists():
        path = sources_path
    elif legacy_path.exists():
        path = legacy_path
        print(f"NOTE: reading the old-layout {legacy_path.name}; "
              f"the current layout names this file sources.json",
              file=sys.stderr)
    else:
        print(f"ERROR: no source list found in {dir_}\n"
              f"Expected {sources_path.name} (or the older "
              f"{legacy_path.name}, before a migration).", file=sys.stderr)
        return 2

    try:
        inputs = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"ERROR: {path} is not valid JSON: {error}", file=sys.stderr)
        return 2

    # I7: unlike the old flat clips.json, which was rewritten from scratch
    # every harvest and so silently dropped an entry the moment it left
    # clip_urls.json, a clip's OWN directory is never deleted by any
    # derivation step here (see clipstore's own docstring: one directory,
    # never rewritten by anything but its owner). Removing an entry from
    # the source list therefore no longer removes the clip on its own -
    # that is reported below instead of silently re-adding the old
    # behaviour, which would mean a derivation step deleting data.
    input_urls = {e.get("url") for e in inputs if e.get("url")}

    existing: dict[str, dict] = {}
    for directory in clipstore.iter_clip_dirs(dir_):
        try:
            stored = clipstore.read_clip(directory)
        except clipstore.ClipStoreError:
            continue
        stored_url = stored.get("url")
        if stored_url and stored_url not in input_urls:
            print(f"NOTE: {directory.name} ({stored.get('hook', '?')!r}) is "
                  f"no longer in the source list. It is kept as-is, not "
                  f"re-downloaded or re-rendered on its own: delete "
                  f"{directory} yourself to remove it, or set "
                  f'"status": "discarded" in its edit.json to keep it on '
                  f"disk but exclude it from render and gallery.",
                  file=sys.stderr)
        if stored_url and not stored.get("error"):
            existing[stored_url] = stored

    to_harvest = [e for e in inputs if e.get("url") not in existing]
    harvest_iter = iter(harvest(to_harvest, ytdlp=ytdlp) if to_harvest else [])

    entries: list[ClipEntry] = []
    for input_ in inputs:
        stored = existing.get(input_.get("url"))
        if stored is not None:
            entries.append(ClipEntry(
                url=stored["url"], hook=stored["hook"],
                source_title=stored["source_title"], start=stored["start"],
                end=stored["end"], duration=stored["duration"], error=None))
        else:
            entries.append(next(harvest_iter))

    failed = False
    for entry in entries:
        try:
            clipstore.write_clip(dir_, asdict(entry))
        except clipstore.ClipStoreError as error:
            failed = True
            print(f"ERROR: {entry.hook}: {error}")
            continue
        if entry.error:
            failed = True
            print(f"ERROR: {entry.hook}: {entry.error}")
        else:
            print(f"{entry.duration:6.1f}s  {entry.hook}")
    return 1 if failed else 0


def cmd_render(dir_: Path, config: dict, footer: str) -> int:
    """Renders every clip in the store, guarded by an exclusive lock on the
    event directory (see yt_shorts.lock): a second concurrent render
    against the same event would collide on each clip's own raw.mp4,
    subs/ and short.mp4, so it refuses to start instead. The lock guards
    the whole command, acquired before anything below reads the clip
    store, and released on every way out - success, a per-clip failure
    (handled below, same as always), or an unexpected exception - never
    left held past this call.
    """
    lock = EventLock(dir_)
    try:
        lock.acquire()
    except LockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        return _cmd_render_locked(dir_, config, footer)
    finally:
        lock.release()


def _cmd_render_locked(dir_: Path, config: dict, footer: str) -> int:
    failed = []
    for directory in clipstore.iter_clip_dirs(dir_):
        # hook_display starts out as the directory name and is only
        # replaced by the real hook once one has been reliably determined
        # (clip.json read, edit.json read). That way, even a clip whose
        # clip.json or edit.json cannot be parsed still gets a usable name
        # in the summary message.
        hook_display = directory.name
        # Every candidate is handled ENTIRELY within this try block - a
        # broken entry (unreadable clip.json, invalid edit.json, a render
        # failure) must only fail THIS one clip, never the whole run. Same
        # approach as harvest.harvest() for the same problem. The reason
        # lands with exception type and message in the summary message
        # instead of being swallowed or aborting the run.
        try:
            clip = clipstore.read_clip(directory)
            if clip.get("error"):
                failed.append((hook_display, clip["error"]))
                continue
            edit = editorial.load(directory)
            if edit.status == editorial.DISCARDED:
                print(f"skipped (discarded): {directory.name}")
                continue

            hook = editorial.effective_title(edit, clip["hook"])
            hook_display = hook
            target = clipstore.short_path(directory)

            # The subtitle pipeline itself - transcribe, apply the
            # glossary, honour an editorial correction, group into
            # captions, build the alpha track - lives in
            # yt_shorts.subtitle_pipeline, not here: the studio's own job
            # runner (yt_shorts.studio.jobs) needs the exact same pipeline,
            # and two copies of it would drift out of sync invisibly. See
            # that module's own docstring for the full behaviour, unchanged
            # from what used to be a closure here.
            provider = make_subtitle_provider(directory, edit, clip, hook, config)

            # keep_raw=True: from here on the raw clip is a cache, not
            # scratch - the Stage C studio (see yt_shorts.preview) needs a
            # clean, caption-free frame to draw a live preview on, and
            # short.mp4 already has captions burned in. Costs roughly the
            # size of the downloaded source clip per rendered clip;
            # deleting a clip's raw.mp4 by hand is always safe, since a
            # later render just re-downloads it.
            build_short(source_for_clip(clip, edit), hook, footer,
                        str(target), config, str(directory),
                        keep_raw=True, subtitle_provider=provider)
            trim.forget_applied(directory)
            trim.ensure_applied(directory, edit)
            print("done:", directory.name)
        except Exception as error:
            failed.append((hook_display, f"{type(error).__name__}: {error}"))
            print("ERROR:", hook_display, file=sys.stderr)
    if failed:
        print(f"\n{len(failed)} candidate(s) failed:", file=sys.stderr)
        for hook, reason in failed:
            print(f"  {hook}: {reason.splitlines()[0]}", file=sys.stderr)
    return 1 if failed else 0


def cmd_gallery(dir_: Path) -> int:
    entries = []
    for directory in clipstore.iter_clip_dirs(dir_):
        short = clipstore.short_path(directory)
        if not short.exists():
            continue
        try:
            clip = clipstore.read_clip(directory)
        except clipstore.ClipStoreError:
            continue
        try:
            edit = editorial.load(directory)
        except editorial.EditError as error:
            print(f"NOTE: {directory.name}: unreadable edit.json "
                  f"({type(error).__name__}: {error})", file=sys.stderr)
            continue
        if edit.status == editorial.DISCARDED:
            continue
        entries.append({
            "file": f"{clipstore.CLIPS_DIRNAME}/{directory.name}/{short.name}",
            "hook": editorial.effective_title(edit, clip.get("hook", directory.name)),
        })
    target = dir_ / "index.html"
    atomicwrite.write_text(target, build_page(entries, dir_.name))
    print("written:", target)
    return 0


def _resolve_stream_title(video_id: str, channel_url: str | None, list_fn) -> str:
    """The stream's real title, or "" when it cannot be looked up.

    "" and NOT the video id, which is what this used to pass. The title is
    recorded in the analysis, and two things read it: the studio's stream
    screen shows it as the heading, and a clip created from a moment there
    carries it as `source_title` - which an upload description template
    interpolates. A video id in either place is a wrong artifact, and in the
    upload case a wrong artifact on the channel. The studio prefers the
    analysis's title over its own stream-list lookup, so a bad one here wins
    over a good one there; an EMPTY one lets that lookup through instead.

    A lookup failure is announced rather than swallowed - it costs the run
    nothing (detection scores a transcript, not a title), but an operator who
    later sees no heading should know why.
    """
    if not channel_url:
        return ""
    try:
        for stream in list_fn(channel_url):
            if stream.video_id == video_id:
                return stream.title
    except Exception as error:      # noqa: BLE001 - a title is not worth failing a run over
        print(f"note: could not look up the stream's title "
              f"({type(error).__name__}) - the analysis records none",
              file=sys.stderr)
        return ""
    print(f"note: {video_id} is not in this channel's stream list - "
          f"the analysis records no title", file=sys.stderr)
    return ""


def _report_progress(done: int, total: int) -> None:
    """The CLI's own `progress(done, total)` callback for `detect_moments` ->
    `moment_scan.scan`, printed for an operator watching a terminal.

    A progress callback must never break the work it reports on - see
    CLAUDE.md's "A reading must never cost the run". `scan` calls its three
    `progress(...)` sites OUTSIDE any try of its own, and a bare `print` here
    used to reach them unwrapped: `yt-shorts detect ... | head` closes stdout
    partway through a run, `print` then raises `BrokenPipeError`, and that
    escaped straight out of `scan`, out of `detect_moments`, and into
    `cmd_detect`'s own blanket `except Exception` (below) - aborting a paid
    scan after one reading and writing no `moments.json` at all. Measured,
    not theorised. One missed reading costs nothing here either; the run must
    not pay for it.
    """
    try:
        print(f"  window {done}/{total}")
    except Exception as error:  # noqa: BLE001 - a reading must never cost the run
        logging.getLogger("ytshorts.cli").debug(
            "progress %s/%s not reported (%s: %s)", done, total,
            type(error).__name__, error)


def cmd_detect(dir_: Path, config: dict, video_id: str, workspace_dir: Path,
               channel_url: str | None = None, detect_fn=None, list_fn=None) -> int:
    """Scans a stream for moments and writes streams/<id>/moments.json.

    Writes no clips - that happens in the studio when the operator picks a
    window - so unlike cmd_render this needs no EventLock for the clip store.
    It still takes the event lock, because a studio-started detect against the
    same event would write the same analysis file.

    `detect_fn` and `list_fn` are injected so this tests without a model, a
    key or a network.
    """
    from yt_shorts.detect import detect_moments
    from yt_shorts.youtube import list_streams

    run = detect_fn if detect_fn is not None else detect_moments
    lookup = list_fn if list_fn is not None else list_streams
    title = _resolve_stream_title(video_id, channel_url, lookup)
    lock = EventLock(dir_)
    try:
        lock.acquire()
    except LockError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        path = run(video_id, workspace_dir, config, stream_title=title,
                   progress=_report_progress)
    except Exception as error:      # noqa: BLE001 - the CLI reports the cause, it does not traceback at the operator
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        lock.release()
    print(f"wrote {path}")
    return 0


def cmd_migrate(identifier: str) -> int:
    """Copies an event from the repository layout into the workspace."""
    space = workspace.resolve()
    if space.origin == "repository":
        print("ERROR: no workspace configured - nothing to migrate into.\n"
              f"Create ~/{workspace.DEFAULT_DIR_NAME} or set "
              f"{workspace.ENV_VAR}, then run migrate again.", file=sys.stderr)
        return 2

    parts = identifier.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        print(f"ERROR: Identifier must have the form 'channel/event', "
              f"not: {identifier!r}", file=sys.stderr)
        return 2
    channel_name, event_name = parts
    old = workspace.REPO_CHANNELS / channel_name / "events" / event_name
    new = space.channels_dir / channel_name / "events" / event_name
    channel_level_copied: list[Path] = []

    try:
        # Verified by checksum like everything else this module copies (see
        # sync_channel_file's own docstring) - a plain shutil.copy2 with no
        # verification contradicted migrate.py's own documented guarantee.
        # An existing workspace copy that DIFFERS from the repository's is
        # left alone (never silently overwritten - it may be an operator's
        # own customization) but reported, rather than the previous
        # "skipped if the target exists at all" behaviour, which could not
        # tell a stale copy from an intentional one and reported neither.
        for name in ("channel.json", "brand.json", "layout.py"):
            source = workspace.REPO_CHANNELS / channel_name / name
            target = space.channels_dir / channel_name / name
            status = sync_channel_file(source, target)
            if status == "copied":
                channel_level_copied.append(target)
                print("copied:", target)
            elif status == "differs":
                print(f"NOTE: {target} already exists and differs from "
                      f"the repository's copy; left alone.", file=sys.stderr)

        for name in ("fonts", "assets"):
            source = workspace.REPO_CHANNELS / channel_name / name
            target = space.channels_dir / channel_name / name
            result = sync_channel_tree(source, target)
            for rel in result["copied"]:
                channel_level_copied.append(target / rel)
                print("copied:", target / rel)
            for rel in result["differs"]:
                print(f"NOTE: {target / rel} already exists and differs "
                      f"from the repository's copy; left alone.", file=sys.stderr)

        report = migrate_event(old, new)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    # Counts what was ACTUALLY written, not just migrate_event's own
    # Report.copied (sources.json, drafts, transcripts and the event's own
    # brand/fonts/layout overrides): the report used to omit the clip.json
    # written for every clip and the channel-level copies just above,
    # understating a real six-clip migration as "1 file(s) copied".
    print(f"migrated {report.clips} clip(s): {report.clips} clip.json "
          f"written, {len(channel_level_copied) + len(report.copied)} "
          f"file(s) copied and verified (sources.json, drafts, "
          f"transcripts, channel-level overrides)")
    print("raw/ is scratch (re-downloaded on render) and is deliberately "
          "not migrated.")
    for item in report.unmapped:
        print("NOTE: not mapped:", item, file=sys.stderr)
    print(f"\nThe original under {old} was NOT deleted. "
          f"Remove it yourself once you are satisfied.")
    return 0


STUDIO_PORT = 8765


def _studio_port(preferred: int, *, log=None) -> int:
    """The port to serve the studio on: the preferred one if free, else an
    OS-assigned free port. A stale studio still holding 8765 used to make the
    launch die with a raw "address already in use"; instead we move to a free
    port and say so. `log` is injected for testing."""
    import socket
    if log is None:
        def log(message):
            print(message, file=sys.stderr)

    def _is_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    if _is_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        chosen = probe.getsockname()[1]
    log(f"NOTE: port {preferred} is in use (another studio still running?); "
        f"using free port {chosen} instead.")
    return chosen


def _open_browser_soon(url: str) -> None:
    """Open the operator's browser at `url` shortly after this returns.

    uvicorn.run blocks, so a short-delay timer fires the open once the server
    is actually listening (the same way the racecast UI launches). Best-effort:
    a headless box with no browser just does nothing, webbrowser.open swallows
    that itself."""
    import threading
    import webbrowser
    threading.Timer(1.0, webbrowser.open, args=(url,)).start()


def cmd_studio(identifier: str | None = None, *, open_url=_open_browser_soon) -> int:
    """Starts the studio's local web server at the workspace level and opens it
    in the operator's browser.

    No profile is bound: the app lists the workspace's channels and resolves
    each event from the URL path (see yt_shorts.studio.api.create_app). With no
    identifier it opens the start screen; `channel` or `channel/event` is a
    convenience deep link into that screen. `open_url` is the browser opener,
    injected so tests assert the URL without launching a real browser.

    FastAPI and uvicorn are imported HERE, inside the command, not at module
    scope - the same reason yt_shorts.studio.api's own docstring gives:
    harvest/render/gallery/migrate must keep working in a venv that never
    installed them (see CLAUDE.md, "FastAPI stays optional"). An ImportError
    here is reported as what to install, not a traceback - the other four
    commands are unaffected either way.

    ONE STUDIO PER WORKSPACE, enforced here with `lock.StudioLock`. A busy
    port is worked around (`_studio_port` picks a free one - a stale studio
    must not brick the tool), so nothing else stops a second studio from
    starting against the same workspace, and two of them share one
    `jobs.json`: the second's own startup marks the first's running jobs
    `interrupted`, and either one's next write deletes what the other
    queued, silently. So the lock is taken BEFORE the app is built and
    released when the server exits. A workspace that will not resolve at all
    is not locked and not refused - `create_app` is deliberately best-effort
    about that (it serves everything but the queue), and there is no shared
    jobs.json to protect when there is no workspace.
    """
    try:
        import uvicorn
        from yt_shorts.studio.api import create_app
    except ImportError as error:
        print(
            "ERROR: the studio needs FastAPI and uvicorn, which are not "
            "installed in this venv.\n"
            "Install them with: .venv/bin/pip install fastapi uvicorn\n"
            "Every other command (harvest, render, gallery, migrate) works "
            f"without them.\n({error})",
            file=sys.stderr,
        )
        return 2

    try:
        studio_lock = StudioLock(workspace.resolve().root)
    except workspace.WorkspaceError:
        # No workspace, so no jobs.json two studios could fight over. The
        # app itself already handles this state (its queue is None and every
        # queue route says so); refusing to start here would take the whole
        # studio away over a queue nobody can use anyway.
        studio_lock = None
    if studio_lock is not None:
        try:
            studio_lock.acquire()
        except LockError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

    try:
        return _serve_studio(create_app(), uvicorn, identifier, open_url)
    finally:
        if studio_lock is not None:
            studio_lock.release()


def _serve_studio(app, uvicorn, identifier: str | None, open_url) -> int:
    """The serving half of `cmd_studio`, once the workspace is locked."""
    host, port = "127.0.0.1", _studio_port(STUDIO_PORT)
    where = f"/{identifier}" if identifier else "/"
    url = f"http://{host}:{port}{where}"
    print(f"Studio: {url}", file=sys.stderr)
    open_url(url)
    # THIS is where the job queue's worker thread starts, AND where the plan
    # is recovered (anything left running by a dead studio becomes
    # `interrupted`) - never in create_app(), which thousands of tests call
    # and none of which may acquire an event lock, spawn a thread or rewrite
    # the plan as a side effect (see yt_shorts/studio/worker.py's own
    # docstring). Stopped again on the way out, so the thread does not
    # outlive the server it belongs to; a job already running is unaffected
    # and is recovered on the next start.
    worker = getattr(app.state, "worker", None)
    if worker is not None:
        worker.start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if worker is not None:
            worker.stop()
    return 0


def cmd_auth(channel_name, channels_dir, auth_dir, *, oauth=None,
             require=google_require) -> int:
    """Authorizes a channel for upload: browser consent (operator's), token stored.

    The heavy Google boundary is `oauth` (defaults to the production adapter),
    injected so this tests without a real consent flow. A render-only channel
    (upload.mode=manual) has no token to store and is refused up front.
    """
    channel_dir = Path(channels_dir) / channel_name
    channel_json = channel_dir / "channel.json"
    if not channel_json.exists():
        print(f"ERROR: no channel.json for channel {channel_name!r}", file=sys.stderr)
        return 2
    # A render-only channel has no token to store - refuse before requiring
    # google or touching consent, and say what to do instead. cmd_auth runs
    # before profile.load, so it reads this channel's own brand.json directly
    # (event-independent); an absent brand.json means the default, api.
    brand_json = channel_dir / "brand.json"
    brand = (json.loads(brand_json.read_text(encoding="utf-8"))
             if brand_json.exists() else {})
    try:
        upload_policy.require_api_upload(brand)
    except upload_policy.RenderOnlyError as error:
        print(f"ERROR: {channel_name}: {error}", file=sys.stderr)
        return 2
    try:
        require("upload")
    except GoogleUnavailable as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    channel = json.loads(channel_json.read_text(encoding="utf-8"))
    channel_id = channel["id"]
    from yt_shorts.auth import AuthError, authorize
    if oauth is None:
        from yt_shorts.google_oauth import GoogleOAuth
        oauth = GoogleOAuth()
    try:
        authorize(channel_id, auth_dir=auth_dir, oauth=oauth)
    except AuthError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Connected channel {channel_id} ({channel.get('handle', '')}).")
    return 0


def cmd_upload(dir_, config, channel, auth_dir, channel_name, *,
               upload_one=None, now=None) -> int:
    """Uploads every kept, rendered, not-yet-uploaded clip as private.

    One failure never aborts the run (same guarantee as render). A render-only
    channel (upload.mode=manual) refuses the whole run up front - the Data API
    cannot upload to it. `upload_one` is the injected boundary that actually
    talks to YouTube; the default composes auth + service + metadata + insert +
    record + quota.
    """
    try:
        upload_policy.require_api_upload(config)
    except upload_policy.RenderOnlyError as error:
        print(f"ERROR: {channel_name}: {error}", file=sys.stderr)
        return 2
    if upload_one is None:
        upload_one = _default_cli_upload_one(config, channel, auth_dir, channel_name)
    failed = []
    uploaded = 0
    for directory in clipstore.iter_clip_dirs(dir_):
        name = directory.name
        try:
            clip = clipstore.read_clip(directory)
            edit = editorial.load(directory)
            if edit.status != editorial.KEPT:
                continue
            if not clipstore.short_path(directory).exists():
                print(f"skipped (not rendered): {name}", file=sys.stderr)
                continue
            if trim.is_pending(directory, edit):
                # Mirrors the studio's own post_upload guard (api.py): a
                # saved edit.trim with no matching short.trim.json means
                # short.mp4 is still the untrimmed render - uploading it
                # would ship the wrong video the moment a studio apply job
                # fails (an over-trim, an ffmpeg failure) after the operator
                # already PATCHed the trim in, and this command later runs
                # against the same clip.
                print(f"skipped (trim not applied): {name}", file=sys.stderr)
                continue
            if upload_record.is_uploaded(directory):
                print(f"skipped (already uploaded): {name}", file=sys.stderr)
                continue
            record = upload_one(directory, clip, edit)
            uploaded += 1
            print(f"uploaded (private): {name} -> {record['url']}")
        except Exception as error:  # noqa: BLE001 - one failure never aborts the run
            failed.append((name, f"{type(error).__name__}: {error}"))
            print(f"ERROR: {name}", file=sys.stderr)
    print(f"\n{uploaded} uploaded, {len(failed)} failed.", file=sys.stderr)
    for name, reason in failed:
        print(f"  {name}: {reason.splitlines()[0]}", file=sys.stderr)
    return 1 if failed else 0


def _default_cli_upload_one(config, channel, auth_dir, channel_name):
    """The production upload boundary for cmd_upload (composes the real pieces)."""
    from datetime import datetime, timezone

    def upload_one(directory, clip, edit):
        from yt_shorts.auth import load_credentials
        from yt_shorts.google_oauth import GoogleOAuth, build_service
        from yt_shorts.quota import QuotaTracker
        from yt_shorts.youtube_upload import build_metadata, upload_short
        channel_id = channel["id"]
        creds = load_credentials(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth())
        if creds is None:
            raise RuntimeError(
                f"channel {channel_id} is not connected; run: bin/yt-shorts auth "
                f"{channel_name}")
        service = build_service(creds)
        metadata = build_metadata(clip, edit, config)
        result = upload_short(clipstore.short_path(directory), metadata, service=service)
        when = datetime.now(timezone.utc).isoformat()
        upload_record.save(directory, result.video_id, result.url, "private", when=when)
        QuotaTracker(auth_dir, channel_id).book_insert(datetime.now(timezone.utc))
        return {"video_id": result.video_id, "url": result.url}

    return upload_one


COMMANDS = {"harvest", "render", "gallery", "migrate", "studio", "auth", "upload", "detect",
           "install-tools", "doctor"}

# Which of the three global-looking flags each command actually accepts. Any
# other command silently ACCEPTED and IGNORED all three before this existed -
# `yt-shorts render erf/ev --update` rendered, refreshed no yt-dlp, and said
# nothing, which is the precise failure the managed yt-dlp exists to prevent.
# A command absent from this dict accepts none of them.
COMMAND_FLAGS = {
    "studio": {"--no-browser"},
    "install-tools": {"--update", "--yes"},
}
_FLAG_NAMES = ("--no-browser", "--update", "--yes")


def main(argv: list[str] | None = None) -> int:
    """The command line, as one callable. Returns an exit code rather than
    raising SystemExit, so `console_scripts` and PyInstaller can both call it
    and so tests can assert on the code without catching an exception."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("--version", "-V"):
        print(f"yt-shorts {version.resolve()}")
        return 0

    # Strip the three flags BEFORE the per-command argument counts below, so
    # `studio --no-browser erf` still reads `erf` as the identifier - but
    # remember which ones were actually present, so a command that does not
    # accept a given flag can refuse it below rather than silently ignoring
    # it (see COMMAND_FLAGS).
    no_browser = "--no-browser" in args
    update = "--update" in args
    assume_yes = "--yes" in args
    flags_present = {a for a in args if a in _FLAG_NAMES}
    args = [a for a in args if a not in _FLAG_NAMES]

    command = args[0] if args else None
    if command not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    disallowed = flags_present - COMMAND_FLAGS.get(command, set())
    if disallowed:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    video_id = None  # only `detect` sets it

    # `install-tools` and `doctor` are workspace-level and take NO identifier
    # - `yt-shorts doctor erf/typo` used to exit 0 having silently ignored
    # the argument. `studio` alone keeps 0-or-1 (no arg opens the start
    # screen; `channel[/event]` is a deep link). Every other command needs
    # exactly one channel/event.
    if command in ("install-tools", "doctor"):
        if len(args) != 1:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        identifier = None
    elif command == "studio":
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

    # A yt-dlp that `install-tools` downloaded into <workspace>/.tools is not on
    # the operator's shell PATH, so nothing that shells out by bare name would
    # find it. One call, here, before anything spawns a subprocess. The studio
    # inherits it: cmd_studio runs uvicorn IN THIS PROCESS and its jobs run on
    # this process's threads.
    install_tools.ensure_tool_path(space.root)

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

    # install-tools runs before profile_load: it is workspace-level and about
    # the machine, not about any one event.
    if command == "install-tools":
        return install_tools.run(space.root, update=update, assume_yes=assume_yes)

    # doctor runs before profile_load: it answers "can this machine run the
    # tool at all", which has nothing to do with any one event's profile.
    if command == "doctor":
        return doctor.report(doctor.checks(space.root))

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
