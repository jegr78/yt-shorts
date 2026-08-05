"""Wires transcription, the glossary, caption grouping and the alpha track
together into the one `subtitle_provider` `render.build_short` accepts.

This used to be a closure built fresh inside `cmd_render` in `bin/yt-shorts`.
The studio's job runner (`yt_shorts.studio.jobs`) needed the exact same
pipeline - transcribe, apply the glossary, honour a hand-made editorial
correction, group into captions, build the alpha track - and a second copy
of it there would drift out of sync with this one invisibly, so both callers
now go through `make_subtitle_provider` instead of keeping their own copy.

This module deliberately does NOT import fastapi or anything under
`yt_shorts.studio`: `bin/yt-shorts` (`harvest`, `render`, `gallery`) must
keep working in a virtualenv that never installed FastAPI, and it is this
module that both `cmd_render` and the studio's job runner call into. See
`yt_shorts.studio.api`'s own docstring for the FastAPI-isolation rule this
module honours by living outside `yt_shorts/studio/`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from . import clipstore, editorial
from .captions import DEFAULT_MAX_SECONDS, DEFAULT_MAX_WORDS, group_words
from .subtitle_track import build_track
from .transcribe import transcribe

__all__ = ["make_subtitle_provider"]

# "ytshorts.*", NOT __name__: logsetup.configure_logging sets up the
# `ytshorts` tree with propagate=False, so a logger named
# "yt_shorts.subtitle_pipeline" would sit in a different tree entirely and
# never reach the central log file or the CLI's TTY handler.
_LOGGER = logging.getLogger("ytshorts.subtitles")


def transcript_source(clip: dict, edit: editorial.Edit) -> str:
    """The identity a clip's cached transcript is keyed on.

    The clip's own URL, EXCEPT when the operator has trimmed the window: then
    the effective window is folded in, so the cache written for the old window
    can no longer answer for the new one.

    Without this the two halves of a render disagreed, silently.
    `render.source_for_clip` downloads `editorial.effective_window` - the
    trimmed span - while this cache was keyed on `clip["url"]`, which an
    editorial trim never touches (it writes edit.json; clip.json's url keeps
    naming the DETECTED window, as it must, since that url IS the clip's
    identity). So trimming six seconds off the front re-downloaded the shorter
    passage, hit the cache, and burned in captions offset by exactly those six
    seconds - reproduced before this existed, with nothing anywhere saying so.

    Folding the window in rather than DELETING transcript.json on a trim is
    deliberate. Deletion would be a new write outside edit.json from the
    studio's edit path, and that boundary has been restated wrongly six times
    already (see CLAUDE.md); this instead reuses the mechanism transcribe()
    was built with - "the cache correctly refusing to hand back someone
    else's transcript" - which already re-derives AND reports itself on
    stderr, in the job log and in the render panel.

    An untrimmed clip returns `clip["url"]` UNCHANGED, byte for byte. That is
    the difference between a fix and a mass re-transcription: every clip in
    every workspace would otherwise miss its cache once, at minutes apiece.

    The window goes in the PATH, never a query or fragment: `clipid.
    canonical_url` strips both before comparing, so `?w=` or `#w=` would be
    erased and this would silently do nothing. Same trap `clip_from_moment.
    moment_url` documents for a moment's own identity.
    """
    if edit.window is None:
        return clip["url"]
    start, end = edit.window
    return f"{clip['url'].rstrip('/')}/w/{start:.3f}-{end:.3f}"


def make_subtitle_provider(directory: Path, edit: editorial.Edit, clip: dict,
                           hook: str, config: dict, *,
                           logger: logging.Logger | None = None,
                           on_note: Callable[[str], None] | None = None,
                           ) -> Callable[[str], str | None] | None:
    """Returns a `subtitle_provider` for `render.build_short`, or None when
    `config["subtitles"]["enabled"]` is not set - the same "no subtitles
    layer at all" render.build_short already accepts.

    ``directory`` is this clip's own directory (see `clipstore`), ``edit``
    its editorial layer (see `editorial.load`), ``clip`` its derived
    `clip.json` payload (only `clip["url"]` is used, as the transcription
    cache's `source`), and ``hook`` the EFFECTIVE title (the editorial one
    if the operator set one, otherwise the harvested one) - the caller
    already resolved that via `editorial.effective_title` for the render
    itself, and passes the same value here so a dropped-segment NOTE names
    the clip the same way every other NOTE in this tool does.

    The returned callable is handed to `render.build_short` as
    ``subtitle_provider``: build_short calls it with the freshly downloaded
    raw file's path once yt-dlp has produced it, so transcription happens
    on the SAME raw download that gets composed.

    Returns None (from the returned callable, not this factory) whenever
    there is nothing to show. A clip without speech is a normal outcome,
    not a failure, and must not cost the short its render - and neither may
    any OTHER failure in this pipeline (a malformed caption list, an ffmpeg
    problem building the track, a cache write that fails, ...). Subtitles
    are the optional layer: build_short still gets called by the caller
    regardless of what happens inside the returned callable, so any
    exception raised past this point degrades to "no subtitles" instead of
    taking the whole clip down with it. Reported with its exception type
    through `note()` below - a WARNING on the logger plus, if the caller asked
    for one, an `on_note` callback - so the loss is visible instead of silent.
    It carries no "NOTE:" prefix of its own any more: the log line already
    says WARNING, and a studio render surfaces the same text as the clip's
    own result reason. KeyboardInterrupt and
    SystemExit are deliberately not caught here - they are not
    subtitle-pipeline failures.

    A failure of the render itself (yt-dlp, the ffmpeg compose step)
    happens OUTSIDE the returned callable, inside build_short, and keeps
    its own behaviour unchanged: it still fails this candidate.

    The transcript is derived from the freshly downloaded raw file and
    cached at this clip's OWN transcript.json - there is no collision-prone
    shared name to key it on, since every clip has its own directory. The
    clip's own URL is passed as transcribe()'s ``source`` so a stale cache
    is detected and re-transcribed rather than silently handing back
    another clip's captions (harmless now that names cannot collide, but
    transcribe() still checks it).

    ``glossary=config["glossary"]`` is this profile's merged Glossary (see
    `yt_shorts.profile.Profile` and `yt_shorts.glossary`) - built from FIVE
    additive layers (the built-in default, now EMPTY; the venue pack the
    event names with `"track"` in its own glossary.json, e.g. the
    Nordschleife's corner names, see `yt_shorts.tracks`; the workspace's own
    glossary.json; the channel's; the event's; see `profile._load_glossary`),
    most specific winning per entry. It CAN legitimately be empty - an event
    that names no track and has no glossary.json anywhere above it gets
    `Glossary(terms=[], replacements={})`, verified by running
    `profile._load_glossary` against a channel/event with none of those. A
    venue's vocabulary reaches this clip-subtitle path the same way the
    workspace/channel/event layers do - through this one merged Glossary,
    with no separate wiring. It biases the decoder and corrects its output,
    but ONLY on a fresh decode: a transcript already cached from before the glossary
    existed, or before it changed, is returned as-is (see transcribe()'s own
    docstring). Deleting a clip's transcript.json is what re-derives it
    against the current glossary.

    A hand-corrected transcript ALWAYS wins over what gets re-derived here,
    even if re-deriving itself fails - only when there is no correction to
    fall back on does a transcribe() failure propagate to the "no
    subtitles" handling below. When the correction was made against a
    transcript that has since changed underneath it, that is reported (not
    an error - the run still proceeds) and the correction is used
    regardless: auto-merging is silently wrong, dropping the correction is
    data loss.

    work_dir (below) is this clip's OWN subs/ subdirectory, removed here -
    not inside build_track - once build_track returns successfully:
    build_track's own contract (see its tests) never assumes it owns the
    whole directory, only the files it wrote and already cleaned up by
    then, so it cannot safely rmdir it itself. This is the one place that
    hands each clip its own subs/ path and nothing else ever writes there,
    so it is the one place that can remove the directory once it is known
    to be empty - mirroring build_short's own "gone after success, left in
    place after a failure" convention for the raw file and overlay one
    level up. On any failure below (including inside build_track itself),
    work_dir is deliberately left untouched together with whatever it
    holds, for the same investigability reason build_short leaves its own
    scratch files in place.
    """
    if not config.get("subtitles", {}).get("enabled"):
        return None

    notes_logger = logger if logger is not None else _LOGGER

    def note(message: str) -> None:
        """Every "this clip is not getting the captions you expected" message
        goes through here, to BOTH a logger and the caller's own channel.

        Two consumers, two mechanisms, on purpose. The logger is the durable
        record: logsetup puts it on stdout only when attached to a TTY, so the
        CLI operator still reads it in the terminal exactly as they did when
        these were bare stderr prints, while a studio job - which has no
        terminal - gets it in logs/jobs/render-<id>.log via the job logger it
        injects. `on_note` is for a caller that surfaces notes in a channel of
        its own; the studio records them against the clip so they appear in the
        render panel, where an operator is actually looking.

        This used to be `print(..., file=sys.stderr)`, which meant a
        studio-started render that lost every caption reported nothing at all,
        anywhere - it simply succeeded.
        """
        notes_logger.warning("%s", message)
        if on_note is not None:
            on_note(message)

    def provider(raw_path: str) -> str | None:
        subtitles = config.get("subtitles", {})
        try:
            derived = None
            try:
                derived = transcribe(
                    raw_path, str(clipstore.transcript_path(directory)),
                    source=transcript_source(clip, edit), display_name=hook,
                    glossary=config["glossary"], on_note=note)
            except Exception:
                # A hand-made correction may still carry the clip on its
                # own; only re-raise (into the "no subtitles" handling
                # below) when there is nothing to fall back on.
                if edit.transcript is None:
                    raise
            words, conflict = editorial.effective_words(edit, derived)
            if conflict:
                note(f"{hook}: subtitle conflict - the correction was made "
                     f"against an older transcript; using the correction")
            groups = group_words(
                words,
                # Falls back to group_words' own defaults (see their
                # definition for the measurement rationale) rather than
                # repeating the numbers here - this and the README were
                # previously two unpinned copies of the same documented
                # values.
                max_words=subtitles.get("max_words", DEFAULT_MAX_WORDS),
                max_seconds=subtitles.get("max_seconds", DEFAULT_MAX_SECONDS),
            )
            if not groups:
                note(f"{hook}: no speech detected, no subtitles")
                return None
            work_dir = clipstore.subs_work_dir(directory)
            track = build_track(
                groups, config, str(clipstore.subs_track_path(directory)),
                str(work_dir))
            try:
                work_dir.rmdir()
            except OSError:
                # Not empty (unexpected - see docstring above) or already
                # gone; either way there is nothing unsafe to do here, so
                # this is left alone rather than escalated into a
                # subtitle-pipeline failure over a directory that is, at
                # worst, merely untidy.
                pass
            return track
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            note(f"{hook}: no subtitles ({type(error).__name__}: {error})")
            return None

    return provider
