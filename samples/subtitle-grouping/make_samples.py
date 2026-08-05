"""Build sample subtitled shorts from existing drafts + cached transcripts.

Reads only from drafts/ and transcripts/. Writes only to the sample directory.
"""
import json
import subprocess
import sys
from pathlib import Path

from yt_shorts.captions import group_words
from yt_shorts.profile import load
from yt_shorts.subtitle_track import build_track

ROOT = Path(__file__).resolve().parent.parent.parent
EVENT = ROOT / "channels/erf/events/community-clips-back-catalogue"
OUT = Path(__file__).resolve().parent

VARIANTS = {
    "a-shipped-3w-1.6s": dict(max_words=3, max_seconds=1.6),
    "b-groupwords-4w-3.0s": dict(max_words=4, max_seconds=3.0),
    "c-proposed-5w-2.5s": dict(max_words=5, max_seconds=2.5),
}


def probe(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries",
         "stream=width,height,sample_aspect_ratio,display_aspect_ratio,pix_fmt",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()


def main():
    profile = load("erf/community-clips-back-catalogue")
    config = profile.config
    OUT.mkdir(parents=True, exist_ok=True)
    report = []

    for clip in ("rei-got-sliced", "speedy"):
        draft = EVENT / "drafts" / f"{clip}.mp4"
        words = json.loads(
            (EVENT / "transcripts" / f"{clip}.json").read_text())["words"]

        for label, kw in VARIANTS.items():
            caps = group_words(words, **kw)
            work = OUT / f"{clip}.{label}.work"
            track = build_track(caps, config, str(OUT / f"{clip}.{label}.mov"),
                                str(work))
            target = OUT / f"{clip}.{label}.mp4"
            chain = ("[0:v][1:v]overlay=0:0:format=auto:eof_action=pass[o];"
                     "[o]setsar=1[v]")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(draft), "-i", track,
                 "-filter_complex", chain, "-map", "[v]", "-map", "0:a?",
                 "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", str(target)], check=True)

            rows = [(c.text, round(c.end - c.start, 2), round(c.start, 2))
                    for c in caps]
            singles = [(t, d) for t, d in
                       ((r[0], r[1]) for r in rows) if len(t.split()) == 1]
            over = [(t, d) for t, d in
                    ((r[0], r[1]) for r in rows) if d > kw["max_seconds"] + 1e-9]
            report.append({
                "clip": clip, "variant": label, "config": kw,
                "file": str(target), "probe": probe(target),
                "captions": rows,
                "longest_single_word": max(singles, key=lambda x: x[1])
                if singles else None,
                "over_max_seconds": sorted(over, key=lambda x: -x[1])[:5],
                "count": len(rows),
            })

    (OUT / "report.json").write_text(json.dumps(report, indent=2))

    for r in report:
        print(f"\n=== {r['clip']} / {r['variant']} "
              f"({r['count']} captions) ===")
        print(f"    probe: {r['probe']}")
        print(f"    longest single-word caption: {r['longest_single_word']}")
        print(f"    over max_seconds: {r['over_max_seconds']}")
    print(f"\nreport: {OUT / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
