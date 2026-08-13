"""Read and update the three writable glossary layers - workspace, channel,
event - the studio's write path onto yt_shorts.glossary. Pure, no FastAPI.

Mirrors lexicon_admin.py's shape deliberately, down to the names: a typed
error carrying a `kind`, segment validation before any filesystem touch, the
"what this accepts, profile.load accepts" invariant (see update's use of
glossary.parse_layer), and an `effective` map that KEEPS disabled entries
where the scoring merge drops them.

The built-in default (glossary.DEFAULT_LAYER) is now empty (see that module's
docstring) - the shipped vocabulary lives in tracks.PACKS instead, selected
per event via the event's own `track` field. This module surfaces that
selection (`read`'s `track` key) and enforces the two rules `glossary.
parse_layer` cannot (the id must name a real pack, and only an event may set
one) the same way `profile._load_glossary` does on the read path.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import glossary as glossary_module
from . import pathnames, tracks, workspace

SOURCES = ("default", "track", "workspace", "channel", "event")


class GlossaryAdminError(Exception):
    """kind: "bad_name" | "not_found" | "bad_glossary".
    Maps to HTTP: bad_name/bad_glossary -> 400, not_found -> 404."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_segment(value: str, what: str) -> None:
    try:
        pathnames.validate_segment(value, what=what)
    except ValueError as error:
        raise GlossaryAdminError(str(error), kind="bad_name") from error


def _resolve(root, channel: str | None, event: str | None) -> tuple[str, Path]:
    """The scope name and its own-layer glossary.json path. Every segment given
    is validated (bad_name) BEFORE either segment's existence is checked
    (not_found), and existence is checked before anything else touches the
    filesystem - mirroring lexicon_admin._resolve.

    One caveat for a direct caller: an `event` given WITHOUT a `channel` is
    still validated, but then silently ignored - the scope resolves to
    workspace, because there is no such thing as an event layer without a
    channel to find it under. The studio cannot produce that call shape (its
    routes carry both segments in the path), and lexicon_admin behaves
    identically, but a script or test helper passing only `event` gets the
    workspace layer rather than an error."""
    if channel is not None:
        _validate_segment(channel, "channel name")
    if event is not None:
        _validate_segment(event, "event name")
    if channel is None:
        return "workspace", workspace.glossary_path(root)
    channel_dir = Path(root) / "channels" / channel
    if not channel_dir.is_dir():
        raise GlossaryAdminError(f"unknown channel: {channel!r}", kind="not_found")
    if event is None:
        return "channel", channel_dir / "glossary.json"
    event_dir = channel_dir / "events" / event
    if not event_dir.is_dir():
        raise GlossaryAdminError(f"unknown event: {event!r}", kind="not_found")
    return "event", event_dir / "glossary.json"


def _load_layer_or_empty(path: Path, problems: list[str] | None = None):
    """glossary.load, degraded: a malformed layer becomes EMPTY_LAYER instead
    of raising, mirroring profile._load_glossary's handling of the identical
    failure mode - a bad glossary.json at one layer must not break every layer
    above it, nor 500 a route that just wrote a DIFFERENT layer successfully.

    When `problems` is given the error is appended to it."""
    try:
        return glossary_module.load(path)
    except (OSError, ValueError) as error:
        # Deliberately swallowed here, not re-raised: see the docstring above.
        if problems is not None:
            problems.append(f"{path}: {error}")
        return glossary_module.EMPTY_LAYER


def _check_track_scope(path: Path, layer, problems: list[str]) -> None:
    """Appends a problem, mirroring profile._load_glossary's wording and
    naming the offending FILE, when `layer` (loaded from a workspace or
    channel glossary.json) carries a `track` - only an event may select one.
    Never called for an event's own layer, where a track is legitimate."""
    if layer.track is not None:
        problems.append(
            f"{path}: only an event selects a track, found 'track': {layer.track!r}")


