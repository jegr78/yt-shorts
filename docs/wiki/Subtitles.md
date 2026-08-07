# Subtitles

Subtitles are off by default. Switch them on per channel or per event in
`brand.json`:

```json
"subtitles": { "enabled": true, "max_words": 4, "max_seconds": 3.0, "size": 78, "y": 1290 }
```

`max_words` and `max_seconds` are the values shown above unless a profile
overrides them. Tighter grouping was tried and reads worse: at 3 words /
1.6 seconds the grouping cuts phrases into single words that then stand
alone for a long time. Measured on the real `speedy` transcript, that
setting leaves "by" on screen for 2.08 seconds and "originally" for 2.00,
with four captions running past their own `max_seconds`; at 4 / 3.0 the
same transcript keeps phrases intact and no caption exceeds it. Note that
`max_seconds` is not a hard bound - a single word whose own duration is
longer will still be shown for that long.

The commentary of each clip is transcribed locally with faster-whisper and
cached as `transcript.json` in the clip's own directory. **The first run downloads the model
(464 MB on disk, `models--Systran--faster-whisper-small`)** and therefore
takes noticeably longer than later ones. A clip with no speech, or any
failure anywhere in the subtitle pipeline (transcription, caption grouping,
building the track), simply gets no subtitles; every such case is reported -
on the terminal for a CLI render, and in the render panel plus the job's log
for a studio one - not treated as a failure: the clip itself still renders
and the run's exit code is unaffected. Only a failure of the render itself (the
clip's actual download or composition) still fails that clip. See
[Architecture](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#architecture)
for the rule that guarantees it.

**The Whisper decode itself has no timeout.** The transcription step is
bounded against a hung audio extraction, but not against a hung decode -
that call can in principle run forever (see
[Hard constraints](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#hard-constraints)
for why it was left that way). If a run appears stuck, interrupt
it with Ctrl-C first; if that doesn't respond within a few seconds, kill
the process outright (`kill -9 <pid>`) and re-run. The transcript cache
means clips already transcribed are not re-transcribed, so nothing already
finished is lost by killing it.

A segment Whisper transcribed but scored as likely non-speech is dropped
rather than shown as a caption, and reported as
`NOTE: <clip>: dropped N segment(s) above no_speech_prob threshold 0.95 (...)`.
The threshold is deliberately near-certainty. An earlier 0.75 was picked to
separate real commentary from silence's hallucinations, and that premise is
dead: once the glossary began biasing the decoder, measured commentary scored
ABOVE the hallucinations, so no value separates the two. What the constant
expresses now is a choice - wrong captions beat missing ones, because
correcting a few words costs less than re-typing every word silently
discarded. A drop means exactly one thing: that stretch of
the short has no caption, because the segment covering it was judged too
likely to be non-speech to show.

**Forcing a re-transcribe.** A cached transcript in a clip's own
`transcript.json` is reused on every later render, keyed to the clip's own
source URL rather than its position in `sources.json` - reordering that
file no longer makes a different clip inherit a stale cache. To get a fresh
transcription of a clip (e.g. after upgrading the model, or to see if a
different result comes out this time), delete that clip's
`clips/<clip>/transcript.json`; the next render transcribes it again from
scratch. Nothing else needs to change - a re-order of `sources.json` is not
a reason to delete anything here. A hand-corrected transcript in the same
clip's `edit.json` is unaffected either way - it always wins over a fresh
transcription (see [The editorial layer](The-editorial-layer)).

**Glossary.** Whisper doesn't know a sim-racing league's own proper nouns -
on a real ERF clip it transcribed "Rei Racing" as "very, very". Up to five
layers feed one glossary, least to most specific: an empty built-in default
(shipped in code, not a file), the circuit pack an event selects (see
"Circuit vocabulary" below), the workspace's own `glossary.json`
(`$YT_SHORTS_DATA/glossary.json`, or `~/YT-Shorts-Data/glossary.json`),
`channels/<channel>/glossary.json`, then `events/<event>/glossary.json`.
Every layer is optional, and they are ADDITIVE, entry by entry — not a
wholesale replacement: the most specific layer that names a given term or
replacement wins that entry, and anything a layer doesn't name is inherited
from the layer below it. Each of the three file-backed layers looks like:

```json
{
  "terms": ["Rei Racing", "Team Fullsend", "Nordschleife"],
  "replacements": { "very very": "Rei Racing" }
}
```

To DISABLE an entry a less specific layer contributed — rather than simply
leaving it unmentioned, which inherits it — name it with a falsy value:
`false` for a term, `null` for a replacement, e.g.
`{"terms": {"Nordschleife": false}, "replacements": {"very very": null}}`.
An empty-string replacement is refused outright, not treated as "delete
these words" — that would be indistinguishable from a typo; use `null` to
disable instead. The studio has an editor for the three writable layers
(workspace, channel, event) that shows every entry alongside the layer that
set it, including an entry a more specific layer disabled — struck through,
not hidden.

**Circuit vocabulary.** An event may name the circuit it races at with
`"track": "<id>"` in its own `glossary.json` — the corner names and any
measured mis-hearings for that one venue then apply to that event only, on
top of the layers above. Pick the track in the studio's Event glossary
editor (the Circuit selector) rather than typing the id by hand; the full
list of ids and their display names lives there, or in
`src/yt_shorts/tracks.py` if you're reading the source.

Both keys are optional and work independently. `terms` are handed to the
Whisper decoder as a bias ("hotwords") - this only has a chance to help on
a FRESH transcription; a term added to the glossary cannot retroactively
change how an already-cached transcript was decoded. `replacements` are
applied to the decoded words afterwards, correcting whatever the decoder
still got wrong (case-, punctuation- and whitespace-insensitively, but
never across a sentence boundary), and are written into the cache already
corrected - so the extra work happens once per clip, not once per render.

**Nothing invalidates a cached transcript when the glossary changes** -
this is deliberate for this pre-alpha stage, not an oversight. If a
`transcript.json` was cached before a glossary existed, or before it named
the term that would have fixed it, editing `glossary.json` alone changes
nothing on that clip's next render. Delete that clip's
`clips/<clip>/transcript.json` (see "Forcing a re-transcribe" above) to
re-derive it against the current glossary.
