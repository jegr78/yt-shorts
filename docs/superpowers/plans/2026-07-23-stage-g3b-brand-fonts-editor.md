# Stage G3b — Brand & Fonts Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a G3a-created channel renderable from the studio — upload/assign fonts, edit colors and the subtitles toggle, with a live overlay preview.

**Architecture:** A pure `font_admin.py` (uploads/lists/deletes fonts, PIL-validates each so a saved font always loads) and `brand_admin.py` (reads/writes `brand.json` colors/fonts/subtitles, validation mirroring `profile._validate_brand`). Five channel-scoped routes (GET/PUT brand, POST/DELETE font via raw body, POST brand/preview → a `build_overlay` PNG). The `/{channel}` screen becomes a tabbed `ChannelScreen` (Events + a Brand editor with a live preview).

**Tech Stack:** Python 3 stdlib + PIL (already a dep); FastAPI (studio only); React + Vite + Mantine; Vitest + Playwright.

## Global Constraints

- `PYTHONPATH=src` mandatory for pytest. Full suite green at the start of this plan.
- **Security:** the font filename and the `{channel}` segment are validated as safe single path segments (`pathnames.validate_segment`) BEFORE any filesystem touch; a font must end `.ttf`/`.otf` (case-insensitive) AND load via `PIL.ImageFont.truetype` (≤ 10 MB) or it is rejected. A brand's `fonts.hook`/`small` must be `fonts/<safe-segment>` whose file exists under the channel's `fonts/`.
- **No new dependency:** font upload reads the raw request body (`await request.body()`), not multipart.
- **Output dims are NOT editable** — `update_brand` never takes `output` from the patch. Channel-level brand only; no logo/`layout.py` editing.
- `font_admin.py`/`brand_admin.py` import no FastAPI (PIL is fine). `create_app()` still pulls no google at module scope.
- Built `static/` stays committed; English only; imperative commits.

---

## Task 1: `font_admin.py` — upload/list/delete fonts (pure + PIL)

**Files:** Create `src/yt_shorts/font_admin.py`, `tests/test_font_admin.py`.

**Interfaces:**
- Consumes: `pathnames.validate_segment`, `PIL.ImageFont`.
- Produces: `FontAdminError(kind)`, `FONT_EXTENSIONS`, `MAX_FONT_BYTES`, `list_fonts`, `save_font`, `delete_font`.

- [ ] **Step 1: Write the failing test** — `tests/test_font_admin.py`:

```python
from pathlib import Path

import pytest

from yt_shorts import font_admin
from yt_shorts.font_admin import FontAdminError

REAL_TTF = (Path(__file__).parent / "fixtures" / "channels" / "erf"
            / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()


def _channel(tmp_path):
    fonts = tmp_path / "channels" / "demo" / "fonts"
    fonts.mkdir(parents=True)
    return tmp_path / "channels"


class TestSave:
    def test_saves_a_valid_font_and_lists_it(self, tmp_path):
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont-Bold.ttf", REAL_TTF)
        assert (channels / "demo" / "fonts" / "MyFont-Bold.ttf").is_file()
        assert font_admin.list_fonts(channels, "demo") == ["MyFont-Bold.ttf"]

    def test_rejects_a_non_font_extension(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "evil.exe", REAL_TTF)
        assert e.value.kind == "bad_type"

    def test_rejects_non_font_bytes(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "broken.ttf", b"not a font")
        assert e.value.kind == "invalid"

    def test_rejects_oversize(self, tmp_path):
        channels = _channel(tmp_path)
        big = b"\x00" * (font_admin.MAX_FONT_BYTES + 1)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "big.ttf", big)
        assert e.value.kind == "too_big"

    def test_rejects_a_traversal_filename(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "demo", "../escape.ttf", REAL_TTF)
        assert e.value.kind == "bad_name"
        assert not (channels.parent / "escape.ttf").exists()

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(FontAdminError) as e:
            font_admin.save_font(channels, "ghost", "MyFont.ttf", REAL_TTF)
        assert e.value.kind == "not_found"


class TestDelete:
    def test_deletes_an_unreferenced_font(self, tmp_path):
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert font_admin.list_fonts(channels, "demo") == []

    def test_refuses_a_font_assigned_in_brand_json(self, tmp_path):
        import json
        channels = _channel(tmp_path)
        font_admin.save_font(channels, "demo", "MyFont.ttf", REAL_TTF)
        (channels / "demo" / "brand.json").write_text(
            json.dumps({"fonts": {"hook": "fonts/MyFont.ttf", "small": "fonts/MyFont.ttf"}}))
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "MyFont.ttf")
        assert e.value.kind == "in_use"
        assert (channels / "demo" / "fonts" / "MyFont.ttf").is_file()

    def test_unknown_font_is_not_found(self, tmp_path):
        channels = _channel(tmp_path)
        with pytest.raises(FontAdminError) as e:
            font_admin.delete_font(channels, "demo", "ghost.ttf")
        assert e.value.kind == "not_found"
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `src/yt_shorts/font_admin.py`:

```python
"""Upload, list and delete a channel's font files (stage G3b). Pure filesystem
ops plus a PIL load-check that a font is renderable - no FastAPI. A saved font
is one build_overlay can use, because save_font rejects anything ImageFont
cannot load."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import ImageFont

from . import pathnames

FONT_EXTENSIONS = (".ttf", ".otf")
MAX_FONT_BYTES = 10 * 1024 * 1024   # 10 MB


class FontAdminError(Exception):
    """kind: "bad_name" | "bad_type" | "too_big" | "invalid" | "not_found" | "in_use".
    Maps to HTTP: bad_*/too_big/invalid -> 400, not_found -> 404, in_use -> 409."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _validate_segment(value: str, what: str) -> None:
    try:
        pathnames.validate_segment(value, what=what)
    except ValueError as error:
        raise FontAdminError(str(error), kind="bad_name") from error