def _layers(root, channel: str | None, event: str | None) -> tuple[list, list[str]]:
    """Every layer that applies at this scope, least specific first, as
    (source, layer) pairs, plus a problem string per layer that failed to
    load or that carries a `track` at a scope other than event. Stops at the
    requested scope.

    The event's own layer is read FIRST when one applies, because its `track`
    decides which pack sits between the default and the workspace layer - the
    pack is more specific than the built-in default and less specific than
    anything the operator wrote. That ordering (default -> track pack ->
    workspace -> channel -> event) is what lets an operator's own entry, at
    ANY of the three writable layers, override a shipped pack entry - see
    TestTrack.test_the_track_pack_sits_between_default_and_workspace."""
    problems: list[str] = []
    workspace_path = workspace.glossary_path(root)
    workspace_layer = _load_layer_or_empty(workspace_path, problems)
    _check_track_scope(workspace_path, workspace_layer, problems)
    layers = [("default", glossary_module.DEFAULT_LAYER)]

    event_layer = None
    if channel is not None and event is not None:
        event_path = Path(root) / "channels" / channel / "events" / event / "glossary.json"
        event_layer = _load_layer_or_empty(event_path, problems)
        pack = tracks.get(event_layer.track) if event_layer.track else None
        if pack is not None:
            layers.append(("track", tracks.as_layer(pack)))
        elif event_layer.track:
            problems.append(
                f"{event_path}: unknown track {event_layer.track!r} - "
                f"valid ids: {', '.join(sorted(tracks.PACKS))}")

    layers.append(("workspace", workspace_layer))
    if channel is None:
        return layers, problems
    channel_path = Path(root) / "channels" / channel / "glossary.json"
    channel_layer = _load_layer_or_empty(channel_path, problems)
    _check_track_scope(channel_path, channel_layer, problems)
    layers.append(("channel", channel_layer))
    if event is None:
        return layers, problems
    layers.append(("event", event_layer))
    return layers, problems


def _own_shape(layer) -> dict:
    """A layer as the file/wire shape a PUT sends back: terms keyed by their
    raw spelling, replacements keyed by their raw key, plus the track when one
    is selected. Rebuilt from the parsed layer rather than echoed from disk,
    so a GET always returns the canonical form regardless of which accepted
    shape the file on disk happens to use.

    The `track` key is omitted entirely when there is none, rather than
    written as null - an absent key and a null mean the same thing to
    parse_layer, and not writing it keeps a hand-edited file clean."""
    shape = {
        "terms": {spelling: enabled for spelling, enabled in layer.terms.values()},
        "replacements": {raw: text for raw, text in layer.replacements.values()},
    }
    if layer.track is not None:
        shape["track"] = layer.track
    return shape


def read(root, *, channel: str | None = None, event: str | None = None) -> dict:
    """{"scope", "own", "own_keys", "track", "effective", "problems"} for the
    given scope.

    `track` is the EVENT scope's own selection, else None - only an event may
    select a pack, so no other scope has one to report. For the same reason
    `own` never carries a `track` key at a non-event scope, even when the
    file on disk has one: `own` is exactly what the client echoes back on the
    next save (see update's docstring), and a track in it would make that
    save raise. A track found at the workspace or channel layer is instead
    surfaced in `problems`, naming the offending file - mirroring how
    profile._load_glossary treats the same defect on the render path, rather
    than silently dropping or silently honouring it here.

    `effective` is NOT profile.merge_glossaries: that function deliberately
    DROPS a disabled entry so neither the decoder bias nor the corrections see
    it, which is correct for transcription and wrong for an editor - an
    operator needs to see that a channel or event disabled something it
    inherited, struck through rather than absent. So this walks `_layers`
    itself, least specific first, letting each later layer's entry (value AND
    source) overwrite the earlier one, disabled entries included. Do not
    "simplify" this into a merge_glossaries call; that would silently make
    disabled entries invisible.

    `own_keys` is `{"terms": [...], "replacements": [...]}` - the SERVER's own
    already-normalised keys for this scope's own layer (exactly
    `layers[-1][1].terms.keys()` / `.replacements.keys()`, the identity
    `glossary.normalise_term`/`normalise_key` produced when this layer was
    parsed). This exists so the studio client can determine which `effective`
    rows are this scope's own WITHOUT re-normalising `own`'s raw keys itself -
    see glossaryLayers.ts's `toTermRows`/`toReplacementRows`. A client-side
    re-normalisation is exactly what broke once already (see that module's
    `normaliseKey` docstring): Python's and JS's whitespace/normalisation
    rules disagree on characters like U+0085 and U+FEFF, so a raw key
    containing one of those could normalise to two different identities
    client- and server-side, silently marking a real own entry as inherited
    and read-only - and since a PUT sends only the rows marked own, the next
    Save of any UNRELATED row would then delete it from disk with no error.
    Handing the client the server's own keys removes the need for it to agree
    with Python's normalisation at all.

    `problems` lists any layer that failed to load, by path and error - the
    caller returns it as-is so the editor can warn while staying usable,
    rather than the whole scope 500ing over one bad file."""
    scope, _target = _resolve(root, channel, event)
    layers, problems = _layers(root, channel, event)
    own_layer = layers[-1][1]  # the requested scope's own layer is last
    own = _own_shape(own_layer)
    if scope != "event":
        own.pop("track", None)
    own_keys = {"terms": list(own_layer.terms.keys()),
                "replacements": list(own_layer.replacements.keys())}
    effective_terms: dict[str, dict] = {}
    effective_reps: dict[str, dict] = {}
    for source, layer in layers:
        for key, (spelling, enabled) in layer.terms.items():
            effective_terms[key] = {"term": spelling, "enabled": enabled, "source": source}
        for key, (raw, text) in layer.replacements.items():
            effective_reps[key] = {"key": raw, "value": text, "source": source}
    return {"scope": scope, "own": own, "own_keys": own_keys, "track": own.get("track"),
            "effective": {"terms": effective_terms, "replacements": effective_reps},
            "problems": problems}


