"""The studio's HTTP surface: a workspace's channels, events and editorial layer.

``create_app()`` builds a FastAPI app at the WORKSPACE level - no bound
profile. It launches with ``bin/yt-shorts studio`` (no event) to a start
screen; each request names its channel (and event) in the path, and the
event routes resolve their ``yt_shorts.profile.Profile`` from that path via
``_load_profile`` (a 404 if the channel/event is unknown or the profile is
malformed). This is a local, single-user tool run on an operator's own
machine - there is no login and no per-user data.

The one rule every route here answers to (see CLAUDE.md and
``yt_shorts.editorial``'s own docstring): **the studio writes edit.json and
nothing else, with two narrow carve-outs.** A route may READ clip.json and
transcript.json - both are derived data, safe to look at - but the only
*editorial* write path is ``editorial.save``. The two carve-outs are a
render the operator explicitly starts (``studio.jobs``, under the same
``EventLock`` ``cmd_render`` takes) and, since the stream view, a clip the
operator explicitly creates from a chosen window (``POST
…/streams/{video_id}/clips``, via ``clip_from_moment.create_clip``, under
that same lock). No route in this module edits an existing transcript.json
or sources.json itself; the one write to a transcript.json that a studio
render can reach is transcribe()'s own cache refresh on a source mismatch,
which is shared with cmd_render and documented in CLAUDE.md's boundary
section. clip.json is a narrower case than the other two: `create_clip`
(via `clipstore.write_clip`) DOES rewrite an existing clip.json in place
when the operator re-picks the SAME window - an ordinary, idempotent
correction to a hook or title, never to the window itself, which is what
the identity is keyed on (see clip_from_moment.py's own docstring) - but
refuses (`ClipIdentityCollision`) rather than overwriting when a
DIFFERENT window rounds to the same identity. The two carve-outs differ
in what they may do to their OWN output - a render may replace the short
it was asked to build (re-rendering is ordinary; render.compose makes the
replacement atomic), while clip creation may update its own clip.json on
an exact re-pick but refuses a colliding one rather than redefining it.
See CLAUDE.md's boundary section, which spells out why this cannot be
compressed into one "never".
G1 is read-only navigation: no route creates, edits or deletes a channel or
an event.

Routes (channel = a channel name, event = one of its events):
  GET   /api/channels                                       every channel, summarised
  GET   /api/channels/{channel}/events                      one channel's events, with counts
  GET   /api/channels/{channel}/auth                        this channel's YouTube connection + upload class
  POST  /api/channels/{channel}/auth/connect                start the OAuth consent job (api channels only)
  GET   /api/channels/{channel}/brand/palette                colours proposed from the channel logo
  GET   /api/channels/{channel}/events/{event}/clips        every clip, summarised
  GET   …/clips/{name}                one clip, plus its effective words
  PATCH …/clips/{name}                set title/status/words - edit.json only
  GET   …/clips/{name}/preview        a PNG frame of the SAVED state (see yt_shorts.preview)
  POST  …/clips/{name}/preview        a PNG frame of words the CLIENT is holding but has not
                                       saved yet - same picture, same rules, just fed an
                                       unsaved word list instead of reading edit.json's
  GET   …/clips/{name}/short          streams short.mp4; ?as=download is refused (409)
                                       while a trim is pending (see get_short's own
                                       comment - the plain URL never is, so the player
                                       can still preview the cut)
  POST  …/clips/{name}/trim           start a job that applies the clip's pending
                                       trim to short.mp4 (409 with no short yet)
  POST  …/clips/{name}/upload         start an upload job (api channels only; refused
                                       while a trim is pending, same as the download
                                       form above)
  GET   …/clips/{name}/upload-preview the exact metadata an upload would send
  GET   …/streams                     the channel's streams (yt-dlp), cached per channel
  POST  …/streams/{video_id}/detect   start a moment-detection job
  GET   …/streams/{video_id}/moments  the stream's detection analysis, or an empty one if
                                       detection never ran (200, not 404 - see the route)
  GET   …/streams/{video_id}/transcript the stream's whole transcript (404 if there is none)
  POST  …/streams/{video_id}/estimate what a detection run over this stream would cost -
                                       local and approximate, no key or network needed
                                       (404 if there is no transcript yet)
  POST  …/streams/{video_id}/clips    create a clip from an operator-chosen window (see
                                       CLAUDE.md's amended write boundary, and the note below)
  POST  …/render                      start a background render job (see yt_shorts.studio.jobs)
  GET   /api/jobs/{job_id}            polls a job (job ids are workspace-global)
  GET   /api/jobs                     the plan: running, queued and recently finished,
                                       plus the pool limits (see yt_shorts.job_queue)
  POST  /api/jobs                     enqueue one unit of work (kind plus params)
  POST  /api/jobs/{id}/stop           ask the running job to stop (202); ?force=true is a
                                       hard stop, refused (409) where the kind has none
  POST  /api/jobs/{id}/pause          hold a queued entry without losing its place
  POST  /api/jobs/{id}/resume         put a paused entry back in line, where it was
  POST  /api/jobs/{id}/move           reorder a queued entry
  POST  /api/jobs/{id}/retry          re-queue a failed, interrupted or stopped entry
  DELETE /api/jobs/{id}               drop a queued entry (409 on a running one - stop it)
  PUT   /api/settings/limits          how many jobs each pool runs at once (workspace-level)
  GET   /api/workspaces                                     current + recent workspaces
  POST  /api/workspaces/switch                              re-root onto an existing workspace
  POST  /api/workspaces/create                              create a workspace, then re-root onto it
  POST  /api/workspaces/copy                                clone the current workspace as a background job
  GET   /api/fs                                             lists directories, for the workspace picker
  GET   /api/moments                                        the workspace's moments lexicon layer
  PUT   /api/moments                                        overwrite the workspace layer
  GET   /api/channels/{channel}/moments                     the channel's lexicon layer, workspace inherited
  PUT   /api/channels/{channel}/moments                      overwrite the channel layer
  GET   …/events/{event}/moments      the event's lexicon layer, channel inherited
  PUT   …/events/{event}/moments      overwrite the event layer
  POST  /api/moments/adopt-default                           copy the built-in default into the workspace layer
  GET   /api/glossary                                       the workspace's glossary layer
  PUT   /api/glossary                                       overwrite the workspace layer
  GET   /api/channels/{channel}/glossary                    the channel's layer, workspace inherited
  PUT   /api/channels/{channel}/glossary                    overwrite the channel layer
  GET   …/events/{event}/glossary     the event's layer, channel inherited
  PUT   …/events/{event}/glossary     overwrite the event layer
  GET   /api/tracks                                         every venue in the shipped registry, id and name
  PUT   /api/providers/{provider_id}/key                    store this provider's API key (0600, under
                                                             <workspace>/auth/); answers booleans only -
                                                             the key is never returned, logged or quoted
  DELETE /api/providers/{provider_id}/key                   forget it again (404 if none was stored)
  GET   /{full_path}                  the built React/Vite/Mantine page, from ./static/, for any
                                       non-/api path (SPA fallback: a deep link/reload lands on
                                       the right screen; see studio/web/ - git-ignored build output)
  POST/PUT/PATCH/DELETE /api/{full_path}   catch-all, registered LAST and unconditionally: any
                                       non-GET /api path no route above claims is a 404, never a
                                       405, even for a path that exists under a different verb

Both preview routes are reads. Neither may write edit.json or anything
else - see the boundary section in CLAUDE.md, which permits exactly two
writes outside edit.json and both of them CREATE something the operator
explicitly asked for (a render, a clip from a picked window). A preview is
neither. The POST route in particular exists so an
unsaved correction can be previewed without ever touching disk; if it
ever gained a write, an operator's un-committed typing would start
leaking into edit.json before they asked for that.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .. import clipstore, editorial, estimate, preview, providers, trim
from .. import profile as _profile_module
from .. import stream_analysis
from ..clip_from_moment import ClipIdentityCollision, ClipIdentityUnreadable, create_clip
from ..lock import EventLock, LockError
from ..merge import deep_merge
from ..profile import Profile
from .. import brand_admin
from .. import channel_admin
from .. import auth
from .. import event_admin
from .. import event_brand_admin
from .. import font_admin
from .. import glossary_admin
from .. import lexicon_admin
from .. import logsetup
from .. import pathnames
from .. import tracks
from .. import upload_record
from .. import upload_policy
from .._google import GoogleUnavailable
from .._google import require as google_require
from ..auth import load_credentials
from ..lexicon import EMPTY as LEXICON_EMPTY
from ..moments import activity_curve
from ..quota import QuotaTracker
from .. import workspace
from ..workspace import resolve as _resolve_workspace
from ..workspace_listing import list_channels, list_events
from ..detect import has_analysis, has_cached_transcript
from ..youtube import Catalogue, YouTubeError, channel_catalogue
from ..youtube_upload import UploadError, build_metadata
from .. import workspaces as _workspaces
from .. import job_queue
from ..job_queue import JobQueue
from . import jobs
from . import worker as _worker_module

# The built React/Vite/Mantine page (src/yt_shorts/studio/web/). This
# directory is build OUTPUT and git-ignored; a wheel and the release binary
# each build it on the way in, so nobody installing the result needs npm
# (see https://github.com/jegr78/yt-shorts/wiki/Studio). Absent in a clone
# where nobody has run `npm run build` yet; mounting is skipped rather than
# raising, so the API keeps working on its own (e.g. under
# tests/test_studio_api.py, which never builds the page).
STATIC_DIR = Path(__file__).parent / "static"


class WordModel(BaseModel):
    start: float
    end: float
    text: str


class PatchClipBody(BaseModel):
    """Every field is optional and independent: a PATCH only touches the
    fields it actually names. ``title: null`` is meaningfully different
    from omitting ``title`` - it clears an existing override back to the
    harvested title - so fields are told apart by whether the client sent
    them at all (``model_fields_set``), not by whether their value is
    None."""
    title: str | None = None
    status: Literal[editorial.CANDIDATE, editorial.KEPT, editorial.DISCARDED] | None = None
    words: list[WordModel] | None = None
    # A moment's window: [start, end] to override it, null to clear back to the
    # detected window. Told apart from "omitted" by model_fields_set, like title.
    window: list[float] | None = None
    # A per-clip upload metadata override (description/tags/category_id/
    # made_for_kids - see editorial.effective_upload); null clears it back to
    # the channel/event default. Told apart from "omitted" by
    # model_fields_set, like title/window.
    upload: dict | None = None
    # Head/tail seconds to cut off the rendered short; null clears it back to
    # no trim. Told apart from "omitted" by model_fields_set, like window.
    # Typed list[Any], not list[float]: Pydantic coerces a JSON `true` into
    # `1.0` for a list[float] field BEFORE patch_clip ever runs, so
    # validate_trim's `isinstance(v, bool)` rejection below never saw a bool
    # to reject - confirmed live, {"trim": [true, 0]} returned 200 and stored
    # [1.0, 0.0]. list[Any] passes the raw JSON value through unconverted, so
    # a bool actually reaches validate_trim as a bool.
    trim: list[Any] | None = None


class UploadRequestBody(BaseModel):
    """Body for POST …/clips/{name}/upload. ``visibility`` defaults to
    "private" and ``publish_at`` to unscheduled - the safe defaults an
    accidental bare POST (no body at all) still gets. Anything else (a
    non-private visibility, or any scheduled ``publish_at``) is an
    irreversible-ish, more-exposed choice the operator must confirm
    explicitly per upload, via ``confirm`` - see post_upload's own guard.
    ``force`` lives here rather than as a query param: re-uploading an
    already-uploaded clip is as deliberate a per-upload choice as visibility,
    so it belongs in the same body."""
    visibility: str = "private"
    publish_at: str | None = None
    confirm: bool = False
    force: bool = False


class PreviewBody(BaseModel):
    """Body for POST /api/clips/{name}/preview: the timestamp, the word
    list, and optionally the title, the client currently holds - none of
    which may be what is saved in edit.json yet (that is the entire point
    of this route, see api.py's module docstring). ``title`` is optional
    and independent of ``words``: omitting it (the default, None) means
    "draw the hook from the clip's saved editorial state", exactly the
    GET route's behaviour - the studio only sends a string here when the
    operator is actively editing the title, so a live correction shows up
    in the hook the same way a word correction already shows up in the
    caption. The footer is never taken from the client; only the hook and
    the caption text can be staged and unsaved."""
    at: float
    words: list[WordModel]
    title: str | None = None


class RenderRequest(BaseModel):
    """``clips`` names which clip directories to render, honoring even a
    discarded one - the operator picked it on purpose (see
    ``yt_shorts.studio.jobs._target_clips``). Omitted or empty means every
    clip whose editorial status is not "discarded", the same set
    ``bin/yt-shorts render`` renders."""
    clips: list[str] | None = None


class EnqueueBody(BaseModel):
    """Body for POST /api/jobs: one unit of work for the queue.

    ``params`` is per kind - see studio/worker.py's own table, which is the
    authority on what each kind needs. It is typed ``dict[str, Any]``
    rather than a per-kind model on purpose: the worker's translation table
    already reports a missing or wrong-typed parameter as that ONE entry's
    failure, and a second copy of it here would be free to drift from the
    thing that actually runs the work. What this route checks itself is only
    what cannot be left until then - the two segments that become a
    directory name, and the privacy of a queued upload (see
    _validate_enqueue).

    ``after`` names an entry this one waits for; the queue fails a
    dependent whose dependency can never finish (see job_queue.claim_next).
    """
    kind: str
    params: dict[str, Any] = {}
    after: str | None = None


class MoveBody(BaseModel):
    """Body for POST /api/jobs/{id}/move: where the entry goes. The index is
    into the WHOLE plan, the same one `GET /api/jobs` reports as each row's
    `position` - only the relative order of the queued entries matters to
    the queue (see JobQueue.move)."""
    index: int


class LimitsBody(BaseModel):
    """Body for PUT /api/settings/limits: how many jobs a pool runs at once.

    A PARTIAL patch - a pool the body does not name keeps its current
    limit. Typed ``dict[str, Any]``, not ``dict[str, int]``, for the same
    reason PatchClipBody's ``trim`` is ``list[Any]``: pydantic coerces a
    JSON ``true`` into ``1`` for an int field BEFORE the route runs, so a
    bool would be silently accepted as "one at a time" instead of being
    reported as the nonsense it is. The raw value reaches
    _validated_limits, which rejects it.
    """
    limits: dict[str, Any]


class ConnectBody(BaseModel):
    """Body for POST /api/auth/connect: the channel id to authorize. Omitted
    (or null) means this studio's own channel - the studio pre-fills it, but the
    field is editable so a multi-channel operator can connect another channel.
    ``force`` re-runs consent even when a valid token is already stored - the way
    to switch which account a channel is connected as."""
    channel_id: str | None = None
    force: bool = False


class EventNameBody(BaseModel):
    """Body for creating or renaming an event: the new event name (a slug;
    validated server-side by event_admin.validate_name)."""
    name: str


class ChannelCreateBody(BaseModel):
    """Body for creating a channel: its directory slug plus the six required
    channel.json identity fields (validated server-side by channel_admin)."""
    slug: str
    id: str
    channel_url: str
    handle: str
    display_name: str
    language: str
    footer: str


class ChannelFieldsBody(BaseModel):
    """A partial channel.json edit - only the fields present are applied."""
    id: str | None = None
    channel_url: str | None = None
    handle: str | None = None
    display_name: str | None = None
    language: str | None = None
    footer: str | None = None


class BrandPatchBody(BaseModel):
    """A partial brand.json edit (live preview or save): any of the renderable
    sections, plus 'detect' (the moment-detection provider/model, which is
    brand-scoped but affects nothing about the rendered picture, so
    brand_preview never reads it). Only the sections present are applied;
    each is validated the same way profile.load validates it (see
    brand_admin.update_brand). Every field is a plain 'dict', not a more
    specific shape, deliberately: a typed sub-model would let pydantic coerce
    a hostile value (e.g. a list) into something the real validator never
    sees, the same trap PatchClipBody's 'trim' field hit with a JSON `true`
    silently becoming `1.0` (see that field's own comment) - a bare dict
    passes every value through UNCONVERTED so profile._validate_detect (and
    its siblings) see exactly what the client sent."""
    colors: dict | None = None
    fonts: dict | None = None
    subtitles: dict | None = None
    logo: dict | None = None
    output: dict | None = None
    upload: dict | None = None
    bands: dict | None = None
    detect: dict | None = None


class UploadModeBody(BaseModel):
    """The channel's upload class for the Settings api/manual toggle."""
    mode: str


class MarkersBody(BaseModel):
    """Body for PUT …/moments: the scope's own layer, in full - see
    lexicon_admin.update's own docstring on why this always overwrites
    rather than merges."""
    markers: dict[str, float]


class GlossaryBody(BaseModel):
    """Body for PUT …/glossary: the scope's own layer, in full - see
    glossary_admin.update's own docstring on why this always overwrites
    rather than merges. `terms` maps a term's spelling to whether it is
    enabled (false disables one inherited from a less specific layer);
    `replacements` maps a key to its replacement text, or null to disable an
    inherited one.

    Both fields are REQUIRED. A PUT overwrites the whole layer, so a client
    that omitted one half would silently wipe it; pydantic refusing the
    request is the correct answer.

    This wire contract is deliberately NARROWER than the on-disk file format
    in one spot, and pydantic makes it slightly LAXER in another. Neither
    lets bad data reach disk, but a client author should know:

    - A hand-edited glossary.json may disable a replacement with `false` as
      well as `null` (glossary._parse_replacements accepts both). Here only
      `null` passes; `false` is a 422 from pydantic rather than the mapped
      400 a malformed value gets. Widening the annotation to a union is what
      it looks like it wants, and is worse - pydantic would then have to
      guess between `str` and `bool` for a value like `"false"`. Send null.
    - pydantic coerces a truthy string or 0/1 into a real bool for `terms`
      before glossary.parse_layer (which accepts only literal true/false)
      ever sees it, so `{"Karussell": "yes"}` is accepted here where a file
      containing it would be refused. The same laxness applies to
      MarkersBody's floats. What lands on disk is still a real boolean.

    `track` names the venue whose shipped vocabulary applies (see tracks.py).
    Only an event may set it; the admin module refuses it at any other
    scope - this route adds no second guard of its own. It is
    OPTIONAL here but NOT sticky: a PUT overwrites the whole layer, so a
    client that means to keep the current track must send it back with every
    save. The editor reads it from the GET and does exactly that.
    """
    terms: dict[str, bool]
    replacements: dict[str, str | None]
    track: str | None = None


class WorkspaceSwitchBody(BaseModel):
    """Body for POST /api/workspaces/switch: the absolute path of an
    existing workspace to re-root onto."""
    path: str


class WorkspaceCreateBody(BaseModel):
    """Body for POST /api/workspaces/create: a new workspace named ``name``
    is created directly under ``parent``, then switched to."""
    parent: str
    name: str


class NewClipWindow(BaseModel):
    """Body for POST …/streams/{video_id}/clips: the window the operator
    picked, plus the hook and source title to seed the new clip.json with."""
    start: float
    end: float
    hook: str = ""
    source_title: str = ""


class EstimateRequest(BaseModel):
    """Body for POST …/streams/{video_id}/estimate: an explicit model
    override, or null to use the channel's configured/default one."""
    model: str | None = None


def _now_iso() -> str:
    """The injected clock for workspace bookkeeping (manifest/create
    timestamps) - a thin module-level function so a test can monkeypatch it,
    the same way every other real-clock read in this file is a live call
    rather than something captured at import."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config_home() -> Path:
    """Where the studio's own workspaces.json lives (XDG_CONFIG_HOME, else
    ~/.config) - read live, not at import, and a thin enough function that a
    test can monkeypatch it to a tmp dir instead of the operator's real
    ~/.config (see TestWorkspaces in tests/test_studio_api.py)."""
    import os
    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    return Path(xdg) if xdg else Path.home() / ".config"


def _derived_words(clip_dir: Path) -> list[dict] | None:
    """The words cached at this clip's own transcript.json, or None if
    there is nothing there yet (not transcribed, or the cache was deleted
    by hand) - exactly the "no derived transcript" case
    ``editorial.effective_words`` already knows how to handle. An
    unreadable or malformed cache is treated the same way: reading it is
    not this route's job (that already happened at render time, into
    clip's own subtitle pipeline - see bin/yt-shorts), so a broken cache
    here degrades to "nothing derived" rather than failing the request."""
    path = clipstore.transcript_path(clip_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    words = payload.get("words") if isinstance(payload, dict) else None
    return words if isinstance(words, list) else None


def _short_version(directory: Path) -> str | None:
    """A token that changes exactly when the rendered short's bytes do.

    The studio's player URL is otherwise a constant: after a re-render the
    refetched clip payload is byte-identical, React never touches the
    <video>'s src attribute, and the element keeps the resource it already
    loaded - so the operator watches the OLD short until a hard browser
    reload. Putting this in the URL makes the src change when the file
    changes, whoever changed it: the studio, a CLI render, another session.

    (mtime_ns, size) rather than a content hash: hashing a multi-megabyte
    video on every clip-list request is O(size) per clip and a list holds
    dozens, where this is one stat() - the same syscall class as the
    .exists() it replaces. It is also the identity Starlette's own
    FileResponse derives its ETag from, so this reuses the server's existing
    notion of file identity instead of inventing a second, disagreeing one.

    OPAQUE by contract: the client passes it through and never parses it.
    Tests pin that it changed, not what it equals.
    """
    try:
        info = clipstore.short_path(directory).stat()
    except OSError:
        return None
    return f"{info.st_mtime_ns}-{info.st_size}"


def _summary(directory: Path, clip: dict, edit: editorial.Edit) -> dict:
    short_version = _short_version(directory)
    return {
        "name": directory.name,
        "harvested_title": clip.get("hook", ""),
        "effective_title": editorial.effective_title(edit, clip.get("hook", "")),
        "status": edit.status,
        "has_short": short_version is not None,
        # The player's cache-busting token - see _short_version. Emitted here
        # so the clip list and both clip-detail responses carry it without
        # three separate additions.
        "short_version": short_version,
        # Two RECORDED values, never a probe: this runs per clip on every
        # list request. `trim` is what the operator wants, `trim_applied` is
        # what short.mp4 currently embodies; unequal means pending.
        "trim": list(edit.trim) if edit.trim is not None else None,
        "trim_applied": (list(_applied) if (_applied := trim.applied(directory))
                         else None),
        # True exactly when short.mp4's cut state cannot be trusted (an
        # untrimmed master survives with no readable record of what
        # short.mp4 currently is - see trim.is_unknown). `trim`/`trim_applied`
        # alone cannot say this: both read null/null in this state, the same
        # as an ordinary never-trimmed clip, which is what let a cut
        # short.mp4 read as "nothing pending" before this field existed. The
        # client uses it to enable the repair (Apply/"Repair trim") even
        # though the desired and last-known-applied values otherwise agree.
        "trim_unknown": trim.is_unknown(directory),
        "has_transcript": clipstore.transcript_path(directory).exists(),
        "has_edit": (directory / editorial.EDIT_FILENAME).exists(),
        # Exposed so the studio's empty-state summary can tell an operator
        # how many clips have no raw video at all - the exact set the
        # preview route would 409 on (see preview.build/PreviewError) and
        # a render would still have to fetch before it could do anything
        # else with them.
        "has_raw": clipstore.raw_path(directory).exists(),
        "duration": clip.get("duration", 0.0),
        # Upload state, so a reload shows "uploaded" (not just within a session).
        # The backend's re-upload guard is the real safety; this is the display.
        "has_upload": upload_record.is_uploaded(directory),
        "upload_url": (upload_record.load(directory) or {}).get("url"),
    }


# Hosts an Origin or a Host header may name for a request to count as local
# (the studio only ever serves 127.0.0.1). A foreign Origin on a mutating
# request is a CSRF attempt; a foreign Host on ANY request - reads included -
# is a DNS-rebound page. See _local_origin_guard for why those need two
# separate checks rather than one.
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _names_a_local_host(value: str, *, authority: bool = False) -> bool:
    """Whether `value` names a host this studio actually serves.

    `value` is an Origin URL (`http://127.0.0.1:8765`) by default, or a bare
    `Host` authority (`127.0.0.1:8765`, `[::1]:8765`) with `authority=True` -
    which is why the two are told apart here rather than by two near-identical
    parses at the call site.

    A value that cannot be parsed at all answers False rather than raising:
    `urlparse(...).hostname` raises ValueError on a malformed authority (an
    unterminated `[::1`, say), and a header this function cannot make sense of
    names no host this studio serves, which is the same answer as a foreign
    one. The literal Origin `"null"` (a sandboxed iframe, a file:// page)
    lands here too - it parses to a hostname of None, which is not in
    _LOCAL_HOSTS.
    """
    try:
        hostname = urlparse(f"//{value}" if authority else value).hostname
    except ValueError:
        return False
    return hostname in _LOCAL_HOSTS

_logger = logging.getLogger("ytshorts.studio")


# How the plan's states are grouped for the Jobs screen. Together these
# partition job_queue.STATES exactly - a state in none of them would simply
# vanish from the screen, and one in two would be shown twice; the
# enumerating test in tests/test_studio_api.py pins both against STATES
# itself rather than against a copy of this list.
_RUNNING_STATES = ("running", "stopping")
_PENDING_STATES = ("queued", "paused")
_FINISHED_STATES = ("done", "failed", "stopped", "interrupted")

# What a params value is replaced with when its NAME looks like a secret.
_REDACTED = "[redacted]"

# What every job-queue route says when there is no queue at all. Both
# app.state.job_queue and app.state.worker are None whenever the workspace
# could not be resolved - see _build_queue_and_worker, which is best-effort
# on purpose. An operator has to be able to act on this, so it names the
# cause and where to look; a 500 would read as a bug in the studio, and an
# empty listing would read as "no jobs", which is a different and much worse
# lie (this project's recurring failure mode - see CLAUDE.md).
_QUEUE_UNAVAILABLE = (
    "the job queue is unavailable: this studio could not open a workspace to "
    "read <workspace>/jobs.json from. Check the workspace on the Settings "
    "screen (and YT_SHORTS_DATA, if it is set), then restart the studio.")

# The same state, said in the terms of the one route that does not read the
# queue at all: PUT /api/settings/limits writes <workspace>/settings.json,
# so an unresolvable workspace leaves it nowhere to write rather than
# nothing to read. Its own message, because "read jobs.json" would be a
# wrong description of what just failed.
_LIMITS_UNAVAILABLE = (
    "the pool limits cannot be saved: this studio could not open a workspace "
    "to write <workspace>/settings.json to. Check the workspace on the "
    "Settings screen (and YT_SHORTS_DATA, if it is set), then restart the "
    "studio.")

# QueueError.kind -> HTTP status, exactly the shape _brand_status has for
# BrandAdminError.kind. `no_hard_stop` and `not_stoppable` come from
# Worker.request_stop; the rest from the queue itself.
_QUEUE_STATUS = {
    "not_found": 404,
    "unknown_kind": 400,
    "not_queueable": 400,
    "secret_in_params": 400,
    "nested_params": 400,
    "invalid_state": 409,
    "no_hard_stop": 409,
    "not_stoppable": 409,
}


def _queue_status(error: job_queue.QueueError) -> int:
    return _QUEUE_STATUS.get(error.kind, 400)


def _queue_pools() -> list[str]:
    """The pools an operator can set a limit for, derived from jobs.KINDS.

    Only QUEUEABLE kinds count: `copy`'s "io" pool is real but nothing can
    ever be queued into it, and offering a limit for it would be a control
    that does nothing.

    Sorted, so the order does not depend on how `jobs.KINDS` happens to be
    written - the Settings screen (`QueueLimitsPanel` in
    `SettingsScreen.tsx`) renders one editable field per pool in exactly
    this order, and an order that moved on every restart would make that
    layout jump around for no reason on the operator's next visit.

    This docstring used to say a Settings-screen control did not exist and
    that the limits could only be edited by hand-editing
    `<workspace>/settings.json` and restarting - true when it was written,
    and false as of the task that added `QueueLimitsPanel`: the browser now
    calls `PUT /api/settings/limits` directly, and the wiki's Studio page
    describes that control rather than the hand-edit workaround.
    Keep this claim honest if it ever goes stale again - say what is
    actually true rather than leaving a claim uncorrected.
    """
    return sorted({spec.pool for spec in jobs.KINDS.values() if spec.queueable})


def _stored_limits(root: Path) -> dict[str, int]:
    """The pool limits for this workspace: the defaults, with whatever the
    operator saved layered over them.

    Every value is type-checked rather than merely defaulted, the same
    stance `get_settings`' `detect` read takes: settings.json is a plain
    hand-editable file that no validation stands in front of, and this runs
    on the path that BUILDS the queue at startup - a stray value must cost
    that one pool its custom limit, never the studio its whole queue.
    """
    limits = dict(_worker_module.DEFAULT_LIMITS)
    stored = workspace.read_settings(root).get("limits")
    if not isinstance(stored, dict):
        return limits
    pools = _queue_pools()
    for pool, value in stored.items():
        if (pool in pools and isinstance(value, int)
                and not isinstance(value, bool) and value >= 1):
            limits[pool] = value
    return limits


def _validated_limits(current: dict[str, int], patch: dict) -> dict[str, int]:
    """`current` with `patch` applied, or an HTTPException(400) naming what
    is wrong with the patch. Pure - it writes nothing."""
    limits = dict(current)
    pools = _queue_pools()
    for pool, value in patch.items():
        if pool not in pools:
            raise HTTPException(
                status_code=400,
                detail=f"unknown pool {pool!r}; this workspace's pools are "
                       f"{', '.join(pools)}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                status_code=400,
                detail=f"the limit for pool {pool!r} must be a whole number, "
                       f"not {type(value).__name__}")
        if value < 1:
            raise HTTPException(
                status_code=400,
                detail=f"the limit for pool {pool!r} must be at least 1: a "
                       f"limit of 0 would leave every {pool} job queued "
                       f"forever with nothing saying why")
        limits[pool] = value
    return limits


def _redacted_value(value):
    """`value` with every key-shaped NAME inside it replaced, at any depth.

    Nested, not top-level-only: `looks_like_a_secret_name` tests a NAME, so
    a name one level down (`{"creds": {"api_key": …}}`) is invisible to a
    flat pass - a review drove exactly that shape through this route and
    watched the key come back out. `JobQueue.enqueue` now refuses a nested
    params value outright, so a jobs.json THIS version wrote can carry
    none; this covers the file it did not write, which is the only case
    `_safe_params` has ever existed for.
    """
    if isinstance(value, dict):
        return {key: (_REDACTED if job_queue.looks_like_a_secret_name(key)
                      else _redacted_value(inner))
                for key, inner in value.items()}
    if isinstance(value, list):
        return [_redacted_value(item) for item in value]
    return value


def _safe_params(params: dict | None) -> dict:
    """One entry's params, with any key-shaped NAME's value replaced.

    `JobQueue.enqueue` already refuses to write such a param, so this can
    only ever fire on a jobs.json written by hand or by an older version -
    but this route is the last thing between that file and a browser, and
    the value is redacted rather than the key dropped so the operator can
    still see (and delete) the entry that carries it. Redacted at any
    DEPTH - see _redacted_value.
    """
    return _redacted_value(dict(params or {}))


def _entry_json(entry, position: int) -> dict:
    """One queue entry as the Jobs screen needs it.

    `pool`/`stop_point`/`hard_stop_allowed` come from `jobs.KINDS`, not from
    the entry: the screen must label a stop button with a real boundary in
    the work before the operator clicks it, and must not offer a hard stop
    for a kind that has none. They are None/False for a kind KINDS no longer
    knows, which an entry written by an older version can still name.

    **`stoppable` is a FACT on the wire, and it used to be prose the client
    parsed.** The screen has to know whether a stop would reach the work at
    all, and for a while the only signal it had was the literal phrase
    `stop_point == "cannot be stopped"` - so a case-only edit to
    `KINDS["upload"].stop_point` ("Cannot be stopped") left 389 Python tests
    green while putting a Stop button on a running upload, the one thing
    `KINDS["upload"]` exists to forbid. It is read from
    `worker.STARTERS[kind].stoppable`, NOT from the phrase and not from
    `hard_stop_allowed`: what decides whether `POST …/stop` can do anything
    is whether the starter TAKES a cancel token, which is precisely what
    `Worker.request_stop` refuses on (`not_stoppable`). A kind with no
    starter at all - `connect`/`copy`, or one this build no longer knows -
    is False, which is the safe answer in both directions: no button rather
    than a lying one.

    The `reason` goes through `logsetup.shorten_urls` for the same reason
    `Worker._fail` does: it can be an external tool's own message, and a
    yt-dlp one carries a signed googlevideo URL. The worker elides it on the
    way IN; this elides it on the way OUT, which covers a jobs.json this
    process never wrote (see tests/test_logging_secrets.py).
    """
    spec = jobs.KINDS.get(entry.kind)
    starter = _worker_module.STARTERS.get(entry.kind)
    return {
        "id": entry.id,
        "kind": entry.kind,
        "state": entry.state,
        "params": _safe_params(entry.params),
        "reason": (logsetup.shorten_urls(entry.reason)
                   if entry.reason is not None else None),
        "progress": entry.progress,
        "created_at": entry.created_at,
        "after": entry.after,
        "job_id": entry.job_id,
        "position": position,
        "pool": spec.pool if spec is not None else None,
        "stop_point": spec.stop_point if spec is not None else None,
        "hard_stop_allowed": bool(spec is not None and spec.hard_stop_allowed),
        "stoppable": bool(starter is not None and starter.stoppable),
    }


def _build_queue_and_worker(app: FastAPI, root: Path | None = None) -> None:
    """Points `app.state.job_queue`/`app.state.worker` at one workspace root.

    The worker is built and NOT started: `bin/yt-shorts studio` starts it,
    and `create_app()` must not, or every test that constructs an app would
    acquire event locks and spawn threads as a side effect (see
    studio/worker.py's own docstring, and tests/test_studio_worker.py's
    TestCreateAppWiring).

    Both are None when the workspace cannot be resolved at all - the same
    best-effort stance the central log's setup below takes: a studio that
    cannot read its queue must still start and serve everything else.

    On a workspace SWITCH the previous worker is stopped and the new one
    inherits its started-ness - not a default of "started", which would turn
    a switch into the one thing this function must never do: start a worker
    nobody asked for. At `create_app` time there is no previous worker, so
    nothing starts.
    """
    previous = getattr(app.state, "worker", None)
    was_running = previous is not None and previous.is_running()
    if previous is not None:
        previous.stop()
    try:
        root = _resolve_workspace().root if root is None else root
        # The limits are workspace SETTINGS, so they are read from this
        # workspace's own settings.json rather than being the shipped
        # defaults forever - a limit an operator set on the Settings screen
        # has to survive a restart, and a switch has to pick up the NEW
        # workspace's, not carry the old one's across.
        queue = JobQueue(str(Path(root) / "jobs.json"), jobs.KINDS,
                         _stored_limits(Path(root)))
    except (OSError, workspace.WorkspaceError) as error:
        _logger.warning("job queue unavailable: %s", error)
        app.state.job_queue = None
        app.state.worker = None
        return
    if queue.load_error:
        _logger.error("%s", queue.load_error)
    app.state.job_queue = queue
    app.state.worker = _worker_module.Worker(queue, app.state.job_store)
    if was_running:
        app.state.worker.start()


def create_app() -> FastAPI:
    app = FastAPI(title="yt-shorts studio")
    app.state.job_store = jobs.JobStore()
    # The queue is workspace data, a sibling of logs/ and auth/ - so it is
    # built here, from the resolved root, and re-pointed by _switch_to below
    # when the operator moves to another workspace.
    _build_queue_and_worker(app)

    # The studio server logs into the same central log the CLI writes, so one
    # file carries the whole session. Per-job logs are separate files (see
    # studio/jobs.py) - a one-hour transcription must not bury the app log.
    try:
        _log_dir = workspace.logs_dir(_resolve_workspace().root)
        logsetup.configure_logging("ytshorts", _log_dir / workspace.CENTRAL_LOG_NAME,
                                   to_stdout=False)
        logsetup.prune_old_logs(_log_dir)
    except (OSError, workspace.WorkspaceError) as error:
        # create_app() must never fail because logging could not be set up -
        # resolve() can raise WorkspaceError (e.g. a misconfigured
        # YT_SHORTS_DATA), not only OSError, and the studio must still start.
        _logger.warning("logging unavailable: %s", error)

    @app.middleware("http")
    async def _local_origin_guard(request: Request, call_next):
        # The studio binds 127.0.0.1 and has no auth by design (single
        # operator), so a page in the operator's browser could otherwise drive
        # its state-changing routes (CSRF) or, via DNS rebinding, read them as
        # same-origin. TWO checks, because one header cannot cover both.
        #
        # HOST, on EVERY method including reads. The studio serves 127.0.0.1
        # and nothing else, so a request naming another host did not come from
        # a page opened at the studio's own address. This is the half that
        # closes DNS rebinding for READS, which the Origin check below
        # structurally cannot: after the rebind the browser considers the page
        # same-origin, and a plain GET carries no Origin to act on - but the
        # Host header still names the attacker's own domain, because that is
        # the origin they have to keep in order to stay same-origin. It is the
        # one thing in the request they cannot change without giving up the
        # attack. Measured before this existed: `GET /api/fs?path=$HOME` with
        # `Host: evil.example.com` answered 200 with the operator's home
        # directory listing - and `/api/fs` lists any directory this process
        # can read.
        #
        # An ABSENT Host is allowed, exactly as an absent Origin is: HTTP/1.1
        # requires one and every browser sends it, so its absence means a
        # non-browser caller (the CLI, curl, a raw socket), which already has
        # whatever local access this could protect.
        host = request.headers.get("host")
        if host is not None and not _names_a_local_host(host, authority=True):
            return JSONResponse(
                {"detail": "request for a foreign host refused"}, status_code=403)
        # ORIGIN, on mutating methods only. A cross-origin browser POST always
        # carries an Origin, and a rebound page's Origin is still the
        # attacker's domain. Non-browser callers send no Origin and are
        # unaffected - reads too, which is what made the Host check above
        # necessary rather than redundant.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin is not None and not _names_a_local_host(origin):
                return JSONResponse(
                    {"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    # Read live (not captured at import) so tests that repoint CHANNELS_DIR
    # before building the app - see tests/conftest.py and the studio tests -
    # list from and resolve against the pinned fixture, not the real workspace.
    channels_dir = _profile_module.CHANNELS_DIR
    _reroot_lock = threading.Lock()

    def _switch_to(root: Path) -> None:
        # Re-roots every route at once: channels_dir is the closure cell
        # every route in this function already reads from, and
        # _profile_module.CHANNELS_DIR is the global profile.load itself
        # reads (see the module import at the top). Both must move together
        # or a scoped route (via profile.load) and an unscoped one (reading
        # channels_dir directly, e.g. get_channels) would disagree about
        # which workspace is current. Task 7's background copy job calls this
        # from a job thread via on_done=_select, not just the request thread,
        # so the pair-write itself needs to be atomic with respect to itself.
        # Serialize the paired reroot writes. Routes read channels_dir lock-free;
        # acceptable for this single-operator local tool, and _guard_reroot
        # already refuses to start a second reroot while any job runs.
        nonlocal channels_dir
        with _reroot_lock:
            channels_dir = root / "channels"
            _profile_module.CHANNELS_DIR = channels_dir
            # logsetup.configure_logging is idempotent by LOGGER NAME, not by
            # path (see its own docstring), and create_app() below calls it
            # exactly once - so without this, the "ytshorts" central logger
            # would keep writing to the PREVIOUS workspace's log file forever
            # after a switch: _logs_root(), GET /api/logs and every new job
            # log move to the new workspace, but the app's own log would not,
            # leaving the Logs screen's central pane permanently empty.
            # close_logging first (a bare re-call of configure_logging is a
            # no-op because of that same name-idempotency), then reconfigure
            # onto the new root. Best-effort, like every logging path (see
            # create_app's own try/except around this same call) - a
            # workspace switch must never fail just because its new log file
            # could not be opened.
            try:
                logsetup.close_logging("ytshorts")
                logsetup.configure_logging(
                    "ytshorts", workspace.logs_dir(root) / workspace.CENTRAL_LOG_NAME,
                    to_stdout=False)
            except (OSError, workspace.WorkspaceError) as error:
                _logger.warning("logging unavailable after workspace switch: %s", error)
            # The queue is workspace data too, and the same hazard applies as
            # for the central log: left alone, the studio would keep driving
            # the PREVIOUS workspace's jobs.json - a plan that is not the
            # plan of the workspace now on screen. _guard_reroot already
            # refuses a switch while any job is running, so nothing this
            # worker is tracking is lost by re-pointing it here.
            _build_queue_and_worker(app, root)

    # The single failure surface for every event-scoped route: an unknown
    # channel/event and a malformed profile both become a 404.
    def _load_profile(channel: str, event: str) -> Profile:
        from ..profile import ProfileError
        from ..profile import load as profile_load
        # Validate both segments as safe path segments BEFORE building any path
        # (a bad segment is a 400, not a 404), matching _load_channel. profile.load
        # validates again defensively, but doing it here keeps the status honest.
        for segment, what in ((channel, "channel name"), (event, "event name")):
            try:
                pathnames.validate_segment(segment, what=what)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            return profile_load(f"{channel}/{event}")
        except ProfileError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    def _load_channel(channel: str) -> dict:
        """This channel's channel.json (its YouTube id), for the channel-scoped
        auth routes - which have no event, so they do not load a full profile.
        The segment is validated as one safe path segment before any filesystem
        touch, so a '..' or dotted {channel} can never read a channel.json
        outside the channels dir (nor let disconnect act on it)."""
        try:
            pathnames.validate_segment(channel, what="channel name")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        path = channels_dir / channel / "channel.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _logger.warning("channel.json unreadable at %s: %s", path, error)
            raise HTTPException(
                status_code=404,
                detail=f"Unreadable channel.json for {channel!r}") from error

    def _channel_config(channel: str) -> dict:
        """The channel-level brand.json, read only for the upload class (the same
        event-independent read cmd_auth does). Absent or malformed -> {} -> the
        default 'api'. Never loads a full profile - auth is channel-scoped.

        Defense in depth: every caller runs _load_channel (which validates) first,
        but validate here too so this never becomes an unvalidated path read if a
        future caller forgets that ordering."""
        try:
            pathnames.validate_segment(channel, what="channel name")
        except ValueError:
            return {}
        path = channels_dir / channel / "brand.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_clip_or_404(profile: Profile, name: str) -> tuple[Path, dict]:
        # `clip_dir_by_name` is the one place a clip name becomes a path AND
        # the one place it is validated (see its own docstring). It used to be
        # spelled out here instead, which is how the BODY-supplied names - a
        # render's `clips`, a queue entry's `clip` - came to have no guard at
        # all: this route had one, and nothing shared it.
        try:
            directory = clipstore.clip_dir_by_name(profile.event_dir, name)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"Unknown clip: {name!r}") from None
        try:
            clip = clipstore.read_clip(directory)
        except clipstore.ClipStoreError:
            raise HTTPException(status_code=404, detail=f"Unknown clip: {name!r}") from None
        return directory, clip

    def _load_edit_or_500(directory: Path) -> editorial.Edit:
        try:
            return editorial.load(directory)
        except editorial.EditError as error:
            # The specifics (absolute workspace path, JSON error offset) go to the
            # server log, not the HTTP client - a 500 body must not disclose them.
            _logger.warning("edit.json unreadable at %s: %s", directory, error)
            raise HTTPException(
                status_code=500, detail="could not read clip edit state") from error

    # Path fragments shared by the scoped routes, so a typo in one prefix
    # cannot silently diverge from the others.
    CH = "/api/channels/{channel}"
    EV = CH + "/events/{event}"

    # ---- Start screen: channels and events (read-only listings) ----

    @app.get("/api/channels")
    def get_channels() -> list[dict]:
        return [vars(c) for c in list_channels(channels_dir)]

    def _channel_status(error: channel_admin.ChannelAdminError) -> int:
        return {"bad_name": 400, "bad_field": 400, "not_found": 404,
                "exists": 409, "locked": 409}.get(error.kind, 400)

    def _channel_entry(slug: str) -> dict:
        for entry in list_channels(channels_dir):
            if entry.name == slug:
                return vars(entry)
        return {"name": slug, "display_name": "", "handle": "",
                "event_count": 0, "error": None}

    @app.post("/api/channels", status_code=201)
    def create_channel(body: ChannelCreateBody) -> dict:
        fields = body.model_dump()
        slug = fields.pop("slug")
        try:
            channel_admin.create_channel(channels_dir, slug, fields)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error)) from error
        return _channel_entry(slug)

    @app.get(CH)
    def get_channel(channel: str) -> dict:
        # The channel.json identity fields, for the channel-details editor.
        # _load_channel validates the segment and 404s an unknown channel.
        return _load_channel(channel)

    @app.patch(CH)
    def edit_channel(channel: str, body: ChannelFieldsBody) -> dict:
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            channel_admin.update_channel(channels_dir, channel, fields)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error)) from error
        return _channel_entry(channel)

    @app.post(CH + "/rename")
    def rename_channel(channel: str, body: EventNameBody) -> dict:
        try:
            channel_admin.rename_channel(channels_dir, channel, body.name)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error)) from error
        return _channel_entry(body.name)

    @app.delete(CH)
    def delete_channel(channel: str) -> dict:
        try:
            channel_admin.delete_channel(channels_dir, channel)
        except channel_admin.ChannelAdminError as error:
            raise HTTPException(status_code=_channel_status(error), detail=str(error)) from error
        return {"deleted": channel}

    def _font_status(error: font_admin.FontAdminError) -> int:
        return {"bad_name": 400, "bad_type": 400, "too_big": 400, "invalid": 400,
                "not_found": 404, "in_use": 409}.get(error.kind, 400)

    def _brand_status(error: brand_admin.BrandAdminError) -> int:
        return {"bad_name": 400, "not_found": 404, "bad_color": 400,
                "bad_font": 400, "bad_subtitles": 400}.get(error.kind, 400)

    @app.get(CH + "/brand")
    def get_brand(channel: str) -> dict:
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error
        return {"brand": brand, "fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.put(CH + "/brand")
    def put_brand(channel: str, body: BrandPatchBody) -> dict:
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            brand_admin.update_brand(channels_dir, channel, patch)
            return {"brand": brand_admin.read_brand(channels_dir, channel)}
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error

    @app.put(CH + "/upload")
    def put_upload_mode(channel: str, body: UploadModeBody) -> dict:
        # Sets only brand.json's upload.mode - the Settings api/manual toggle.
        # A lighter path than put_brand: it does not require an otherwise-complete
        # brand, so the class is settable on a channel with no fonts yet.
        try:
            brand_admin.set_upload_mode(channels_dir, channel, body.mode)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error
        return {"mode": body.mode}

    async def _read_body_capped(request: Request, limit: int) -> bytes:
        # Enforce the size cap WHILE reading, not after: reject on a declared
        # Content-Length over the limit, and abort the stream once the accumulated
        # body exceeds it - so a huge upload can never be fully buffered into RAM
        # (a cross-origin 'simple' POST otherwise OOMs the studio) before
        # font_admin's own len() check runs.
        too_big = HTTPException(
            status_code=413,
            detail=f"font is larger than {limit // (1024 * 1024)} MB")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise too_big
            except ValueError:
                pass  # unparsable length: fall through to counting bytes
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise too_big
        return bytes(body)

    @app.post(CH + "/fonts/{filename}", status_code=201)
    async def upload_font(channel: str, filename: str, request: Request) -> dict:
        data = await _read_body_capped(request, font_admin.MAX_FONT_BYTES)
        try:
            font_admin.save_font(channels_dir, channel, filename, data)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.delete(CH + "/fonts/{filename}")
    def delete_font(channel: str, filename: str) -> dict:
        try:
            font_admin.delete_font(channels_dir, channel, filename)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.post(CH + "/brand/preview")
    def brand_preview(channel: str, body: BrandPatchBody):
        import io as _io

        from ..overlay import build_overlay
        from ..profile import _resolve_logo
        # read_brand validates the {channel} segment before any filesystem
        # touch; do it BEFORE _load_channel so an unsafe segment is rejected
        # first (400), not read from disk.
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error
        channel_json = _load_channel(channel)
        for key in ("colors", "fonts", "subtitles", "logo", "output", "bands"):
            value = getattr(body, key)
            if value is not None:
                brand[key] = value
        base = channels_dir / channel
        fonts = brand.get("fonts") or {}
        # Resolve each assigned font ref through the SAME validator PUT /brand
        # uses (fonts/<safe-segment> existing under the channel's fonts/), so a
        # client-supplied ref like "/etc/passwd" or "fonts/../.." can never
        # reach ImageFont.truetype - it is rejected here as a 409 before any
        # font file is opened.
        try:
            resolved_fonts = {
                role: str(brand_admin.resolve_font_ref(base, ref, what=f"font {role!r}"))
                for role, ref in fonts.items()
            }
        except brand_admin.BrandAdminError as error:
            raise HTTPException(
                status_code=409,
                detail=f"cannot render preview (check the assigned fonts): {error}") from error
        config = {
            "colors": brand.get("colors") or {},
            "fonts": resolved_fonts,
            "output": brand.get("output") or {},
            "bands": brand.get("bands"),
        }
        # Optional logo: resolve its file (variant + event/channel assets) the
        # same way profile.load does, so the preview draws exactly what a render
        # would. resolve keeps only safe 'fonts/<seg>'-style refs; a missing or
        # invalid logo file falls through to the same 409 a bad font raises.
        if isinstance(brand.get("logo"), dict):
            holder = {"logo": brand["logo"]}
            _resolve_logo(holder, base, base)
            config["logo"] = holder["logo"]
        hook = channel_json.get("display_name", channel)
        footer = channel_json.get("footer", "")
        try:
            image = build_overlay(hook, footer, config)
        except Exception as error:   # noqa: BLE001 - a missing/invalid font or bad geometry
            _logger.warning("brand preview render failed for %s: %s", channel, error)
            raise HTTPException(
                status_code=409,
                detail="cannot render preview (check the assigned fonts)") from error
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")

    @app.get(CH + "/brand/palette")
    def brand_palette(channel: str):
        """The channel logo's own colours, and a proposed role for each.

        A READ: it opens the logo the brand already names and writes nothing.
        409 rather than 500 when the brand names no logo or the file cannot be
        opened - the same contract POST .../brand/preview uses for a render it
        cannot perform, because both are "your brand is not ready for this
        yet", not "the server is broken"."""
        from .. import palette
        from ..profile import _resolve_logo
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error
        base = channels_dir / channel
        resolved = dict(brand)
        _resolve_logo(resolved, base, base)
        logo = resolved.get("logo") or {}
        if not logo.get("file"):
            raise HTTPException(
                status_code=409, detail="this channel's brand names no logo")
        try:
            roles, found = palette.derive(logo["file"])
        except palette.PaletteError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"colors": roles,
                "swatches": [{"hex": s.hex, "share": s.share} for s in found]}

    # ---- Brand: event-scoped override (colors/fonts/logo/output/subtitles
    # only - upload is never event-overridable, see event_brand_admin) ----

    def _event_brand_status(error: event_brand_admin.EventBrandError) -> int:
        return 404 if error.kind == "not_found" else 400

    def _event_brand_with_fonts(channel: str, event: str) -> dict:
        data = event_brand_admin.read_event_brand(channels_dir, channel, event)
        data["fonts"] = {"channel": font_admin.list_fonts(channels_dir, channel),
                         "event": font_admin.list_event_fonts(channels_dir, channel, event)}
        return data

    @app.get(EV + "/brand")
    def get_event_brand(channel: str, event: str) -> dict:
        try:
            return _event_brand_with_fonts(channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error

    @app.put(EV + "/brand")
    def put_event_brand(channel: str, event: str, body: BrandPatchBody) -> dict:
        # upload is never event-overridable (see event_brand_admin's module
        # docstring): refuse loudly rather than silently no-op a request that
        # named it, which would look like success while writing nothing.
        if body.upload is not None:
            raise HTTPException(
                status_code=400,
                detail="'upload' cannot be overridden at the event level")
        # 'detect' is the same case and needs the same guard. It is not in
        # OVERRIDE_SECTIONS, so the patch filter below would DROP it and this
        # route would answer 200 having written nothing - a silent no-op, the
        # failure shape this project names as its recurring one. Refusal
        # rather than support because the provider choice is ACCOUNT-scoped
        # (it decides whose API key and whose quota get spent), which places
        # it with 'upload' rather than with colors/fonts: the channel Brand
        # editor sets it, and an event-level override stays a hand edit.
        if body.detect is not None:
            raise HTTPException(
                status_code=400,
                detail="'detect' cannot be overridden at the event level")
        # Derive the patch from the fields the client explicitly SET
        # (model_fields_set), not from "is not None": an event editor sends
        # an explicit `logo: null` to mean "override to no logo", which is
        # indistinguishable from an absent/inherited field under an
        # is-not-None filter - that filter silently dropped the override.
        # A field merely absent from the body is never in model_fields_set,
        # so omission still means "inherit" as before.
        patch = {k: getattr(body, k) for k in body.model_fields_set
                 if k in event_brand_admin.OVERRIDE_SECTIONS}
        try:
            event_brand_admin.update_event_brand(channels_dir, channel, event, patch)
            return _event_brand_with_fonts(channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error

    @app.post(EV + "/fonts/{filename}", status_code=201)
    async def upload_event_font(channel: str, event: str, filename: str, request: Request) -> dict:
        data = await _read_body_capped(request, font_admin.MAX_FONT_BYTES)
        try:
            font_admin.save_event_font(channels_dir, channel, event, filename, data)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_event_fonts(channels_dir, channel, event)}

    @app.delete(EV + "/fonts/{filename}")
    def delete_event_font(channel: str, event: str, filename: str) -> dict:
        try:
            font_admin.delete_event_font(channels_dir, channel, event, filename)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_event_fonts(channels_dir, channel, event)}

    @app.post(EV + "/brand/preview")
    def event_brand_preview(channel: str, event: str, body: BrandPatchBody):
        import io as _io

        from ..overlay import build_overlay
        from ..profile import _resolve_logo
        try:
            data = event_brand_admin.read_event_brand(channels_dir, channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error
        # Same explicit-null-vs-absent distinction as put_event_brand: use
        # model_fields_set so a previewed `logo: null` renders WITHOUT the
        # channel logo instead of silently re-inheriting it.
        patch = {k: getattr(body, k) for k in body.model_fields_set
                 if k in event_brand_admin.OVERRIDE_SECTIONS}
        merged = deep_merge(data["channel"], patch)
        channel_dir = channels_dir / channel
        event_dir = channel_dir / "events" / event
        fonts = merged.get("fonts") or {}
        # Resolve event-first (event_brand_admin.resolve_event_font_ref), so an
        # unsaved font ref the client is trying out - even one that only exists
        # under the event's own fonts/ - previews the same as PUT would accept.
        try:
            resolved_fonts = {
                role: str(event_brand_admin.resolve_event_font_ref(
                    event_dir, channel_dir, ref, what=f"font {role!r}"))
                for role, ref in fonts.items()}
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(
                status_code=409,
                detail=f"cannot render preview (check the assigned fonts): {error}") from error
        config = {"colors": merged.get("colors") or {}, "fonts": resolved_fonts,
                  "output": merged.get("output") or {}, "bands": merged.get("bands")}
        if isinstance(merged.get("logo"), dict):
            holder = {"logo": merged["logo"]}
            _resolve_logo(holder, event_dir, channel_dir)
            config["logo"] = holder["logo"]
        channel_json = _load_channel(channel)
        hook = channel_json.get("display_name", channel)
        footer = channel_json.get("footer", "")
        try:
            image = build_overlay(hook, footer, config)
        except Exception as error:   # noqa: BLE001 - a missing/invalid font or bad geometry
            _logger.warning("event brand preview failed for %s/%s: %s", channel, event, error)
            raise HTTPException(
                status_code=409,
                detail="cannot render preview (check the assigned fonts)") from error
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")

    @app.get(CH + "/events")
    def get_events(channel: str) -> list[dict]:
        # list_events already refuses a bad segment by returning [], but every
        # other channel route answers 400 - match them.
        try:
            pathnames.validate_segment(channel, what="channel name")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return [vars(e) for e in list_events(channels_dir, channel)]

    def _admin_status(error: event_admin.EventAdminError) -> int:
        return {"bad_name": 400, "not_found": 404,
                "exists": 409, "locked": 409}.get(error.kind, 400)

    def _event_entry(channel: str, name: str) -> dict:
        for entry in list_events(channels_dir, channel):
            if entry.name == name:
                return vars(entry)
        # Just created/renamed but not found in the listing (should not happen)
        # - return a zero-count entry rather than 500 the successful mutation.
        return {"name": name, "clip_count": 0, "kept_count": 0, "rendered_count": 0}

    @app.post(CH + "/events", status_code=201)
    def create_event(channel: str, body: EventNameBody) -> dict:
        try:
            event_admin.create_event(channels_dir, channel, body.name)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error)) from error
        return _event_entry(channel, body.name)

    @app.patch(EV)
    def rename_event(channel: str, event: str, body: EventNameBody) -> dict:
        try:
            event_admin.rename_event(channels_dir, channel, event, body.name)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error)) from error
        return _event_entry(channel, body.name)

    @app.delete(EV)
    def delete_event(channel: str, event: str) -> dict:
        try:
            event_admin.delete_event(channels_dir, channel, event)
        except event_admin.EventAdminError as error:
            raise HTTPException(status_code=_admin_status(error), detail=str(error)) from error
        return {"deleted": event}

    # ---- Moments lexicon: workspace, channel and event layers ----
    # Segment validation (bad_name) and existence checks (not_found) both
    # happen INSIDE lexicon_admin, before any filesystem touch (see its own
    # _resolve) - these routes only map the resulting kind to a status, the
    # same way every other admin route in this file does. No second, possibly
    # divergent guard is added here.

    def _moments_or_http(call):
        """Run a lexicon_admin call, mapping its typed error to a status the
        way every other admin route in this file does."""
        try:
            return call()
        except lexicon_admin.LexiconAdminError as error:
            status = {"bad_name": 400, "not_found": 404, "bad_markers": 400}
            raise HTTPException(status_code=status.get(error.kind, 400),
                                detail=str(error)) from error

    @app.get("/api/moments")
    def get_workspace_moments() -> dict:
        root = _resolve_workspace().root
        return _moments_or_http(lambda: lexicon_admin.read(root))

    @app.put("/api/moments")
    def put_workspace_moments(body: MarkersBody) -> dict:
        root = _resolve_workspace().root
        _moments_or_http(lambda: lexicon_admin.update(root, body.markers))
        return _moments_or_http(lambda: lexicon_admin.read(root))

    @app.get(CH + "/moments")
    def get_channel_moments(channel: str) -> dict:
        root = _resolve_workspace().root
        return _moments_or_http(lambda: lexicon_admin.read(root, channel=channel))

    @app.put(CH + "/moments")
    def put_channel_moments(channel: str, body: MarkersBody) -> dict:
        root = _resolve_workspace().root
        _moments_or_http(lambda: lexicon_admin.update(root, body.markers, channel=channel))
        return _moments_or_http(lambda: lexicon_admin.read(root, channel=channel))

    @app.get(EV + "/moments")
    def get_event_moments(channel: str, event: str) -> dict:
        root = _resolve_workspace().root
        return _moments_or_http(
            lambda: lexicon_admin.read(root, channel=channel, event=event))

    @app.put(EV + "/moments")
    def put_event_moments(channel: str, event: str, body: MarkersBody) -> dict:
        root = _resolve_workspace().root
        _moments_or_http(
            lambda: lexicon_admin.update(root, body.markers, channel=channel, event=event))
        return _moments_or_http(
            lambda: lexicon_admin.read(root, channel=channel, event=event))

    @app.post("/api/moments/adopt-default")
    def post_adopt_default_moments() -> dict:
        root = _resolve_workspace().root
        _moments_or_http(lambda: lexicon_admin.adopt_default(root))
        return _moments_or_http(lambda: lexicon_admin.read(root))

    # ---- Glossary: workspace, channel and event layers ----
    # Segment validation (bad_name) and existence checks (not_found) both
    # happen INSIDE glossary_admin, before any filesystem touch (see its own
    # _resolve) - these routes only map the resulting kind to a status, the
    # same way the moments routes above do. No second, possibly divergent
    # guard is added here.

    def _glossary_or_http(call):
        """Run a glossary_admin call, mapping its typed error to a status the
        way every other admin route in this file does."""
        try:
            return call()
        except glossary_admin.GlossaryAdminError as error:
            status = {"bad_name": 400, "not_found": 404, "bad_glossary": 400}
            raise HTTPException(status_code=status.get(error.kind, 400),
                                detail=str(error)) from error

    @app.get("/api/glossary")
    def get_workspace_glossary() -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(lambda: glossary_admin.read(root))

    @app.put("/api/glossary")
    def put_workspace_glossary(body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, track=body.track))
        return _glossary_or_http(lambda: glossary_admin.read(root))

    @app.get(CH + "/glossary")
    def get_channel_glossary(channel: str) -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(lambda: glossary_admin.read(root, channel=channel))

    @app.put(CH + "/glossary")
    def put_channel_glossary(channel: str, body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, track=body.track, channel=channel))
        return _glossary_or_http(lambda: glossary_admin.read(root, channel=channel))

    @app.get(EV + "/glossary")
    def get_event_glossary(channel: str, event: str) -> dict:
        root = _resolve_workspace().root
        return _glossary_or_http(
            lambda: glossary_admin.read(root, channel=channel, event=event))

    @app.put(EV + "/glossary")
    def put_event_glossary(channel: str, event: str, body: GlossaryBody) -> dict:
        root = _resolve_workspace().root
        _glossary_or_http(lambda: glossary_admin.update(
            root, body.terms, body.replacements, track=body.track,
            channel=channel, event=event))
        return _glossary_or_http(
            lambda: glossary_admin.read(root, channel=channel, event=event))

    @app.get("/api/tracks")
    def get_tracks() -> dict:
        """Every venue in the registry, id and name only - what the event
        editor's track selector renders. A read of shipped code, so it needs
        neither the workspace nor a profile."""
        return {"tracks": tracks.listing()}

    # ---- Auth: channel-scoped (no event) ----

    @app.get(CH + "/auth")
    def get_auth(channel: str) -> dict:
        import datetime

        from ..google_oauth import GoogleOAuth
        channel_id = _load_channel(channel)["id"]
        auth_dir = _resolve_workspace().root / "auth"
        try:
            creds = load_credentials(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth())
            connected = creds is not None
        except Exception:
            connected = False
        remaining = QuotaTracker(auth_dir, channel_id).remaining_uploads(
            datetime.datetime.now(datetime.timezone.utc))
        return {"connected": connected, "channel_id": channel_id,
                "remaining_uploads": remaining,
                "upload_mode": upload_policy.mode(_channel_config(channel))}

    @app.post(CH + "/auth/connect")
    def post_connect(channel: str, body: ConnectBody) -> dict:
        # Runs the browser OAuth consent from the studio so an operator never has
        # to drop to a terminal. The channel id is pre-filled from channel.json but
        # editable (a multi-channel operator may connect another channel); an
        # omitted id defaults to this channel's own id. A render-only channel has
        # no token to store - refuse before touching google or starting a job.
        # _load_channel first: it validates the {channel} segment before any
        # filesystem read from it (so the _channel_config brand.json read below
        # never touches an out-of-tree path).
        channel_json = _load_channel(channel)
        try:
            upload_policy.require_api_upload(_channel_config(channel))
        except upload_policy.RenderOnlyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        channel_id = body.channel_id or channel_json.get("id")
        if not channel_id:
            raise HTTPException(
                status_code=404, detail=f"channel {channel!r} has no stored id to connect")
        try:
            google_require("upload")
        except GoogleUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        # Connect writes only to the workspace auth dir and takes no event lock,
        # so start_connect_job needs no profile - it uses only the channel id. A
        # duplicate in-flight connect for the same channel is refused (409).
        try:
            job = jobs.start_connect_job(None, app.state.job_store, channel_id,
                                         force=body.force)
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}

    @app.get("/api/settings")
    def get_settings() -> dict:
        import datetime

        from ..google_oauth import GoogleOAuth
        ws = _resolve_workspace()
        auth_dir = ws.root / "auth"
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            google_require("upload")
            google_ok = True
        except GoogleUnavailable:
            google_ok = False
        infos = list_channels(channels_dir)
        rows = []
        for info in infos:
            row = {"channel": info.name, "channel_id": "", "upload_mode": "api",
                   "connected": False, "remaining_uploads": None,
                   "detect_provider": providers.DEFAULT_PROVIDER, "detect_model": "",
                   "error": info.error}
            if info.error is None:
                try:
                    channel_id = _load_channel(info.name).get("id", "")
                except HTTPException as error:
                    # An odd directory name (fails segment validation) or a
                    # channel.json that vanished between listing and read - list
                    # the row with the reason, never 500 the whole page.
                    row["error"] = error.detail
                    rows.append(row)
                    continue
                row["channel_id"] = channel_id
                config = _channel_config(info.name)
                row["upload_mode"] = upload_policy.mode(config)
                # Which provider scores this channel's moments, read-only (the
                # channel Brand editor is where it is set). Every value is
                # type-checked before it is trusted rather than merely
                # defaulted: brand.json is hand-editable and this read does NOT
                # go through profile.load's validation, so `"detect": "gemini"`
                # or `{"provider": ["gemini"]}` reaches here intact - and a
                # bare .get() on the first would AttributeError the WHOLE
                # Settings page over one channel's typo, which is the failure
                # the neighbouring guards in this loop exist to prevent.
                detect = config.get("detect")
                if isinstance(detect, dict):
                    selected = detect.get("provider")
                    if isinstance(selected, str) and selected:
                        row["detect_provider"] = selected
                    model = detect.get("model")
                    if isinstance(model, str):
                        row["detect_model"] = model
                if channel_id:
                    try:
                        creds = load_credentials(channel_id, auth_dir=auth_dir, oauth=GoogleOAuth())
                        row["connected"] = creds is not None
                    except Exception:   # noqa: BLE001 - any auth failure = "not connected", never a 500
                        row["connected"] = False
                    try:
                        row["remaining_uploads"] = QuotaTracker(
                            auth_dir, channel_id).remaining_uploads(now)
                    except Exception:   # noqa: BLE001 - a warn-only estimate; null if uncomputable
                        row["remaining_uploads"] = None
            rows.append(row)
        queue = getattr(app.state, "job_queue", None)
        queue_worker = getattr(app.state, "worker", None)
        return {"workspace": {"root": str(ws.root), "origin": ws.origin,
                              "channel_count": len(infos),
                              "google_upload_available": google_ok},
                "channels": rows,
                # The job queue's pool limits, workspace-level like
                # everything else on this screen. Read from the LIVE queue
                # where there is one, so what is shown is what is actually
                # in force, and from the settings file otherwise - a studio
                # whose workspace would not resolve can still show (and set)
                # the number it will use once one does. `pools` is derived
                # from jobs.KINDS so the screen renders one field per pool
                # without a hardcoded list of its own.
                "queue": {
                    "available": queue is not None,
                    "limits": (queue.limits() if queue is not None
                               else _stored_limits(ws.root)),
                    "pools": _queue_pools(),
                    "worker_running": bool(queue_worker is not None
                                           and queue_worker.is_running()),
                },
                # Booleans and shipped constants only - never the key, never a
                # path, exactly like the YouTube connection state above.
                # `sdk_installed` answers with importlib.util.find_spec, so
                # rendering this page does not import three vendor SDKs into
                # the studio process.
                # `prices` is the provider's own PRICES table verbatim (USD per
                # 1M input/output tokens), carried so the brand editor's
                # provider picker can disclose what a choice costs AT the
                # moment of choosing - the selected model's rate beside that
                # provider's cheapest priced one. It is a shipped constant like
                # `install` and `default_model`, dated in each provider module
                # and documented there as a FLOOR rather than a bill; nothing
                # here is derived from a key or from usage.
                "providers": [
                    {"id": module.PROVIDER_ID,
                     "default_model": module.DEFAULT_MODEL,
                     "key_present": providers.has_api_key(auth_dir, module.KEY_FILENAME),
                     "sdk_installed": providers.sdk_installed(module.PACKAGE),
                     "install": module.INSTALL,
                     "verified": module.VERIFIED,
                     "prices": {model: list(rate)
                                for model, rate in module.PRICES.items()}}
                    for module in providers.ordered()
                ]}

    @app.delete(CH + "/auth")
    def disconnect_auth(channel: str) -> dict:
        # Forget the stored token for this channel (its channel.json id). Writes
        # nothing but the removal of auth/token-<id>.json; never touches the
        # client secret or quota, and imports no google. {channel} is validated
        # inside _load_channel before any filesystem touch.
        channel_id = _load_channel(channel).get("id")
        if not channel_id:
            # A well-formed channel.json missing its id has no token to forget -
            # a clean 404, never a KeyError 500 (get_settings guards the same read).
            raise HTTPException(status_code=404, detail=f"channel {channel!r} has no stored id")
        auth_dir = _resolve_workspace().root / "auth"
        if not auth.forget_credentials(channel_id, auth_dir=auth_dir):
            raise HTTPException(
                status_code=404, detail=f"channel {channel!r} was not connected")
        return {"disconnected": channel_id}

    # ---- Model-provider API keys: workspace-scoped, one file per provider ----

    def _provider_or_404(provider_id: str):
        """The provider module, or 404.

        Validated against the REGISTRY, not against a name pattern: the set of
        valid providers is CLOSED, which is stronger than
        pathnames.validate_segment and means a request-supplied id cannot
        reach the filesystem at all. The key filename then comes from the
        resolved module (module.KEY_FILENAME), never from the URL - which is
        what makes it safe that providers.save_api_key/forget_api_key take
        their `filename` unvalidated. These routes are the first place a
        client-supplied identifier reaches that code; every other caller
        passes a module constant.
        """
        try:
            return providers.get(provider_id)
        except providers.UnknownProvider as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.put("/api/providers/{provider_id}/key")
    async def put_provider_key(provider_id: str, request: Request) -> dict:
        """Stores this provider's API key at <workspace>/auth/<provider>.json.

        The key is written 0600 and atomically (see providers.save_api_key)
        and is NEVER returned, logged or quoted in an error message. Neither
        this response nor GET /api/settings hands it back: every field either
        of them returns is a BOOLEAN, a shipped constant (`default_model`,
        `install`, `verified`, the `prices` table) or a public identifier -
        never a token, a secret, a key or a path into `auth/`, exactly as the
        YouTube connection state does. This said "booleans only" for a
        while, which was the right security claim in the wrong shape: neither
        payload has ever carried only booleans. Every rejection is save_api_key's
        own ValueError mapped to a 400; nothing about what a usable key is is
        re-implemented here, so there is no second copy to drift and no
        f-string that could interpolate the value.

        The body is read and shaped HERE rather than declared as a pydantic
        model, which is the same `request: Request` idiom `upload_font` uses
        and is what closes the last hole in that rule. A model - even one
        typing `api_key` as `Any`, which is what this route used to have -
        only ever sees a body pydantic could BUILD it from: a body that is
        not a JSON object at all (a bare `"sk-real-key"`, a bare list, or
        unparsable JSON) never reaches the route, and FastAPI's own 422
        handler quotes the offending `input` straight back to the client.
        Reading it here means every unusable body - wrong shape, wrong type,
        missing field - is one 400 whose detail is a FIXED sentence naming a
        constraint and interpolating nothing.
        """
        provider = _provider_or_404(provider_id)
        try:
            body = await request.json()
        except Exception as error:      # noqa: BLE001 - any decode failure, one answer
            # Never str(error): a JSON decoder's own message can quote a
            # fragment of the input it choked on, and the input is a key.
            raise HTTPException(
                status_code=400,
                detail="the request body must be a JSON object") from error
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="the request body must be a JSON object with an 'api_key' field")
        auth_dir = _resolve_workspace().root / "auth"
        try:
            providers.save_api_key(auth_dir, provider.KEY_FILENAME, body.get("api_key"))
        except ValueError as error:
            # save_api_key's messages name the constraint, never the value.
            raise HTTPException(status_code=400, detail=str(error)) from error
        except OSError as error:
            # Type name only: an OSError's own message carries the path, and
            # this one is a key file - the operator learns the class of
            # failure, the log learns nothing it should not.
            raise HTTPException(
                status_code=500,
                detail=f"cannot write the key file: {type(error).__name__}") from error
        return {"provider": provider.PROVIDER_ID, "key_present": True}

    @app.delete("/api/providers/{provider_id}/key")
    def delete_provider_key(provider_id: str) -> dict:
        """Forgets this provider's key. Reversible by pasting it again - the
        same shape as disconnecting a YouTube channel, and equally touching
        nothing else in auth/ (not the client secret, not another provider's
        key, not a token)."""
        provider = _provider_or_404(provider_id)
        auth_dir = _resolve_workspace().root / "auth"
        try:
            forgotten = providers.forget_api_key(auth_dir, provider.KEY_FILENAME)
        except OSError as error:
            # Mirrors PUT's OSError branch above: type name only, never the
            # error's own message, which would carry the key file's path.
            raise HTTPException(
                status_code=500,
                detail=f"cannot remove the key file: {type(error).__name__}") from error
        if not forgotten:
            raise HTTPException(
                status_code=404,
                detail=f"no key was stored for {provider.PROVIDER_ID}")
        return {"provider": provider.PROVIDER_ID, "key_present": False}

    # ---- Workspaces: list/switch/create, plus the FS browser that feeds them ----
    # The one write path outside editorial.save/the admin modules: this manages
    # WHICH workspace the whole app points at, not anything inside one. See
    # _switch_to above for the re-root mechanism this all funnels through.

    @app.get("/api/workspaces")
    def get_workspaces() -> dict:
        ws = _resolve_workspace()
        recent = _workspaces.read_config(_config_home()).get("recent", [])
        return {
            "current": {
                "path": str(ws.root),
                "name": _workspaces.workspace_name(ws.root),
                "origin": ws.origin,
                "locked": ws.origin == "YT_SHORTS_DATA",
            },
            "recent": [
                {"path": p, "name": _workspaces.workspace_name(Path(p)),
                 "valid": _workspaces.is_workspace(Path(p))}
                for p in recent
            ],
        }

    def _guard_reroot() -> None:
        # Refuse to re-root while YT_SHORTS_DATA pins the workspace (an
        # explicit operator override this UI must not silently move out from
        # under), or while a job is running against the CURRENT workspace -
        # a render/upload/detect job holds paths captured before the switch,
        # so letting one finish against a workspace that just moved would
        # write into the wrong place.
        if _resolve_workspace().origin == "YT_SHORTS_DATA":
            raise HTTPException(
                status_code=409,
                detail="workspace is set by the YT_SHORTS_DATA environment variable; "
                       "unset it to manage workspaces here")
        if app.state.job_store.any_running():
            raise HTTPException(
                status_code=409,
                detail="a job is running - wait for it to finish before switching workspace")

    def _select(root: Path) -> dict:
        _workspaces.adopt(root, _now_iso())
        _switch_to(root)
        cfg = _workspaces.read_config(_config_home())
        cfg["current"] = str(root)
        cfg["recent"] = _workspaces.push_recent(cfg.get("recent", []), str(root))
        _workspaces.write_config(_config_home(), cfg)
        return get_workspaces()

    @app.post("/api/workspaces/switch")
    def switch_workspace(body: WorkspaceSwitchBody) -> dict:
        _guard_reroot()
        root = Path(body.path)
        if not _workspaces.is_workspace(root):
            raise HTTPException(status_code=400, detail=f"{root} is not a workspace")
        return _select(root)

    @app.post("/api/workspaces/create")
    def create_workspace_route(body: WorkspaceCreateBody) -> dict:
        _guard_reroot()
        try:
            root = _workspaces.create_workspace(Path(body.parent), body.name, _now_iso())
        except _workspaces.WorkspaceError as error:
            status = {"bad_name": 400, "exists": 409, "not_found": 404}.get(error.kind, 400)
            raise HTTPException(status_code=status, detail=str(error)) from error
        return _select(root)

    @app.post("/api/workspaces/copy")
    def copy_workspace_route(body: WorkspaceCreateBody) -> dict:
        _guard_reroot()
        # The copy source is the CURRENT workspace root. If resolution fell
        # all the way through to the repository fallback (no YT_SHORTS_DATA,
        # no ~/YT-Shorts-Data), that root IS the git repository itself -
        # cloning it would copytree .git, src/, everything. _guard_reroot
        # does not catch this (it only refuses origin == "YT_SHORTS_DATA"),
        # so refuse here too.
        if _resolve_workspace().origin == "repository":
            raise HTTPException(
                status_code=409,
                detail="the current workspace is the repository fallback; "
                       "open or create a real workspace before copying")
        src = channels_dir.parent  # the CURRENT workspace root (channels_dir is <root>/channels)
        job = jobs.start_copy_job(app.state.job_store, src, Path(body.parent), body.name,
                                  _now_iso(), _select)
        return {"job_id": job.id}

    @app.get("/api/fs")
    def browse_fs(path: str | None = None) -> dict:
        base = Path(path) if path else Path.home()
        return _workspaces.list_dir(base)

    # ---- Streams: the channel's catalogue, cached per channel ----
    # Cache lives in this closure so it dies with the app - the same
    # in-memory, single-session limit the render job store has. Keyed by
    # channel so a multi-channel session does not serve one channel's
    # catalogue for another. What is cached is the EXPENSIVE half only: the
    # yt-dlp reads. `has_transcript`/`has_analysis` are stat'd fresh on
    # every response, because a cached "no transcript" would survive a
    # finished transcription until someone pressed refresh - and a marker
    # that is not true is worse than no marker.
    streams_cache: dict[str, Catalogue] = {}

    @app.get(EV + "/streams")
    def get_streams(channel: str, event: str, refresh: bool = False) -> dict:
        profile = _load_profile(channel, event)
        if channel not in streams_cache or refresh:
            try:
                streams_cache[channel] = channel_catalogue(
                    profile.channel["channel_url"])
            except YouTubeError as error:
                raise HTTPException(status_code=502, detail=str(error)) from error
        catalogue = streams_cache[channel]
        root = _resolve_workspace().root
        return {
            "videos": [
                {"video_id": video.video_id, "title": video.title,
                 "duration_seconds": video.duration_seconds,
                 "view_count": video.view_count,
                 "playlist_ids": list(video.playlist_ids),
                 "has_transcript": has_cached_transcript(root, video.video_id),
                 "has_analysis": has_analysis(root, video.video_id)}
                for video in catalogue.videos],
            "playlists": [
                {"id": playlist.id, "title": playlist.title,
                 "count": playlist.count, "unavailable": playlist.unavailable}
                for playlist in catalogue.playlists],
            "failed_playlists": [
                {"title": failure.title, "reason": failure.reason}
                for failure in catalogue.failed_playlists],
        }

    @app.post(EV + "/streams/{video_id}/detect")
    def post_detect(channel: str, event: str, video_id: str) -> dict:
        """Starts a detection job DIRECTLY, outside the queue.

        **The studio's own page no longer calls this**, and neither do
        `POST …/render` or `POST …/clips/{name}/trim` below: all three
        actions enqueue instead (`POST /api/jobs`), so an operator can
        schedule, reorder, pause and stop them and can see them on the Jobs
        screen. A click that started work here appeared nowhere in the plan
        and could not be stopped at all, which is the gap that change closed.

        It stays anyway, deliberately, and not as dead weight: it is the API
        for anything that is not this browser (a script, a curl, another
        tool), several tests drive it, and it is the shape the queue's own
        starter mirrors. What it does NOT have is a queue's guarantees - it
        takes the EventLock synchronously and 409s a busy event rather than
        waiting, and its job lives only in this process's memory.
        """
        profile = _load_profile(channel, event)
        title = ""
        catalogue = streams_cache.get(channel)
        if catalogue is not None:
            for video in catalogue.videos:
                if video.video_id == video_id:
                    title = video.title
                    break
        try:
            # NO detect_fn: start_detect_job's own default requires a CACHED
            # transcript (detect.require_cached_transcript) and never
            # transcribes on its own. The override that kept the old
            # on-demand-transcribe behaviour alive here is gone (Task 6), so
            # this route and a queued `detect` entry now do the same thing -
            # detection no longer silently starts an hour of Whisper decode
            # off the back of a click. A stream nobody has transcribed fails
            # this job with TranscriptNotCached; the way to get one is a
            # `transcribe` job (POST /api/jobs), which the Jobs screen and
            # the stream view both reach.
            job = jobs.start_detect_job(profile, app.state.job_store, video_id,
                                        title)
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}

    @app.get(EV + "/streams/{video_id}/moments")
    def get_stream_analysis(channel: str, event: str, video_id: str) -> dict:
        """The detection analysis, or an EMPTY one when detection never ran.

        Deliberately not a 404 for the absent case: this screen is specified to
        be useful with no API key and no detection at all, so "not analysed
        yet" is an ordinary state the client renders as an empty hit list. A
        404 would push the client into its error path for a normal condition.
        A corrupt file IS a fault and does not get the same treatment.

        The "not analysed" shape still carries a REAL `activity` and
        `duration_seconds` when a transcript exists, computed locally with
        `moments.activity_curve` - the same function `detect_moments` calls,
        just run here instead of cached in moments.json. Without this, the
        overview strip was empty on the screen's own flagship state (no key,
        no detection run), which contradicts activity_curve's own docstring
        promise ("useful before detection has ever run"): the curve used to
        exist ONLY as a side effect of a detection run, so a stream that was
        never analysed showed zero bars no matter how much transcript it had.
        A transcript-less stream (no derived files at all yet) still gets the
        fully empty shape - there is nothing to compute a curve FROM.

        "Instead of cached" is the whole cost, and it is not free: growth is
        QUADRATIC in stream duration, because activity_curve re-scans the
        whole word list once per 60-second bucket rather than walking it
        once. Measured with the 39-marker default lexicon on synthetic
        transcripts at ~2.8 words/s: a 2h stream (20,160 words) costs 0.046s,
        4h (40,320 words) costs 0.168s, and 8h (80,641 words) costs 0.639s -
        roughly quadrupling each time duration doubles. At 8h, reading and
        parsing transcript.json itself (6.1 MB) adds a further 0.045s, for
        0.726s total per request. This is acceptable TODAY for three
        independent reasons, not one: it runs in Starlette's threadpool and
        so never blocks the event loop; it only happens on the never-analysed
        path (once moments.json exists, the cached curve is one file read,
        not a recompute); and StreamScreen has no focus/visibilitychange
        refetch, so this runs per-mount rather than per-tab-switch. Scaling
        this to a 24h+ stream without re-checking those three would change
        the trade-off - do not assume it stays cheap.
        """
        profile = _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            analysis = stream_analysis.read_analysis(root, video_id)
            # `configured_provider` (detect.py) postdates the first analyses
            # ever written, and the stream screen's StreamAnalysis type
            # declares it as a plain `string | null`. Default it here so a
            # moments.json written before the field existed answers with null
            # rather than handing the client `undefined` - the same reason the
            # never-analysed synthesis below carries it explicitly.
            analysis.setdefault("configured_provider", None)
            return analysis
        except ValueError as error:            # a bad path segment
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            if error.kind == "not_found":
                activity: list[float] = []
                duration_seconds = 0.0
                try:
                    transcript = stream_analysis.read_transcript(root, video_id)
                except stream_analysis.AnalysisError:
                    pass    # no transcript either - the fully empty shape below
                else:
                    lexicon = profile.config.get("lexicon", LEXICON_EMPTY)
                    # Same degrade-to-empty-curve treatment as the corrupt-JSON
                    # branch above (read_transcript's AnalysisError, caught two
                    # lines up): a transcript.json that parses but has no
                    # "words" key raises KeyError here, and a word missing
                    # "end"/"start" raises KeyError inside activity_curve
                    # itself - both reproduced through this route. Malformed
                    # SHAPE is not a more serious fault than malformed JSON;
                    # it gets the same 200-with-empty-curve rather than a 500.
                    try:
                        activity = activity_curve(transcript.get("words", []), lexicon)
                    except (KeyError, TypeError):
                        activity = []
                    duration_seconds = transcript.get("duration_seconds", 0.0)
                return {"video_id": video_id, "stream_title": "", "engine": None,
                        "configured_provider": None,
                        "created_at": None, "duration_seconds": duration_seconds,
                        "activity": activity, "moments": [], "missing_windows": [],
                        "missing_chunks": []}
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get(EV + "/streams/{video_id}/transcript")
    def get_stream_transcript(channel: str, event: str, video_id: str) -> dict:
        """The whole stream transcript. 404 when there is none.

        The opposite of the analysis route above, on purpose: without a
        transcript this screen's main pane has nothing to show, and an empty
        document would read as a silent stream rather than a missing file.
        """
        _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            return stream_analysis.read_transcript(root, video_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            status = 404 if error.kind == "not_found" else 500
            raise HTTPException(status_code=status, detail=str(error)) from error

    @app.post(EV + "/streams/{video_id}/estimate")
    def post_estimate(channel: str, event: str, video_id: str,
                      body: EstimateRequest) -> dict:
        """What a detection run over this stream would cost. Reads only.

        Local and approximate on purpose (see yt_shorts.estimate's own
        docstring) - this screen is specified to work with no API key at
        all, so a preview that itself needed one would fail exactly where it
        is most wanted. 404 when there is no transcript yet, same mapping
        `get_stream_transcript` above uses.
        """
        profile = _load_profile(channel, event)
        root = _resolve_workspace().root
        try:
            transcript = stream_analysis.read_transcript(root, video_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except stream_analysis.AnalysisError as error:
            status = 404 if error.kind == "not_found" else 500
            raise HTTPException(status_code=status, detail=str(error)) from error
        settings = profile.config.get("detect", {}) or {}
        # The SELECTED provider supplies both the default model and the price
        # table. Reading either from the default provider would quote one
        # vendor's rates for a run that will be billed by another - and
        # plausibly enough that nobody would notice.
        #
        # `settings.get(key) or default`, not `settings.get(key, default)`:
        # profile._validate_detect blesses an explicit null as equivalent to
        # an absent key (same as the `or {}` on the line above, one level
        # up), and dict.get's default argument does not cover a key present
        # with value None - only its absence.
        provider = providers.get(settings.get("provider") or providers.DEFAULT_PROVIDER)
        model = body.model or settings.get("model") or provider.DEFAULT_MODEL
        return estimate.estimate_run(transcript["words"],
                                     profile.config.get("lexicon", LEXICON_EMPTY),
                                     model=model, prices=provider.PRICES)

    @app.post(EV + "/streams/{video_id}/clips")
    def post_clip_from_window(channel: str, event: str, video_id: str,
                              body: NewClipWindow) -> dict:
        """Create a clip from a window the operator chose. The ONE studio write
        outside edit.json and a render (see CLAUDE.md's amended boundary).

        The three failures are three different sentences on purpose: a
        collision, an unreadable neighbour and a held lock each have a
        different remedy, and clip_from_moment defines two exception types
        precisely so a caller can tell the first two apart.
        """
        profile = _load_profile(channel, event)
        try:
            pathnames.validate_segment(video_id, what="video id")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        # The same EventLock a render and a detect take: creating a clip
        # restructures the event's clip directory, and a concurrent render
        # walking that directory must not see it half-built.
        lock = EventLock(profile.event_dir)
        try:
            lock.acquire()
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            directory = create_clip(profile.event_dir, video_id=video_id,
                                    start=body.start, end=body.end, hook=body.hook,
                                    source_title=body.source_title)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (ClipIdentityCollision, ClipIdentityUnreadable) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            lock.release()
        return {"name": directory.name}

    # ---- Clips + editor: event-scoped ----

    @app.post(EV + "/clips/{name}/upload")
    def post_upload(channel: str, event: str, name: str,
                    body: UploadRequestBody | None = None) -> dict:
        """Starts an upload job directly - and unlike `post_render`,
        `post_detect` and `post_trim` above, this is still what the studio's
        own page calls.

        Upload is the one action that cannot go through the queue, and the
        reason is the guardrail two lines below rather than anything about
        scheduling. A non-private or scheduled upload is legal only when the
        operator confirmed THAT exposure, per upload, in front of a modal
        naming it (see `UploadRequestBody` and `UploadPanel`); an entry in the
        plan is a request that runs minutes or hours later, from a stored
        `params` dict, and a consent recorded at click time is not a consent
        for whatever the clip and the channel look like by then. So
        `_validate_enqueue` has no `upload` kind at all, `upload_policy`'s
        refusal for a `manual` channel stays an answer AT the click (409, not
        a failed entry discovered later), and this route keeps its direct
        `EventLock` - which is also why `blockedByForUpload` is still a
        disabled button in App.tsx where the render and detect ones are not.
        """
        body = body or UploadRequestBody()
        # Default-private guardrail (server half): anything non-private or
        # scheduled must be explicitly confirmed, per upload - see
        # UploadRequestBody's own docstring.
        if (body.visibility != "private" or body.publish_at is not None) and not body.confirm:
            raise HTTPException(
                status_code=400,
                detail="a non-private or scheduled upload must be confirmed (confirm=true)")
        profile = _load_profile(channel, event)
        try:
            upload_policy.require_api_upload(profile.config)
        except upload_policy.RenderOnlyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)
        if edit.status != editorial.KEPT:
            raise HTTPException(
                status_code=409,
                detail=f"only a kept clip can be uploaded; {name} is {edit.status!r}")
        if not clipstore.short_path(directory).exists():
            raise HTTPException(
                status_code=409, detail=f"{name} has no rendered short.mp4 to upload")
        if trim.is_pending(directory, edit):
            raise HTTPException(
                status_code=409,
                detail=f"{name} has a trim that has not been applied yet - "
                       f"apply it before uploading")
        if upload_record.is_uploaded(directory) and not body.force:
            raise HTTPException(
                status_code=409,
                detail=f"{name} was already uploaded; pass force=true to upload again")
        try:
            job = jobs.start_upload_job(profile, app.state.job_store, name, force=body.force,
                                        visibility=body.visibility, publish_at=body.publish_at)
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}

    @app.get(EV + "/clips")
    def list_clips(channel: str, event: str) -> list[dict]:
        profile = _load_profile(channel, event)
        out = []
        for directory in clipstore.iter_clip_dirs(profile.event_dir):
            try:
                clip = clipstore.read_clip(directory)
            except clipstore.ClipStoreError:
                continue
            try:
                edit = editorial.load(directory)
            except editorial.EditError:
                continue
            out.append(_summary(directory, clip, edit))
        return out

    @app.get(EV + "/clips/{name}")
    def get_clip(channel: str, event: str, name: str) -> dict:
        profile = _load_profile(channel, event)
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)
        derived = _derived_words(directory)
        words, conflict = editorial.effective_words(edit, derived)
        detected = [clip.get("start", 0.0), clip.get("end", 0.0)]
        effective = list(editorial.effective_window(edit, detected[0], detected[1]))
        return {**_summary(directory, clip, edit), "words": words, "conflict": conflict,
                "detected_window": detected, "effective_window": effective}

    @app.patch(EV + "/clips/{name}")
    def patch_clip(channel: str, event: str, name: str, body: PatchClipBody) -> dict:
        profile = _load_profile(channel, event)
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)

        fields_set = body.model_fields_set
        title = edit.title
        status = edit.status
        transcript = edit.transcript
        window = edit.window
        upload = edit.upload
        trim_value = edit.trim

        if "title" in fields_set:
            title = body.title
        if "status" in fields_set:
            status = body.status
        if "upload" in fields_set:
            # Validated UPFRONT, same as window below: editorial.save does
            # no validation of its own, and every clip route starts with
            # _load_edit_or_500, so an unvalidated bad shape would not just
            # 500 this request - it would 500 EVERY future request against
            # this clip until edit.json is fixed by hand.
            try:
                editorial.validate_upload_override(body.upload, "upload")
            except editorial.EditError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            upload = body.upload
        if "window" in fields_set:
            if body.window is None:
                window = None
            elif len(body.window) != 2:
                raise HTTPException(
                    status_code=422, detail="window must be [start, end]")
            elif body.window[1] <= body.window[0]:
                # Unvalidated until now: an inverted window ([70, 10]) yields
                # a negative span, and the trim block below computes its
                # validation floor from window[1] - window[0] - so an
                # inverted window silently poisoned every trim request
                # against this clip, including a bare {"trim": [0, 0]} sent
                # only to CLEAR one - "leaves less than 3.0s of a -60.0s
                # clip" for a request that should have succeeded trivially.
                # Reject the cause here rather than let its symptom surface
                # somewhere else.
                raise HTTPException(
                    status_code=422, detail="window must have end > start")
            else:
                window = (float(body.window[0]), float(body.window[1]))
        if "trim" in fields_set:
            if body.trim is None:
                trim_value = None
            else:
                # validate_trim checks the SHAPE as well as the duration
                # relationship - it is the single source of truth `load` itself
                # calls - so this route must not re-implement a length check of
                # its own (that once turned a one-element list into a bare
                # IndexError, i.e. a 500 instead of a 422). Its bool rejection
                # only works here because PatchClipBody.trim is typed
                # list[Any]: a list[float] field lets Pydantic coerce a JSON
                # `true` to `1.0` before this line ever runs, so
                # validate_trim would see a float and never get the chance to
                # reject it - see the field's own comment.
                # clip["duration"] is the DETECTED span - wrong once a window
                # override exists, because nudging the window writes
                # edit.window and never rewrites clip.json, and the render
                # uses the EFFECTIVE window. Reproduced: clip.json
                # duration=20.0, edit.window=(10.0, 70.0) (a real 60s
                # short), PATCH {"trim": [10, 10]} 422'd "leaves less than
                # 3.0s of a 20.0s clip" while the operator watched a
                # 60-second video. Reads the LOCAL `window` (already updated
                # above if this same request also sets "window"), not
                # `edit.window`, so a combined window+trim PATCH validates
                # against the window it is actually saving. ensure_applied's
                # own ffprobe check stays the authority on the real rendered
                # length; this is only the up-front estimate that lets a
                # clean request skip the round trip to a failing ffmpeg call.
                if window is not None:
                    trim_duration = window[1] - window[0]
                else:
                    trim_duration = float(clip.get("duration", 0.0))
                try:
                    editorial.validate_trim(body.trim, trim_duration, "trim")
                except editorial.EditError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
                trim_value = (float(body.trim[0]), float(body.trim[1]))
        if "words" in fields_set:
            # Recorded against the CURRENTLY CACHED derived transcript, not
            # against whatever "based_on" a previous correction carried -
            # this PATCH is a brand new correction, and it supersedes
            # whatever was there before, conflict or not. See
            # editorial.effective_words: the editorial version always
            # wins, so there is nothing to reconcile the old correction
            # against - it is simply replaced.
            derived = _derived_words(directory)
            based_on = editorial.checksum(derived if derived is not None else [])
            # Normalised on the way IN: the operator types "Rei", and
            # captions._to_caption relies on faster-whisper's leading space to
            # know where a word starts (see editorial.normalise_word_boundaries).
            words_payload = editorial.normalise_word_boundaries(
                [w.model_dump() for w in body.words] if body.words is not None else [])
            transcript = {"based_on": based_on, "words": words_payload}

        new_edit = editorial.Edit(title=title, status=status, transcript=transcript,
                                  window=window, upload=upload, trim=trim_value)
        editorial.save(directory, new_edit)

        saved = _load_edit_or_500(directory)
        derived = _derived_words(directory)
        words, conflict = editorial.effective_words(saved, derived)
        return {**_summary(directory, clip, saved), "words": words, "conflict": conflict}

    def _preview_response(profile: Profile, directory: Path, clip: dict,
                           edit: editorial.Edit, at: float, words: list[dict],
                           title: str | None = None) -> Response:
        """Shared by both preview routes: builds the PNG for whatever
        `words` the caller supplied (the saved-and-derived set for GET,
        the client's own unsaved set for POST) against whatever hook the
        caller supplied - `title`, when given, overrides the saved
        editorial title the same way `words` overrides the saved
        transcript; None (the GET route, and the POST route when the
        operator has not touched the title field) falls back to the
        saved effective title, unchanged from before this route learned
        about titles. Pure read: preview.build never writes anything, and
        neither does anything below this line."""
        hook = title if title is not None else editorial.effective_title(
            edit, clip.get("hook", directory.name))
        footer = profile.channel["footer"]
        try:
            png_bytes = preview.build(directory, profile.config, at, words, hook, footer)
        except preview.PreviewError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(content=png_bytes, media_type="image/png")

    @app.get(EV + "/clips/{name}/preview")
    def get_preview(channel: str, event: str, name: str,
                    at: float = Query(..., description="seconds into the clip")):
        profile = _load_profile(channel, event)
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)
        derived = _derived_words(directory)
        words, _conflict = editorial.effective_words(edit, derived)
        return _preview_response(profile, directory, clip, edit, at, words)

    @app.post(EV + "/clips/{name}/preview")
    def post_preview(channel: str, event: str, name: str, body: PreviewBody):
        """The live-preview route: renders `body.words` (and, when given,
        `body.title`) exactly as given, never reading transcript.json or
        edit.json for them - a client mid-edit is holding text that has
        not been saved anywhere yet, and this route's entire reason to
        exist is to draw THAT rather than the last saved state (see
        api.py's module docstring and CLAUDE.md's Stage C note on the
        studio's "correct and see it" promise). `body.title` being None
        (the field's default, and what the studio sends whenever the
        title field itself is untouched) falls back to the saved
        effective title, same as the GET route. No write happens here,
        same as GET: this is a read that happens to take its input from
        the request body instead of from disk."""
        profile = _load_profile(channel, event)
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)
        # Same normalisation as the words PATCH, and for the reason this route
        # exists: it draws what the operator is holding unsaved. Without it the
        # preview would show IT'SREIRACING while the saved render showed
        # IT'S REI RACING.
        words = editorial.normalise_word_boundaries(
            [word.model_dump() for word in body.words])
        return _preview_response(profile, directory, clip, edit, body.at, words,
                                 title=body.title)

    @app.get(EV + "/clips/{name}/short")
    def get_short(channel: str, event: str, name: str, v: str | None = None,
                  as_: str | None = Query(None, alias="as")):
        profile = _load_profile(channel, event)
        directory, _clip = _load_clip_or_404(profile, name)
        target = clipstore.short_path(directory)
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail=f"No short rendered yet for clip: {name!r}",
            )
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
        # `v` is a cache KEY, never a precondition: a stale or garbage token
        # still serves the current file, because refusing it would turn a
        # bookmarked link - or a request already in flight when a render
        # lands - into a 404, a new failure mode invented to protect against
        # nothing. It is read for exactly one purpose: a token that MATCHES
        # the file's current version identifies bytes that cannot change, so
        # THAT response may be cached hard, which is what keeps scrubbing and
        # seeking fast. Anything else must be revalidated, because such a URL
        # carries no identity and caching it is the staleness bug this exists
        # to fix. Checking the match is what keeps `immutable` from being a
        # lie in the window where a render lands between a payload read and
        # the video fetch.
        fresh = v is not None and v == _short_version(directory)
        policy = ("private, max-age=31536000, immutable" if fresh
                  else "private, no-cache")
        return FileResponse(str(target), media_type="video/mp4", filename=target.name,
                            headers={"Cache-Control": policy})

    @app.get(EV + "/clips/{name}/upload-preview")
    def get_upload_preview(channel: str, event: str, name: str) -> dict:
        # The exact metadata an upload would send, computed server-side by
        # build_metadata (title = effective hook, description/tags/category from
        # the channel's upload config; visibility/scheduling are a per-upload
        # operator choice made at upload time, not reflected here) - so the
        # studio's upload confirmation (api) or copy-to-paste panel (manual)
        # shows the real effective values instead of guessing. Reachable in
        # both upload modes. build_metadata validates YouTube's own limits
        # (title/description/tags length, template correctness) and raises
        # UploadError on a violation - mapped to 409 here, the same way
        # preview.PreviewError is mapped to 409 in _preview_response, rather
        # than surfacing as an opaque 500 and hiding the metadata editor
        # (UploadPanel gates its whole editor on this call succeeding).
        profile = _load_profile(channel, event)
        directory, clip = _load_clip_or_404(profile, name)
        edit = _load_edit_or_500(directory)
        try:
            meta = build_metadata(clip, edit, profile.config)
        except UploadError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "title": meta["snippet"]["title"],
            "description": meta["snippet"]["description"],
            "tags": meta["snippet"]["tags"],
            "category_id": meta["snippet"]["categoryId"],
            "made_for_kids": meta["status"]["selfDeclaredMadeForKids"],
        }

    @app.post(EV + "/render")
    def post_render(channel: str, event: str, body: RenderRequest) -> dict:
        """Starts a render job DIRECTLY, outside the queue.

        The studio's page enqueues a `render` entry instead - see
        `post_detect` above for why all three of these routes remain and what
        they do not give an operator.
        """
        profile = _load_profile(channel, event)
        try:
            job = jobs.start_render_job(profile, app.state.job_store, body.clips)
        except ValueError as error:
            # A named clip that is not one safe path segment. `clips` is a
            # BODY list, so it never went through a URL path param's own
            # validation and used to be joined onto the clips dir raw - see
            # clipstore.clip_dir_by_name for what that let through. 400, and
            # before the event lock is taken.
            raise HTTPException(status_code=400, detail=str(error)) from error
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}

    @app.post(EV + "/clips/{name}/trim")
    def post_trim(channel: str, event: str, name: str) -> dict:
        """Starts a trim job DIRECTLY, outside the queue.

        The studio's page enqueues a `trim` entry instead - see `post_detect`
        above for why all three of these routes remain. One thing this route
        does that the queue cannot: it refuses a clip with no rendered short
        up front (409). A queued trim discovers that when it runs and fails
        that one entry with the reason, which is the ordinary queue trade -
        the answer arrives later, on the entry, instead of at the click.
        """
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

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = app.state.job_store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id!r}")
        return job.snapshot()

    # ---- The job queue: the plan, its order, and what may run ----
    #
    # A thin layer over yt_shorts.job_queue (the plan) and studio.worker
    # (what drives it), mapping QueueError.kind to a status exactly the way
    # _brand_status maps BrandAdminError.kind. Registered here, well before
    # the SPA fallback and the /api catch-all at the bottom of this
    # function, like every other /api route.
    #
    # TWO LOCKS, and the division is job_queue.py's, not this file's: the
    # queue holds its own lock, so ONE call into it is already atomic and
    # needs nothing here. `Worker.lock` is the coarser one, for a SEQUENCE
    # of calls that has to be one decision - which is why the routes that
    # mutate AND then read the plan back (to report the entry's new place)
    # take it, and `remove` (one call, nothing read back) does not. The
    # order is always Worker.lock first, then the queue's; nothing below
    # ever calls the other way round.

    def _require_queue() -> JobQueue:
        queue = getattr(app.state, "job_queue", None)
        if queue is None:
            raise HTTPException(status_code=503, detail=_QUEUE_UNAVAILABLE)
        return queue

    def _plan_lock():
        """`Worker.lock` if there is a worker, else nothing to hold.

        The worker is absent exactly when the queue is (see
        _build_queue_and_worker), so in practice every caller here has
        already passed _require_queue - the null case is defensive rather
        than a state a route reaches.
        """
        worker = getattr(app.state, "worker", None)
        return worker.lock if worker is not None else contextlib.nullcontext()

    def _rows(queue: JobQueue) -> list[dict]:
        return [_entry_json(entry, index) for index, entry in enumerate(queue.list())]

    def _entry_response(queue: JobQueue, entry_id: str) -> dict:
        """One entry, with the place it now holds in the plan."""
        for row in _rows(queue):
            if row["id"] == entry_id:
                return {"entry": row}
        # Only reachable if the entry vanished between the mutation and this
        # read - which the Worker.lock the callers hold rules out today.
        raise HTTPException(status_code=404,
                            detail=f"no such queue entry: {entry_id!r}")

    def _validate_enqueue(kind: str, params: dict) -> None:
        """What cannot be left until the entry runs. Raises HTTPException(400).

        Deliberately NOT a second copy of the worker's per-kind parameter
        table (studio/worker.py): a missing `video_id` fails that one entry
        with a reason the Jobs screen shows, and a duplicate table here
        would be free to drift from the one that actually starts the work.
        Two things cannot wait for that:

        - `channel`/`event` become a DIRECTORY NAME when the worker resolves
          the profile, so they are validated as safe segments here, before
          anything is written - the same rule every other write path in this
          project follows.
        - a CLIP NAME, for exactly the same reason and by the same rule:
          `trim`/`upload` take one as `clip` and `render` a list of them as
          `clips`, and each becomes a directory under `<event>/clips/`. This
          was missing while `channel`/`event` were checked, which is precisely
          how it went unnoticed - the guard looked present. Measured: a `trim`
          entry naming `"../../../OUTSIDE"` was accepted here, claimed by the
          worker, and handed to `trim.ensure_applied` - the one function that
          renames `short.mp4` aside and re-encodes it - with a directory
          outside the event. The starters refuse it again at run time (see
          `jobs._refuse_unusable_clip_names`); that is the backstop for a
          hand-edited jobs.json, not the gate.
        - a queued upload's PRIVACY. Anything more exposed than private is an
          explicit, confirmed, per-upload operator choice, and a queue entry
          is written now and run later from a state file that can carry no
          confirmation. `_start_upload` refuses it at run time as well; that
          is the backstop, not the gate - an operator who asked for a public
          upload must be told at the click, not left to find a failed entry.
        """
        spec = jobs.KINDS.get(kind)
        if spec is None or not spec.queueable:
            return      # the queue itself reports these, with the right kind
        for field in ("channel", "event"):
            value = params.get(field)
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"a {kind} job needs a {field!r} in its params")
            try:
                pathnames.validate_segment(value, what=f"{field} name")
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        # `clip`/`clips` are OPTIONAL here, deliberately: a missing one is the
        # worker's own report (a `trim` with no `clip` fails that one entry
        # with a reason), and `render` with no `clips` legitimately means
        # "every clip in the event". Only a name that IS present has to be a
        # usable one.
        clip_names = params.get("clips") if kind == "render" else params.get("clip")
        if isinstance(clip_names, str):
            clip_names = [clip_names]
        for name in clip_names if isinstance(clip_names, list) else ():
            if not isinstance(name, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"a {kind} job's clip name must be a string, not "
                           f"{type(name).__name__}")
            try:
                pathnames.validate_segment(name, what="clip name")
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        if kind != "upload":
            return
        if params.get("visibility") not in (None, "private"):
            raise HTTPException(
                status_code=400,
                detail="a queued upload is always private: a non-private upload "
                       "has to be confirmed per upload, so start that one from "
                       "the clip's own upload panel instead")
        if params.get("publish_at") is not None:
            raise HTTPException(
                status_code=400,
                detail="a queued upload cannot be scheduled: a publish time has "
                       "to be confirmed per upload, so start that one from the "
                       "clip's own upload panel instead")

    @app.get("/api/jobs")
    def list_queue() -> dict:
        """The whole plan, in three sections plus the pool limits.

        `running` holds `stopping` too - the work is still in progress and
        still holding its pool slot - and `queued` holds `paused`, which
        keeps its place in line. Every row carries its `position` in the
        WHOLE plan, which is the index POST …/move takes.

        Five things about this payload that a screen built on it gets
        wrong by default, recorded here rather than in a report because a
        report is not what the next author reads:

        - **`progress` is `{"unit", "done", "total"}` on a RUNNING entry of
          a kind that counts something, and `null` everywhere else.** A
          `transcribe` counts chunks, a `detect` windows and a `render`
          clips; `trim` and `upload` count nothing
          (`jobs.KINDS[kind].progress_unit` is the authority, and a kind
          with no unit is never handed a callback). The reading is cleared
          whenever an entry leaves `running`, so a `null` here is never
          "the reading was lost" - it is one of "this kind reports
          nothing", "this row is not running" or "the first unit is not
          finished yet". A screen must render nothing for all three rather
          than "0 of 0".
        - **`worker_running: false` is not "idle".** `create_app` builds
          the worker and never starts it (only `bin/yt-shorts studio`
          does), so a full plan can sit completely motionless with nothing
          wrong. Say "the worker is not running", never "nothing to do".
        - **`finished` is in QUEUE order - oldest first**, like the other
          two sections; it is the plan sliced by state, not a most-recent-
          first activity feed. A screen that wants the newest at the top
          has to reverse it (`_trim_finished` keeps the most recent 50).
        - **`params` may contain the literal `"[redacted]"`** where a name
          looked like a secret (see `_safe_params`). Render it; never send
          it back as a value - a re-enqueue built from a row read here
          would write that string into the plan as if it were the real
          parameter.
        - **`stoppable` is the field to read, never the `stop_point`
          phrase.** Whether a stop would reach the work at all is a boolean
          on every row (see `_entry_json`); inferring it from the wording of
          `stop_point` is how a Stop button once came to be offered on a
          running upload. `stop_point` is a LABEL - what to tell the
          operator about when a stop would land - and nothing else.
        - **`/api/jobs/{id}` is TWO disjoint id spaces.** `GET
          /api/jobs/{job_id}` (and `…/log`) addresses a `studio.jobs.Job`,
          the in-process record of one running unit of work; `DELETE
          /api/jobs/{id}` and every `POST …/{id}/…` below address a queue
          `Entry`. They are minted by different code and never equal. The
          only link is `Entry.job_id`, set once the worker starts an
          entry - which is why a row's log link has to come from that
          field and not from the row's own `id`.
        """
        queue = _require_queue()
        rows = _rows(queue)
        worker = getattr(app.state, "worker", None)
        return {
            "running": [r for r in rows if r["state"] in _RUNNING_STATES],
            "queued": [r for r in rows if r["state"] in _PENDING_STATES],
            "finished": [r for r in rows if r["state"] in _FINISHED_STATES],
            "limits": queue.limits(),
            # False in every test and in a studio whose worker was stopped:
            # a plan that nothing is draining looks exactly like a busy one
            # otherwise (see worker.py - create_app never starts it).
            "worker_running": bool(worker is not None and worker.is_running()),
            # The queue starts empty and says so when jobs.json could not be
            # read (it is renamed aside, never overwritten - see
            # JobQueue.load): an operator's lost plan must not be silent.
            "load_error": queue.load_error,
        }

    @app.post("/api/jobs")
    def enqueue_job(body: EnqueueBody) -> dict:
        """Adds one unit of work to the plan.

        An `after` naming no entry is refused HERE, and only here: the
        queue itself treats an unknown dependency as satisfied, on purpose
        (`_trim_finished` ages a long-since-done one out of the plan, so
        "not on record" is the ordinary end state of a constraint that WAS
        met - see `job_queue._dependency_status`). At enqueue time the two
        cases are still distinguishable, because a dependency that exists
        has not been trimmed yet, so this is the one moment a typo can be
        told from a satisfied constraint rather than silently running the
        entry immediately.
        """
        queue = _require_queue()
        params = dict(body.params)
        _validate_enqueue(body.kind, params)
        with _plan_lock():
            if body.after is not None and not any(
                    entry.id == body.after for entry in queue.list()):
                raise HTTPException(
                    status_code=400,
                    detail=f"no queue entry {body.after!r} to wait for: the "
                           f"plan holds no entry with that id. Enqueue the job "
                           f"this one depends on first, then pass back the id "
                           f"it answered with.")
            try:
                entry = queue.enqueue(body.kind, params, after=body.after)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            rows = _rows(queue)
        position = next(r["position"] for r in rows if r["id"] == entry.id)
        return {
            "entry": next(r for r in rows if r["id"] == entry.id),
            "position": position,
            # How many entries are actually WAITING in front of it - the
            # number an operator reads as "my place in line". The position
            # counts finished entries too, since it indexes the whole plan.
            "queued_ahead": sum(1 for r in rows[:position]
                                if r["state"] in _PENDING_STATES),
        }

    @app.post("/api/jobs/{entry_id}/stop", status_code=202)
    def stop_queue_entry(entry_id: str, force: bool = False) -> dict:
        """Asks the job running this entry to stop. 202, not 200: the stop
        was ACCEPTED - the work ends at its own safe point.

        Through `Worker.request_stop`, NEVER `JobQueue.mark_stopping`: the
        queue holds no cancel token and reaches no thread, so calling it
        directly would move the entry to `stopping` while nothing whatsoever
        had told the work to stop - a button that lies. `request_stop`
        reaches the token first and marks the entry only then.
        """
        queue = _require_queue()
        worker = getattr(app.state, "worker", None)
        if worker is None:
            raise HTTPException(status_code=503, detail=_QUEUE_UNAVAILABLE)
        with _plan_lock():
            # An id the plan does not know is a 404; request_stop would call
            # it `invalid_state` ("not running here"), which is true but
            # answers a different question than the operator asked.
            if not any(entry.id == entry_id for entry in queue.list()):
                raise HTTPException(status_code=404,
                                    detail=f"no such queue entry: {entry_id!r}")
            try:
                worker.request_stop(entry_id, force=force)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            return _entry_response(queue, entry_id)

    @app.post("/api/jobs/{entry_id}/pause")
    def pause_queue_entry(entry_id: str) -> dict:
        queue = _require_queue()
        with _plan_lock():
            try:
                queue.pause(entry_id)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            return _entry_response(queue, entry_id)

    @app.post("/api/jobs/{entry_id}/resume")
    def resume_queue_entry(entry_id: str) -> dict:
        queue = _require_queue()
        with _plan_lock():
            try:
                queue.resume(entry_id)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            return _entry_response(queue, entry_id)

    @app.post("/api/jobs/{entry_id}/move")
    def move_queue_entry(entry_id: str, body: MoveBody) -> dict:
        queue = _require_queue()
        with _plan_lock():
            try:
                queue.move(entry_id, body.index)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            return _entry_response(queue, entry_id)

    @app.post("/api/jobs/{entry_id}/retry")
    def retry_queue_entry(entry_id: str) -> dict:
        """Re-queues a `failed`, `interrupted` or `stopped` entry. It does not
        resume from a saved position - the queue holds none, and neither long
        kind needs one: a transcribe restarts at the first missing chunk and a
        detect at the first unscored window, because each caches its own unit
        of work.

        `stopped` is here because that same caching is what the stop dialog
        already PROMISES ("a retry resumes at the first window nobody
        reached"), and this route answering 409 for a stopped entry made that
        promise name a control the operator was not given. `done` is still
        refused: re-running work that succeeded is a new request, not a retry.
        """
        queue = _require_queue()
        with _plan_lock():
            try:
                queue.retry(entry_id)
            except job_queue.QueueError as error:
                raise HTTPException(status_code=_queue_status(error),
                                    detail=str(error)) from error
            return _entry_response(queue, entry_id)

    @app.delete("/api/jobs/{entry_id}")
    def remove_queue_entry(entry_id: str) -> dict:
        """Drops an entry from the plan. Dropping a plan and halting work in
        progress are two different operations with two different buttons: a
        running entry is refused here (409) and the message points at stop.

        One queue call and nothing read back, so it holds no worker lock -
        the queue's own lock already makes this atomic (see the note above).
        """
        queue = _require_queue()
        try:
            queue.remove(entry_id)
        except job_queue.QueueError as error:
            raise HTTPException(status_code=_queue_status(error),
                                detail=str(error)) from error
        return {"removed": entry_id}

    @app.put("/api/settings/limits")
    def put_queue_limits(body: LimitsBody) -> dict:
        """How many jobs each pool runs at once, for THIS workspace.

        Written to the workspace's settings.json AND applied to the live
        queue: a limit that only landed on disk would do nothing until the
        next restart, and one that only landed in memory would be forgotten
        by it. Not refused merely because there is no QUEUE - the setting is
        workspace-level and still worth recording; GET /api/settings reports
        `queue.available` for the rest.

        No workspace at all is a different matter and answers 503 like every
        other route here, because there is then nowhere to write. This route
        is the only one that needs its own guard for it (the others read
        `app.state.job_queue`, which is already None in that state), and it
        answered 500 out of an unhandled WorkspaceError until a review drove
        it - which is exactly why
        `test_every_route_says_why_when_the_queue_is_unavailable` now
        derives the routes it enumerates from the router itself rather than
        from a hand-kept list this one had fallen out of.
        """
        try:
            root = _resolve_workspace().root
        except workspace.WorkspaceError as error:
            raise HTTPException(
                status_code=503, detail=_LIMITS_UNAVAILABLE) from error
        # `_stored_limits` has already dropped any UNRELATED pool whose
        # stored value was unusable back to its default, and the merged
        # result is what gets written below - so saving a change to `cpu`
        # silently rewrites a hand-edited `"net": "lots"` to 3. That is the
        # right outcome (the file ends up holding what the queue is actually
        # running, rather than a value that was never in force) but it is a
        # silent one: nothing tells the operator their other pool's edit was
        # discarded. Said here because the code cannot say it anywhere else.
        limits = _validated_limits(_stored_limits(root), body.limits)
        settings = workspace.read_settings(root)
        settings["limits"] = limits
        try:
            workspace.write_settings(root, settings)
        except OSError as error:
            # Type name only: an OSError's message carries the path, and this
            # one is inside the operator's workspace.
            raise HTTPException(
                status_code=500,
                detail=f"cannot write the workspace settings: "
                       f"{type(error).__name__}") from error
        queue = getattr(app.state, "job_queue", None)
        if queue is not None:
            queue.set_limits(limits)
        return {"limits": limits}

    # ---- Logs: read-only views over <workspace>/logs/ (see logsetup.py) ----
    # The central app log, its rotated/gzipped archives, and each background
    # job's own log file. Nothing outside <workspace>/logs/ is ever reachable
    # from here - see _resolve_log's own docstring for why that is the point
    # of this section (the workspace's auth/ directory, the only place
    # secrets live, is a SIBLING of logs/, never inside it).

    def _describe_log(path: Path) -> dict:
        """{"name", "size", "modified"} for one log file - 0/0.0 when the
        file has vanished (e.g. mid-rotation) between being listed and being
        described, so a listing never 500s over a file that is merely gone."""
        try:
            stat = path.stat()
            size, modified = stat.st_size, stat.st_mtime
        except OSError:
            # Vanished between listing and stat (rotation/prune race) - describe
            # it as empty rather than fail the whole listing over one entry.
            size, modified = 0, 0.0
        return {"name": path.name, "size": size, "modified": modified}

    def _logs_root() -> Path:
        return workspace.logs_dir(_resolve_workspace().root)

    def _resolve_log(name: str, date: str | None) -> str | None:
        """The file a log request names, or None. Every path is inside
        <workspace>/logs: the central log by exact name, a job log inside
        jobs/, a dated archive via logsetup.resolve_archive (which guards
        traversal). Nothing else is reachable - and the workspace's auth/
        directory, the only place secrets live, is not under logs/ at all."""
        try:
            root = _logs_root()
        except (OSError, workspace.WorkspaceError):
            # A misconfigured workspace (workspace.py: a set-but-missing
            # YT_SHORTS_DATA raises rather than falling back silently) or an
            # unwritable logs/ dir must read as "no such log" to this
            # read-only route, not 500 the request.
            return None
        if date is not None:
            return logsetup.resolve_archive(root, name, date)
        # Mirrors logsetup.resolve_archive's own containment guard: a
        # candidate is accepted only after realpath resolution keeps it
        # inside the (also realpath'd) logs root, so a symlink planted
        # under logs/ cannot point a "plain" candidate out to auth/ or
        # anywhere else. The two guards must not drift apart.
        real_root = os.path.realpath(str(root))
        for candidate in (root / name, root / "jobs" / name,
                          root / "jobs" / f"{name}.gz"):
            full = os.path.realpath(str(candidate))
            try:
                inside = os.path.commonpath([real_root, full]) == real_root
            except ValueError:
                continue  # different drives on Windows - never inside root
            if inside and candidate.is_file():
                return full
        return None

    def _log_body(path: str, after: int) -> dict:
        if path.endswith(".gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                    return {"lines": handle.read().splitlines(), "position": after}
            except (OSError, EOFError) as error:
                # A truncated/corrupt archive (e.g. a rollover caught
                # mid-write) raises BadGzipFile (an OSError subclass) or a
                # bare EOFError from inside the gzip module - a read-only log
                # route must report that as "not readable" (404), not 500
                # the whole request over one bad file.
                raise HTTPException(
                    status_code=404,
                    detail=f"log archive is unreadable: {error}") from error
        lines, position = logsetup.read_new_lines(path, after)
        return {"lines": lines, "position": position}

    @app.get("/api/logs")
    def list_log_files() -> dict:
        try:
            root = _logs_root()
        except (OSError, workspace.WorkspaceError) as error:
            # A misconfigured workspace must not 500 the whole Logs screen -
            # degrade to "nothing to show", the same best-effort contract
            # every other logging path in this project keeps (see CLAUDE.md).
            _logger.warning("logs unavailable: %s", error)
            return {"central": {"name": workspace.CENTRAL_LOG_NAME, "size": 0, "modified": 0.0},
                    "archives": [], "jobs": []}
        central = root / workspace.CENTRAL_LOG_NAME
        return {
            "central": _describe_log(central),
            "archives": logsetup.archive_dates(root, [workspace.CENTRAL_LOG_NAME]),
            "jobs": [_describe_log(Path(p))
                     for p in logsetup.list_logs(root / "jobs")],
        }

    @app.get("/api/logs/{name}")
    def read_log(name: str, after: int = 0, date: str | None = None) -> dict:
        try:
            pathnames.validate_segment(name, what="log name")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        path = _resolve_log(name, date)
        if path is None:
            raise HTTPException(status_code=404, detail=f"No such log: {name}")
        return {**_log_body(path, after), "name": name, "date": date}

    @app.get("/api/jobs/{job_id}/log")
    def read_job_log(job_id: str, after: int = 0) -> dict:
        job = app.state.job_store.get(job_id)
        if job is None or job.log_path is None:
            raise HTTPException(status_code=404, detail="No log for that job")
        path = job.log_path if Path(job.log_path).is_file() else f"{job.log_path}.gz"
        if not Path(path).is_file():
            raise HTTPException(status_code=404, detail="No log for that job")
        return {**_log_body(path, after), "name": Path(path).name, "date": None}

    # SPA fallback, registered LAST: FastAPI matches routes in registration
    # order, so every /api/* route above already wins. Any /api/* path that
    # reached here is a genuine 404; every other path serves a built asset if
    # one exists at that name, else index.html - so a deep link or reload on a
    # client-side route (/, /{channel}, /{channel}/{event}) lands on the right
    # screen instead of a 404. Registered only when the built page exists (the
    # API tests run without it); a path is contained under STATIC_DIR so a
    # crafted "../" cannot escape it.
    if STATIC_DIR.is_dir():
        static_root = STATIC_DIR.resolve()

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            asset = (STATIC_DIR / full_path).resolve()
            if full_path and asset.is_file() and static_root in asset.parents:
                return FileResponse(str(asset))
            return FileResponse(str(static_root / "index.html"))

    # Unconditional, deliberate, and registered LAST - after every real
    # route above AND after the SPA fallback block above (when it exists):
    # any non-GET request to a path under /api that no real route claims is
    # answered 404, not 405 - including the case where that exact path DOES
    # exist, just for a different verb (e.g. POST to a GET-only route).
    # This must NOT live inside `if STATIC_DIR.is_dir():` - it is a pure-API
    # semantic and must not depend on whether the frontend build
    # happens to be on disk. Measured before this fix: with static/ present
    # this answered 404; with it absent, 405 - because spa_fallback (GET
    # only) is what previously forced the 404 for a since-removed or
    # never-existing /api/* path (e.g. the deleted POST
    # /api/glossary/adopt-default), by matching it as a PARTIAL route (path
    # matches, method doesn't) that Starlette otherwise answers 405 for; with
    # no static/ there was no GET route to force that at all. The cost is
    # real and must stay documented rather than "fixed" by moving this
    # earlier or widening spa_fallback: this gives up a genuine 405 for ANY
    # /api path, API-wide, in exchange for a 404 whenever nothing above
    # claims the (path, method) pair. Four single-method decorators (not one
    # api_route with methods=[...]) so each gets its own explicit
    # operation_id - a single multi-method api_route here produced duplicate
    # operationIds in /openapi.json (invalid OpenAPI, breaks client codegen).
    @app.post("/api/{full_path:path}", operation_id="api_post_not_found")
    @app.put("/api/{full_path:path}", operation_id="api_put_not_found")
    @app.patch("/api/{full_path:path}", operation_id="api_patch_not_found")
    @app.delete("/api/{full_path:path}", operation_id="api_delete_not_found")
    def api_method_not_found(full_path: str):
        raise HTTPException(status_code=404, detail="Not found")

    return app
