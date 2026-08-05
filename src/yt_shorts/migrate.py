"""Copy an old-layout event into the workspace, verified.

This copies and never moves, and verifies every copied file by checksum
before reporting success. The original is left in place for the operator to
delete once satisfied.

That is not caution for its own sake: four reference shorts were destroyed
during development by a run that wrote into the directory holding them, and
they were unrecoverable because nothing had verified a copy first. A
migration that deletes before verifying is the same mistake wearing a
different hat.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import clipid, clipstore


class MigrationError(Exception):
    """Understandable error message about a migration that cannot be trusted."""


@dataclass
class Report:
    clips: int = 0
    copied: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_verified(source: Path, target: Path, report: Report) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if _digest(source) != _digest(target):
        raise MigrationError(
            f"Copy does not match its source: {source} -> {target}. "
            f"Nothing was deleted; the original is untouched."
        )
    report.copied.append(str(target))


def sync_channel_file(source: Path, target: Path) -> str:
    """Copies one channel-level file (channel.json, brand.json, layout.py)
    into the workspace, verified by checksum - the same guarantee every
    other copy in this module gives, which this one used to skip entirely
    (bin/yt-shorts's cmd_migrate used to call plain shutil.copy2 with no
    verification at all, contradicting this module's own docstring).

    Returns one of:
      "absent"    - the repository has no such file; nothing to do.
      "copied"    - the workspace had none yet; copied and verified.
      "unchanged" - the workspace's copy already matches the repository's.
      "differs"   - the workspace's copy exists and does NOT match. It is
                    left alone rather than overwritten - an operator's own
                    workspace customization (a hand-edited brand.json, say)
                    must never be silently clobbered by a later migration
                    run - but the caller must report this rather than stay
                    silent about it, unlike the previous "skip if the
                    target exists" behaviour, which could not tell a stale
                    copy from an intentional one and reported neither.
    """
    if not source.exists():
        return "absent"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _digest(source) != _digest(target):
            raise MigrationError(
                f"Copy does not match its source: {source} -> {target}. "
                f"Nothing was deleted; the original is untouched."
            )
        return "copied"
    return "unchanged" if _digest(source) == _digest(target) else "differs"


def sync_channel_tree(source: Path, target: Path) -> dict[str, list[str]]:
    """Like sync_channel_file, applied file by file to a whole directory
    tree (fonts/, assets/): a whole-directory "copy only if the target
    directory doesn't exist yet" (the previous behaviour) meant a font
    added to the repository after the first migration was never picked up
    by a second one, because the target directory already existed and the
    entire copytree() was skipped. Every file is now checked (and, if
    missing from the workspace, copied and verified) independently, so a
    second migration keeps closing exactly that gap.

    Returns {"copied": [...], "unchanged": [...], "differs": [...]},
    each a list of paths relative to ``source`` - "absent" results (a
    workspace file with no repository counterpart) are not meaningful at
    tree level and are omitted, mirroring sync_channel_file's per-file
    "absent" only ever describing the repository side.
    """
    result: dict[str, list[str]] = {"copied": [], "unchanged": [], "differs": []}
    if not source.is_dir():
        return result
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(source)
        status = sync_channel_file(item, target / rel)
        if status != "absent":
            result[status].append(str(rel))
    return result


def migrate_event(old_event_dir: str | Path,
                  new_event_dir: str | Path) -> Report:
    """Copies one event from the repository layout into the workspace."""
    old = Path(old_event_dir).resolve()
    new = Path(new_event_dir).resolve()
    if old == new:
        raise MigrationError(f"Source and target are the same directory: {old}")

    clips_file = old / "clips.json"
    if not clips_file.exists():
        raise MigrationError(f"No clips.json in {old} - nothing to migrate.")

    try:
        clips = json.loads(clips_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise MigrationError(f"clips.json is unreadable: {clips_file}\n{error}") from error

    report = Report()
    new.mkdir(parents=True, exist_ok=True)

    sources = old / "clip_urls.json"
    if sources.exists():
        _copy_verified(sources, new / "sources.json", report)

    # The event's OWN overrides. An event may carry its own brand.json,
    # fonts, assets and layout.py; a migration that silently dropped them
    # would destroy configuration the tool documents as supported, and the
    # loss would only surface as a differently branded short much later.
    for name in ("brand.json", "layout.py"):
        source = old / name
        if source.exists():
            _copy_verified(source, new / name, report)
    for name in ("fonts", "assets"):
        source = old / name
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    _copy_verified(item, new / name / item.relative_to(source),
                                   report)

    old_names = _old_short_names(clips)
    matched_drafts: set[str] = set()

    by_url: dict[str, Path] = {}
    for index, entry in enumerate(clips, 1):
        if not isinstance(entry, dict) or not entry.get("url"):
            report.unmapped.append(f"clips.json entry without url: {entry!r}")
            continue
        directory = clipstore.write_clip(new, entry)
        # Key on the canonical URL (what clipstore uses for clip identity), not
        # the raw string, so a transcript whose source differs only by a share
        # query param / trailing slash / scheme case still maps to its clip
        # instead of being reported unmapped and silently skipped.
        by_url[clipid.canonical_url(entry["url"])] = directory
        report.clips += 1

        old_name = old_names.get(index)
        if old_name is not None:
            old_draft = old / "drafts" / f"{old_name}.mp4"
            if old_draft.exists():
                _copy_verified(old_draft, clipstore.short_path(directory), report)
                matched_drafts.add(old_name)

    # Every draft actually on disk that this migration could NOT account
    # for is reported, not silently left behind: the operator is invited to
    # delete the repository originals once satisfied, and an unlocatable
    # draft is exactly the unrecoverable loss that invitation makes
    # possible if it goes unnoticed. Matched by filename - the only link
    # a draft carries back to a clip in the old layout - against the
    # faithfully reproduced old naming rules above (see _old_short_names),
    # not the (buggy) base-slug-only guess this replaced.
    old_drafts_dir = old / "drafts"
    if old_drafts_dir.is_dir():
        for draft_file in sorted(old_drafts_dir.glob("*.mp4")):
            if draft_file.stem not in matched_drafts:
                report.unmapped.append(
                    f"{draft_file.name}: no clip in clips.json maps to this draft")

    for transcript in sorted((old / "transcripts").glob("*.json")):
        try:
            payload = json.loads(transcript.read_text(encoding="utf-8"))
            source_url = payload.get("source")
        except (json.JSONDecodeError, OSError):
            source_url = None
        directory = (by_url.get(clipid.canonical_url(source_url))
                     if source_url else None)
        if directory is None:
            report.unmapped.append(
                f"{transcript.name}: no clip with source {source_url!r}")
            continue
        _copy_verified(transcript, clipstore.transcript_path(directory), report)

    return report


def _old_short_names(clips: list) -> dict[int, str]:
    """Reproduces bin/yt-shorts's old-layout unique_short_name(), including
    the two rules a base-slug-only guess misses: the ``clip-<N>`` fallback
    (``N`` is the 1-indexed position in clips.json, matching the old
    cmd_render's own ``enumerate(clips, 1)``) for a hook with no usable
    slug, and the ``-2``/``-3``/... suffix old cmd_render appended when two
    hooks slugified to the same name. Without both, an event with a
    collision or an empty hook has this function look for the wrong
    filename, find nothing, and the draft is silently not migrated - see
    the module's Report.unmapped, which is exactly what a missed draft
    here is now surfaced through.

    Mirrors old cmd_render's control flow entry by entry, including which
    entries never reach unique_short_name() at all (not a dict, marked
    with an error, or missing/non-string "hook" - the same conditions that
    raised there, caught by its own outer try/except, and skipped the
    render instead) - only entries that would actually have been rendered
    consume a name and a slot in the taken set.
    """
    import re
    taken: set[str] = set()
    names: dict[int, str] = {}
    for index, clip in enumerate(clips, 1):
        fallback = f"clip-{index}"
        try:
            if not isinstance(clip, dict):
                raise TypeError(f"Entry is not an object: {clip!r}")
            if clip.get("error"):
                continue
            base = re.sub(r"[^a-z0-9]+", "-", clip["hook"].lower()).strip("-")[:50] or fallback
        except (TypeError, KeyError, AttributeError):
            continue
        name = base
        counter = 2
        while name in taken:
            name = f"{base}-{counter}"
            counter += 1
        taken.add(name)
        names[index] = name
    return names
