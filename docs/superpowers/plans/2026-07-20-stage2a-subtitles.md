# Stage 2a Subtitles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Burn word-group subtitles from the clip's own commentary into the finished shorts, switchable per channel and per event.

**Architecture:** Subtitles are a separate timed layer, not part of the brand overlay. Words with timestamps come from Whisper, pure logic groups them into 2–4 word captions, each caption becomes a PNG, and the PNGs become one alpha video track that the renderer overlays in a single extra step.

**Tech Stack:** Python 3.14 (venv), Pillow, faster-whisper, ffmpeg 8.1.2 (`concat`, `qtrle`/`argb`, `overlay`)

## Global Constraints

- **ffmpeg is not reinstalled or modified.** The build at `/opt/homebrew/bin/ffmpeg` has no `libfreetype` and no `libass`; `drawtext` and `subtitles` do not exist. All text is drawn in Pillow. The racecast broadcast project depends on this exact binary.
- **`setsar=1` stays at the end of the filter chain.** Without it the output carries a non-square sample aspect ratio and players stretch it back to 16:9.
- **No `crop` and no `force_original_aspect_ratio=increase` in the sharp branch.** The timing tower and leaderboard are burned into the source; the picture is never cropped.
- **Output contract unchanged:** exactly 1080x1920, `SAR 1:1`, `DAR 9:16`, `yuv420p`, h264/aac, `+faststart`.
- **Subtitles are off by default.** With `subtitles.enabled` false, the filter chain must be character-for-character the one in use today.
- **The six existing shorts stay byte-identical while subtitles are off.** Reference hooks: `WHAT IS HAPPENING?!?`, `Jegr and the Barbie`, `rei got sliced`, `Forcing a SC`, `Speedy!`, `Jegr Tunes`, footer `ERF | @ERFofficial`.
- **A failed clip never aborts a run.** Errors are isolated per entry, collected, reported with a reason, exit code 1.
- All Python dependencies go into `/Users/jegr/Documents/github/YT-Shorts/.venv`, never system Python. Tests run as `PYTHONPATH=src .venv/bin/pytest`.
- `/Users/jegr/racecast/` is read-only.
- Everything in English: identifiers, comments, docstrings, output, commit messages.

## File Structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/captions.py` | Words with timestamps → caption groups. Pure logic, no I/O. |
| `src/yt_shorts/transcribe.py` | Video file → words with timestamps, cached as JSON. Calls ffmpeg and faster-whisper. |
| `src/yt_shorts/subtitle_track.py` | Caption groups → alpha `.mov`. Calls Pillow (via `overlay`) and ffmpeg. |
| `src/yt_shorts/overlay.py` | Gains `build_caption`; `build_overlay` untouched. |
| `src/yt_shorts/render.py` | `compose` gains an optional subtitle track argument. |
| `bin/yt-shorts` | Wires transcription into the render loop when enabled. |

---

### Task 1: Caption grouping

**Files:**
- Create: `src/yt_shorts/captions.py`
- Test: `tests/test_captions.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Caption` — dataclass with `start: float`, `end: float`, `text: str`
  - `group_words(words: list[dict], max_words: int = 3, max_seconds: float = 1.6) -> list[Caption]` — each input dict has keys `start` (float), `end` (float), `text` (str)

This is where subtitle quality is decided, and the only component testable without Whisper, ffmpeg or network. It is built first so its rules can be argued over at the result.

Measured input characteristics it must handle (from three real clips): median word duration 0.26 s, gaps between words are all 0.00 s so pauses cannot be used to split, and Whisper emits leading spaces on word text (`' drives'`).

- [ ] **Step 1: Write the failing test**

`tests/test_captions.py`:

