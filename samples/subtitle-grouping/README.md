# Subtitle grouping samples

Six shorts with subtitles burned in, two clips in three grouping variants
each, made to answer one question by eye: **do the caption groups read well
at speaking pace?**

That question decided the shipped defaults. It cannot be answered from a
test — a caption can satisfy every rule in `captions.py` and still be
unreadable, because "one word alone on screen for two seconds" is not a
rule violation, it is a judgement.

## The finding

`rei-got-sliced` reads well in every variant. `speedy` does not, and the
difference is what settled the defaults:

| | 3 words / 1.6 s | 4 words / 3.0 s |
|---|---|---|
| captions | 44 | 27 |
| longest single-word caption | **"by" — 2.08 s** | "Speedy," — 0.42 s |
| captions over their own `max_seconds` | 4 | 0 |

```
3w/1.6s:   'for'            1.58 s     <- one word, one and a half seconds
           'the last'       0.64 s
           'two'            1.36 s     <- and again
           'months or so,'  1.08 s

4w/3.0s:   'for the last'       2.22 s
           'two months or so,'  2.44 s
```

The tighter setting cuts phrases into single words that then hold the
screen. The looser one keeps them intact. A third variant (5 words /
2.5 s) was tried and was worse than 4 / 3.0 — it still left "originally"
alone for 2.00 s.

Note that `max_seconds` is not a hard bound: a single word whose own
duration is longer will be shown for that long. That is why the tighter
setting produces captions exceeding its own limit, and why this cannot be
fixed by lowering the numbers further.

`report.json` holds every caption of every variant with its start and
on-screen duration, so the comparison can be re-read without opening a
video.

## The videos are not in git

Six 1080x1920 mp4s are about 135 MB. They are reproducible output, so
only the script and the measurements are tracked; the mp4s are ignored.

## Regenerating them

```bash
PYTHONPATH=src .venv/bin/python samples/subtitle-grouping/make_samples.py
```

No network needed. The script reads the finished shorts from the event's
`drafts/` and the cached word transcripts from its `transcripts/`, builds
a subtitle track for each variant and overlays it — which is exactly what
the renderer produces. It writes only into this directory.

If `drafts/` or `transcripts/` are missing, run
`bin/yt-shorts render erf/community-clips-back-catalogue` first with
subtitles enabled in the profile.
