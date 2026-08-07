# The editorial layer (`edit.json`)

Every clip's directory may hold an `edit.json` — hand-made corrections, kept
strictly apart from everything derived (`clip.json`, `transcript.json`,
`raw.mp4`, `short.mp4`). Nothing but a human writes it: `harvest`, `render`
and `transcribe` never do, so re-running any of them never loses a
correction — see
[Architecture](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#architecture)
for the rule that guarantees it. An untouched clip has no `edit.json` at
all — the file is created by the first editorial action, so its mere
existence means a human has looked at this clip.

`render` and `gallery` both read it; a malformed `edit.json` fails only that
one clip (reported with its exception type), not the run.

It is a JSON object with up to three keys, all optional:

```json
{
  "title": "Abschied von Speedy",
  "status": "kept",
  "transcript": {
    "based_on": "sha256:...",
    "words": [{"start": 0.0, "end": 0.5, "text": " hi"}]
  }
}
```

- **`title`** — overrides the harvested hook everywhere it is shown
  (`render`'s overlay, `gallery`'s page). The harvested title in `clip.json`
  is frozen after the clip's first harvest (see
  [What the tool guarantees](https://github.com/jegr78/yt-shorts/blob/main/README.md#what-the-tool-guarantees),
  "A second `harvest` run never destroys good data") — fixing a typo in
  `sources.json` and re-running `harvest` does **nothing** to an
  already-harvested clip; a title correction always goes through `edit.json`
  instead. This is currently a silent surprise if you don't know it, which is
  exactly why it is written down here.
- **`status`** — one of three values, default `"candidate"` if the key is
  absent:
  - `"candidate"` — not yet reviewed. The default state; `render` and
    `gallery` treat it exactly like `"kept"`.
  - `"kept"` — reviewed and approved. No behavioural difference from
    `"candidate"` today; it exists for the operator's own review bookkeeping.
  - `"discarded"` — excluded from `render` (skipped, reported as
    `skipped (discarded): <clip>`) and from `gallery`'s page. The clip's
    directory, and everything in it, stays on disk untouched — discarding
    is reversible by editing `status` back, and is a different action from
    deleting the directory outright.
- **`transcript`** — a hand-corrected caption transcript, `{"based_on":
  "<checksum of the words it was corrected from>", "words": [{"start",
  "end", "text"}, ...]}`. A correction **always** wins over a fresh
  transcription, even if re-transcribing itself fails. If the underlying
  transcript has since changed (a different `based_on` checksum than what
  `transcribe` would now produce), the correction is still used — auto-
  merging would silently produce a wrong caption, and dropping the
  correction would destroy hand work — but the mismatch is reported as a
  `NOTE:` on stderr rather than merged silently. `editorial.checksum()`
  computes the checksum from a word list the same way every time
  (normalized number formatting, sorted keys), so an unchanged transcript
  never spuriously reports a conflict.

**Removing a clip.** Deleting an entry from `sources.json` and re-running
`harvest` does **not** remove the clip — no derivation step ever deletes a
clip's directory (see
[What the tool guarantees](https://github.com/jegr78/yt-shorts/blob/main/README.md#what-the-tool-guarantees),
"Removing a clip from `sources.json` does not delete it"); `harvest` reports
it instead:

```
NOTE: speedy--dde9b753 ('Speedy!') is no longer in the source list. It is
kept as-is, not re-downloaded or re-rendered on its own: delete
.../clips/speedy--dde9b753 yourself to remove it, or set "status":
"discarded" in its edit.json to keep it on disk but exclude it from render
and gallery.
```

Use `"status": "discarded"` to exclude the clip while keeping it (and its
raw download, transcript and any prior short) on disk for reference; delete
the clip's directory yourself when you actually want it gone.
