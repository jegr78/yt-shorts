"""Read and update the three writable lexicon layers - workspace, channel,
event (stage D2c) - the studio's write path onto yt_shorts.lexicon. Pure, no
FastAPI. Mirrors brand_admin.py's shape: a typed error carrying a `kind`,
segment validation before any filesystem touch, and the "what this accepts,
profile.load accepts" invariant (see update's use of lexicon.normalise).

The built-in default (lexicon.DEFAULT_MARKERS) is never written here except by
adopt_default, which copies it into the workspace layer so an operator can
start editing it (e.g. disabling one entry by setting its weight to 0) without
retyping the other 38.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import atomicwrite, lexicon, pathnames, workspace

SOURCES = ("default", "workspace", "channel", "event")


class LexiconAdminError(Exception):
    """kind: "bad_name" | "not_found" | "bad_markers".
    Maps to HTTP: bad_name/bad_markers -> 400, not_found -> 404."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_segment(value: str, what: str) -> None:
    try:
        pathnames.validate_segment(value, what=what)
    except ValueError as error:
        raise LexiconAdminError(str(error), kind="bad_name") from error


def _resolve(root, channel: str | None, event: str | None) -> tuple[str, Path]:
    """The scope name and its own-layer moments.json path. Every segment given
    is validated (bad_name) BEFORE either segment's existence is checked
    (not_found), and existence is checked before anything else touches the
    filesystem - mirroring event_brand_admin._dirs."""
    if channel is not None:
        _validate_segment(channel, "channel name")
    if event is not None:
        _validate_segment(event, "event name")
    if channel is None:
        return "workspace", workspace.moments_path(root)
    channel_dir = Path(root) / "channels" / channel
    if not channel_dir.is_dir():
        raise LexiconAdminError(f"unknown channel: {channel!r}", kind="not_found")
    if event is None:
        return "channel", channel_dir / "moments.json"
    event_dir = channel_dir / "events" / event
    if not event_dir.is_dir():
        raise LexiconAdminError(f"unknown event: {event!r}", kind="not_found")
    return "event", event_dir / "moments.json"


def _load_markers_or_empty(path: Path, problems: list[str] | None = None) -> dict[str, float]:
    """lexicon.load, degraded: a malformed layer becomes {} instead of
    raising, mirroring profile._load_lexicon's own handling of the identical
    failure mode (a bad moments.json at one layer must not break every layer
    above it). When `problems` is given, the error is appended to it so the
    caller can surface it; adopt_default has no problems list to report into
    - its own workspace layer is about to be overwritten anyway, so a broken
    file there just degrades to "nothing to preserve" rather than blocking
    the very operation that would fix it."""
    try:
        return lexicon.load(path).markers
    except (OSError, ValueError) as error:
        # Deliberately swallowed here, not re-raised: see the docstring above
        # - a malformed layer must read as empty, not 500 the whole route.
        if problems is not None:
            problems.append(f"{path}: {error}")
        return {}


def _layers(root, channel: str | None, event: str | None) -> tuple[list, list[str]]:
    """Every layer that applies at this scope, least specific first, as
    (source, path-or-None, markers) triples, plus the problem strings for any
    layer that failed to load. `default` has no path. Stops at the requested
    scope: a channel-scoped call yields default/workspace/channel only, never
    an event layer the caller did not ask about.

    A malformed moments.json at any layer is collected as a defect string,
    never a raise - see _load_markers_or_empty - so one bad layer degrades
    the editor's view of THAT layer to empty rather than 500ing every moments
    route at this scope, including a route that just finished a successful
    write to a DIFFERENT layer."""
    problems: list[str] = []
    layers = [("default", None, dict(lexicon.DEFAULT_MARKERS))]
    workspace_path = workspace.moments_path(root)
    layers.append(("workspace", workspace_path, _load_markers_or_empty(workspace_path, problems)))
    if channel is None:
        return layers, problems
    channel_path = Path(root) / "channels" / channel / "moments.json"
    layers.append(("channel", channel_path, _load_markers_or_empty(channel_path, problems)))
    if event is None:
        return layers, problems
    event_path = Path(root) / "channels" / channel / "events" / event / "moments.json"
    layers.append(("event", event_path, _load_markers_or_empty(event_path, problems)))
    return layers, problems


def read(root, *, channel: str | None = None, event: str | None = None) -> dict:
    """{"scope", "own", "effective", "problems"} for the given scope.

    `effective` is NOT profile.merge_lexicons: that function deliberately DROPS
    a winning weight of 0 so scoring never sees a disabled marker, which is
    correct for rendering but wrong for an editor - an operator needs to see
    that a channel or event disabled a marker it inherited, struck through
    rather than absent. So this walks `_layers` itself, least specific first,
    and lets each later layer's entry (weight AND source) overwrite the
    earlier one - zeros included. Do not "simplify" this back into a
    merge_lexicons call; that would silently make disabled markers invisible.

    `problems` lists any layer that failed to load (see _layers), by path and
    error - the caller (the studio's moments routes) returns it as-is so the
    editor can warn while staying usable, rather than the whole scope 500ing
    over one bad file."""
    scope, _target = _resolve(root, channel, event)
    layers, problems = _layers(root, channel, event)
    own = layers[-1][2]  # the requested scope's own layer is the last one walked
    effective: dict[str, dict] = {}
    for source, _path, markers in layers:
        for marker, weight in markers.items():
            effective[marker] = {"weight": float(weight), "source": source}
    return {"scope": scope, "own": own, "effective": effective, "problems": problems}


def update(root, markers, *, channel: str | None = None, event: str | None = None) -> None:
    """Overwrites the scope's own layer with exactly `markers` - never a
    merge. An empty dict still writes `{"markers": {}}` (rather than deleting
    the file) so "I cleared this layer" is an explicit, re-editable state.

    Validated the same way lexicon.normalise validates any other moments.json
    - a payload this accepts is one profile.load accepts - so a bad payload
    never reaches disk."""
    try:
        normalised = lexicon.normalise(markers)
    except ValueError as error:
        raise LexiconAdminError(str(error), kind="bad_markers") from error
    _scope, target = _resolve(root, channel, event)
    atomicwrite.write_text(target, json.dumps({"markers": normalised}, indent=2) + "\n")


def adopt_default(root) -> None:
    """Copies lexicon.DEFAULT_MARKERS into the workspace layer's own entries,
    ADDITIVELY, so an operator can start editing the built-in default instead
    of only adding on top of it - existing own entries are PRESERVED and WIN
    over the default for any marker both define, exactly like a more specific
    layer already wins over a less specific one everywhere else in this
    module.

    This must NOT be `update(root, dict(DEFAULT_MARKERS))`: `update` OVERWRITES
    the whole layer (see its own docstring), so that call would silently
    DELETE every custom marker the operator had already saved at this layer
    and re-enable anything they had disabled here at weight 0 - both change
    what scores a clip, directly contradicting the studio's own confirmation
    dialog ("This does not change what currently scores a clip - the built-in
    default already underlies every scope. It only makes it editable.") and
    this function's own promise below. Building the merged dict here instead
    keeps that promise true.

    The default layer still applies underneath regardless (see
    profile._load_lexicon) - adopting it changes nothing about what's
    effective for a marker with no own entry, only what's editable - so this
    stays idempotent: `own` after a first adopt already carries every default
    key with its final value, so a second adopt's `{**DEFAULT, **own}`
    reproduces `own` byte-for-byte."""
    own = _load_markers_or_empty(workspace.moments_path(root))
    update(root, {**lexicon.DEFAULT_MARKERS, **own})
