# Event-level Brand Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator edit an event's brand overrides (colors, fonts, logo, output geometry, subtitles) from a Drawer in the event editor — a partial override deep-merged over the channel brand.

**Architecture:** A new pure `event_brand_admin.py` stores only the overridden sections into the event's `brand.json` and validates the *merged* (channel + override) result with the existing `profile` validators; event fonts reuse a base-dir-generalized `font_admin`. Thin studio routes under `…/events/{event}/…` expose brand read/write, font CRUD and a merged preview. The frontend adds a right-hand Drawer in the editor with a section-by-section "inherit ⇄ override" editor that reuses the channel editor's widgets and `brand.ts` helpers.

**Tech Stack:** Python 3 / FastAPI (backend), React + Mantine + Vite + TypeScript (frontend), pytest + FastAPI TestClient + Playwright-in-pytest (backend/E2E), Vitest (frontend units).

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation; tests: `PYTHONPATH=src .venv/bin/pytest -q`.
- Pure admin modules (`event_brand_admin.py`, the generalized `font_admin.py`) import no FastAPI and take injected paths — same style as `brand_admin.py`/`font_admin.py`.
- Every path segment that becomes a directory/filename (`channel`, `event`, font filename) and every stored `fonts/<name>` ref goes through `pathnames.validate_segment` (`^[A-Za-z0-9][A-Za-z0-9._-]*\Z`) BEFORE any filesystem touch.
- **Validate the MERGED brand, never the partial override alone.** Reuse `profile._validate_brand/_validate_logo/_validate_subtitles` on `merge.deep_merge(channel_brand, override)`, resolving fonts/logo **event-first** (event dir, then channel dir) exactly as `profile.load` does.
- `upload.mode` is NEVER event-overridable (excluded from the event brand patch whitelist).
- The override brand.json holds only overridden sections; overriding writes the whole section (seeded from effective); a fully-inherited event deletes its `brand.json`.
- Overridable sections whitelist: `("colors", "fonts", "logo", "output", "subtitles")`.
- Frontend: pure logic lives in non-component `.ts` modules (Vite fast-refresh boundary); run `npm test` before committing a frontend change; `npm run build` regenerates `src/yt_shorts/studio/static/` which MUST be committed. The Drawer must own its scroll and be reachable at a short viewport (mandatory visual-acceptance criterion).
- The mechanical linter must stay green: `python3 tools/lint.py`. No bare `except: pass` without a comment.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work on the `master` branch.

---

### Task 1: `event_brand_admin.py` — read + update the event override

**Files:**
- Create: `src/yt_shorts/event_brand_admin.py`
- Test: `tests/test_event_brand_admin.py`

**Interfaces:**
- Consumes: `merge.deep_merge`, `pathnames.validate_segment`, `brand_admin` (constants + `_validate` pieces), `profile._resolve_logo`/`_validate_brand`/`_validate_logo`/`_validate_subtitles`/`_resolve_relative`.
- Produces:
  - `OVERRIDE_SECTIONS = ("colors", "fonts", "logo", "output", "subtitles")`
  - `class EventBrandError(Exception)` with `.kind` (`"bad_name" | "not_found" | "bad_field" | "bad_color" | "bad_font" | "bad_subtitles" | "bad_brand"`).
  - `read_event_brand(channels_dir, channel, event) -> dict` → `{"override": <event brand.json or {}>, "channel": <channel brand>, "effective": deep_merge(channel, override)}`.
  - `update_event_brand(channels_dir, channel, event, patch: dict) -> None`.
  - `resolve_event_font_ref(event_dir, channel_dir, ref, *, what="font") -> Path` (event-first font resolver, safe-segment).

**Design notes for the implementer:**
- Read `src/yt_shorts/brand_admin.py` first — mirror `_channel_dir`/`_load`/`resolve_font_ref`/`_validate` but event-scoped and override-only.
- The test's hand-built channel brand MUST be one `profile._validate_brand/_validate_subtitles` accepts on its own (the merge inherits it). If the test fails on `bad_subtitles`/`bad_brand` for the base, read `tests/fixtures/channels/erf/brand.json` and mirror its exact section shapes (subtitles block included if the validators require one). The point of the tests is the OVERRIDE behavior, not re-deriving a valid base brand.
- `event_dir = channels_dir/channel/events/event`; `channel_dir = channels_dir/channel`. Validate BOTH `channel` and `event` segments before touching disk. `not_found` if the event dir or the channel `brand.json` is missing.
- `read_event_brand`: load channel brand (required; `not_found` if absent), load event `brand.json` if present (else `{}`), return the three views.
- `update_event_brand`:
  1. Reject any key not in `OVERRIDE_SECTIONS` (esp. `"upload"`) → `bad_field`.
  2. `merged = deep_merge(channel_brand, patch)`.
  3. Validate `merged` (see `_validate_merged` below).
  4. If `patch` is empty → delete the event `brand.json` if it exists. Else write `patch` (indent=2 + newline) to `<event>/brand.json`.
