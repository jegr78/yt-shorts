# YT-Shorts wiki

YT-Shorts turns sim-racing livestream clips into finished, branded
1080x1920 YouTube Shorts that only need reviewing and uploading. This
wiki covers setup, the per-channel profile format and the render
workflow; the repository's own `README.md` and `CLAUDE.md` stay the
source of truth for anything not yet copied here.

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

Making a video public, scheduling, thumbnails, playlists, and deleting an
upload stay manual in YouTube Studio after you review the private upload.
Live-chat activity as an extra moment signal is a possible later addition;
moment detection currently scores transcript evidence only (by model, or by the
offline lexicon fallback) — an earlier loudness-ranking signal was tried and
removed, see the
[detection-and-providers skill](https://github.com/jegr78/yt-shorts/blob/main/.claude/skills/detection-and-providers/SKILL.md)
for why. The studio picker for turning a detected moment (or a hand-picked
window) into a clip is the stream view — see
[Moment detection](Moment-detection) — so that item is done, not outstanding.