def _fonts_dir(channels_dir, channel: str) -> Path:
    _validate_segment(channel, "channel name")
    base = Path(channels_dir) / channel
    if not base.is_dir():
        raise FontAdminError(f"unknown channel: {channel!r}", kind="not_found")
    return base / "fonts"


def list_fonts(channels_dir, channel: str) -> list[str]:
    fonts = _fonts_dir(channels_dir, channel)
    if not fonts.is_dir():
        return []
    return sorted(p.name for p in fonts.iterdir()
                  if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS)


def save_font(channels_dir, channel: str, filename: str, data: bytes) -> None:
    fonts = _fonts_dir(channels_dir, channel)
    _validate_segment(filename, "font filename")
    if not filename.lower().endswith(FONT_EXTENSIONS):
        raise FontAdminError(
            f"a font must be a {' or '.join(FONT_EXTENSIONS)} file", kind="bad_type")
    if len(data) > MAX_FONT_BYTES:
        raise FontAdminError(
            f"font is larger than {MAX_FONT_BYTES // (1024 * 1024)} MB", kind="too_big")
    try:
        ImageFont.truetype(io.BytesIO(data))
    except Exception as error:   # noqa: BLE001 - any PIL failure means "not a usable font"
        raise FontAdminError(f"not a usable font file: {error}", kind="invalid") from error
    fonts.mkdir(parents=True, exist_ok=True)
    (fonts / filename).write_bytes(data)