```python
import pytest

from yt_shorts.captions import Caption, group_words


def w(start, end, text):
    """Shorthand for a word as transcribe.py emits it."""
    return {"start": start, "end": end, "text": text}


class TestGrouping:
    def test_empty_input_yields_nothing(self):
        assert group_words([]) == []

    def test_fewer_words_than_the_limit_form_one_caption(self):
        words = [w(0.0, 0.2, " it"), w(0.2, 0.5, " drives")]
        assert group_words(words, max_words=3) == [Caption(0.0, 0.5, "it drives")]

    def test_splits_at_the_word_limit(self):
        words = [w(i * 0.3, i * 0.3 + 0.3, f" w{i}") for i in range(6)]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["w0 w1 w2", "w3 w4 w5"]

    def test_splits_before_a_group_would_exceed_the_time_limit(self):
        """[one, two] would span 2.4s, past the 1.6s limit, so the group is
        closed BEFORE 'two' joins it. [two, three] spans exactly 1.6s and is
        therefore still allowed."""
        words = [w(0.0, 1.0, " one"), w(1.0, 2.4, " two"), w(2.4, 2.6, " three")]
        captions = group_words(words, max_words=99, max_seconds=1.6)
        assert [c.text for c in captions] == ["one", "two three"]

    def test_a_group_never_exceeds_the_time_limit(self):
        words = [w(i * 0.5, i * 0.5 + 0.5, f" w{i}") for i in range(20)]
        captions = group_words(words, max_words=99, max_seconds=1.6)
        assert all(c.end - c.start <= 1.6 for c in captions)

    def test_sentence_punctuation_closes_a_group_early(self):
        words = [w(0.0, 0.3, " no,"), w(0.3, 0.6, " what"), w(0.6, 0.9, " is")]
        captions = group_words(words, max_words=3, max_seconds=99.0)
        assert [c.text for c in captions] == ["no,", "what is"]

    def test_timestamps_span_first_to_last_word(self):
        words = [w(1.5, 1.8, " a"), w(1.8, 2.9, " b")]
        assert group_words(words, max_words=2)[0] == Caption(1.5, 2.9, "a b")

    def test_leading_whitespace_is_stripped(self):
        assert group_words([w(0.0, 0.2, "  hello  ")])[0].text == "hello"

    def test_blank_words_are_dropped(self):
        words = [w(0.0, 0.2, " a"), w(0.2, 0.3, "   "), w(0.3, 0.5, " b")]
        assert [c.text for c in group_words(words, max_words=9)] == ["a b"]

    def test_a_single_word_longer_than_the_time_limit_still_becomes_a_caption(self):
        assert group_words([w(0.0, 5.0, " loooong")], max_seconds=1.6) == [
            Caption(0.0, 5.0, "loooong")
        ]

    def test_captions_never_overlap_and_stay_in_order(self):
        words = [w(i * 0.3, i * 0.3 + 0.3, f" w{i}") for i in range(20)]
        captions = group_words(words, max_words=3, max_seconds=1.6)
        assert all(a.end <= b.start for a, b in zip(captions, captions[1:]))
        assert all(c.end >= c.start for c in captions)

    def test_every_word_survives_grouping(self):
        words = [w(i * 0.3, i * 0.3 + 0.3, f" w{i}") for i in range(20)]
        captions = group_words(words, max_words=3, max_seconds=1.6)
        assert " ".join(c.text for c in captions).split() == [f"w{i}" for i in range(20)]


class TestGuards:
    def test_max_words_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            group_words([w(0.0, 0.2, " a")], max_words=0)

    def test_max_seconds_of_zero_or_less_is_rejected(self):
        with pytest.raises(ValueError):
            group_words([w(0.0, 0.2, " a")], max_seconds=0.0)
```

- [ ] **Step 2: Run the test and watch it fail**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_captions.py -q
```

Expected: `ModuleNotFoundError: No module named 'yt_shorts.captions'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/captions.py`:

```python
"""Groups transcribed words into short caption lines.

Pure logic: no files, no network, no drawing. This is where subtitle
quality is decided, so it is kept testable with invented word lists.

Whisper emits one segment per sentence, which is far too long to read as a
subtitle (one measured clip produced a single 14.6 second segment of 33
words). It also emits no measurable pauses between words, so groups cannot
be split on silence. Grouping therefore runs on three rules: a word count,
an elapsed time, and sentence punctuation closing a group early.
"""

from __future__ import annotations

from dataclasses import dataclass

# A word ending in one of these closes the group after it, so a caption
# does not straddle a sentence boundary.
CLOSING_PUNCTUATION = ".!?,;:"


@dataclass
class Caption:
    start: float
    end: float
    text: str


