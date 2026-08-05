import subprocess

import pytest

from yt_shorts.overlay import build_overlay
from yt_shorts.profile import load
from yt_shorts.render import compose


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def _test_video(path, seconds=2):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=1280x720:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


def probe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestWithoutTrack:
    def test_output_contract_is_unchanged(self, config, tmp_path):
        raw, layer, target = tmp_path / "r.mp4", tmp_path / "l.png", tmp_path / "o.mp4"
        _test_video(raw)
        build_overlay("HOOK", "FOOTER", config).save(layer)
        compose(str(raw), str(layer), str(target), config)
        assert probe(target, "stream=width,height,sample_aspect_ratio,pix_fmt") == \
            "1080,1920,1:1,yuv420p"


class TestWithTrack:
    def test_track_is_visible_and_contract_holds(self, config, tmp_path):
        from yt_shorts.captions import Caption
        from yt_shorts.subtitle_track import build_track

        raw, layer, target = tmp_path / "r.mp4", tmp_path / "l.png", tmp_path / "o.mp4"
        _test_video(raw)
        build_overlay("HOOK", "FOOTER", config).save(layer)
        track = build_track([Caption(0.0, 2.0, "SHADOW REALM")], config,
                            str(tmp_path / "s.mov"), str(tmp_path / "work"))

        compose(str(raw), str(layer), str(target), config, subtitle_track=track)

        assert probe(target, "stream=width,height,sample_aspect_ratio,pix_fmt") == \
            "1080,1920,1:1,yuv420p"

        frame = tmp_path / "f.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(target),
                        "-frames:v", "1", str(frame)], check=True)
        from PIL import Image
        pixels = Image.open(frame).convert("RGBA").load()
        band = [pixels[x, y] for y in range(1290, 1420, 4) for x in range(200, 900, 4)]
        assert any(p[:3] == (255, 255, 255) for p in band), "no caption pixels found"


class TestEofActionPass:
    """Finding F3: overlay's default eof_action is "repeat", not
    truncation - the code comment used to claim the opposite (that the
    output would be cut short without eof_action=pass). The real
    consequence of dropping the flag is that the subtitle track's last
    frame (the last caption's own image) keeps getting repeated once the
    track runs out, so that caption stays frozen on screen for the rest of
    the video instead of disappearing. tests/test_render_subtitles.py's
    other tests never exercise this: their source video is exactly as
    long as the last caption, so the window past the last caption's end is
    never reached."""

    def test_no_caption_pixels_remain_after_the_last_caption_ends(self, config, tmp_path):
        from yt_shorts.captions import Caption
        from yt_shorts.subtitle_track import build_track

        raw, layer, target = tmp_path / "r.mp4", tmp_path / "l.png", tmp_path / "o.mp4"
        _test_video(raw, seconds=3)  # materially longer than the last caption
        build_overlay("HOOK", "FOOTER", config).save(layer)
        track = build_track([Caption(0.0, 1.0, "SHADOW REALM")], config,
                            str(tmp_path / "s.mov"), str(tmp_path / "work"))

        compose(str(raw), str(layer), str(target), config, subtitle_track=track)

        frame = tmp_path / "f.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2.6", "-i", str(target),
                        "-frames:v", "1", str(frame)], check=True)
        from PIL import Image
        pixels = Image.open(frame).convert("RGBA").load()
        band = [pixels[x, y] for y in range(1290, 1420, 4) for x in range(200, 900, 4)]
        assert not any(p[:3] == (255, 255, 255) for p in band), (
            "caption pixels found well after the last caption ended - it "
            "froze on screen instead of disappearing"
        )