def delete_font(channels_dir, channel: str, filename: str) -> None:
    fonts = _fonts_dir(channels_dir, channel)
    _validate_segment(filename, "font filename")
    target = fonts / filename
    if not target.is_file():
        raise FontAdminError(f"unknown font: {filename!r}", kind="not_found")
    brand_path = fonts.parent / "brand.json"
    if brand_path.exists():
        try:
            brand = json.loads(brand_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            brand = {}
        refs = brand.get("fonts", {}) if isinstance(brand, dict) else {}
        if f"fonts/{filename}" in {refs.get("hook"), refs.get("small")}:
            raise FontAdminError(
                f"font {filename!r} is assigned in brand.json; reassign it first",
                kind="in_use")
    target.unlink()
```

- [ ] **Step 4: Run tests + no-FastAPI check** — `PYTHONPATH=src .venv/bin/pytest tests/test_font_admin.py -q` → PASS; `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.font_admin; assert 'fastapi' not in sys.modules; print('clean')"`.

- [ ] **Step 5: Commit** — `git add src/yt_shorts/font_admin.py tests/test_font_admin.py && git commit -m "Add font_admin: upload, list and delete a channel's fonts"`.

---

## Task 2: `brand_admin.py` — read/update brand.json (pure + PIL colors)

**Files:** Create `src/yt_shorts/brand_admin.py`, `tests/test_brand_admin.py`.

**Interfaces:**
- Consumes: `pathnames.validate_segment`, `PIL.ImageColor`.
- Produces: `BrandAdminError(kind)`, `read_brand(channels_dir, channel) -> dict`, `update_brand(channels_dir, channel, patch: dict) -> None`.

- [ ] **Step 1: Write the failing test** — `tests/test_brand_admin.py`:

```python
import json
from pathlib import Path

import pytest

from yt_shorts import brand_admin, channel_admin
from yt_shorts.brand_admin import BrandAdminError

REAL_TTF = (Path(__file__).parent / "fixtures" / "channels" / "erf"
            / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()

FIELDS = {"id": "UCabc", "channel_url": "https://www.youtube.com/channel/UCabc",
          "handle": "@demo", "display_name": "Demo League", "language": "en",
          "footer": "DEMO | @demo"}


def _channel_with_font(tmp_path):
    channels = tmp_path / "channels"
    channels.mkdir()
    channel_admin.create_channel(channels, "demo", FIELDS)   # scaffolds brand.json + fonts/
    (channels / "demo" / "fonts" / "MyFont.ttf").write_bytes(REAL_TTF)
    return channels


VALID_FONTS = {"hook": "fonts/MyFont.ttf", "small": "fonts/MyFont.ttf"}


class TestRead:
    def test_returns_the_channels_brand(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        brand = brand_admin.read_brand(channels, "demo")
        assert "colors" in brand and "output" in brand

    def test_unknown_channel_is_not_found(self, tmp_path):
        channels = tmp_path / "channels"
        channels.mkdir()
        with pytest.raises(BrandAdminError) as e:
            brand_admin.read_brand(channels, "ghost")
        assert e.value.kind == "not_found"


class TestUpdate:
    def test_applies_colors_fonts_subtitles_and_keeps_output(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        before = brand_admin.read_brand(channels, "demo")["output"]
        brand_admin.update_brand(channels, "demo", {
            "colors": {"text": "#000000", "base": "#FFFFFF", "accent": "#FF0000", "edge": "#00FF00"},
            "fonts": VALID_FONTS,
            "subtitles": {"enabled": True}})
        brand = brand_admin.read_brand(channels, "demo")
        assert brand["colors"]["text"] == "#000000"
        assert brand["fonts"] == VALID_FONTS
        assert brand["subtitles"]["enabled"] is True
        assert brand["output"] == before          # output untouched

    def test_rejects_a_bad_color(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "colors": {"text": "nope", "base": "#000", "accent": "#000", "edge": "#000"},
                "fonts": VALID_FONTS})
        assert e.value.kind == "bad_color"

    def test_rejects_a_font_not_present(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": {"hook": "fonts/absent.ttf", "small": "fonts/MyFont.ttf"}})
        assert e.value.kind == "bad_font"

    def test_rejects_a_font_ref_that_escapes(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": {"hook": "fonts/../../evil.ttf", "small": "fonts/MyFont.ttf"}})
        assert e.value.kind == "bad_font"

    def test_rejects_non_bool_subtitles(self, tmp_path):
        channels = _channel_with_font(tmp_path)
        with pytest.raises(BrandAdminError) as e:
            brand_admin.update_brand(channels, "demo", {
                "fonts": VALID_FONTS, "subtitles": {"enabled": "yes"}})
        assert e.value.kind == "bad_subtitles"

    def test_an_accepted_brand_then_loads_via_profile(self, tmp_path, monkeypatch):
        from yt_shorts import profile as profile_module
        channels = _channel_with_font(tmp_path)
        (channels / "demo" / "events" / "round-1").mkdir(parents=True)
        brand_admin.update_brand(channels, "demo", {
            "colors": {"text": "#FFFFFF", "base": "#101010", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": VALID_FONTS})
        monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
        loaded = profile_module.load("demo/round-1")   # must not raise
        assert loaded.config["colors"]["text"] == "#FFFFFF"
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `src/yt_shorts/brand_admin.py`:

```python
"""Read and update a channel's brand.json colors/fonts/subtitles (stage G3b).
Pure, no FastAPI. Validation mirrors profile._validate_brand so a brand this
accepts is one profile.load accepts. Output dimensions are never changed here."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import ImageColor

from . import pathnames

REQUIRED_COLOR_KEYS = ["text", "base", "accent", "edge"]
REQUIRED_FONT_KEYS = ["hook", "small"]


class BrandAdminError(Exception):
    """kind: "bad_name" | "not_found" | "bad_color" | "bad_font" | "bad_subtitles".
    Maps to HTTP: bad_*/-> 400, not_found -> 404."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _channel_dir(channels_dir, channel: str) -> Path:
    try:
        pathnames.validate_segment(channel, what="channel name")
    except ValueError as error:
        raise BrandAdminError(str(error), kind="bad_name") from error
    return Path(channels_dir) / channel


def _load(path: Path, channel: str) -> dict:
    if not path.exists():
        raise BrandAdminError(f"unknown channel or brand: {channel!r}", kind="not_found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrandAdminError(
            f"brand.json for {channel!r} is unreadable: {error}", kind="not_found") from error


def read_brand(channels_dir, channel: str) -> dict:
    base = _channel_dir(channels_dir, channel)
    return _load(base / "brand.json", channel)


def update_brand(channels_dir, channel: str, patch: dict) -> None:
    base = _channel_dir(channels_dir, channel)
    path = base / "brand.json"
    brand = _load(path, channel)
    for key in ("colors", "fonts", "subtitles"):
        if key in patch:
            brand[key] = patch[key]
    _validate(brand, base)
    path.write_text(json.dumps(brand, indent=2) + "\n", encoding="utf-8")


def _validate(brand: dict, channel_dir: Path) -> None:
    colors = brand.get("colors")
    if not isinstance(colors, dict):
        raise BrandAdminError("the 'colors' section is required", kind="bad_color")
    for key in REQUIRED_COLOR_KEYS:
        value = colors.get(key)
        if not value:
            raise BrandAdminError(f"color {key!r} is required", kind="bad_color")
        try:
            ImageColor.getrgb(value)
        except ValueError as error:
            raise BrandAdminError(
                f"color {key!r} is not a valid color: {value!r}", kind="bad_color") from error

    fonts = brand.get("fonts")
    if not isinstance(fonts, dict):
        raise BrandAdminError("the 'fonts' section is required", kind="bad_font")
    for key in REQUIRED_FONT_KEYS:
        ref = fonts.get(key)
        if not ref or not isinstance(ref, str) or not ref.startswith("fonts/"):
            raise BrandAdminError(
                f"font {key!r} must be 'fonts/<file>'", kind="bad_font")
        name = ref[len("fonts/"):]
        try:
            pathnames.validate_segment(name, what="font filename")
        except ValueError as error:
            raise BrandAdminError(f"font {key!r} name is invalid: {name!r}", kind="bad_font") from error
        if not (channel_dir / "fonts" / name).is_file():
            raise BrandAdminError(f"font {key!r} file not found: {ref!r}", kind="bad_font")

    subtitles = brand.get("subtitles")
    if subtitles is not None:
        if not isinstance(subtitles, dict):
            raise BrandAdminError("'subtitles' must be an object", kind="bad_subtitles")
        if "enabled" in subtitles and not isinstance(subtitles["enabled"], bool):
            raise BrandAdminError("'subtitles.enabled' must be true or false", kind="bad_subtitles")
```

- [ ] **Step 4: Run tests + no-FastAPI check** — `PYTHONPATH=src .venv/bin/pytest tests/test_brand_admin.py -q` → PASS; `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.brand_admin; assert 'fastapi' not in sys.modules; print('clean')"`.

- [ ] **Step 5: Commit** — `git add src/yt_shorts/brand_admin.py tests/test_brand_admin.py && git commit -m "Add brand_admin: read and validate-update a channel's brand.json"`.

---

## Task 3: Studio API — brand & fonts routes + live preview

**Files:** Modify `src/yt_shorts/studio/api.py`; test in `tests/test_studio_api.py` (a `TestBrandFonts` class).

**Interfaces:**
- Consumes: `font_admin.*`, `brand_admin.*`, `overlay.build_overlay`, the existing `_load_channel` helper (reads `channel.json`, 404 if missing), the `channels_dir`/`CH` in scope.
- Produces routes:
  - `GET  /api/channels/{channel}/brand` → `{"brand": <dict>, "fonts": [names]}`
  - `PUT  /api/channels/{channel}/brand` body `{colors?, fonts?, subtitles?}` → `{"brand": <dict>}`
  - `POST /api/channels/{channel}/fonts/{filename}` (raw body bytes) → 201 `{"fonts": [names]}`
  - `DELETE /api/channels/{channel}/fonts/{filename}` → `{"fonts": [names]}`
  - `POST /api/channels/{channel}/brand/preview` body `{colors?, fonts?, subtitles?}` → `image/png`

- [ ] **Step 1: Write the failing tests** — add to `tests/test_studio_api.py` (`profile_module`, `CHANNEL`, `json`, `Path` imported; `client`/`studio_profile` give the tmp `CHANNELS_DIR` with the `erf` channel, which has real fonts under `fonts/`):

```python
class TestBrandFonts:
    def _erf_font_bytes(self):
        return (FIXTURE_CHANNELS / "erf" / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()

    def test_get_brand_returns_brand_and_fonts(self, client, studio_profile):
        body = client.get(f"/api/channels/{CHANNEL}/brand").json()
        assert "colors" in body["brand"]
        assert "BarlowCondensed-Bold.ttf" in body["fonts"]

    def test_upload_font_saves_and_lists_it(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        r = client.post(f"/api/channels/{CHANNEL}/fonts/Uploaded.ttf",
                        content=self._erf_font_bytes())
        assert r.status_code == 201
        assert "Uploaded.ttf" in r.json()["fonts"]
        assert (channels / "erf" / "fonts" / "Uploaded.ttf").is_file()

    def test_upload_rejects_non_font_bytes_400(self, client):
        r = client.post(f"/api/channels/{CHANNEL}/fonts/broken.ttf", content=b"nope")
        assert r.status_code == 400

    def test_upload_traversal_filename_400_not_escaped(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        r = client.post(f"/api/channels/{CHANNEL}/fonts/%2e%2e%2fescape.ttf",
                        content=self._erf_font_bytes())
        assert r.status_code in (400, 404)
        assert not (channels / "escape.ttf").exists()

    def test_put_brand_persists_valid_changes(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={
            "colors": {"text": "#000000", "base": "#FFFFFF", "accent": "#FF0000", "edge": "#00FF00"},
            "fonts": {"hook": "fonts/BarlowCondensed-Bold.ttf", "small": "fonts/BarlowCondensed-Bold.ttf"}})
        assert r.status_code == 200
        assert json.loads((channels / "erf" / "brand.json").read_text())["colors"]["text"] == "#000000"

    def test_put_brand_bad_color_400(self, client):
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={
            "colors": {"text": "nope", "base": "#000", "accent": "#000", "edge": "#000"},
            "fonts": {"hook": "fonts/BarlowCondensed-Bold.ttf", "small": "fonts/BarlowCondensed-Bold.ttf"}})
        assert r.status_code == 400

    def test_delete_font_refuses_when_assigned_409(self, client, studio_profile):
        # The erf brand.json already assigns BarlowCondensed-Bold.ttf.
        r = client.delete(f"/api/channels/{CHANNEL}/fonts/BarlowCondensed-Bold.ttf")
        assert r.status_code == 409

    def test_preview_renders_a_png_with_an_assigned_font(self, client, studio_profile):
        r = client.post(f"/api/channels/{CHANNEL}/brand/preview", json={
            "colors": {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": {"hook": "fonts/BarlowCondensed-Bold.ttf", "small": "fonts/BarlowCondensed-Bold.ttf"}})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 100

    def test_preview_missing_font_409(self, client):
        r = client.post(f"/api/channels/{CHANNEL}/brand/preview", json={
            "colors": {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": {"hook": "fonts/absent.ttf", "small": "fonts/absent.ttf"}})
        assert r.status_code == 409
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement in `studio/api.py`.** Add imports with the others:

```python
from .. import brand_admin
from .. import font_admin
```

Add `Request` to the FastAPI import (`from fastapi import FastAPI, HTTPException, Query, Request`). Add a body near the other `BaseModel`s:

```python
class BrandPatchBody(BaseModel):
    """A partial brand.json edit (live preview or save). Only colors/fonts/
    subtitles are ever applied; output is never editable here."""
    colors: dict | None = None
    fonts: dict | None = None
    subtitles: dict | None = None
```

Inside `create_app`, near `get_channels`/the channel routes (with `channels_dir`, `CH`, and `_load_channel` in scope), add the mappers and five routes:

```python
    def _font_status(error: font_admin.FontAdminError) -> int:
        return {"bad_name": 400, "bad_type": 400, "too_big": 400, "invalid": 400,
                "not_found": 404, "in_use": 409}.get(error.kind, 400)

    def _brand_status(error: brand_admin.BrandAdminError) -> int:
        return {"bad_name": 400, "not_found": 404, "bad_color": 400,
                "bad_font": 400, "bad_subtitles": 400}.get(error.kind, 400)

    @app.get(CH + "/brand")
    def get_brand(channel: str) -> dict:
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error))
        return {"brand": brand, "fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.put(CH + "/brand")
    def put_brand(channel: str, body: BrandPatchBody) -> dict:
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            brand_admin.update_brand(channels_dir, channel, patch)
            return {"brand": brand_admin.read_brand(channels_dir, channel)}
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error))

    @app.post(CH + "/fonts/{filename}", status_code=201)
    async def upload_font(channel: str, filename: str, request: Request) -> dict:
        data = await request.body()
        try:
            font_admin.save_font(channels_dir, channel, filename, data)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error))
        return {"fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.delete(CH + "/fonts/{filename}")
    def delete_font(channel: str, filename: str) -> dict:
        try:
            font_admin.delete_font(channels_dir, channel, filename)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error))
        return {"fonts": font_admin.list_fonts(channels_dir, channel)}

    @app.post(CH + "/brand/preview")
    def brand_preview(channel: str, body: BrandPatchBody):
        import io as _io

        from ..overlay import build_overlay
        channel_json = _load_channel(channel)
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error))
        for key in ("colors", "fonts", "subtitles"):
            value = getattr(body, key)
            if value is not None:
                brand[key] = value
        base = channels_dir / channel
        fonts = brand.get("fonts") or {}
        config = {
            "colors": brand.get("colors") or {},
            "fonts": {role: str(base / ref) for role, ref in fonts.items()},
            "output": brand.get("output") or {},
        }
        hook = channel_json.get("display_name", channel)
        footer = channel_json.get("footer", "")
        try:
            image = build_overlay(hook, footer, config)
        except Exception as error:   # noqa: BLE001 - a missing/invalid font or bad geometry
            raise HTTPException(
                status_code=409,
                detail=f"cannot render preview (check the assigned fonts): {error}")
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")
```

`GET/PUT CH+"/brand"`, `CH+"/brand/preview"`, and `CH+"/fonts/{filename}"` are all new distinct paths (the existing channel routes are bare `CH`, `CH+"/auth"`, `CH+"/events"`, `CH+"/rename"`); `{filename}`/`{channel}` never match a slash, so no route collision, and all register before the SPA fallback.

- [ ] **Step 4: Run the studio tests + no-google check** — `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q` → PASS; `PYTHONPATH=src .venv/bin/python -c "import sys, yt_shorts.studio.api as a; a.create_app; assert 'googleapiclient' not in sys.modules; print('clean')"`.

- [ ] **Step 5: Commit** — `git add src/yt_shorts/studio/api.py tests/test_studio_api.py && git commit -m "Studio API: brand read/update, font upload/delete, live overlay preview"`.

---

## Task 4: Frontend — the Brand tab + editor + live preview

**Files:**
- Create: `src/yt_shorts/studio/web/src/brand.ts` + `src/brand.test.ts`, `src/components/BrandEditor.tsx`.
- Modify: `src/api.ts` (five calls), `src/components/EventsScreen.tsx` → a tabbed `src/components/ChannelScreen.tsx` (Events + Brand), `src/Root.tsx` (render `ChannelScreen` for the `events` route), `tests/test_studio_e2e.py`, rebuild `../static/`.

Dispatched to a focused frontend agent with the Task-3 API contract.

**API contract (Task 3, already built):**
- `GET /api/channels/{channel}/brand` → `{brand: <brand.json dict>, fonts: string[]}`.
- `PUT /api/channels/{channel}/brand` body `{colors?, fonts?, subtitles?}` → `{brand}`; 400 (bad_color/font/subtitles), 404.
- `POST /api/channels/{channel}/fonts/{filename}` — **raw body = the font bytes** (set `Content-Type: application/octet-stream`, body = the file's ArrayBuffer) → 201 `{fonts: string[]}`; 400 (bad name/type/too_big/invalid), 404.
- `DELETE /api/channels/{channel}/fonts/{filename}` → `{fonts}`; 404, 409 (in_use).
- `POST /api/channels/{channel}/brand/preview` body `{colors?, fonts?, subtitles?}` → an `image/png` blob; 409 if a selected font is missing.
- Errors are FastAPI `{detail}` — surface via `ApiError.message`.

**What to build:**
1. `src/brand.ts` — pure, not exported from a component, Vitest-tested:
   - `isValidHexColor(value: string): boolean` — accepts `#RGB`/`#RRGGBB`.
   - `brandReadyToSave(form: {colors: Record<string,string>, fonts: {hook?: string, small?: string}}): boolean` — the four colors (`text`/`base`/`accent`/`edge`) all valid hex AND both `hook` and `small` assigned to a non-empty `fonts/<name>`.
   - `fontFilename(name: string): string` — take an uploaded file's name, keep its lowercased extension if it is `.ttf`/`.otf`, replace every run of chars outside `[A-Za-z0-9._-]` in the stem with `-`, strip a leading `.`; throw/return `''` if it has no font extension. (So the name sent to the backend passes `pathnames.validate_segment`.)
   `src/brand.test.ts` covers each (accept/reject hex table; ready-to-save true only when complete; `fontFilename('My Font.ttf') === 'My-Font.ttf'`, and a non-font name → `''`).
