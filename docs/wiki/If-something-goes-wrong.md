What each failure looks like, and what to do about it.

## Run `yt-shorts doctor` first

```bash
yt-shorts doctor
```

It probes rather than repairs: one line per check, `ok` for a pass, `FAIL` for
a required check that did not, and `--` for an optional layer that is simply
not installed (transcription, the studio's FastAPI and uvicorn, a provider SDK,
the upload libraries). An absent optional layer is reported and forgiven — the
code degrades rather than refusing. If anything required failed it says how
many and tells you to run `yt-shorts install-tools`, and exits 1.

## macOS refuses to run the binary

The release binaries are **unsigned and not notarised**, so macOS quarantines
the archive you downloaded and Gatekeeper refuses to open what came out of it.
Nothing is wrong with the build; there is no Apple Developer signature on it.

Clear the quarantine attribute on the unpacked bundle and run it again:

```bash
xattr -dr com.apple.quarantine /path/to/yt-shorts
```

`-r` because the bundle is a **directory**, not a single file (see
[Building from source](Building-from-source#a-binary-for-one-os)) — the
attribute sits on everything inside it.

## A render appears stuck

**This is the one place the "one failed clip never aborts the run" guarantee
does not hold.** `transcribe.TIMEOUT_SECONDS` bounds only the ffmpeg audio
extraction that precedes the Whisper decode; the decode itself has no timeout
and can in principle run forever. That is a deliberate, investigated omission —
the reasoning is in
[`src/yt_shorts/transcribe.py`](https://github.com/jegr78/yt-shorts/blob/main/src/yt_shorts/transcribe.py)'s
module docstring, under "Unbounded decode", and is not repeated here.

It applies to a **clip's** subtitles, inside a render. Whole-stream
transcription is not affected: it decodes each chunk in a separate, killable
process with a per-chunk timeout.

What to check:

- how long it has been going, against what a decode normally costs — 3.7 to
  10.7 seconds for the 15-60 second clips those timings were measured on, so
  minutes on one clip is already far outside it (see the same docstring);
- whether the clip it is on has subtitles enabled and no cached
  `transcript.json` yet, i.e. whether it is decoding at all.

What to do:

1. **Ctrl-C.** Try this first.
2. If it does not respond within a few seconds, that is itself the sign the
   decode is stuck somewhere Python's own interrupt delivery cannot reach.
   **`kill -9 <pid>`** and re-run. The render loop is not resumable mid-run
   either way, so killing it loses nothing that a re-run does not redo.

For a clip that hangs reproducibly, turn subtitles off for that one event and
render it without them.

A render started from the studio is the same decode on a background thread, so
the Jobs screen's Stop cannot end it: a graceful stop lands between clips and a
hard stop terminates the subprocess the work is waiting on — and a hung decode
is neither. Kill the studio process itself.

## A clip rendered without subtitles

The short is there and plays; it just has no captions. **This is a degrade, not
a failure** — an optional layer was lost, so the clip stays `done` rather than
being reported as broken. The reason is recorded, in two places:

- in the studio, on the clip's **render panel**, as that clip's reason —
  `<hook>: no subtitles (<ErrorType>: <message>)`, or `<hook>: no speech
  detected, no subtitles` when the decode legitimately heard nothing;
- from the CLI, in the terminal and in `<workspace>/logs/yt-shorts.log`. From
  the studio, in that job's own log under `<workspace>/logs/jobs/` instead.

One cause you can fix yourself is an **overlapping word list** in the studio's
transcript editor. The subtitle track refuses a caption list that is unsorted
or overlapping, so the clip loses every caption rather than getting mistimed
ones — the editor warns about such a row rather than blocking the save, and
that warning is what it means (see
[`CLAUDE.md`](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#architecture)).
Two rows that merely touch (`end` of one equal to `start` of the next) are the
decoder's normal shape and are fine.

## The studio refuses to start

```
A studio is already running against this workspace (<workspace>), as process
<pid> (lock file: <workspace>/.studio.lock). Two studios on one workspace
share one jobs.json and silently destroy each other's queued plan, so this
one is refusing to start. …
```

**One studio per workspace**, and the refusal names the process holding it.
Merely starting a second one against the same workspace was measured to mark
the first's running transcription `interrupted` and to delete whatever the
other had queued, with nothing logged and nothing warned — which is why it
refuses instead of quietly picking a free port and starting.

Use the studio that is already running (it prints its own URL when it starts),
or point this one at a different workspace with `YT_SHORTS_DATA`. If you are
certain that process is not a studio, remove the lock file and try again.

A studio that **crashed** leaves its lock behind: the next one recognises the
dead pid, takes the lock over and says so (`NOTE: taking over stale studio
lock ...`). You do not have to clean up after a crash by hand.

## A render refuses to start

```
Event '<event>' is locked by process <pid> (lock file: .../.render.lock).
Another render is already running against this event - wait for it to finish
before starting another, or if you are certain process <pid> is not actually
a render of this event, remove the lock file and try again.
```

One job at a time per event, CLI or studio, and the refusal is immediate rather
than a wait — so you find out before paying for a download and a transcription
that get thrown away. Two racing renders once truncated files in a real event
and destroyed artifacts that could not be recovered; this lock is what stops a
repeat.

A **queued** studio job is not refused by this at all: it stays `queued` with
"waiting for the event lock" as its reason and starts when the event is free.
Only a direct start (a CLI `render`, or a job the worker is about to claim)
sees the message above. A stale lock from a crashed render is taken over the
same way the studio's is.

## A download fails

`yt-dlp` goes stale in weeks — YouTube changes, and an old version stops
getting past the bot check. Before a session:

```bash
yt-shorts install-tools --update
```

See
[Requirements](https://github.com/jegr78/yt-shorts/blob/main/README.md#requirements)
for what the tool installs and why Linux gets a pinned, checksum-verified
`yt-dlp` in the workspace rather than the distribution's package.
