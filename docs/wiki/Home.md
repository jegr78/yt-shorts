YT-Shorts turns sim-racing livestream clips into finished, branded
1080x1920 YouTube Shorts that only need reviewing and uploading. This wiki
is the manual: setup, the per-channel profile format, the studio, the
render workflow, upload, and what to do when something breaks.

Two things are deliberately not here. The repository's
[README](https://github.com/jegr78/yt-shorts/blob/main/README.md) covers
installing the tool and the workflow after a race weekend — start there if
you have not run it yet. Anything about changing the code is
[CONTRIBUTING.md](https://github.com/jegr78/yt-shorts/blob/main/CONTRIBUTING.md)
and
[CLAUDE.md](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md), which
carries the constraints that are expensive to violate.

The source of these pages is `docs/wiki/` in the repository. Edit them there,
not in the wiki's own editor.

## Pages

- [Home](Home)
- [Layout](Layout) — what the repository holds, what the workspace holds
- [Where the data lives](Where-the-data-lives) — the workspace, and the
  channel/event profile format
- [The editorial layer](The-editorial-layer) — `edit.json`: title, status
  and caption corrections
- [Setting up a new channel](Setting-up-a-new-channel) — from the template
  to a first render
- [Studio](Studio) — the local editor: review, correct, render, upload
- [Subtitles](Subtitles) — the caption layer, the transcript cache and the
  glossary
- [Whole-stream transcription](Whole-stream-transcription) — turning a whole
  stream into a timed transcript
- [Moment detection](Moment-detection) — scanning a transcript for moments
  worth clipping
- [Model providers](Model-providers) — which vendor scores a stream, what it
  costs, where its key goes
- [Upload](Upload) — sending a rendered short to YouTube as a private video
- [Building from source](Building-from-source) — the wheel, the per-OS binary,
  and what is not bundled
- [If something goes wrong](If-something-goes-wrong) — the failure catalogue:
  what you see, and what to do

## Not built yet (later)

Thumbnails, playlists and deleting an upload stay manual in YouTube Studio.
Making a video public and scheduling one are built - see [Upload](Upload).
Live-chat activity as an extra moment signal is a possible later addition;
moment detection currently scores transcript evidence only (by model, or by the
offline lexicon fallback) — an earlier loudness-ranking signal was tried and
removed, see the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md)
for why. The studio picker for turning a detected moment (or a hand-picked
window) into a clip is the stream view — see
[Moment detection](Moment-detection) — so that item is done, not outstanding.