def group_words(words: list[dict], max_words: int = 3,
                max_seconds: float = 1.6) -> list[Caption]:
    """Groups words into captions of at most max_words and max_seconds."""
    if max_words < 1:
        raise ValueError(f"max_words must be at least 1, got {max_words}")
    if max_seconds <= 0:
        raise ValueError(f"max_seconds must be positive, got {max_seconds}")

    captions: list[Caption] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        captions.append(Caption(
            start=current[0]["start"],
            end=current[-1]["end"],
            text=" ".join(word["text"].strip() for word in current),
        ))
        current.clear()

    for word in words:
        text = word["text"].strip()
        if not text:
            continue
        # Would this word push the group past the time limit? Decide before
        # appending, so a group never exceeds the limit - unless it is a
        # single word that is longer than the limit all by itself.
        if current and word["end"] - current[0]["start"] > max_seconds:
            flush()
        current.append(word)
        if len(current) >= max_words or text[-1] in CLOSING_PUNCTUATION:
            flush()

    flush()
    return captions
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_captions.py -q
```

Expected: `14 passed`

- [ ] **Step 5: Check the rules against real transcribed words**

The point of building this first is to judge the rules at the result, not on paper. Run the grouping over the real commentary measured for the design:

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/python -c "
from yt_shorts.captions import group_words
# Word timings measured from the real clip 'rei got sliced' on 2026-07-20.
raw = ('It 0.00 0.18|drives 0.18 0.52|quite 0.52 0.82|slow 0.82 1.20|and 1.20 2.78|'
       \"it's 2.78 3.16|very 3.16 3.66|easy 3.66 4.02|to 4.02 4.20|pick 4.20 4.52|\"
       'up 4.52 4.70|a 4.70 4.82|penalty 4.82 5.40|as 5.40 6.10|that 6.10 6.36|'
       'is 6.36 6.54|Ray 6.54 6.90|Ray 6.90 7.30|so 7.30 7.90|is 7.90 8.06|'
       'he 8.06 8.20|going 8.20 8.50|to 8.50 8.62|go 8.62 8.80|for 8.80 9.00|'
       'the 9.00 9.14|double 9.14 9.54|overtake 9.54 10.10|again 10.10 10.70|'
       'and 10.70 11.10|oh 11.10 11.40|no 11.40 11.70|he 11.70 11.86|'
       \"he's 11.86 12.04|flying 12.04 12.58|off 12.58 13.12|into 13.12 13.62|\"
       'the 13.62 13.98|shadow 13.98 14.22|realm 14.22 14.62')
words = [{'text': ' ' + t, 'start': float(s), 'end': float(e)}
         for t, s, e in (p.split() for p in raw.split('|'))]
for c in group_words(words):
    print(f'{c.start:6.2f}-{c.end:6.2f}  {c.text}')
"
```

Read the output. Are the groups readable at speaking pace? If they consistently break in awkward places, adjust `max_words`/`max_seconds` defaults or the punctuation rule now — before three more components depend on this.

- [ ] **Step 6: Commit**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
git add src/yt_shorts/captions.py tests/test_captions.py
git commit -m "Group transcribed words into short captions"
```

---

### Task 2: Drawing a caption

**Files:**
- Modify: `src/yt_shorts/overlay.py`
- Test: `tests/test_caption_drawing.py`

**Interfaces:**
- Consumes: `overlay.wrap_text`, `overlay._fitting_size`, `overlay.MARGIN` (all already present)
- Produces: `overlay.build_caption(text: str, config: dict) -> PIL.Image.Image` — a 1080x1920 RGBA image, transparent everywhere except the caption

The caption sits in the lower band, between the video window and the footer. It must never reach into the video window (the sharp picture would be obscured) nor into the footer.

Reads from `config`: `output.width`, `output.height`, `output.video_y`, `output.video_height`, `colors.text`, `fonts.hook`, and optionally `subtitles.size` (default 78) and `subtitles.y` (default 1290). Nothing is hardcoded that belongs in the profile.

- [ ] **Step 1: Write the failing test**

`tests/test_caption_drawing.py`:

```python
import pytest

from yt_shorts.overlay import build_caption
from yt_shorts.profile import load


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def white_pixels(image):
    """Coordinates of fully opaque white pixels."""
    pixels = image.load()
    return [(x, y) for y in range(image.height) for x in range(image.width)
            if pixels[x, y] == (255, 255, 255, 255)]


class TestBuildCaption:
    def test_dimensions_and_mode(self, config):
        image = build_caption("SHADOW REALM", config)
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"

    def test_text_is_drawn(self, config):
        assert len(white_pixels(build_caption("SHADOW REALM", config))) > 1000

    def test_nothing_reaches_into_the_video_window(self, config):
        output = config["output"]
        top, bottom = output["video_y"], output["video_y"] + output["video_height"]
        for text in ["SHADOW REALM", "A" * 80, " ".join(["word"] * 40)]:
            ys = [y for _, y in white_pixels(build_caption(text, config))]
            assert all(y >= bottom or y < top for y in ys), f"caption reached the window: {text!r}"

    def test_nothing_leaves_the_side_margins(self, config):
        for text in ["SHADOW REALM", "A" * 80, " ".join(["word"] * 40)]:
            xs = [x for x, _ in white_pixels(build_caption(text, config))]
            assert min(xs) >= 40 and max(xs) <= 1040, f"caption left the margins: {text!r}"

    def test_nothing_reaches_the_very_bottom(self, config):
        """The footer lives there."""
        for text in ["SHADOW REALM", " ".join(["word"] * 40)]:
            ys = [y for _, y in white_pixels(build_caption(text, config))]
            assert max(ys) < 1800, f"caption reached the footer area: {text!r}"

    def test_empty_text_draws_nothing(self, config):
        assert white_pixels(build_caption("", config)) == []

    def test_video_window_is_fully_transparent(self, config):
        image = build_caption("SHADOW REALM", config)
        pixels = image.load()
        assert pixels[540, 900][3] == 0
