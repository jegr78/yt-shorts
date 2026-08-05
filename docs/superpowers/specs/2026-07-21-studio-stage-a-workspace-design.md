# Stage A — Workspace, clip identity and the editorial layer

**Date:** 2026-07-21
**Scope:** the data foundation for the studio application. No user interface,
no glossary, no change to what a rendered short looks like.

## Problem

Three properties of the current layout make an editor impossible to build on
top of it.

**The clip's identity is its title.** `short_name()` derives every filename
from the hook: `speedy.mp4`, `speedy.json`, `speedy.raw.mp4`. Editing a title
would orphan the transcript, the raw clip and the draft under the old name,
and a collision suffix (`speedy-2`) shifts as soon as the order in the source
list changes. The transcript cache already had to be pinned to the clip's
source URL for exactly this reason; the same hazard remains in three other
places.

**There is no place for hand-made data.** Everything on disk today is
derived: harvested timecodes, cached transcripts, rendered shorts. All of it
may be deleted and recomputed. A corrected transcript is not like that —
losing it destroys work that no amount of compute recreates.

**The tool's repository doubles as the data store.** A channel profile, its
fonts, its logos and six rendered shorts live inside the code checkout. That
conflates three things with different lifetimes and different backup needs.

Whisper makes the first two urgent rather than theoretical. It does not know
the proper nouns of a sim-racing league: in `rei-got-sliced` it hears
"very very" for "Rei Racing". That class of error is systematic, not
occasional — league, team, driver and track names are precisely the words
absent from its training data and most load-bearing in a caption.

## The distinction that carries the design

Not "repository versus runtime" but **derived versus editorial**:

| Data | Kind | Losing it costs |
|---|---|---|
| Raw clip, transcript, caption groups, finished short | derived | compute time |
| Title override, transcript correction, keep/discard | editorial | hand work, unrecoverable |
| Channel profile, fonts, assets, `layout.py` | configuration | setup effort |

From which follows the rule the whole stage exists to enforce: **derived data
is never edited in place, and editorial data is never written by a derivation
step.** Corrections live in their own additive layer. Rendering is
`derived + editorial -> short`, so any derived artifact stays safe to delete
at any time.

## Workspace resolution

Resolved in this order, and the tool reports once which one it uses:

1. `YT_SHORTS_DATA`, if set. A path that does not exist is an error, not a
   silent fallback.
2. otherwise `~/YT-Shorts-Data`, if it exists.
3. otherwise `channels/` inside the repository — today's behaviour.

Creating `~/YT-Shorts-Data` is therefore the entire migration switch. No flag,
no cutover date. Reporting the resolved path removes the one ambiguity this
arrangement introduces: which data a run is actually touching.

## Clip identity

A clip's identity is **its source URL** — that is what the clip is. The
directory name pairs a readable slug with a short hash of that URL:

```
speedy--a3f19c2b/
```

The slug is derived from the harvested title **once, at creation**, and never
touched again. Titles become an attribute; the directory is stable across any
number of editorial renames. Two clips sharing a title get distinct
directories from their distinct URLs, so the `-2` collision suffix — which
couples titles to filenames and shifts with list order — is removed outright.

## One clip, one directory

```
~/YT-Shorts-Data/
  channels/erf/
    channel.json  brand.json  fonts/  assets/  layout.py
    events/community-clips-back-catalogue/
      event.json                      deferred - not implemented in Stage A
      sources.json                    collected clip addresses
      clips/
        speedy--a3f19c2b/
          clip.json                   derived: URL, timecodes, harvested title
          edit.json                   editorial: title, status, corrections
          transcript.json             derived (cache)
          raw.mp4                     derived (cache)
          short.mp4                   derived (output)
```

Backing up, deleting or inspecting a clip becomes one operation on one
directory. The scattered layout is what made the transcript cache fragile and
what made losing four reference drafts hard to reason about.

**The cost, stated plainly:** "delete all intermediates" and "show me the
finished shorts" are no longer `ls drafts/`; they need a command. The gallery
page remains the way to review.

## The editorial file

`edit.json` holds only what was actually set by hand. **An untouched clip has
no `edit.json` at all** — the file is created by the first editorial action,
never by a derivation step, so its mere existence means "a human touched this
clip". Within it, an absent field means "no override", not "empty".

```json
{
  "title": "Abschied von Speedy",
  "status": "kept",
  "transcript": {
    "based_on": "sha256:9f2c1a…",
    "words": [ { "start": 10.46, "end": 10.88, "text": " Speedy," } ]
  }
}
```

Harvesting, transcribing and rendering never write it. Editorial actions never
write `clip.json` or `transcript.json`.

## Conflict detection

`based_on` is the checksum of the derived transcript at the moment of
correction. Rendering and listing recompute the current checksum and compare.

On mismatch:

- **The editorial version is used.** Without exception. Hand work is never
  discarded automatically.
- The clip is reported as conflicted — `NOTE:` on stderr, later a marker in
  the application.
- **The run does not abort.** A conflict is information, not a failure.

The three possible behaviours are auto-merge (silently wrong), drop the
correction (data loss), and keep-and-report (this). Two near-misses during
stage 2a came from "it probably matches" assumptions in the cache; a visible
conflict costs a click, a silent mismatch costs a wrong caption under the
channel's logo.

## Status

Three values: `candidate` (default), `kept`, `discarded`.

`discarded` means **do not render** — that is its practical value: discarded
clips stop costing compute and disk. There is deliberately no `uploaded`
state; that belongs to the upload stage, and a state nothing sets is exactly
the kind of speculative structure that gets in the way later.

## Migration

A dedicated command that **copies rather than moves**:

1. Copy everything to the workspace, mapping clips to new identities via their
   URL in `clips.json`.
2. Map existing transcripts through their recorded `source` field — every
   transcript now carries one, so the match is by URL, not by filename.
3. **Verify every copied file by checksum.**
4. Report what went where, and anything that could not be mapped.
5. **Leave the original in place.** The operator deletes it when satisfied,
   not the tool.

Steps 3 and 5 are the direct lesson from the destroyed reference drafts: a
migration that deletes before verifying is the same mistake wearing a
different hat.

## Acceptance

- **Byte-identical output.** The same clip rendered before and after
  migration produces identical bytes. Verified old-code-against-new-code on
  the same local raw material, without network — the only method that has
  proved reliable in this project.
- Resolution order: all three cases plus the error case of a set but invalid
  `YT_SHORTS_DATA`.
- Identity: a title change does not change the directory; two clips with the
  same title get different ones.
- Conflict: correct a transcript, change the derived one, confirm it is both
  reported and that the editorial version is used.
- Migration: checksum of every file before and after, and no file left
  unaccounted for.
- The existing guarantees hold: one failed clip never aborts a run, and the
  event lock still refuses a second concurrent render.

## Known risk

While both layouts exist, two runs resolving to *different* data paths could
work on the same logical event; the lock lives inside an event directory and
those are two different directories. Reporting the resolved path at startup
makes the only unprotected case visible.

## Not in scope

The glossary and Whisper prompt biasing (stage B), the application itself
(stage C), listing a channel's streams and clips through the YouTube API
(stage D), and upload (stage E). Stage A deliberately builds the data
foundation those need and nothing more: at the end of it the tool renders
exactly as it does today, from a different place, with an editorial layer that
nothing yet uses.