- `_validate_merged(merged, event_dir, channel_dir)`: mirror `brand_admin._validate` but resolve fonts/logo event-first:
  - colors: `brand_admin.REQUIRED_COLOR_KEYS` each present + `ImageColor.getrgb` valid (`bad_color`).
  - fonts: `brand_admin.REQUIRED_FONT_KEYS` each resolve via `resolve_event_font_ref(event_dir, channel_dir, ...)` (`bad_font`).
  - subtitles: `profile._validate_subtitles(merged, event_dir/"brand.json")` (`bad_subtitles`).
  - logo/output: build `resolved = {**merged, "fonts": resolved_fonts}`, `profile._resolve_logo(resolved, event_dir, channel_dir)`, then `profile._validate_brand(resolved, ...) + profile._validate_logo(resolved, ...)` (`bad_brand`). Do NOT run `_validate_upload` (upload is not event-editable).
- `resolve_event_font_ref`: require `ref` is `fonts/<safe-segment>`; return `event_dir/fonts/<name>` if it exists, else `channel_dir/fonts/<name>` if it exists, else raise `bad_font`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_brand_admin.py
import json
from pathlib import Path

import pytest

from yt_shorts import event_brand_admin as eba


def _channel(tmp_path, *, with_font="Hook.ttf") -> Path:
    """A minimal complete channel brand + one font, mirroring the shape
    brand_admin._validate accepts."""
    ch = tmp_path / "erf"
    (ch / "fonts").mkdir(parents=True)
    (ch / "events").mkdir()
    if with_font:
        (ch / "fonts" / with_font).write_bytes(b"not-really-a-font")
    brand = {
        "colors": {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"},
        "fonts": {"hook": "fonts/Hook.ttf", "small": "fonts/Hook.ttf"},
        "output": {"width": 1080, "height": 1920, "video_width": 1080,
                   "video_height": 608, "video_y": 600},
        "upload": {"mode": "manual"},
    }
    (ch / "brand.json").write_text(json.dumps(brand), encoding="utf-8")
    return tmp_path


def _event(channels_dir: Path, name="ev") -> Path:
    d = channels_dir / "erf" / "events" / name
    d.mkdir(parents=True)
    return d


def test_read_returns_override_channel_effective(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["override"] == {}
    assert out["channel"]["colors"]["accent"] == "#144E53"
    assert out["effective"]["colors"]["accent"] == "#144E53"  # inherited


def test_override_one_color_writes_only_that_section_and_merges(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    eba.update_event_brand(channels, "erf", "ev",
                           {"colors": {"text": "#FFFFFF", "base": "#004625",
                                       "accent": "#FF0000", "edge": "#B8F5CA"}})
    written = json.loads((event_dir / "brand.json").read_text())
    assert set(written) == {"colors"}          # ONLY the overridden section
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["effective"]["colors"]["accent"] == "#FF0000"   # override wins
    assert out["effective"]["fonts"]["hook"] == "fonts/Hook.ttf"  # inherited


def test_empty_patch_deletes_the_override_file(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    (event_dir / "brand.json").write_text('{"colors": {}}', encoding="utf-8")
    eba.update_event_brand(channels, "erf", "ev", {})
    assert not (event_dir / "brand.json").exists()


def test_upload_section_is_rejected(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    with pytest.raises(eba.EventBrandError) as e:
        eba.update_event_brand(channels, "erf", "ev", {"upload": {"mode": "api"}})
    assert e.value.kind == "bad_field"


def test_override_that_breaks_the_merge_is_rejected(tmp_path):
    channels = _channel(tmp_path)
    _event(channels)
    with pytest.raises(eba.EventBrandError) as e:
        eba.update_event_brand(channels, "erf", "ev",
                               {"colors": {"text": "not-a-color", "base": "#004625",
                                           "accent": "#144E53", "edge": "#B8F5CA"}})
    assert e.value.kind == "bad_color"


def test_partial_override_valid_only_after_merge_is_accepted(tmp_path):
    # override sets ONLY accent; text/base/edge + fonts come from the channel.
    channels = _channel(tmp_path)
    _event(channels)
    eba.update_event_brand(channels, "erf", "ev", {"colors": {"accent": "#FF0000"}})
    out = eba.read_event_brand(channels, "erf", "ev")
    assert out["effective"]["colors"]["accent"] == "#FF0000"
    assert out["effective"]["colors"]["text"] == "#FFFFFF"   # from channel


def test_event_font_ref_resolves_event_first(tmp_path):
    channels = _channel(tmp_path)
    event_dir = _event(channels)
    (event_dir / "fonts").mkdir()
    (event_dir / "fonts" / "Special.ttf").write_bytes(b"x")
    p = eba.resolve_event_font_ref(event_dir, channels / "erf", "fonts/Special.ttf")
    assert p == event_dir / "fonts" / "Special.ttf"


def test_bad_segment_rejected(tmp_path):
    channels = _channel(tmp_path)
    with pytest.raises(eba.EventBrandError) as e:
        eba.read_event_brand(channels, "erf", "../escape")
    assert e.value.kind == "bad_name"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_event_brand_admin.py -q`
Expected: FAIL (ModuleNotFoundError: yt_shorts.event_brand_admin)

- [ ] **Step 3: Write minimal implementation**

```python
# src/yt_shorts/event_brand_admin.py
"""Read and update an event's brand.json OVERRIDE (colors/fonts/logo/output/
subtitles) - the partial layer profile.load deep-merges over the channel brand.
Pure, no FastAPI. Unlike brand_admin (which validates a COMPLETE channel brand),
this validates the MERGED result (channel + override) and stores only the
overridden sections; a fully-inherited event has no brand.json at all. upload
is never event-overridable."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import ImageColor

from . import brand_admin, pathnames
from .merge import deep_merge

OVERRIDE_SECTIONS = ("colors", "fonts", "logo", "output", "subtitles")


class EventBrandError(Exception):
    """kind: bad_name | not_found | bad_field | bad_color | bad_font |
    bad_subtitles | bad_brand. HTTP: not_found -> 404, everything else -> 400."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _dirs(channels_dir, channel: str, event: str) -> tuple[Path, Path]:
    for value, what in ((channel, "channel name"), (event, "event name")):
        try:
            pathnames.validate_segment(value, what=what)
        except ValueError as error:
            raise EventBrandError(str(error), kind="bad_name") from error
    channel_dir = Path(channels_dir) / channel
    return channel_dir, channel_dir / "events" / event


def _load_json(path: Path, label: str, *, optional: bool) -> dict:
    if not path.exists():
        if optional:
            return {}
        raise EventBrandError(f"{label} not found", kind="not_found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EventBrandError(f"{label} is unreadable: {error}", kind="not_found") from error


def resolve_event_font_ref(event_dir: Path, channel_dir: Path, ref, *, what: str = "font") -> Path:
    if not ref or not isinstance(ref, str) or not ref.startswith("fonts/"):
        raise EventBrandError(f"{what} must be 'fonts/<file>'", kind="bad_font")
    name = ref[len("fonts/"):]
    try:
        pathnames.validate_segment(name, what="font filename")
    except ValueError as error:
        raise EventBrandError(f"{what} name is invalid: {name!r}", kind="bad_font") from error
    for base in (event_dir, channel_dir):
        candidate = base / "fonts" / name
        if candidate.is_file():
            return candidate
    raise EventBrandError(f"{what} file not found: {ref!r}", kind="bad_font")


def read_event_brand(channels_dir, channel: str, event: str) -> dict:
    channel_dir, event_dir = _dirs(channels_dir, channel, event)
    if not event_dir.is_dir():
        raise EventBrandError(f"unknown event: {event!r}", kind="not_found")
    channel_brand = _load_json(channel_dir / "brand.json", "channel brand.json", optional=False)
    override = _load_json(event_dir / "brand.json", "event brand.json", optional=True)
    return {"override": override, "channel": channel_brand,
            "effective": deep_merge(channel_brand, override)}


def update_event_brand(channels_dir, channel: str, event: str, patch: dict) -> None:
    channel_dir, event_dir = _dirs(channels_dir, channel, event)
    if not event_dir.is_dir():
        raise EventBrandError(f"unknown event: {event!r}", kind="not_found")
    for key in patch:
        if key not in OVERRIDE_SECTIONS:
            raise EventBrandError(
                f"{key!r} cannot be overridden at the event level", kind="bad_field")
    channel_brand = _load_json(channel_dir / "brand.json", "channel brand.json", optional=False)
    merged = deep_merge(channel_brand, patch)
    _validate_merged(merged, event_dir, channel_dir)
    path = event_dir / "brand.json"
    if patch:
        path.write_text(json.dumps(patch, indent=2) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def _validate_merged(merged: dict, event_dir: Path, channel_dir: Path) -> None:
    colors = merged.get("colors")
    if not isinstance(colors, dict):
        raise EventBrandError("the 'colors' section is required", kind="bad_color")
    for key in brand_admin.REQUIRED_COLOR_KEYS:
        value = colors.get(key)
        if not value:
            raise EventBrandError(f"color {key!r} is required", kind="bad_color")
        try:
            ImageColor.getrgb(value)
        except ValueError as error:
            raise EventBrandError(
                f"color {key!r} is not a valid color: {value!r}", kind="bad_color") from error

    fonts = merged.get("fonts")
    if not isinstance(fonts, dict):
        raise EventBrandError("the 'fonts' section is required", kind="bad_font")
    resolved_fonts = {
        key: str(resolve_event_font_ref(event_dir, channel_dir, fonts.get(key),
                                        what=f"font {key!r}"))
        for key in brand_admin.REQUIRED_FONT_KEYS}

    from . import profile
    brand_path = event_dir / "brand.json"
    problems = profile._validate_subtitles(merged, brand_path)
    if problems:
        raise EventBrandError(problems[0], kind="bad_subtitles")

    resolved = {**merged, "fonts": resolved_fonts}
    profile._resolve_logo(resolved, event_dir, channel_dir)
    problems = (profile._validate_brand(resolved, brand_path)
                + profile._validate_logo(resolved, brand_path))
    if problems:
        raise EventBrandError(problems[0], kind="bad_brand")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_event_brand_admin.py -q`
Expected: PASS (8 passed). Then `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/event_brand_admin.py tests/test_event_brand_admin.py
git commit -m "feat(event-brand): partial override admin (merged validation, section-only writes)"
```

---

### Task 2: Generalize `font_admin` to a base dir + event font wrappers

**Files:**
- Modify: `src/yt_shorts/font_admin.py`
- Test: `tests/test_font_admin.py` (append; keep existing channel tests passing)

**Interfaces:**
- Consumes: existing `font_admin` constants + validation.
- Produces (additions; the existing `list_fonts/save_font/delete_font` channel signatures stay unchanged):
  - `save_event_font(channels_dir, channel, event, filename, data) -> None`
  - `list_event_fonts(channels_dir, channel, event) -> list[str]`
  - `delete_event_font(channels_dir, channel, event, filename) -> None` — refuses (`in_use`) if the EVENT brand.json assigns it as hook/small.

**Design notes:** Refactor the core into base-dir helpers and keep the channel functions as thin wrappers so channel behavior is byte-identical (existing tests pin it). The event delete guard checks ONLY the event's own `brand.json`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_font_admin.py
import json as _json

from yt_shorts import font_admin as _fa


def _chan_with_event(tmp_path):
    ch = tmp_path / "erf"
    (ch / "fonts").mkdir(parents=True)
    ev = ch / "events" / "ev"
    ev.mkdir(parents=True)
    return tmp_path, ev


def test_event_font_save_list_delete(tmp_path):
    channels, ev = _chan_with_event(tmp_path)
    from PIL import ImageFont  # a real loadable font so save passes
    # use an existing fixture font's bytes
    import pathlib
    font_bytes = next((pathlib.Path("tests/fixtures/channels/erf/fonts")).glob("*.ttf")).read_bytes()
    _fa.save_event_font(channels, "erf", "ev", "Special.ttf", font_bytes)
    assert "Special.ttf" in _fa.list_event_fonts(channels, "erf", "ev")
    _fa.delete_event_font(channels, "erf", "ev", "Special.ttf")
    assert "Special.ttf" not in _fa.list_event_fonts(channels, "erf", "ev")


def test_event_font_in_use_is_refused(tmp_path):
    channels, ev = _chan_with_event(tmp_path)
    import pathlib
    font_bytes = next((pathlib.Path("tests/fixtures/channels/erf/fonts")).glob("*.ttf")).read_bytes()
    _fa.save_event_font(channels, "erf", "ev", "Special.ttf", font_bytes)
    (ev / "brand.json").write_text(_json.dumps(
        {"fonts": {"hook": "fonts/Special.ttf", "small": "fonts/Special.ttf"}}), encoding="utf-8")
    with pytest.raises(_fa.FontAdminError) as e:
        _fa.delete_event_font(channels, "erf", "ev", "Special.ttf")
    assert e.value.kind == "in_use"
```

(Ensure `import pytest` is present at the top of the file — it already is.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_font_admin.py -q`
Expected: FAIL (AttributeError: save_event_font)

- [ ] **Step 3: Write minimal implementation**

Refactor `font_admin.py`: extract base-dir cores and add event wrappers. Keep the existing channel functions but route them through the cores.

```python
# --- add near the bottom of font_admin.py ---

def _event_fonts_dir(channels_dir, channel: str, event: str) -> Path:
    _validate_segment(channel, "channel name")
    _validate_segment(event, "event name")
    base = Path(channels_dir) / channel / "events" / event
    if not base.is_dir():
        raise FontAdminError(f"unknown event: {event!r}", kind="not_found")
    return base / "fonts"


def _list_in(fonts: Path) -> list[str]:
    if not fonts.is_dir():
        return []
    return sorted(p.name for p in fonts.iterdir()
                  if p.is_file() and p.suffix.lower() in FONT_EXTENSIONS)


def _save_in(fonts: Path, filename: str, data: bytes) -> None:
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


def list_event_fonts(channels_dir, channel: str, event: str) -> list[str]:
    return _list_in(_event_fonts_dir(channels_dir, channel, event))


def save_event_font(channels_dir, channel: str, event: str, filename: str, data: bytes) -> None:
    _save_in(_event_fonts_dir(channels_dir, channel, event), filename, data)


def delete_event_font(channels_dir, channel: str, event: str, filename: str) -> None:
    fonts = _event_fonts_dir(channels_dir, channel, event)
    _validate_segment(filename, "font filename")
    target = fonts / filename
    if not target.is_file():
        raise FontAdminError(f"unknown font: {filename!r}", kind="not_found")
    # The event's OWN brand.json is the only brand that can assign an event font.
    brand_path = fonts.parent / "brand.json"
    if _brand_assigns_font(brand_path, f"{channel}/{event}", fonts, target, filename):
        raise FontAdminError(
            f"font {filename!r} is assigned in {brand_path.name}; reassign it first",
            kind="in_use")
    target.unlink()
```

(The existing channel `list_fonts/save_font/delete_font` are unchanged; you may optionally route them through `_list_in`/`_save_in` for DRY, but only if the existing channel tests stay green.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_font_admin.py -q`
Expected: PASS (existing + 2 new). Then `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/font_admin.py tests/test_font_admin.py
git commit -m "feat(event-brand): event-scoped font add/list/delete (in-use guard vs event brand)"
```

---

### Task 3: Studio routes — event brand read/write, fonts, merged preview

**Files:**
- Modify: `src/yt_shorts/studio/api.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- Consumes: Tasks 1–2, `overlay.build_overlay`, `profile._resolve_logo`.
- Produces routes under `EV = "/api/channels/{channel}/events/{event}"`:
  - `GET  EV + "/brand"` → `read_event_brand` + `{"fonts": {"channel": list_fonts(...), "event": list_event_fonts(...)}}`.
  - `PUT  EV + "/brand"` (body `BrandPatchBody`, `upload` ignored) → `update_event_brand`, returns the fresh `read_event_brand`.
  - `POST EV + "/fonts/{filename}"` (201, raw body via `_read_body_capped`) / `DELETE EV + "/fonts/{filename}"`.
  - `POST EV + "/brand/preview"` → render `overlay.build_overlay` on `deep_merge(channel_brand, edited_override)` (fonts/logo resolved event-first), PNG or 409.

**Design notes:** Read the existing channel `get_brand`/`put_brand`/`upload_font`/`delete_font`/`brand_preview` (api.py:481–607) and mirror them at event scope. Add an `_event_brand_status`/`_event_font_status` kind→HTTP mapper. For the preview, mirror `brand_preview` but start from `deep_merge(channel_brand, patch)` and resolve fonts with `event_brand_admin.resolve_event_font_ref(event_dir, channel_dir, ...)`, logo with `_resolve_logo(holder, event_dir, channel_dir)`. Import `from .. import event_brand_admin`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_studio_api.py — a new class
class TestEventBrand:
    def _seed_event(self, studio_profile):
        # studio_profile already points CHANNELS_DIR at a tmp erf copy with an event.
        # Reuse whatever helper the other event tests use to ensure the event dir
        # exists; the erf fixture's studio-test event is created by the suite.
        return None

    def test_get_event_brand_reports_channel_and_effective(self, client, studio_profile):
        r = client.get("/api/channels/erf/events/studio-test/brand")
        assert r.status_code == 200
        body = r.json()
        assert "override" in body and "channel" in body and "effective" in body
        assert body["override"] == {}                      # nothing overridden yet
        assert "channel" in body["fonts"] and "event" in body["fonts"]

    def test_put_event_brand_stores_only_overridden_section(self, client, studio_profile):
        eff = client.get("/api/channels/erf/events/studio-test/brand").json()["effective"]
        colors = {**eff["colors"], "accent": "#FF0000"}
        r = client.put("/api/channels/erf/events/studio-test/brand", json={"colors": colors})
        assert r.status_code == 200
        assert r.json()["effective"]["colors"]["accent"] == "#FF0000"
        # override holds ONLY colors
        assert set(client.get("/api/channels/erf/events/studio-test/brand").json()["override"]) == {"colors"}

    def test_put_event_brand_rejects_upload(self, client, studio_profile):
        r = client.put("/api/channels/erf/events/studio-test/brand",
                       json={"upload": {"mode": "api"}})
        assert r.status_code == 400

    def test_event_brand_preview_returns_png(self, client, studio_profile):
        eff = client.get("/api/channels/erf/events/studio-test/brand").json()["effective"]
        r = client.post("/api/channels/erf/events/studio-test/brand/preview",
                        json={"colors": eff["colors"], "fonts": eff["fonts"],
                              "output": eff["output"]})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
```

(If the erf fixture has no `studio-test` event, mirror the exact seeding the existing `TestEvent*`/clip tests use in this file — read them first and reuse that fixture/helper.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k EventBrand`
Expected: FAIL (404 — routes absent)

- [ ] **Step 3: Write minimal implementation** — add the routes to `create_app` mirroring the channel brand routes; add `from .. import event_brand_admin`; map kinds:

```python
    def _event_brand_status(error: event_brand_admin.EventBrandError) -> int:
        return 404 if error.kind == "not_found" else 400

    @app.get(EV + "/brand")
    def get_event_brand(channel: str, event: str) -> dict:
        try:
            data = event_brand_admin.read_event_brand(channels_dir, channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error
        data["fonts"] = {"channel": font_admin.list_fonts(channels_dir, channel),
                         "event": font_admin.list_event_fonts(channels_dir, channel, event)}
        return data

    @app.put(EV + "/brand")
    def put_event_brand(channel: str, event: str, body: BrandPatchBody) -> dict:
        patch = {k: v for k, v in body.model_dump().items()
                 if v is not None and k in event_brand_admin.OVERRIDE_SECTIONS}
        try:
            event_brand_admin.update_event_brand(channels_dir, channel, event, patch)
            data = event_brand_admin.read_event_brand(channels_dir, channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error
        data["fonts"] = {"channel": font_admin.list_fonts(channels_dir, channel),
                         "event": font_admin.list_event_fonts(channels_dir, channel, event)}
        return data

    @app.post(EV + "/fonts/{filename}", status_code=201)
    async def upload_event_font(channel: str, event: str, filename: str, request: Request) -> dict:
        data = await _read_body_capped(request, font_admin.MAX_FONT_BYTES)
        try:
            font_admin.save_event_font(channels_dir, channel, event, filename, data)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_event_fonts(channels_dir, channel, event)}

    @app.delete(EV + "/fonts/{filename}")
    def delete_event_font(channel: str, event: str, filename: str) -> dict:
        try:
            font_admin.delete_event_font(channels_dir, channel, event, filename)
        except font_admin.FontAdminError as error:
            raise HTTPException(status_code=_font_status(error), detail=str(error)) from error
        return {"fonts": font_admin.list_event_fonts(channels_dir, channel, event)}

    @app.post(EV + "/brand/preview")
    def event_brand_preview(channel: str, event: str, body: BrandPatchBody):
        import io as _io

        from ..overlay import build_overlay
        from ..profile import _resolve_logo
        try:
            data = event_brand_admin.read_event_brand(channels_dir, channel, event)
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(status_code=_event_brand_status(error), detail=str(error)) from error
        patch = {k: getattr(body, k) for k in event_brand_admin.OVERRIDE_SECTIONS
                 if getattr(body, k) is not None}
        from ..merge import deep_merge
        merged = deep_merge(data["channel"], patch)
        channel_dir = channels_dir / channel
        event_dir = channel_dir / "events" / event
        fonts = merged.get("fonts") or {}
        try:
            resolved_fonts = {
                role: str(event_brand_admin.resolve_event_font_ref(
                    event_dir, channel_dir, ref, what=f"font {role!r}"))
                for role, ref in fonts.items()}
        except event_brand_admin.EventBrandError as error:
            raise HTTPException(
                status_code=409,
                detail=f"cannot render preview (check the assigned fonts): {error}") from error
        config = {"colors": merged.get("colors") or {}, "fonts": resolved_fonts,
                  "output": merged.get("output") or {}}
        if isinstance(merged.get("logo"), dict):
            holder = {"logo": merged["logo"]}
            _resolve_logo(holder, event_dir, channel_dir)
            config["logo"] = holder["logo"]
        channel_json = _load_channel(channel)
        hook = channel_json.get("display_name", channel)
        footer = channel_json.get("footer", "")
        try:
            image = build_overlay(hook, footer, config)
        except Exception as error:   # noqa: BLE001 - a missing/invalid font or bad geometry
            _logger.warning("event brand preview failed for %s/%s: %s", channel, event, error)
            raise HTTPException(
                status_code=409,
                detail="cannot render preview (check the assigned fonts)") from error
        buffer = _io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(content=buffer.getvalue(), media_type="image/png")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k EventBrand` → PASS. Then the whole file: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q` → all pass. `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py tests/test_studio_api.py
git commit -m "feat(studio): event brand read/write, fonts, merged preview routes"
```

---

### Task 4: Frontend API client + pure `eventBrand.ts` helpers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Create: `src/yt_shorts/studio/web/src/eventBrand.ts`
- Test: `src/yt_shorts/studio/web/src/eventBrand.test.ts`

**Interfaces:**
- In `api.ts` (reuse the existing `eventBase`, `asJson`, `ApiError`, `BrandPatch`, `BrandResponse`-style types):
  - `interface EventBrandResponse { override: Record<string, unknown>; channel: Record<string, unknown>; effective: Record<string, unknown>; fonts: { channel: string[]; event: string[] } }`
  - `getEventBrand(channel, event): Promise<EventBrandResponse>` (GET)
  - `saveEventBrand(channel, event, patch: BrandPatch): Promise<EventBrandResponse>` (PUT)
  - `uploadEventFont(channel, event, filename, bytes): Promise<{ fonts: string[] }>` (POST raw body — mirror the existing `uploadFont`)
  - `deleteEventFont(channel, event, filename): Promise<{ fonts: string[] }>` (DELETE)
  - `eventBrandPreview(channel, event, patch: BrandPatch): Promise<Blob>` (POST → blob, mirror `brandPreview`)
- In `eventBrand.ts` (pure, no React):
  - `SECTIONS = ['colors','fonts','logo','output','subtitles'] as const`
  - `overriddenSections(override: Record<string, unknown>): Set<string>` — the section keys present in the override.
  - `buildOverridePayload(effective, overridden: Set<string>): BrandPatch` — for each section in `overridden`, take the whole section from `effective`; omit the rest. (This is what PUT receives.)

- [ ] **Step 1: Write the failing test**

```typescript
// src/yt_shorts/studio/web/src/eventBrand.test.ts
import { describe, expect, it } from 'vitest'
import { buildOverridePayload, overriddenSections } from './eventBrand'

describe('overriddenSections', () => {
  it('returns the section keys present in the override', () => {
    expect(overriddenSections({ colors: {}, logo: {} })).toEqual(new Set(['colors', 'logo']))
    expect(overriddenSections({})).toEqual(new Set())
  })
  it('ignores unknown keys', () => {
    expect(overriddenSections({ colors: {}, upload: {} })).toEqual(new Set(['colors']))
  })
})

describe('buildOverridePayload', () => {
  const effective = {
    colors: { text: '#fff', base: '#000', accent: '#f00', edge: '#0f0' },
    fonts: { hook: 'fonts/H.ttf', small: 'fonts/S.ttf' },
    output: { width: 1080, height: 1920 },
  }
  it('includes only overridden sections, whole', () => {
    expect(buildOverridePayload(effective, new Set(['colors']))).toEqual({ colors: effective.colors })
  })
  it('is empty when nothing is overridden (fully inherited)', () => {
    expect(buildOverridePayload(effective, new Set())).toEqual({})
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `.../web`): `npm test -- eventBrand` → FAIL (cannot find './eventBrand')

- [ ] **Step 3: Write minimal implementation**

```typescript
// src/yt_shorts/studio/web/src/eventBrand.ts
/** Pure helpers for the event-level brand override editor, kept out of
 * components so Vite's fast-refresh boundary stays component-only and each
 * rule is unit-tested. The event brand.json is a PARTIAL override deep-merged
 * over the channel brand (see profile.load / event_brand_admin); these shape
 * the per-section override the editor sends. The server is the real boundary. */
import type { BrandPatch } from './api'

export const SECTIONS = ['colors', 'fonts', 'logo', 'output', 'subtitles'] as const
export type Section = (typeof SECTIONS)[number]

export function overriddenSections(override: Record<string, unknown>): Set<string> {
  return new Set(SECTIONS.filter((s) => Object.prototype.hasOwnProperty.call(override, s)))
}

/** The override payload PUT receives: each overridden section taken WHOLE from
 * the effective (merged) brand, inherited sections omitted. */
export function buildOverridePayload(
  effective: Record<string, unknown>,
  overridden: Set<string>,
): BrandPatch {
  const payload: Record<string, unknown> = {}
  for (const s of SECTIONS) {
    if (overridden.has(s) && effective[s] !== undefined) payload[s] = effective[s]
  }
  return payload as BrandPatch
}
```

Then append the `api.ts` functions after the channel brand functions, mirroring `getBrand`/`saveBrand`/`uploadFont`/`deleteFont`/`brandPreview` but with `eventBase(channel, event)`.

- [ ] **Step 4: Run test + typecheck**

Run (in `.../web`): `npm test -- eventBrand` → PASS; `npx tsc -b` → exit 0; `npm run lint` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/eventBrand.ts src/yt_shorts/studio/web/src/eventBrand.test.ts
git commit -m "feat(studio-web): event brand API client + pure override helpers"
```

---

### Task 5: `EventBrandEditor` component (section inherit/override)

**Files:**
- Create: `src/yt_shorts/studio/web/src/components/EventBrandEditor.tsx`

**Interfaces:**
- Consumes: `getEventBrand`/`saveEventBrand`/`uploadEventFont`/`deleteEventFont`/`eventBrandPreview`/`EventBrandResponse`/`ApiError` from `../api`; `overriddenSections`/`buildOverridePayload`/`SECTIONS` from `../eventBrand`; `brand.ts` helpers (`isValidHexColor`, `fontFilename`, `LOGO_VARIANTS`, `LOGO_POSITIONS`, `outputReadyToSave`); `useDebouncedValue` (already used by BrandEditor).
- Produces: `export function EventBrandEditor({ channel, event }: { channel: string; event: string }): JSX.Element`

**Design notes for the implementer:**
- **Read `BrandEditor.tsx` in full first** — you reuse its exact per-section field widgets (colors `ColorInput` grid, fonts `Select`+`FileButton` upload, logo variant/placement/opacity, output `NumberInput` grid, subtitles switch) and its debounced-preview + object-URL lifecycle. Do NOT re-invent those; mirror them.
- On mount, `getEventBrand(channel, event)`. Hold state:
  - `effective` (the merged brand, the working values the fields edit),
  - `overridden: Set<Section>` seeded from `overriddenSections(response.override)`,
  - the two font lists (`channel`, `event`).
- **Per section**, render a header with a Mantine `Switch`/`SegmentedControl` "Inherit from channel ⇄ Override":
  - When **inherited**: show that section's fields **disabled** (read-only), displaying the channel's values (`response.channel[section]`), so the operator sees what will be used.
  - When **overridden**: show the same fields **editable**, bound to `effective[section]` (seeded from effective when the toggle flips on).
  - Toggling on adds the section to `overridden`; toggling off removes it AND resets `effective[section]` back to `channel[section]`.
- **Fonts section (override):** the `Select` options are the UNION of `fonts.channel` + `fonts.event` (label event fonts, e.g. "Special.ttf (event)"). Upload goes to `uploadEventFont`; delete (only event fonts) to `deleteEventFont`; refresh both lists after.
- **Live preview:** debounce the working state; call `eventBrandPreview(channel, event, buildOverridePayload(effective, overridden))`; show the PNG (revoke old object URLs), mirroring BrandEditor's preview effect exactly. `previewError` on 409.
- **Save:** `saveEventBrand(channel, event, buildOverridePayload(effective, overridden))`; on success update `overridden`/`effective` from the response, toast "Saved.". Save disabled unless the would-be merged brand is complete — reuse `brandReadyToSave`/`outputReadyToSave` on `effective` (the merged brand must always be complete; that is guaranteed as long as inherited sections come from a valid channel, so effectively Save is gated on the overridden sections being valid — keep it simple: gate on `outputReadyToSave(effective.output)` when output is overridden, and colors valid when colors overridden).
- Keep pure logic in `eventBrand.ts`; this file exports only the component.

- [ ] **Step 1: Write the component** (no unit test — exercised by Task 7's E2E; the gate is typecheck + lint, and the pure logic is already Vitest-tested in Task 4)

Write `EventBrandEditor.tsx` per the design notes, mirroring `BrandEditor.tsx`'s widgets and preview lifecycle, wrapping each section in the inherit/override control. (Complete component; follow the established BrandEditor patterns for every field widget.)

- [ ] **Step 2: Typecheck + lint**

Run (in `.../web`): `npx tsc -b` → exit 0; `npm run lint` → clean; `npm test` → still all pass.

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/EventBrandEditor.tsx
git commit -m "feat(studio-web): EventBrandEditor with per-section inherit/override"
```

---

### Task 6: Drawer trigger in the editor header

**Files:**
- Modify: `src/yt_shorts/studio/web/src/App.tsx`

**Interfaces:**
- Consumes: `EventBrandEditor`, Mantine `Drawer`, `useDisclosure` (or a `useState` boolean).
- Produces: a header button "Event branding" that opens a right-hand `Drawer` hosting `<EventBrandEditor channel={channel} event={event} />`.

**Design notes:**
- Read `App.tsx`'s `AppShell.Header` (around lines 309–344). Add a `Button`/`ActionIcon` in the right-hand `Group` (near "LOCAL EDITOR"). Wire `opened`/`open`/`close` state.
- The `Drawer` must own its scroll: give it `scrollAreaComponent={ScrollArea.Autosize}` OR a body with `overflow-y:auto` and a bounded height, so all sections + preview are reachable at a short viewport (mandatory scroll criterion). `position="right"`, `size="lg"` (or `"xl"`).
- `channel`/`event` are already props of `App`.

- [ ] **Step 1: Implement the Drawer trigger** — add the button + `Drawer` wrapping `EventBrandEditor`.

- [ ] **Step 2: Typecheck + lint + build**

Run (in `.../web`): `npx tsc -b` → 0; `npm run lint` → clean; `npm test` → all pass.

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/App.tsx
git commit -m "feat(studio-web): open the event brand editor from the editor header"
```

---

### Task 7: Build, E2E, full verification, commit static

**Files:**
- Modify: `src/yt_shorts/studio/static/**` (built), `tests/test_studio_e2e.py` (one E2E)

- [ ] **Step 1: Add the E2E** in `tests/test_studio_e2e.py` using the existing `event_dir`/`live_server`/`page` fixtures and `editor_url`:
  - open the editor, click "Event branding" to open the Drawer,
  - flip the **Colors** section to "Override", change the accent color, Save,
  - assert (a) the event `brand.json` on disk contains **only** `colors` (read the file via the `event_dir` fixture), (b) `GET …/events/{event}/brand`'s `effective.colors.accent` reflects it, (c) the Drawer scroll container is scrollable at a short viewport (measure `scrollHeight > clientHeight` on the drawer body via `page.evaluate`, mirroring the workspace/editor scroll checks).
  Model the seeding + selectors on the existing editor E2E tests.

- [ ] **Step 2: Build the frontend**

Run (in `.../web`): `npm run lint` (clean) → `npm run build` (exit 0 — regenerates `../static`).

- [ ] **Step 3: Run the full suites**

Run: `npm test` (in `.../web`) → all pass.
Run: `PYTHONPATH=src .venv/bin/pytest -q` → all pass.
Run: `python3 tools/lint.py` → All checks passed.

- [ ] **Step 4: Commit the built static + E2E**

```bash
git add src/yt_shorts/studio/static tests/test_studio_e2e.py
git commit -m "build(studio): rebuild static for event brand editing; e2e override flow"
```

- [ ] **Step 5: Manual smoke (optional)**

Start `bin/yt-shorts studio`, open an event, click "Event branding", override a color/logo, Save, and confirm the effective look changes and the Drawer scrolls.

---

## Notes for the implementer

- **The merge/validation contract is the crux (Task 1):** always validate `deep_merge(channel, override)`, resolving fonts/logo **event-first**. A partial override valid only after merge must be accepted; a merge that produces an invalid brand must be rejected. Task 1's tests pin both directions.
- **Whole-section override:** overriding a section stores the full section (seeded from effective); inheriting removes it; empty override deletes the file. `deep_merge` still merges per key, so a future per-leaf refinement stays compatible.
- **`upload.mode` is never event-editable** — excluded from `OVERRIDE_SECTIONS` and dropped from the PUT/preview patch.
- **DRY vs risk:** reuse `brand.ts` helpers and mirror `BrandEditor.tsx`'s field widgets; do NOT refactor the working channel `BrandEditor` in a way that could change its pinned behavior. Some field-widget JSX duplication is acceptable; the shared *logic* lives in `brand.ts`/`eventBrand.ts`.
- **Scroll:** the Drawer is a new full-height surface — it must own its scroll, verified at a short viewport (mandatory visual-acceptance criterion).