def update(root, terms, replacements, *, track: str | None = None,
           channel: str | None = None, event: str | None = None) -> None:
    """Overwrites the scope's own layer with exactly `terms`, `replacements`
    and `track` - never a merge. Empty dicts and a null track still write the
    file (without a `track` key) so "I cleared this layer" is an explicit,
    re-editable state.

    A caller that means to keep the current track must PASS it back: this
    overwrites, so omitting it clears it. The studio's editor reads it from
    `read`'s `track` and sends it with every save for exactly that reason.

    Validated through glossary.parse_layer - the same function profile.load
    validates a file with - plus two rules parse_layer cannot know because it
    sees one file without knowing which layer it is: the track must name a
    real pack, and only an event may select one.

    The payload validation (parse_layer) still runs BEFORE the scope is
    resolved - that ordering is unchanged and is safe only because
    parse_layer is pure and touches no filesystem. But the track-scope check
    runs AFTER `_resolve`, against the RESOLVED scope, not against whether an
    `event` argument was merely passed: `_resolve`'s own docstring documents
    that an `event` given WITHOUT a `channel` is validated and then silently
    ignored, resolving to the WORKSPACE scope - so gating on `event is None`
    would let `update(root, {}, {}, track="monza", event="race")` slip past
    the guard and write a workspace-wide track that breaks every event of
    every channel in the workspace (profile.load raises for each, per
    _load_glossary's "only an event selects a track" rule). Resolving before
    the track check is still safe: `_resolve` only validates segments and
    checks existence, it never creates or writes anything, so no unvalidated
    segment reaches disk any earlier than it did before."""
    payload = {"terms": terms, "replacements": replacements}
    if track is not None:
        payload["track"] = track
    try:
        layer = glossary_module.parse_layer(payload)
    except ValueError as error:
        raise GlossaryAdminError(str(error), kind="bad_glossary") from error
    scope, target = _resolve(root, channel, event)
    if layer.track is not None:
        if scope != "event":
            raise GlossaryAdminError(
                "only an event selects a track", kind="bad_glossary")
        if tracks.get(layer.track) is None:
            raise GlossaryAdminError(
                f"unknown track {layer.track!r}", kind="bad_glossary")
    _write_atomically(
        target, json.dumps(_own_shape(layer), indent=2, ensure_ascii=False) + "\n")


def _write_atomically(target: Path, text: str) -> None:
    """Scratch sibling then os.replace, the mechanic `quota._atomic_write` and
    `workspace.write_settings` use: this file is read on every profile.load,
    and a truncating write leaves it EMPTY for the length of the write."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".glossary-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        # mkstemp is 0600; this is a plain config file the operator edits.
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass  # already moved into place; nothing to clean up
        raise
