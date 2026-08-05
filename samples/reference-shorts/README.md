# Reference shorts

The last two shorts rendered **before** subtitles were switched on for the
ERF channel, kept as a visual and byte-level reference.

`SHA256SUMS` is the point of this directory. The mp4s themselves are
ignored by git (they are large and reproducible); the checksums are
tracked, so the baseline survives even if the files do not.

```bash
cd samples/reference-shorts && shasum -a 256 -c SHA256SUMS
```

## Why this exists

Four of the six original reference drafts were destroyed during stage 2a
development by a verification run that rendered into the real `drafts/`
directory while a second render raced it. They were not tracked, no
backup existed, and re-rendering does not restore them: the render
downloads from YouTube, and yt-dlp may return a differently encoded copy,
so the bytes differ for reasons that have nothing to do with the code.

These two survived. Two lessons are baked into this directory:

- **Never render into the directory holding the reference artifacts.**
  A verification run writes to a scratch directory and compares.
- **A network-derived artifact is a poor baseline.** The stronger check,
  and the one the test suite and every review now use, compares old code
  against new code on the *same local input*: identical filter chain
  string, identical output SHA-256. That needs no reference file at all.

These two files remain useful for the thing that check cannot do — looking
at what the shorts actually looked like before subtitles existed.