```

- [ ] **Step 2: Run the test and watch it fail**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_caption_drawing.py -q
```

Expected: `ImportError: cannot import name 'build_caption'`

- [ ] **Step 3: Write the implementation**

Append to `src/yt_shorts/overlay.py`:

```python
DEFAULT_CAPTION_SIZE = 78
DEFAULT_CAPTION_Y = 1290
CAPTION_MAX_LINES = 2


def build_caption(text: str, config: dict) -> Image.Image:
    """Draws one caption group into the lower band.

    Returns a full-size transparent image so the renderer can overlay it at
    0:0 exactly like the brand overlay, instead of tracking an offset.

    The caption must never reach into the video window above it or the
    footer below it, so it is laid out inside an explicit box and uses the
    same size search as the hook: shrink until it fits, truncate rather
    than overflow.
    """
    output = config["output"]
    subtitles = config.get("subtitles", {})
    width, height = output["width"], output["height"]

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text.strip():
        return image

    top = int(subtitles.get("y", DEFAULT_CAPTION_Y))
    size = int(subtitles.get("size", DEFAULT_CAPTION_SIZE))
    max_width = width - 2 * MARGIN

    font, lines = _fitting_size(
        text.upper(), config["fonts"]["hook"], max_width,
        CAPTION_MAX_LINES, start=size, minimum=min(MIN_SIZE, size),
    )

    draw = ImageDraw.Draw(image)
    line_height = int(font.size * 1.12)
    y = top
    for line in lines:
        draw.text((width // 2, y), line, font=font,
                  fill=_with_alpha(config["colors"]["text"], ALPHA_OPAQUE), anchor="ma")
        y += line_height
    return image
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_caption_drawing.py -q
```

Expected: `7 passed`

- [ ] **Step 5: Confirm the existing overlays are untouched**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest -q
```

Expected: all previous tests still pass, including `tests/test_event_layer_no_regression.py`, which pins the six reference overlays by SHA-256.

- [ ] **Step 6: Look at one**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/python -c "
from yt_shorts.profile import load
from yt_shorts.overlay import build_overlay, build_caption
from PIL import Image
p = load('erf/community-clips-back-catalogue')
base = build_overlay('WHAT IS HAPPENING?!?', p.channel['footer'], p.config)
Image.alpha_composite(base, build_caption('shadow realm', p.config)).save('/tmp/caption-preview.png')
print('written: /tmp/caption-preview.png')
"
```

The pixel tests prove it stays inside its box; they cannot say whether it reads well. That is a human judgement — look at the file.

- [ ] **Step 7: Commit**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
git add src/yt_shorts/overlay.py tests/test_caption_drawing.py
git commit -m "Draw a caption group into the lower band"
```

---

### Task 3: The subtitle track

**Files:**
- Create: `src/yt_shorts/subtitle_track.py`
- Test: `tests/test_subtitle_track.py`

**Interfaces:**
- Consumes: `captions.Caption`, `overlay.build_caption`
- Produces: `subtitle_track.build_track(captions: list[Caption], config: dict, target: str, work_dir: str, ffmpeg: str = "ffmpeg") -> str | None` — writes an alpha `.mov` and returns its path, or `None` when there are no captions

An alpha track keeps the filter chain the same length no matter how many captions there are. The alternative — one timeline-gated `overlay` per caption — would grow to 60–80 chained filters on a 60 second short.

Gaps between captions are filled with a fully transparent image, so the track runs continuously from 0 to the last caption's end.

- [ ] **Step 1: Write the failing test**

`tests/test_subtitle_track.py`:

```python
import subprocess

import pytest

