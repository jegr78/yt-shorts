#!/usr/bin/env python3
"""Regenerate docs/wiki/images/ from the test fixture. No third-party frame and
no real channel data: the picture is ffmpeg's `testsrc`, the workspace a
temporary copy of tests/fixtures/channels/.

Usage:  PYTHONPATH=src .venv/bin/python tools/build-wiki-images.py

Not while a test suite is in flight - this serves studio/static/, and a
frontend build deletes it. Byte-stable on one machine, only geometry across
machines: review these as a diff, not by checksum.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE_CHANNELS = REPO / "tests" / "fixtures" / "channels"
IMAGES = REPO / "docs" / "wiki" / "images"

CHANNEL = "erf"
EVENT = "community-clips-back-catalogue"
PROFILE_ID = f"{CHANNEL}/{EVENT}"
HOOK = "WHAT IS HAPPENING?!?"

# Half of 1080x1920: readable in a wiki page, a quarter of the file size.
PREVIEW_SIZE = (540, 960)
VIEWPORT = {"width": 1440, "height": 900}
BACKDROP = (110, 110, 110)


def die(message: str) -> None:
    raise SystemExit(f"build-wiki-images: {message}")


def check_dependencies() -> None:
    """Fail loudly and name the install command - this is a maintainer tool,
    so a missing dependency must not degrade into a missing image."""
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} is not on PATH - install ffmpeg (brew install ffmpeg)")
    try:
        import PIL  # noqa: F401
    except ImportError:
        die("Pillow is missing - .venv/bin/pip install pillow")
    try:
        import playwright  # noqa: F401
    except ImportError:
        die("Playwright is missing - .venv/bin/pip install playwright")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as error:  # noqa: BLE001 - report whatever went wrong
        die(f"Chromium for Playwright is unusable ({error}) - run "
            f".venv/bin/python -m playwright install chromium")


def seeded_workspace(root: Path):
    """A temporary workspace holding a copy of the checked-in erf fixture,
    with profile.CHANNELS_DIR and YT_SHORTS_DATA both pointed at it."""
    from yt_shorts import profile as profile_module

    channels = root / "channels"
    shutil.copytree(FIXTURE_CHANNELS / CHANNEL, channels / CHANNEL,
                    ignore=shutil.ignore_patterns("__pycache__"))
    profile_module.CHANNELS_DIR = channels
    os.environ["YT_SHORTS_DATA"] = str(root)
    return profile_module.load(PROFILE_ID)


def flatten(image, size):
    """Composite the transparent overlay onto mid-grey so the veil, the edges
    and the alpha-0 video window are all visible in a static picture."""
    from PIL import Image

    backdrop = Image.new("RGB", image.size, BACKDROP)
    backdrop.paste(image, (0, 0), image)
    return backdrop.resize(size, Image.LANCZOS)


def build_overlay_image(profile, out: Path) -> None:
    from yt_shorts.overlay import build_overlay

    image = build_overlay(HOOK, profile.channel["footer"], profile.config)
    flatten(image, PREVIEW_SIZE).save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes, {PREVIEW_SIZE[0]}x{PREVIEW_SIZE[1]})")


def testsrc_video(target: Path, size: str = "1920x1080", duration: float = 1.0) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size={size}:rate=10:duration={duration}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)], check=True)


def probe(short: Path) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
         "-of", "csv=p=0", str(short)],
        check=True, capture_output=True, text=True)
    return result.stdout.strip().rstrip(",")


def build_frame_image(profile, work: Path, out: Path) -> None:
    """A real short built from a synthetic source, then one frame of it with
    the sample aspect ratio applied - an extracted frame ignores SAR, which is
    how a distorted render once passed six reviewed stills (see CLAUDE.md)."""
    from yt_shorts import render
    from yt_shorts.overlay import build_overlay

    source = work / "source.mp4"
    overlay_png = work / "overlay.png"
    short = work / "short.mp4"
    testsrc_video(source)
    build_overlay(HOOK, profile.channel["footer"], profile.config).save(overlay_png)
    render.compose(str(source), str(overlay_png), str(short), profile.config)

    geometry = probe(short)
    print(f"ffprobe {short.name}: {geometry}")
    if geometry != "1080,1920,1:1,9:16":
        die(f"the short is not a square-pixel 9:16 file: {geometry}")

    frame = work / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(short),
                    "-vf", "scale=iw*sar:ih", "-frames:v", "1", str(frame)],
                   check=True)

    from PIL import Image

    with Image.open(frame) as image:
        image.convert("RGB").resize(PREVIEW_SIZE, Image.LANCZOS).save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes, {PREVIEW_SIZE[0]}x{PREVIEW_SIZE[1]})")


CLIPS = [
    ("https://www.youtube.com/clip/UgkxWhatIsHappening", HOOK, None),
    ("https://www.youtube.com/clip/UgkxBarbie456", "Jegr and the Barbie", "kept"),
    ("https://www.youtube.com/clip/UgkxSliced789", "rei got sliced", None),
    ("https://www.youtube.com/clip/UgkxSafetyCar012", "Forcing a SC", None),
]

WORDS = [
    {"start": 0.10, "end": 0.44, "text": " and"},
    {"start": 0.44, "end": 0.92, "text": " that"},
    {"start": 0.92, "end": 1.30, "text": " is"},
    {"start": 1.30, "end": 1.86, "text": " contact"},
    {"start": 1.86, "end": 2.20, "text": " into"},
    {"start": 2.20, "end": 2.58, "text": " turn"},
    {"start": 2.58, "end": 3.05, "text": " one"},
]


def seed_clips(profile, work: Path) -> None:
    """The clip list the screenshot shows: one selected clip with a transcript
    and a raw file to preview, one kept clip that already has a short, two
    untouched candidates."""
    from yt_shorts import clipstore, editorial

    raw_source = work / "raw.mp4"
    testsrc_video(raw_source, size="1280x720", duration=4.0)
    for index, (url, hook, status) in enumerate(CLIPS):
        start = 12.0 + index * 30
        directory = clipstore.write_clip(profile.event_dir, {
            "url": url, "hook": hook, "source_title": "ERF Endurance Series Round 3",
            "start": start, "end": start + 8.0, "duration": 8.0, "error": None})
        if status == "kept":
            editorial.save(directory, editorial.Edit(
                title=hook, status=editorial.KEPT, transcript=None))
            clipstore.short_path(directory).write_bytes(b"a rendered short")
        if index == 0:
            clipstore.transcript_path(directory).write_text(json.dumps(
                {"model": "small", "source": url, "words": WORDS}), encoding="utf-8")
            shutil.copy(raw_source, clipstore.raw_path(directory))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_serving(base_url: str, deadline_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/channels", timeout=0.5)
            return
        except Exception as error:  # noqa: BLE001 - just a readiness probe
            last = error
            time.sleep(0.05)
    die(f"the studio server did not start in time: {last}")


def build_studio_image(profile, work: Path, out: Path) -> None:
    """The real built studio page, served by a real uvicorn server against the
    seeded temporary workspace - the same wiring tests/test_studio_e2e.py uses,
    minus its per-test app switch."""
    import uvicorn
    from playwright.sync_api import sync_playwright

    from yt_shorts.studio.api import create_app

    seed_clips(profile, work)
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_until_serving(base_url)
        with sync_playwright() as play:
            browser = play.chromium.launch()
            try:
                page = browser.new_context(viewport=VIEWPORT).new_page()
                page.goto(f"{base_url}/{CHANNEL}/{EVENT}")
                page.get_by_role("button", name=HOOK).click()
                page.get_by_role("textbox", name="Title").wait_for()
                page.locator("img[alt^='Preview at']").wait_for()
                page.wait_for_timeout(500)
                page.screenshot(path=str(out))
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            die("the studio server thread outlived this script")
    print(f"wrote {out} ({out.stat().st_size} bytes, "
          f"{VIEWPORT['width']}x{VIEWPORT['height']})")


def main() -> int:
    check_dependencies()
    IMAGES.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        work = root / "work"
        work.mkdir()
        profile = seeded_workspace(root)
        build_overlay_image(profile, IMAGES / "overlay.png")
        build_frame_image(profile, work, IMAGES / "frame.png")
        build_studio_image(profile, work, IMAGES / "studio-editor.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
