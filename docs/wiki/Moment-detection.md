Given a whole-stream transcript (see
[Whole-stream transcription](Whole-stream-transcription)), the tool
scans it for moments worth clipping and writes what it finds to an analysis
file. **It does not create clips on its own:**

```bash
bin/yt-shorts detect <channel>/<event> <video-id>   # -> streams/<video-id>/moments.json
```

or, in the studio, **queue a transcription first and then detect**: open the
channel's Streams panel, press **Transcribe** on the stream (that enqueues a
`transcribe` job — follow it on the Jobs screen; an 8-hour race takes over two
hours), and once it is `done` press **Detect moments** on the same stream.
Detection in the studio never transcribes on its own — a stream with no
transcript fails the detect job with a message saying to transcribe it first —
so the two steps are planned separately: you can queue five streams to
transcribe overnight without ordering five paid detections at the same time.
Detection itself runs as a background job, and the panel reports which engine
ran and how many moments it found once it finishes. From the CLI it is still
one command: `detect` transcribes and then scores.

**Why it stops at an analysis.** Detection only writes
`streams/<video_id>/moments.json` — a scored list of
candidates (start, end, category, a one-sentence reason, an optional on-screen
hook suggestion) plus a stream-activity overview — and it may be generous,
because a weak suggestion in that file costs you a glance rather than a clip
you have to clean up. Turning one of those candidates into an actual clip
(`clip_from_moment.create_clip`) is a separate, explicit step, and the studio's
**stream view** is where you take it. The
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md)
carries the earlier engine this replaced and why it was thrown away.

**The operator's flow.** Open an event, click its **Streams** tab, and click a
stream — that opens `/{channel}/{event}/streams/{video_id}`, one level deeper
than the editor. You get a searchable transcript, a small player, an overview
strip over the whole stream and, below it, a zoom lane for setting a clip's
exact window (the overview only locates — click it to re-centre the zoom lane
— an eight-hour stream is far too coarse over one strip to set a boundary on
directly; the zoom lane is what you actually drag). Read the transcript,
search it for what you remember being said, and either click a detected
moment in the hit list to jump the player and the zoom lane to it, or drag a
window on the zoom lane yourself. Either way, type a hook and press **Make a
clip** to write a real clip directory from that window.

**None of this needs detection to have run, or any API key at all.** A
transcript alone is enough to open the screen, search it, drag a window and
make a clip — detection only adds the ranked hit list on top, and the screen
says plainly when a stream "has not been analysed yet" rather than hiding the
zoom lane or erroring. If you do run detection, the hit list also tells you
which **engine** produced it (the model, or the weaker offline lexicon
fallback) and names any **window that failed** to scan — both surfaced
directly rather than left for you to notice their absence, the same reason
"reduced quality" gets logged below.

**How a moment is scored.** By default, an hour of transcript at a time is
sent to Claude, which is asked to pick out moments by category — the race
start/finish, incidents (crash, spin, contact...), highlights (overtakes,
fastest laps, pole...), race control (safety car, penalties...) and
commentator reactions — using the channel's own excitement lexicon (see
below) as a vocabulary hint. This needs the optional `anthropic` package:

```bash
.venv/bin/pip install anthropic
```

and an API key at `<workspace>/auth/anthropic.json`, mode 600, gitignored and
never committed — either a bare `sk-ant-...` string or `{"api_key":
"sk-ant-..."}`.

Anthropic is only the **default**. A channel can be pointed at Google Gemini or
OpenAI instead — see [Model providers](Model-providers) for how to choose one,
where each one's key goes, and how far each has actually been measured.

**Without a key, or if the model can't be reached, detection falls back
automatically** to a weaker, fully offline engine that scores marker hits from
the same excitement lexicon, amplified by how much faster the commentary is
running than the stream's own baseline. This never fails outright — a stream
with no lexicon and no key still finishes, it just finds nothing — but the
fallback is always announced: the job's log (and the CLI's own output) say
which engine actually ran, so a quietly worse result is never mistaken for a
normal one.

**The excitement lexicon** (`moments.json`, additive across the workspace,
channel and event, the same layering the glossary uses) is a weighted marker
list, not a flat one — `crash` counts for more than `pole`, because an
unweighted list flags nearly every mention of a word commentators say
constantly. Editing a marker's weight changes what the offline fallback finds
immediately; it only changes what the model is told to look for when the
edit enables or disables a marker outright (crossing zero), since the model
is only ever given the marker names, never their numbers.

A moment, once it exists as a clip, renders through the same pipeline as a
community clip: it carries the stream's video id and a time range, so
`render.Source`'s `--download-sections` path fetches exactly its window, and
nudging that window afterwards is an **editorial** decision stored in
`edit.json` — the render always downloads the effective window, your
override if you set one, the detected one otherwise.