from yt_shorts.captions import Caption
from yt_shorts.profile import load
from yt_shorts.subtitle_track import build_track


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def probe(path, entries):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestBuildTrack:
    def test_no_captions_yields_no_track(self, config, tmp_path):
        assert build_track([], config, str(tmp_path / "t.mov"), str(tmp_path)) is None

    def test_track_has_an_alpha_channel(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one")], config, str(target), str(tmp_path))
        assert probe(target, "stream=pix_fmt").startswith("argb")

    def test_track_has_the_output_dimensions(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one")], config, str(target), str(tmp_path))
        assert probe(target, "stream=width,height") == "1080,1920"

    def test_track_runs_until_the_last_caption_ends(self, config, tmp_path):
        target = tmp_path / "t.mov"
        build_track([Caption(0.0, 1.0, "one"), Caption(2.0, 3.5, "two")],
                    config, str(target), str(tmp_path))
        assert float(probe(target, "format=duration")) == pytest.approx(3.5, abs=0.15)

    def test_a_leading_gap_is_filled_with_transparency(self, config, tmp_path):
        """A caption starting at 2.0s must not shift to 0.0s."""
        target = tmp_path / "t.mov"
        build_track([Caption(2.0, 3.0, "late")], config, str(target), str(tmp_path))
        assert float(probe(target, "format=duration")) == pytest.approx(3.0, abs=0.15)
```

- [ ] **Step 2: Run the test and watch it fail**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_subtitle_track.py -q
```

Expected: `ModuleNotFoundError: No module named 'yt_shorts.subtitle_track'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/subtitle_track.py`:

```python
"""Turns caption groups into a transparent video track.

ffmpeg here has no libass, so subtitles cannot be burned in as text. Each
caption is drawn as a PNG instead and the PNGs are concatenated into an
alpha video (qtrle/argb) that the renderer overlays in a single step. The
alternative - one timeline-gated overlay filter per caption - would grow
the filter chain to 60-80 links on a one minute short.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .captions import Caption
from .overlay import build_caption

TIMEOUT_SECONDS = 300


def _timeline(captions: list[Caption]) -> list[tuple[str, float]]:
    """Caption list -> (text, duration) pairs covering 0 to the last end,
    with gaps represented as empty text."""
    entries: list[tuple[str, float]] = []
    position = 0.0
    for caption in captions:
        if caption.start > position:
            entries.append(("", caption.start - position))
        entries.append((caption.text, max(caption.end - caption.start, 0.0)))
        position = caption.end
    return entries


def build_track(captions: list[Caption], config: dict, target: str,
                work_dir: str, ffmpeg: str = "ffmpeg") -> str | None:
    """Writes an alpha .mov of the captions. Returns None if there are none."""
    if not captions:
        return None

    directory = Path(work_dir)
    directory.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for index, (text, duration) in enumerate(_timeline(captions)):
        png = directory / f"caption-{index:04d}.png"
        build_caption(text, config).save(png)
        lines.append(f"file '{png.name}'")
        lines.append(f"duration {duration:.3f}")
    # The concat demuxer ignores the final entry's duration, so the last
    # image is repeated to give it one.
    lines.append(lines[-2])

    script = directory / "captions.txt"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")

    command = [
        ffmpeg, "-v", "error", "-y",
        "-f", "concat", "-i", str(script),
        "-c:v", "qtrle", "-pix_fmt", "argb",
        target,
    ]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed building the subtitle track.\nCommand: " + " ".join(command)
            + "\nOutput: " + result.stderr.strip()
        )
    return target
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_subtitle_track.py -q
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
git add src/yt_shorts/subtitle_track.py tests/test_subtitle_track.py
git commit -m "Build a transparent subtitle track from caption groups"
```

---

### Task 4: Transcription

**Files:**
- Create: `src/yt_shorts/transcribe.py`
- Test: `tests/test_transcribe.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `transcribe.transcribe(video: str, cache: str, model_name: str = "small", ffmpeg: str = "ffmpeg") -> list[dict]` — returns words as `{"start": float, "end": float, "text": str}`, reading and writing the JSON cache at `cache`
  - `transcribe.TranscriptionError` — raised when audio extraction or transcription fails

Measured on this machine: 15–26 s of audio takes 3–5 s with the `small` model on CPU. The model is downloaded once on first use (~150 MB).

- [ ] **Step 1: Install the dependency**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
.venv/bin/pip install --quiet faster-whisper
.venv/bin/python -c "import faster_whisper; print('faster-whisper', faster_whisper.__version__)"
```

Expected: a version line, no traceback.

- [ ] **Step 2: Write the failing test**

`tests/test_transcribe.py`:

```python
import json
import subprocess

import pytest

from yt_shorts.transcribe import TranscriptionError, transcribe


def silent_video(path, seconds=1):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)


class TestCache:
    def test_a_present_cache_is_used_without_touching_the_video(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text(json.dumps(
            {"words": [{"start": 0.0, "end": 0.5, "text": "cached"}]}), encoding="utf-8")
        words = transcribe(str(tmp_path / "does-not-exist.mp4"), str(cache))
        assert words == [{"start": 0.0, "end": 0.5, "text": "cached"}]

    def test_a_corrupt_cache_is_reported_not_ignored(self, tmp_path):
        cache = tmp_path / "t.json"
        cache.write_text("{not json", encoding="utf-8")
        with pytest.raises(TranscriptionError):
            transcribe(str(tmp_path / "v.mp4"), str(cache))


class TestFailures:
    def test_a_missing_video_is_reported_with_the_command(self, tmp_path):
        with pytest.raises(TranscriptionError) as error:
            transcribe(str(tmp_path / "missing.mp4"), str(tmp_path / "t.json"))
        assert "ffmpeg" in str(error.value)


class TestSilence:
    def test_silence_yields_no_words_and_still_writes_a_cache(self, tmp_path):
        video = tmp_path / "silent.mp4"
        silent_video(video)
        cache = tmp_path / "t.json"
        assert transcribe(str(video), str(cache)) == []
        assert json.loads(cache.read_text())["words"] == []
```

- [ ] **Step 3: Run the test and watch it fail**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_transcribe.py -q
```

Expected: `ModuleNotFoundError: No module named 'yt_shorts.transcribe'`

- [ ] **Step 4: Write the implementation**

`src/yt_shorts/transcribe.py`:

```python
"""Transcribes a clip's commentary into words with timestamps.

Only the finished excerpt is transcribed, not the source stream: for
subtitles the 15-60 seconds already on disk are enough. faster-whisper is
used rather than mlx-whisper because the latter pulls in torch (~2 GB);
measured here, 15-26 seconds of audio take 3-5 seconds on CPU with the
small model.

The result is cached per clip, so re-rendering costs no transcription.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

TIMEOUT_SECONDS = 900
SAMPLE_RATE = 16000  # what Whisper expects


class TranscriptionError(Exception):
    """Audio extraction or transcription failed."""


def _extract_audio(video: str, wav: Path, ffmpeg: str) -> None:
    command = [ffmpeg, "-v", "error", "-y", "-i", video,
               "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), str(wav)]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=TIMEOUT_SECONDS)
    if result.returncode != 0 or not wav.exists():
        raise TranscriptionError(
            "ffmpeg failed extracting audio.\nCommand: " + " ".join(command)
            + "\nOutput: " + result.stderr.strip()
        )


def transcribe(video: str, cache: str, model_name: str = "small",
               ffmpeg: str = "ffmpeg") -> list[dict]:
    """Returns words as {"start", "end", "text"}, using the cache if present."""
    cache_path = Path(cache)
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))["words"]
        except (json.JSONDecodeError, KeyError) as error:
            raise TranscriptionError(
                f"Cached transcript is unreadable: {cache_path}\n{error}\n"
                "Delete the file to transcribe again."
            ) from error

    wav = cache_path.with_suffix(".wav")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _extract_audio(video, wav, ffmpeg)

    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(wav), word_timestamps=True)
        words = [
            {"start": word.start, "end": word.end, "text": word.word}
            for segment in segments for word in (segment.words or [])
        ]
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        raise TranscriptionError(
            f"{type(error).__name__}: {error}"
        ) from error
    finally:
        wav.unlink(missing_ok=True)

    cache_path.write_text(
        json.dumps({"model": model_name, "words": words}, indent=2),
        encoding="utf-8",
    )
    return words
```

- [ ] **Step 5: Run the test and watch it pass**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_transcribe.py -q
```

Expected: `4 passed`. The silence test downloads the model on first run; allow a few minutes once.

- [ ] **Step 6: Check it against a real draft**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/python -c "
from yt_shorts.transcribe import transcribe
from yt_shorts.captions import group_words
words = transcribe('channels/erf/events/community-clips-back-catalogue/drafts/rei-got-sliced.mp4', '/tmp/rei.json')
print(f'{len(words)} words')
for c in group_words(words):
    print(f'{c.start:6.2f}-{c.end:6.2f}  {c.text}')
"
```

Expected: roughly 40 words, ending in "shadow realm".

- [ ] **Step 7: Document the first-run download in the README**

Add to `README.md` under a new "Subtitles" heading:

```markdown
## Subtitles

Subtitles are off by default. Switch them on per channel or per event in
`brand.json`:

```json
"subtitles": { "enabled": true, "max_words": 3, "max_seconds": 1.6, "size": 78, "y": 1290 }
```

The commentary of each clip is transcribed locally with faster-whisper and
cached under the event's `transcripts/`. **The first run downloads the model
(~150 MB)** and therefore takes noticeably longer than later ones. A clip
with no speech simply gets no subtitles; that is reported, not treated as an
error.
```

- [ ] **Step 8: Commit**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
git add src/yt_shorts/transcribe.py tests/test_transcribe.py README.md
git commit -m "Transcribe a clip's commentary into words with timestamps"
```

---

### Task 5: Wire it into the renderer

**Files:**
- Modify: `src/yt_shorts/render.py`
- Modify: `bin/yt-shorts`
- Modify: `src/yt_shorts/profile.py`
- Test: `tests/test_render_subtitles.py`

**Interfaces:**
- Consumes: `transcribe.transcribe`, `captions.group_words`, `subtitle_track.build_track`
- Produces: `render.compose(raw, overlay_png, target, config, ffmpeg="ffmpeg", subtitle_track=None)` — the new argument is keyword-only with a default, so every existing call is unchanged

- [ ] **Step 1: Write the failing test**

`tests/test_render_subtitles.py`:

```python
import subprocess

import pytest

from yt_shorts.overlay import build_overlay
from yt_shorts.profile import load
from yt_shorts.render import compose


@pytest.fixture
def config():
    return load("erf/community-clips-back-catalogue").config


def test_video(path, seconds=2):
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
        test_video(raw)
        build_overlay("HOOK", "FOOTER", config).save(layer)
        compose(str(raw), str(layer), str(target), config)
        assert probe(target, "stream=width,height,sample_aspect_ratio,pix_fmt") == \
            "1080,1920,1:1,yuv420p"


class TestWithTrack:
    def test_track_is_visible_and_contract_holds(self, config, tmp_path):
        from yt_shorts.captions import Caption
        from yt_shorts.subtitle_track import build_track

        raw, layer, target = tmp_path / "r.mp4", tmp_path / "l.png", tmp_path / "o.mp4"
        test_video(raw)
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
```

- [ ] **Step 2: Run the test and watch it fail**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_render_subtitles.py -q
```

Expected: `TypeError: compose() got an unexpected keyword argument 'subtitle_track'`

- [ ] **Step 3: Extend `compose`**

In `src/yt_shorts/render.py`, change the signature and the tail of the filter chain. The signature becomes:

```python
def compose(raw: str, overlay_png: str, target: str, config: dict,
            ffmpeg: str = "ffmpeg", *, subtitle_track: str | None = None) -> None:
```

Replace the two lines building `filter_chain`'s tail and the input list. The chain without a track must stay exactly as it is today; with a track, one more overlay is inserted before `setsar=1`:

```python
    chain = (
        f"[0:v]split=2[fg][bgsrc];"
        f"[bgsrc]scale=320:-2,boxblur=12:2,scale={a['width']}:{a['height']}[bg];"
        f"[fg]scale={a['video_width']}:{a['video_height']}:force_original_aspect_ratio=decrease[vf];"
        f"[bg][vf]overlay=x=(main_w-overlay_w)/2:"
        f"y={a['video_y']}+({a['video_height']}-overlay_h)/2[base];"
        f"[base][1:v]overlay=0:0:format=auto[ov];"
    )
    inputs = ["-i", raw, "-i", overlay_png]
    if subtitle_track:
        inputs += ["-i", subtitle_track]
        chain += "[ov][2:v]overlay=0:0:format=auto:eof_action=pass[sub];[sub]setsar=1[v]"
    else:
        chain += "[ov]setsar=1[v]"
    filter_chain = chain
```

and the command becomes:

```python
    command = [
        ffmpeg, "-v", "error", "-y",
        *inputs,
        "-filter_complex", filter_chain,
        "-map", "[v]", "-map", "0:a?",
        ...
    ]
```

`eof_action=pass` matters: the subtitle track ends before the video does, and without it the output would be cut short at the last caption.

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /Users/jegr/Documents/github/YT-Shorts && PYTHONPATH=src .venv/bin/pytest tests/test_render_subtitles.py -q
```

Expected: `2 passed`

- [ ] **Step 5: Accept `subtitles` in the profile**

`subtitles` is optional, so it must NOT go into any `REQUIRED_*` list. `src/yt_shorts/profile.py` already has the pattern for an optional section in `_validate_logo`. Add alongside it:

```python
def _validate_subtitles(config: dict, path: Path) -> list[str]:
    """The optional 'subtitles' section, if present, must hold sane values.
    Absent means off, which is the default."""
    subtitles = config.get("subtitles")
    if subtitles is None:
        return []
    if not isinstance(subtitles, dict):
        return [f"{path.name}: 'subtitles' must be an object"]

    problems = []
    if "enabled" in subtitles and not isinstance(subtitles["enabled"], bool):
        problems.append(f"{path.name}: 'subtitles.enabled' must be true or false")
    if "max_words" in subtitles:
        value = subtitles["max_words"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"{path.name}: 'subtitles.max_words' must be an integer of at least 1")
    if "max_seconds" in subtitles:
        value = subtitles["max_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            problems.append(f"{path.name}: 'subtitles.max_seconds' must be a positive number")
    for key in ("size", "y"):
        if key in subtitles:
            value = subtitles[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"{path.name}: 'subtitles.{key}' must be a positive integer")
    return problems
```

Then call it exactly where `_validate_logo` is already called, extending the same list of problems, so a subtitle typo is reported together with every other profile defect rather than in a separate run.

Add a test to `tests/test_profile.py` in the style of the existing ones: a throwaway channel whose `brand.json` sets `"subtitles": {"max_words": 0, "max_seconds": -1, "enabled": "yes"}` must raise `ProfileError` naming all three.

- [ ] **Step 6: Let `build_short` accept a subtitle provider**

The CLI calls `build_short`, not `compose`. Handing it a finished draft to overlay subtitles onto would mean encoding an already-encoded video a second time. Instead `build_short` takes a callable that turns the freshly downloaded raw file into a track, so everything still happens in one encode — and `render.py` keeps knowing nothing about Whisper.

In `src/yt_shorts/render.py`, change the signature to:

```python
def build_short(source: Source, hook: str, footer: str, target: str,
                config: dict, work_dir: str, *,
                cleanup_intermediates: bool = True,
                subtitle_provider: Callable[[str], str | None] | None = None) -> str:
```

Add `from typing import Callable` to the imports. Immediately before the `compose(...)` call, insert:

```python
    track = subtitle_provider(str(raw)) if subtitle_provider else None
```

and pass it through:

```python
    compose(str(raw), str(layer), target, config, subtitle_track=track)
```

Keep the existing `cleanup_intermediates` behaviour unchanged; if a track was produced it lives in the same work folder and is cleaned up with the rest.

- [ ] **Step 7: Wire the provider into the render loop**

In `bin/yt-shorts`, add to the imports:

```python
from yt_shorts.captions import group_words                      # noqa: E402
from yt_shorts.subtitle_track import build_track                # noqa: E402
from yt_shorts.transcribe import TranscriptionError, transcribe  # noqa: E402
```

Inside `cmd_render`, within the per-candidate `try` block and directly after `name` is determined, build the provider and pass it to `build_short`:

```python
            provider = None
            if config.get("subtitles", {}).get("enabled"):
                def provider(raw_path: str, _name: str = name) -> str | None:
                    """Turns the downloaded raw clip into a subtitle track.

                    Returns None whenever there is nothing to show. A clip
                    without speech is a normal outcome, not a failure, and
                    must not cost the short its render."""
                    subtitles = config.get("subtitles", {})
                    try:
                        words = transcribe(
                            raw_path, str(dir_ / "transcripts" / f"{_name}.json"))
                    except TranscriptionError as error:
                        print(f"NOTE: {_name}: no subtitles ({error})", file=sys.stderr)
                        return None
                    groups = group_words(
                        words,
                        max_words=subtitles.get("max_words", 3),
                        max_seconds=subtitles.get("max_seconds", 1.6),
                    )
                    if not groups:
                        print(f"NOTE: {_name}: no speech detected, no subtitles",
                              file=sys.stderr)
                        return None
                    return build_track(groups, config,
                                       str(dir_ / "raw" / f"{_name}.subs.mov"),
                                       str(dir_ / "raw"))

            build_short(Source(clip_url=clip["url"]), hook,
                        footer, str(target), config,
                        str(dir_ / "raw"),
                        subtitle_provider=provider)
```

Note what this does and does not do: a transcription failure or a speechless clip yields a short **without** subtitles and a `NOTE:` on stderr — it does not enter the `failed` list and does not change the exit code. Only a genuine render failure does. That keeps the promise that one awkward clip never costs you the others.

- [ ] **Step 8: Verify the whole suite and one real clip**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
PYTHONPATH=src .venv/bin/pytest -q
```

Then switch subtitles on for the event only, render, and look:

```bash
cd /Users/jegr/Documents/github/YT-Shorts
mkdir -p channels/erf/events/community-clips-back-catalogue
cat > channels/erf/events/community-clips-back-catalogue/brand.json <<'EOF'
{ "subtitles": { "enabled": true } }
EOF
bin/yt-shorts render erf/community-clips-back-catalogue
ffmpeg -v error -y -ss 12 -i channels/erf/events/community-clips-back-catalogue/drafts/rei-got-sliced.mp4 -vf "scale=iw*sar:ih" -frames:v 1 /tmp/subtitle-proof.png
```

`/tmp/subtitle-proof.png` should show "SHADOW REALM" or its neighbours in the lower band. Note that this uses the event-level override built in the previous stage — proof that the layering carries a real feature.

- [ ] **Step 9: Commit**

```bash
cd /Users/jegr/Documents/github/YT-Shorts
git add -A
git commit -m "Overlay an optional subtitle track when the profile enables it"
```

---

## Acceptance

1. `PYTHONPATH=src .venv/bin/pytest -q` passes; the count rises from 128.
2. With subtitles off, the six reference overlays stay byte-identical and the filter chain is unchanged.
3. With subtitles on, a rendered short shows caption text in the lower band, and the output is still 1080x1920, `SAR 1:1`, `yuv420p`, h264/aac.
4. A clip with no speech renders without subtitles and says so, rather than failing.
5. You have looked at a real subtitled short and judged whether the grouping reads well at speaking pace.

## Not in scope

Moment detection from full streams. The transcription and caption machinery built here is what it will reuse: `transcribe` already returns plain words with timestamps regardless of source length, and `render.Source` plus `timecode.with_padding` have been waiting for it since stage 1.