2. `src/api.ts` — add (reusing `asJson`, `ApiError`, and the `previewBlobUrl` helper that `fetchPreview` uses for object URLs):
```ts
export interface BrandResponse { brand: Record<string, unknown>; fonts: string[] }
export interface BrandPatch {
  colors?: Record<string, string>
  fonts?: { hook: string; small: string }
  subtitles?: { enabled: boolean }
}
export function getBrand(channel: string): Promise<BrandResponse> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/brand`).then(asJson<BrandResponse>)
}
export function saveBrand(channel: string, patch: BrandPatch): Promise<{ brand: Record<string, unknown> }> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/brand`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  }).then(asJson)
}
export function uploadFont(channel: string, filename: string, bytes: ArrayBuffer): Promise<{ fonts: string[] }> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: bytes,
  }).then(asJson)
}
export function deleteFont(channel: string, filename: string): Promise<{ fonts: string[] }> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/fonts/${encodeURIComponent(filename)}`, {
    method: 'DELETE',
  }).then(asJson)
}
export function brandPreview(channel: string, patch: BrandPatch): Promise<string> {
  return fetch(`/api/channels/${encodeURIComponent(channel)}/brand/preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  }).then(previewBlobUrl)
}
```
   (If `previewBlobUrl` is a local helper in api.ts, reuse it; it turns a 2xx blob response into an object URL and throws `ApiError` on non-2xx — mirror `fetchPreview` exactly.)
3. `src/components/ChannelScreen.tsx` — rename/replace `EventsScreen`: the same component, wrapped in Mantine `Tabs` with `defaultValue="events"`. The **Events** tab panel holds the ENTIRE current EventsScreen body (list + New/Rename/Delete dialogs) verbatim. The **Brand** tab panel renders `<BrandEditor channel={channel} />`. Keep the `NavScreen` chrome/breadcrumbs. Update `Root.tsx` to render `<ChannelScreen key={channel} channel={channel} />` for the `events` route.
4. `src/components/BrandEditor.tsx` — on mount `getBrand(channel)`; two columns:
   - Left: an upload control (`<FileButton accept=".ttf,.otf">` or `<input type=file>`; on select read `await file.arrayBuffer()`, `uploadFont(channel, fontFilename(file.name), bytes)`, refresh the fonts list); the fonts list each with a delete button (surface a 409 `in_use` message); two `Select`s for `hook`/`small` (options = the fonts as `fonts/<name>`); four Mantine `ColorInput`s (`text`/`base`/`accent`/`edge`); a `Switch` for `subtitles.enabled`; a **Save** button (`saveBrand`), disabled unless `brandReadyToSave(form)` and something changed.
   - Right: an `<img>` whose `src` is the object URL from `brandPreview(channel, form)`, re-fetched debounced (~300 ms) on any form change; revoke the previous object URL on replace/unmount; a 409 shows "upload and assign a font to see the preview" instead of a broken image.
   - Loading/empty/error states; server `detail` surfaced inline.
5. Match the existing dark Mantine styling. Rebuild `../static/` (`npm run build`).

**E2E (`tests/test_studio_e2e.py`, real Chromium):** add a test that opens a channel (the `erf` fixture already has a font, OR create a fresh channel first), goes to the **Brand** tab, uploads a font (bytes from `tests/fixtures/channels/erf/fonts/BarlowCondensed-Bold.ttf` via the file input — Playwright `set_input_files`), assigns hook+small, changes a color, waits for the preview `<img>` to have a `src`, saves; then asserts the channel's `brand.json` on disk reflects the change. Reuse the file's server/seeding/on-disk fixtures; read it first for patterns (how it sets file inputs, finds the channels dir).

**Verify:** `npm test -- --run` (Vitest green incl. `brand.test.ts`); `npm run build` (typecheck clean, `../static/` rebuilt); `PYTHONPATH=src .venv/bin/pytest tests/test_studio_e2e.py -q` (green, real Chromium). Drive the real page and confirm the Brand tab: upload → assign → preview updates → save.

- [ ] Build to the above; rebuild+commit `static/`; commit — `Add the channel Brand & fonts editor to the studio`.

---

## Task 5: Documentation

**Files:** `CLAUDE.md`, `README.md`.

- [ ] **Step 1: CLAUDE.md** — extend the studio write-boundary note: the studio now also writes a channel's `brand.json` (colors/fonts/subtitles via `brand_admin`, validation mirroring `profile._validate_brand`, output never editable) and adds/removes files under a channel's `fonts/` (`font_admin`: a `.ttf`/`.otf` that PIL can load, ≤ 10 MB, a safe-segment filename; a font assigned in `brand.json` cannot be deleted). The `/api/channels/{channel}/brand/preview` route renders `overlay.build_overlay` on the edited (unsaved) brand — a read, like the clip preview. Still no editing of event content; still `auth/` only via connect.

- [ ] **Step 2: README.md** — in the Studio section, note a channel now has a **Brand** tab: upload `.ttf`/`.otf` fonts, assign hook/small, edit colors and the subtitles toggle, with a live preview; a channel becomes renderable once a font is uploaded and assigned. Output dimensions stay at the portrait default.

- [ ] **Step 3: Commit** — `Document the studio brand and fonts editor`.

---

## Verification for the branch

- Full `pytest` suite green, E2E included; `npm test` green; `static/` rebuilt.
- Upload/assign a font, edit colors, save, and see a live preview end-to-end (E2E); each refusal (bad type/bytes/size/name 400, in_use 409, bad color/font 400, missing-font preview 409) exercised.
- A traversal `{channel}`/`{filename}` is rejected before any filesystem touch — nothing escapes `channels/`/`fonts/` (tested at the admin and HTTP levels).
- After `update_brand` with a real uploaded font, `profile.load` succeeds (round-trip test) — a saved brand is a loadable brand.
- `font_admin.py`/`brand_admin.py` import no FastAPI; `create_app()` pulls no google at module scope.

## Self-review notes

- The font ref stored in `brand.json` is validated as `fonts/<safe-segment>` (not just "exists"), so a `fonts/../..`-style ref cannot smuggle a path traversal into a later `profile.load`/render — the same segment discipline as the filename and slug.
- `build_overlay` reads `config.get("decorate")`/`config.get("logo")`, so the minimal preview config (colors/fonts/output only) does not KeyError; the preview catches any render failure as a 409 rather than a 500.
- The raw-body font upload avoids adding `python-multipart`; the frontend sends the file's ArrayBuffer with `Content-Type: application/octet-stream`.
- Deferred with reason: output-dimension editing + caption geometry, per-event brand overrides, logo/`layout.py` editing, a subtitle-caption live preview (`build_overlay` draws no captions) — all out of G3b scope.
