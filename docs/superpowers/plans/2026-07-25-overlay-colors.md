# Overlay Colour Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every channel a palette derived from its own logo, and make the opacity of the overlay's upper and lower bands adjustable per channel and per event — down to zero, leaving only the blurred backdrop under the hook and footer.

**Architecture:** A new optional `bands` section in `brand.json` carries one float per band. `overlay.build_overlay` scales the alpha of everything it has drawn so far at exactly one point — after the veil, decoration and edge accents, before the logo, hook and footer — so the factor reaches every surface including a channel decoration this module never sees. A new pure `palette.py` quantises a logo into swatches and proposes the four brand colours; the studio's existing brand editors gain a button and two sliders.

**Tech Stack:** Python 3.14 + Pillow (no new dependency), FastAPI (studio routes only), React + Mantine + TypeScript + Vitest (studio frontend), pytest + Playwright.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q`.
- `python3 tools/lint.py` (NO `PYTHONPATH`) must print `All checks passed!` before every commit.
- Frontend gates, from `src/yt_shorts/studio/web`: `npx tsc -b`, `npm run lint`, `npm test`. Only Task 8 runs `npm run build` and commits `src/yt_shorts/studio/static/`.
- **The six pinned overlay hashes in `tests/test_event_layer_no_regression.py` must not change and must not be re-pinned.** They are the only guard that catches a rendering regression the assertions miss.
- `overlay.py` knows nothing channel-specific and must not import `profile` (`profile` imports `overlay`, not the reverse).
- `palette.py` is pure: no FastAPI, no google, no studio import. Pillow is fine.
- Band opacity is a float in `[0.0, 1.0]`. `1.0`, an absent key, and an absent `bands` section all mean the same thing.
- Opacity affects SURFACES only — veil, channel decoration, edge accents. Logo, hook and footer always render at full strength.
- Studio routes stay a thin layer over the pure admin modules, mapping `*Error.kind` to a status; they add no second guard.
- Every path segment reaching the filesystem goes through `pathnames.validate_segment` before any filesystem touch.
- A test that would pass even if the behaviour under test were broken is a defect. This project has shipped that four times; two of them came from plan text like this one.

---

### Task 1: `bands` in the profile

**Files:**
- Modify: `src/yt_shorts/overlay.py` (new constants + `band_opacities`)
- Modify: `src/yt_shorts/profile.py` (`_validate_brand`, `load`)
- Test: `tests/test_overlay.py`, `tests/test_profile.py`

**Interfaces:**
- Produces: `overlay.BAND_KEYS = ("top", "bottom")`; `overlay.band_opacities(config: dict) -> dict[str, float]` returning both keys, always floats; `profile.load` stores the normalised result under `config["bands"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_overlay.py`:

```python
class TestBandOpacities:
    def test_an_absent_section_is_full_strength(self):
        assert overlay.band_opacities({}) == {"top": 1.0, "bottom": 1.0}

    def test_an_absent_key_is_full_strength(self):
        assert overlay.band_opacities({"bands": {"top": 0.25}}) == {"top": 0.25, "bottom": 1.0}

    def test_an_int_becomes_a_float(self):
        values = overlay.band_opacities({"bands": {"top": 0, "bottom": 1}})
        assert values == {"top": 0.0, "bottom": 1.0}
        assert all(isinstance(v, float) for v in values.values())

    def test_a_malformed_section_falls_back_rather_than_raising(self):
        """Validation is profile._validate_brand's job and runs before this
        is ever called on a loaded profile. This must still not raise for a
        caller that hands it a raw dict - build_overlay is also called
        directly by the studio's preview on an UNSAVED brand."""
        assert overlay.band_opacities({"bands": "nope"}) == {"top": 1.0, "bottom": 1.0}
```

Append to `tests/test_profile.py`:

```python
class TestBandValidation:
    def _brand(self, tmp_path, monkeypatch, bands):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        channel_dir = _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        brand = json.loads((channel_dir / "brand.json").read_text(encoding="utf-8"))
        brand["bands"] = bands
        (channel_dir / "brand.json").write_text(json.dumps(brand), encoding="utf-8")
        return channel_dir

    def test_a_valid_section_loads(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": 0.5, "bottom": 0.0})
        assert profile.load("chan/event").config["bands"] == {"top": 0.5, "bottom": 0.0}

    def test_an_absent_section_defaults_to_full_strength(self, monkeypatch, tmp_path):
        monkeypatch.setattr(profile, "CHANNELS_DIR", tmp_path / "channels")
        _build_channel_dir(tmp_path / "channels", "chan", events=["event"])
        assert profile.load("chan/event").config["bands"] == {"top": 1.0, "bottom": 1.0}

    def test_a_non_dict_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, [1, 0])
        with pytest.raises(profile.ProfileError, match="'bands' must be an object"):
            profile.load("chan/event")

    def test_an_unknown_key_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"middle": 0.5})
        with pytest.raises(profile.ProfileError, match="unknown band 'bands.middle'"):
            profile.load("chan/event")

    def test_a_bool_is_a_reported_defect(self, monkeypatch, tmp_path):
        """True is an int in Python - the same trap output's integer check
        already guards. Without this, `"top": true` would load as 1.0."""
        self._brand(tmp_path, monkeypatch, {"top": True})
        with pytest.raises(profile.ProfileError, match="bands.top must be a number"):
            profile.load("chan/event")

    def test_a_string_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": "0.5"})
        with pytest.raises(profile.ProfileError, match="bands.top must be a number"):
            profile.load("chan/event")

    def test_a_value_above_one_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"top": 1.5})
        with pytest.raises(profile.ProfileError, match="bands.top must be between 0 and 1"):
            profile.load("chan/event")

    def test_a_negative_value_is_a_reported_defect(self, monkeypatch, tmp_path):
        self._brand(tmp_path, monkeypatch, {"bottom": -0.1})
        with pytest.raises(profile.ProfileError, match="bands.bottom must be between 0 and 1"):
            profile.load("chan/event")

    def test_every_defect_is_reported_together(self, monkeypatch, tmp_path):
        """profile collects all defects rather than stopping at the first -
        someone typing a profile should not need one run per typo."""
        self._brand(tmp_path, monkeypatch, {"top": 2, "bottom": "x", "middle": 1})
        with pytest.raises(profile.ProfileError) as caught:
            profile.load("chan/event")
        message = str(caught.value)
        assert "bands.top" in message
        assert "bands.bottom" in message
        assert "bands.middle" in message

    def test_an_event_overrides_the_channel_band(self, monkeypatch, tmp_path):
        channel_dir = self._brand(tmp_path, monkeypatch, {"top": 0.5, "bottom": 0.5})
        event_dir = channel_dir / "events" / "event"
        (event_dir / "brand.json").write_text(
            json.dumps({"bands": {"top": 0.0}}), encoding="utf-8")
        # deep_merge is per key: the event's top wins, the channel's bottom
        # survives untouched.
        assert profile.load("chan/event").config["bands"] == {"top": 0.0, "bottom": 0.5}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_overlay.py::TestBandOpacities tests/test_profile.py::TestBandValidation -q
```
Expected: FAIL — `AttributeError: module 'yt_shorts.overlay' has no attribute 'band_opacities'`.

- [ ] **Step 3: Add the constants and the normaliser to `overlay.py`**

Directly below `ALPHA_OPAQUE = 255`:

```python
# The two bands whose surface opacity a brand may scale, and the factor that
# means "exactly as this tool has always drawn it".
BAND_KEYS = ("top", "bottom")
FULL_STRENGTH = 1.0


def band_opacities(config: dict) -> dict[str, float]:
    """The two band opacity factors, defaulted and coerced to float.

    An absent 'bands' section, an absent key inside it and the value 1.0 all
    mean the same thing - full strength - so a brand.json written before
    this feature existed renders identically with no migration.

    Deliberately TOLERANT of a malformed section rather than raising: the
    real check is profile._validate_brand, which reports every bad value as
    a profile defect before a profile is ever loaded. But build_overlay is
    also called on an UNSAVED brand by the studio's live preview, where a
    half-typed value must degrade to full strength rather than 500 the
    preview route.
    """
    raw = config.get("bands")
    values = raw if isinstance(raw, dict) else {}
    result: dict[str, float] = {}
    for key in BAND_KEYS:
        value = values.get(key)
        result[key] = FULL_STRENGTH if not isinstance(value, (int, float)) or isinstance(
            value, bool) else float(value)
    return result
```

- [ ] **Step 4: Add validation to `profile._validate_brand`**

In `src/yt_shorts/profile.py`, add `band_opacities` and `BAND_KEYS` to the existing overlay import at line 57:

```python
from .overlay import BAND_KEYS, LOGO_POSITIONS, band_opacities, caption_geometry, validate_caption_box
```

Then, inside `_validate_brand`, immediately before its final `return problems`:

```python
    # 'bands' is OPTIONAL at every layer: an absent section, an absent key
    # and 1.0 are the same request, so nothing written before this feature
    # existed becomes invalid. Only a PRESENT value is checked.
    bands = config.get("bands")
    if bands is not None:
        if not isinstance(bands, dict):
            problems.append(
                f"{path.name}: 'bands' must be an object with 'top'/'bottom', "
                f"got {bands!r}")
        else:
            for key in bands:
                if key not in BAND_KEYS:
                    problems.append(
                        f"{path.name}: unknown band 'bands.{key}' "
                        f"(expected {' or '.join(BAND_KEYS)})")
            for key in BAND_KEYS:
                if key not in bands:
                    continue
                value = bands[key]
                # bool before number: True is an int in Python, so
                # `"top": true` would otherwise load as full strength.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    problems.append(
                        f"{path.name}: bands.{key} must be a number between 0 and 1, "
                        f"got {value!r}")
                elif not 0.0 <= float(value) <= 1.0:
                    problems.append(
                        f"{path.name}: bands.{key} must be between 0 and 1, "
                        f"got {value!r}")
```

- [ ] **Step 5: Normalise in `load`**

In `profile.load`, inside the `if problems:`/`raise` block's successor region — specifically between the `raise ProfileError(...)` block and the `decorate = _load_decorate(...)` line — add:

```python
    # AFTER validation, never before: band_opacities coerces with float(),
    # which would raise a raw TypeError on the very values _validate_brand
    # exists to report as a readable profile defect.
    config["bands"] = band_opacities(config)
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_overlay.py tests/test_profile.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 7: Run the pinned hashes**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS — this task adds a config key and draws nothing differently.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/overlay.py src/yt_shorts/profile.py tests/test_overlay.py tests/test_profile.py
git commit -m "feat(brand): optional bands section with per-band opacity, validated and normalised"
```

---

### Task 2: The bands actually fade

**Files:**
- Modify: `src/yt_shorts/overlay.py` (`build_overlay`, new `_fade_bands`)
- Test: `tests/test_overlay.py`

**Interfaces:**
- Consumes: `overlay.band_opacities`, `overlay.BAND_KEYS` from Task 1.
- Produces: nothing new in the public surface — `build_overlay(hook, footer, config)` keeps its signature, as `CLAUDE.md` requires.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_overlay.py`. These measure pixels, which is this suite's established style — the dimension-only assertions are what once let the SAR bug through.

```python
class TestBandFading:
    """Uses this module's existing `config` fixture (the loaded erf profile),
    never a hand-built dict: a fading rule proven only against a synthetic
    config would not be proven against the real one, which carries a
    layout.py decoration and an accent_offset."""

    def _with(self, config, bands):
        """A COPY of the fixture's config carrying a bands section. A copy so
        one test can render the same profile both faded and unfaded without
        the first render's mutation reaching the second."""
        return {**config, "bands": bands}

    def _alpha(self, image, x, y):
        return image.getpixel((x, y))[3]

    def test_full_strength_leaves_the_veil_at_its_usual_alpha(self, config):
        image = build_overlay("HOOK", "footer", config)
        assert self._alpha(image, 40, 40) == overlay.ALPHA_BASE

    def test_zero_removes_the_veil_from_that_band_only(self, config):
        window_bottom = config["output"]["video_y"] + config["output"]["video_height"]
        image = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        assert self._alpha(image, 40, 40) == 0
        assert self._alpha(image, 40, window_bottom + 40) == overlay.ALPHA_BASE

    def test_zero_removes_the_edge_accent_too(self, config):
        """The edge is a surface, not content - decision 3 of the spec."""
        window_top = config["output"]["video_y"]
        full = build_overlay("HOOK", "footer", config)
        faded = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        assert self._alpha(full, 40, window_top - 3) == overlay.ALPHA_OPAQUE
        assert self._alpha(faded, 40, window_top - 3) == 0

    def test_a_half_factor_halves_every_surface_in_that_band(self, config):
        window_top = config["output"]["video_y"]
        faded = build_overlay("HOOK", "footer", self._with(config, {"top": 0.5}))
        assert self._alpha(faded, 40, 40) == int(overlay.ALPHA_BASE * 0.5)
        assert self._alpha(faded, 40, window_top - 3) == int(overlay.ALPHA_OPAQUE * 0.5)

    def test_the_hook_survives_a_zero_band(self, config):
        """Decision 2: the surfaces fade, the content does not. Without this
        the feature would silently delete the hook along with the veil."""
        window_top = config["output"]["video_y"]
        image = build_overlay("HOOK", "footer", self._with(config, {"top": 0.0}))
        band = image.crop((0, 0, image.width, window_top))
        assert max(pixel[3] for pixel in band.getdata()) == 255

    def test_the_footer_survives_a_zero_band(self, config):
        window_bottom = config["output"]["video_y"] + config["output"]["video_height"]
        image = build_overlay("HOOK", "footer", self._with(config, {"bottom": 0.0}))
        band = image.crop((0, window_bottom, image.width, image.height))
        assert max(pixel[3] for pixel in band.getdata()) == 255

    def test_the_video_window_stays_fully_transparent_at_every_factor(self, config):
        """The one invariant that must never bend: the window is where the
        burned-in timing tower and leaderboard live."""
        window_top = config["output"]["video_y"]
        for bands in ({"top": 1.0, "bottom": 1.0}, {"top": 0.0, "bottom": 0.0},
                      {"top": 0.5, "bottom": 0.25}):
            image = build_overlay("HOOK", "footer", self._with(config, bands))
            assert self._alpha(image, 540, window_top + 10) == 0, bands

    def test_a_decoration_fades_with_its_band(self, config):
        """The reason the factor is applied to the image rather than passed
        into decorate(): a channel's own layout.py never learns about bands,
        and must not have to. The injected decoration replaces the erf
        fixture's own so the assertion can name an exact pixel."""
        def decorate(draw, _config, _window_top, _window_bottom):
            draw.rectangle([0, 100, 200, 140], fill=(255, 0, 0, 255))

        faded = {**self._with(config, {"top": 0.5}), "decorate": decorate}
        image = build_overlay("HOOK", "footer", faded)
        assert self._alpha(image, 50, 120) == int(255 * 0.5)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_overlay.py::TestBandFading -q
```
Expected: FAIL — every fading assertion, because nothing scales the alpha yet.

- [ ] **Step 3: Implement `_fade_bands`**

Add to `src/yt_shorts/overlay.py`, directly above `build_overlay`:

```python
def _fade_bands(image: Image.Image, bands: dict, window_top: int, window_bottom: int) -> None:
    """Scales the alpha of everything drawn so far, per band, in place.

    Called at exactly ONE point in build_overlay: after the veil, the channel
    decoration and the edge accents, and BEFORE the logo, hook and footer.
    That position is the whole design. At this moment the image holds nothing
    but the band SURFACES, so scaling its alpha scales precisely them -
    including a decoration this module has never seen, since
    config["decorate"] comes from the channel's own layout.py. Passing a
    factor into that call instead would push the responsibility into every
    channel's layout.py, where it would be silently forgotten by the next one
    written.

    A factor of 1.0 is SKIPPED, not applied as an identity multiply, so the
    default path produces the exact bytes it produced before this feature
    existed - see tests/test_event_layer_no_regression.py's six pinned
    hashes, which must never be re-pinned.

    The video window's own rows are never touched: they are alpha 0 and must
    stay exactly that, or the sharp picture would be veiled - the one thing
    this format exists to preserve.
    """
    width, height = image.size
    for key, top, bottom in (("top", 0, window_top), ("bottom", window_bottom, height)):
        factor = bands[key]
        if factor >= FULL_STRENGTH or bottom <= top:
            continue
        region = image.crop((0, top, width, bottom))
        # A 256-entry lookup table rather than a lambda: point() takes either,
        # and a table avoids defining a closure over the loop variable.
        table = [int(value * factor) for value in range(256)]
        region.putalpha(region.getchannel("A").point(table))
        image.paste(region, (0, top))
```

- [ ] **Step 4: Call it from `build_overlay`**

In `build_overlay`, between the two edge-accent rectangles and the `logo_reserved, footer_logo = _place_logo(image, config)` line, insert:

```python
    # Everything drawn above is a SURFACE; everything below is content. This
    # is the seam the band opacity acts on - see _fade_bands.
    _fade_bands(image, band_opacities(config), window_top, window_bottom)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_overlay.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 6: Prove the default path is byte-identical**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS, unchanged. If it fails, the bug is in this task — do NOT re-pin the hashes.

- [ ] **Step 7: Mutation-check the skip**

Temporarily change `if factor >= FULL_STRENGTH or bottom <= top:` to `if bottom <= top:` (so 1.0 goes through the multiply), re-run `tests/test_event_layer_no_regression.py`, and record in your report whether the hashes still match. Then revert. This measures whether the skip is load-bearing or merely defensive; report the real answer either way.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/overlay.py tests/test_overlay.py
git commit -m "feat(overlay): scale band surface alpha, leaving hook, footer and logo at full strength"
```

---

### Task 3: Palette derivation

**Files:**
- Create: `src/yt_shorts/palette.py`
- Test: `tests/test_palette.py`

**Interfaces:**
- Produces:
  - `palette.PaletteError` with `.kind` in `{"not_found", "unreadable", "empty"}`
  - `palette.Swatch` — frozen dataclass, fields `hex: str`, `share: float`
  - `palette.swatches(path) -> list[Swatch]`, ordered by share, descending
  - `palette.derive(path) -> tuple[dict[str, str], list[Swatch]]` — the proposed roles and the swatches they came from

- [ ] **Step 1: Write the failing tests**

Create `tests/test_palette.py`:

```python
"""Palette derivation from a channel logo.

Every fixture here is generated in-process rather than committed: the rules
under test are about colour relationships, and a synthetic image states the
relationship it is testing far more legibly than a PNG nobody can inspect in
a diff.
"""

from __future__ import annotations

import pytest
from PIL import Image

from yt_shorts import palette


def _logo(tmp_path, blocks, size=(100, 100), name="logo.png"):
    """An RGBA image built from (colour, pixel count) blocks, padded with
    fully transparent pixels - the shape of a real logo, which is mostly
    padding."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = image.load()
    x = y = 0
    for colour, count in blocks:
        for _ in range(count):
            pixels[x, y] = colour
            x += 1
            if x >= size[0]:
                x = 0
                y += 1
    path = tmp_path / name
    image.save(path)
    return path


class TestSwatches:
    def test_shares_are_of_opaque_pixels_only(self, tmp_path):
        """A logo is mostly transparent padding. Counting those pixels would
        make every logo's dominant colour the same one, so the shares must
        add up over the OPAQUE pixels alone."""
        path = _logo(tmp_path, [((0, 0, 0, 255), 300), ((255, 255, 255, 255), 100)])
        found = palette.swatches(path)
        total = sum(s.share for s in found)
        assert 0.99 <= total <= 1.01
        assert found[0].share == pytest.approx(0.75, abs=0.02)

    def test_ordered_by_share_descending(self, tmp_path):
        path = _logo(tmp_path, [((10, 10, 10, 255), 100), ((200, 30, 30, 255), 400)])
        found = palette.swatches(path)
        assert [s.share for s in found] == sorted((s.share for s in found), reverse=True)

    def test_a_semi_transparent_pixel_is_excluded(self, tmp_path):
        """Anti-aliased edges carry the blend of two colours and belong to
        neither."""
        path = _logo(tmp_path, [((0, 0, 0, 255), 200), ((255, 0, 0, 120), 200)])
        found = palette.swatches(path)
        assert all(s.hex != "#FF0000" for s in found)

    def test_a_missing_file_is_not_found(self, tmp_path):
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(tmp_path / "nope.png")
        assert caught.value.kind == "not_found"

    def test_a_non_image_is_unreadable(self, tmp_path):
        path = tmp_path / "logo.png"
        path.write_text("not a png", encoding="utf-8")
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(path)
        assert caught.value.kind == "unreadable"

    def test_a_fully_transparent_image_is_empty(self, tmp_path):
        path = _logo(tmp_path, [])
        with pytest.raises(palette.PaletteError) as caught:
            palette.swatches(path)
        assert caught.value.kind == "empty"


class TestDerive:
    def test_base_is_the_dark_colour_and_edge_the_bright_one(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["base"] == "#050505"
        assert roles["edge"] == "#DC1E14"

    def test_text_is_white_on_a_dark_base(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["text"] == palette.LIGHT_TEXT

    def test_text_is_dark_on_a_light_base(self, tmp_path):
        """A light mark on nothing else - the veil ends up light, so white
        text would be unreadable on it."""
        path = _logo(tmp_path, [((240, 235, 225, 255), 500), ((250, 248, 245, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["text"] == palette.DARK_TEXT

    def test_a_rare_dark_speck_does_not_become_the_base(self, tmp_path):
        """MIN_BASE_SHARE exists for exactly this: a handful of near-black
        anti-aliasing pixels on an otherwise light mark must not be chosen as
        the channel's ground colour."""
        path = _logo(tmp_path, [((250, 250, 250, 255), 980), ((0, 0, 0, 255), 20)])
        roles, _ = palette.derive(path)
        assert roles["base"] != "#000000"

    def test_edge_is_never_the_base(self, tmp_path):
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["edge"] != roles["base"]

    def test_a_greyscale_logo_still_yields_a_visible_edge(self, tmp_path):
        """With no saturated colour to pick, the brightest swatch is the one
        that reads against a dark base. Returning the base again would give
        the operator an invisible frame."""
        path = _logo(tmp_path, [((20, 20, 20, 255), 500), ((230, 230, 230, 255), 300)])
        roles, _ = palette.derive(path)
        assert roles["edge"] == "#E6E6E6"

    def test_a_single_colour_mark_proposes_no_edge_or_accent(self, tmp_path):
        """Spec: return what was found, do not invent colours."""
        path = _logo(tmp_path, [((17, 34, 51, 255), 600)])
        roles, found = palette.derive(path)
        assert set(roles) == {"base", "text"}
        assert len(found) == 1

    def test_the_swatches_are_returned_with_the_roles(self, tmp_path):
        """The editor renders them as clickable chips, so the proposal is a
        starting point rather than an automatic overwrite."""
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, found = palette.derive(path)
        assert roles["base"] in {s.hex for s in found}
        assert len(found) >= 2

    def test_every_role_is_a_valid_hex_colour(self, tmp_path):
        from PIL import ImageColor
        path = _logo(tmp_path, [((5, 5, 5, 255), 500), ((220, 30, 20, 255), 300)])
        roles, _ = palette.derive(path)
        for value in roles.values():
            ImageColor.getrgb(value)


class TestNoHeavyImports:
    def test_module_imports_no_web_framework_or_google(self):
        """CLAUDE.md's rule for every pure module, checked over the AST's
        import statements rather than the source text so the docstring stays
        free to NAME the constraint it upholds."""
        import ast
        import pathlib
        source = pathlib.Path(palette.__file__).read_text(encoding="utf-8")
        banned = {"fastapi", "starlette", "pydantic", "googleapiclient",
                  "google", "google_auth_oauthlib"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in banned, name
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_palette.py -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.palette'`.

- [ ] **Step 3: Implement `palette.py`**

Create `src/yt_shorts/palette.py`:

```python
"""Proposes a brand palette from a channel's logo.

Pure and stdlib-plus-Pillow only, like pathnames.py and upload_policy.py: no
FastAPI, no google, no import from the studio package. The studio calls IN
(GET /api/channels/{channel}/brand/palette), never the reverse.

What this module is FOR: four of the five channels in the operator's
workspace shipped byte-identical placeholder colours, two of which were
another channel's green. Deriving from the logo makes the common case one
click instead of four eyedropper trips, and makes it repeatable for the next
channel.

What it deliberately is NOT: an automatic overwrite. `derive` returns a
proposal AND the swatches it came from; the editor fills its colour fields
and renders the swatches as chips, and nothing is written until the operator
saves. A palette is a taste decision with a measurable starting point, not a
computation with one right answer.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Pixels below this alpha are dropped before anything is measured. A logo is
# mostly transparent padding, and its glyph edges are anti-aliased blends of
# two colours that belong to neither - counting either would bias every
# result toward whatever the mark sits on.
MIN_ALPHA = 200

# Quantisation target. Eight is enough to separate a mark's real colours
# without splitting one flat fill into near-identical neighbours.
MAX_SWATCHES = 8

# The smallest share a swatch may hold and still be considered for `base`.
# Without it a handful of near-black anti-aliasing survivors on an otherwise
# light mark would be chosen as the channel's ground colour.
MIN_BASE_SHARE = 0.05

LIGHT_TEXT = "#FFFFFF"
DARK_TEXT = "#111111"


class PaletteError(Exception):
    """kind: not_found | unreadable | empty. HTTP: everything -> 409, the
    same way the brand preview route reports a render it cannot perform."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Swatch:
    """One colour the logo actually contains, and how much of it there is.

    `share` is of the OPAQUE pixels only, so it answers "how much of the mark
    is this colour", not "how much of the file"."""
    hex: str
    share: float


def _hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(rgb)


def _rgb(value: str) -> tuple[int, int, int]:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def _luminance(value: str) -> float:
    """WCAG relative luminance - the perceptual measure, not a naive mean.
    Green contributes far more to how light a colour looks than blue does,
    which a mean would ignore and then pick unreadable text for."""
    channels = []
    for raw in _rgb(value):
        c = raw / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _saturation(value: str) -> float:
    r, g, b = (channel / 255 for channel in _rgb(value))
    return colorsys.rgb_to_hls(r, g, b)[2]


def _contrast(a: str, b: str) -> float:
    first, second = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def swatches(path) -> list[Swatch]:
    """The logo's own colours, most prominent first.

    Raises PaletteError rather than returning an empty list for a file that
    is missing, unreadable, or has no opaque pixel at all - each is a
    different thing for the operator to fix, and the route reports which.
    """
    path = Path(path)
    if not path.is_file():
        raise PaletteError(f"no logo file at {path.name}", kind="not_found")
    try:
        with Image.open(path) as opened:
            image = opened.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise PaletteError(f"{path.name} is not a readable image", kind="unreadable") from error

    opaque = [pixel[:3] for pixel in image.getdata() if pixel[3] >= MIN_ALPHA]
    if not opaque:
        raise PaletteError(f"{path.name} has no opaque pixels", kind="empty")

    strip = Image.new("RGB", (len(opaque), 1))
    strip.putdata(opaque)
    quantised = strip.quantize(colors=MAX_SWATCHES, method=Image.Quantize.FASTOCTREE)
    table = quantised.getpalette()
    total = len(opaque)
    found = [
        Swatch(hex=_hex(table[index * 3:index * 3 + 3]), share=count / total)
        for count, index in quantised.getcolors()
    ]
    found.sort(key=lambda swatch: swatch.share, reverse=True)
    return found


def derive(path) -> tuple[dict[str, str], list[Swatch]]:
    """A proposed {text, base, accent, edge} plus the swatches behind it.

    The assignment rules, and why each is what it is:

    - `base` is the DARKEST swatch holding at least MIN_BASE_SHARE. The veil
      sits behind white hook text, so darkness outranks prominence: a light
      base is a legibility bug, not a style choice.
    - `edge` is the most SATURATED swatch that is not the base - the crispest
      brand element, the one that reads as "this channel's colour". With
      nothing saturated to pick (a greyscale mark) it becomes the brightest
      other swatch, because an edge the colour of the base is an invisible
      frame.
    - `accent` is the next most saturated after those two, else a darkened
      edge. Only channel decorations use it, so a reasonable fallback beats
      failing.
    - `text` is whichever of LIGHT_TEXT/DARK_TEXT has more contrast against
      the chosen base.

    A mark with only ONE swatch yields only `base` and `text`: the caller
    fills what it was given and leaves the rest alone, rather than being
    handed invented colours it cannot tell apart from measured ones.
    """
    found = swatches(path)
    candidates = [s for s in found if s.share >= MIN_BASE_SHARE] or found
    base = min(candidates, key=lambda swatch: _luminance(swatch.hex)).hex

    roles = {"base": base}
    roles["text"] = max(
        (LIGHT_TEXT, DARK_TEXT), key=lambda candidate: _contrast(candidate, base))

    others = [s for s in found if s.hex != base]
    if not others:
        return roles, found

    coloured = sorted(others, key=lambda swatch: _saturation(swatch.hex), reverse=True)
    edge = coloured[0].hex
    if _saturation(edge) < 0.05:
        edge = max(others, key=lambda swatch: _luminance(swatch.hex)).hex
    roles["edge"] = edge

    remaining = [s for s in coloured if s.hex != edge]
    if remaining:
        roles["accent"] = remaining[0].hex
    else:
        roles["accent"] = _hex(tuple(int(channel * 0.6) for channel in _rgb(edge)))
    return roles, found
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_palette.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 5: Measure it against the operator's five real logos**

```bash
PYTHONPATH=src .venv/bin/python -c "
import pathlib
from yt_shorts import palette
root = pathlib.Path.home() / 'YT-Shorts-Data/channels'
for channel in sorted(p.name for p in root.iterdir() if p.is_dir()):
    logo = root / channel / 'assets/logo.png'
    if not logo.is_file():
        continue
    roles, found = palette.derive(logo)
    print(f'{channel:26} {roles}')
"
```
This is a READ of the operator's workspace; it writes nothing. Paste the real output into your report. A result that looks obviously wrong for a channel is a finding to report, not something to force with a special case.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/palette.py tests/test_palette.py
git commit -m "feat(palette): propose brand colours from a channel logo"
```

---

### Task 4: The studio routes

**Files:**
- Modify: `src/yt_shorts/brand_admin.py:84` (the editable-section tuple)
- Modify: `src/yt_shorts/event_brand_admin.py:18` (`OVERRIDE_SECTIONS`)
- Modify: `src/yt_shorts/studio/api.py` (`BrandPatchBody`, both preview routes, the new palette route, the route-list docstring)
- Test: `tests/test_brand_admin.py`, `tests/test_event_brand_admin.py`, `tests/test_studio_brand_api.py`

**Interfaces:**
- Consumes: `palette.derive`, `palette.PaletteError` from Task 3.
- Produces: `GET /api/channels/{channel}/brand/palette` → `{"colors": {...}, "swatches": [{"hex", "share"}, ...]}`; `bands` accepted by `PUT …/brand`, `POST …/brand/preview`, `PUT …/events/{event}/brand` and `POST …/events/{event}/brand/preview`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brand_admin.py`:

```python
class TestBands:
    def test_a_valid_bands_section_is_stored(self, channels_dir):
        brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": 0.5, "bottom": 0.0}})
        brand = brand_admin.read_brand(channels_dir, "erf")
        assert brand["bands"] == {"top": 0.5, "bottom": 0.0}

    def test_an_out_of_range_band_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError) as caught:
            brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": 4}})
        assert caught.value.kind == "bad_brand"

    def test_a_bool_band_is_refused(self, channels_dir):
        with pytest.raises(brand_admin.BrandAdminError):
            brand_admin.update_brand(channels_dir, "erf", {"bands": {"top": True}})
```

Append to `tests/test_event_brand_admin.py`:

```python
class TestBandsOverride:
    def test_an_event_may_override_bands(self, channels_dir):
        event_brand_admin.update_event_brand(
            channels_dir, "erf", "community-clips-back-catalogue",
            {"bands": {"top": 0.0, "bottom": 1.0}})
        state = event_brand_admin.read_event_brand(
            channels_dir, "erf", "community-clips-back-catalogue")
        assert state["override"]["bands"] == {"top": 0.0, "bottom": 1.0}
        assert state["effective"]["bands"] == {"top": 0.0, "bottom": 1.0}

    def test_an_invalid_override_is_refused(self, channels_dir):
        with pytest.raises(event_brand_admin.EventBrandError):
            event_brand_admin.update_event_brand(
                channels_dir, "erf", "community-clips-back-catalogue",
                {"bands": {"top": -1}})
```

Append to `tests/test_studio_brand_api.py`:

```python
class TestPaletteRoute:
    def test_returns_roles_and_swatches(self, client):
        body = client.get("/api/channels/erf/brand/palette").json()
        assert "base" in body["colors"]
        assert body["swatches"]
        assert set(body["swatches"][0]) == {"hex", "share"}

    def test_every_returned_colour_is_a_hex_string(self, client):
        body = client.get("/api/channels/erf/brand/palette").json()
        for value in body["colors"].values():
            assert value.startswith("#") and len(value) == 7

    def test_a_channel_with_no_logo_is_409(self, client, channels_dir):
        """A read that cannot be performed, reported the way the brand
        preview route reports the same class of failure."""
        import json
        path = channels_dir / "erf" / "brand.json"
        brand = json.loads(path.read_text(encoding="utf-8"))
        brand.pop("logo", None)
        path.write_text(json.dumps(brand), encoding="utf-8")
        assert client.get("/api/channels/erf/brand/palette").status_code == 409

    def test_an_unsafe_channel_segment_is_refused(self, client):
        assert client.get("/api/channels/..%2F../brand/palette").status_code in (400, 404)

    def test_an_unknown_channel_is_404(self, client):
        assert client.get("/api/channels/nope/brand/palette").status_code == 404


class TestBandsThroughTheRoutes:
    def test_put_stores_bands(self, client):
        response = client.put("/api/channels/erf/brand", json={"bands": {"top": 0.25}})
        assert response.status_code == 200
        assert response.json()["brand"]["bands"]["top"] == 0.25

    def test_put_refuses_an_out_of_range_band(self, client):
        assert client.put(
            "/api/channels/erf/brand", json={"bands": {"top": 9}}).status_code == 400

    def test_preview_accepts_bands(self, client):
        """The preview must honour an UNSAVED band value, or the slider
        would show the operator the old picture while they drag it."""
        assert client.post(
            "/api/channels/erf/brand/preview",
            json={"bands": {"top": 0.0}}).status_code == 200
```

If `tests/test_studio_brand_api.py` does not exist under that name, append these classes to whichever existing test module already covers `PUT /api/channels/{channel}/brand`, and say in your report which file you chose.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_brand_admin.py tests/test_event_brand_admin.py -q
```
Expected: FAIL — `'bands' cannot be overridden at the event level`, and the channel patch silently dropping the section.

- [ ] **Step 3: Let the admin modules carry the section**

In `src/yt_shorts/brand_admin.py`, extend the editable-section tuple at line 84:

```python
    for key in ("colors", "fonts", "subtitles", "logo", "output", "upload", "bands"):
```

In `src/yt_shorts/event_brand_admin.py`, extend `OVERRIDE_SECTIONS`:

```python
OVERRIDE_SECTIONS = ("colors", "fonts", "logo", "output", "subtitles", "bands")
```

Neither module needs its own band validation: both already delegate to
`profile._validate_brand`, which gained the rules in Task 1. Do not add a
second copy — a divergent duplicate is exactly what that delegation exists to
prevent.

- [ ] **Step 4: Let the routes carry it**

In `src/yt_shorts/studio/api.py`, add to `BrandPatchBody` (line 233):

```python
    bands: dict | None = None
```

and add `"bands"` to the section tuple in BOTH preview routes (`brand_preview` at line ~691 and the event preview at line ~797):

```python
        for key in ("colors", "fonts", "subtitles", "logo", "output", "bands"):
```

- [ ] **Step 5: Add the palette route**

Beside the other channel brand routes in `src/yt_shorts/studio/api.py`, and BEFORE the SPA fallback like every other `/api` route:

```python
    @app.get(CH + "/brand/palette")
    def brand_palette(channel: str):
        """The channel logo's own colours, and a proposed role for each.

        A READ: it opens the logo the brand already names and writes nothing.
        409 rather than 500 when the brand names no logo or the file cannot be
        opened - the same contract POST .../brand/preview uses for a render it
        cannot perform, because both are "your brand is not ready for this
        yet", not "the server is broken"."""
        from .. import palette
        from ..profile import _resolve_logo
        try:
            brand = brand_admin.read_brand(channels_dir, channel)
        except brand_admin.BrandAdminError as error:
            raise HTTPException(status_code=_brand_status(error), detail=str(error)) from error
        base = channels_dir / channel
        resolved = dict(brand)
        _resolve_logo(resolved, base, base)
        logo = resolved.get("logo") or {}
        if not logo.get("file"):
            raise HTTPException(
                status_code=409, detail="this channel's brand names no logo")
        try:
            roles, found = palette.derive(logo["file"])
        except palette.PaletteError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"colors": roles,
                "swatches": [{"hex": s.hex, "share": s.share} for s in found]}
```

Add one line to the module's route-list docstring beside the other brand routes:

```
  GET   /api/channels/{channel}/brand/palette          colours proposed from the channel logo
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_brand_admin.py tests/test_event_brand_admin.py tests/test_studio_brand_api.py tests/test_studio_api.py -q
python3 tools/lint.py
```
Expected: all pass; `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/brand_admin.py src/yt_shorts/event_brand_admin.py src/yt_shorts/studio/api.py tests/
git commit -m "feat(studio): bands through both brand PUTs and previews; GET brand/palette"
```

---

### Task 5: Frontend client and shared form shaping

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Modify: `src/yt_shorts/studio/web/src/brandForm.ts`
- Modify: `src/yt_shorts/studio/web/src/eventBrand.ts`
- Test: `src/yt_shorts/studio/web/src/eventBrand.test.ts`, `src/yt_shorts/studio/web/src/brandForm.test.ts`

**Interfaces:**
- Produces:
  - `BrandBands { top: number; bottom: number }`
  - `BrandPatch.bands?: BrandBands`
  - `PaletteResponse { colors: Record<string, string>; swatches: { hex: string; share: number }[] }`
  - `getPalette(channel: string): Promise<PaletteResponse>`
  - `BrandEditorForm.bands: BrandBands`, filled by `formFromBrand`
  - `BAND_FIELDS: { key: 'top' | 'bottom'; label: string }[]`
  - `SECTIONS` gains `'bands'`

- [ ] **Step 1: Write the failing tests**

Append to `src/yt_shorts/studio/web/src/eventBrand.test.ts`:

```ts
describe('bands as an overridable section', () => {
  it('is one of the sections', () => {
    expect(SECTIONS).toContain('bands')
  })

  it('reports bands as overridden when the event stores it', () => {
    expect(overriddenSections({ bands: { top: 0, bottom: 1 } })).toContain('bands')
  })

  it('carries the whole bands section in the payload', () => {
    const payload = buildOverridePayload(
      { colors: {}, bands: { top: 0.5, bottom: 1 } },
      new Set(['bands']),
    )
    expect(payload).toEqual({ bands: { top: 0.5, bottom: 1 } })
  })

  it('omits bands entirely when inherited', () => {
    const payload = buildOverridePayload(
      { bands: { top: 0.5, bottom: 1 } },
      new Set(['colors']),
    )
    expect(payload).not.toHaveProperty('bands')
  })
})
```

Create `src/yt_shorts/studio/web/src/brandForm.test.ts` if it does not exist, and append:

```ts
import { describe, expect, it } from 'vitest'
import { formFromBrand } from './brandForm'

describe('formFromBrand bands', () => {
  it('defaults a missing section to full strength', () => {
    expect(formFromBrand({}).bands).toEqual({ top: 1, bottom: 1 })
  })

  it('defaults a missing key to full strength', () => {
    expect(formFromBrand({ bands: { top: 0.25 } }).bands).toEqual({ top: 0.25, bottom: 1 })
  })

  it('ignores a malformed value rather than rendering NaN', () => {
    expect(formFromBrand({ bands: { top: 'x' } }).bands).toEqual({ top: 1, bottom: 1 })
  })

  it('reads both values', () => {
    expect(formFromBrand({ bands: { top: 0, bottom: 0.5 } }).bands).toEqual({
      top: 0,
      bottom: 0.5,
    })
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd src/yt_shorts/studio/web && npm test -- brandForm eventBrand && cd -
```
Expected: FAIL — `bands` is not in `SECTIONS` and `formFromBrand` returns no `bands`.

- [ ] **Step 3: Extend `api.ts`**

Add beside `BrandLogo`/`BrandOutput`:

```ts
/** The opacity of the overlay's two band SURFACES - the veil, the channel
 * decoration and the edge accents (see overlay._fade_bands). 1 is exactly
 * how this tool has always drawn them; 0 leaves the blurred backdrop alone,
 * with the hook, footer and logo still on top at full strength. Optional at
 * every layer: an absent section and 1 are the same request. */
export interface BrandBands {
  top: number
  bottom: number
}

/** GET /api/channels/{channel}/brand/palette (see api.py's brand_palette) -
 * colours proposed from the channel's logo, plus the swatches they came
 * from so the editor can offer them as chips. A proposal, never a write:
 * nothing changes until the operator saves. 409 when the brand names no
 * logo or the file cannot be read. */
export interface PaletteResponse {
  colors: Record<string, string>
  swatches: { hex: string; share: number }[]
}

export function getPalette(channel: string): Promise<PaletteResponse> {
  return fetch(`${channelBase(channel)}/brand/palette`).then(asJson<PaletteResponse>)
}
```

Add to `BrandPatch`:

```ts
  bands?: BrandBands
```

- [ ] **Step 4: Extend `brandForm.ts`**

Add the import of `BrandBands` from `./api`, add the field to `BrandEditorForm`:

```ts
  bands: BrandBands
```

add the label table beside `COLOR_FIELDS`:

```ts
/** The two band-opacity sliders, in display order. Labelled by where they
 * are on the picture rather than by their key, because "top" alone reads as
 * a position in the form rather than a third of the frame. */
export const BAND_FIELDS: { key: 'top' | 'bottom'; label: string }[] = [
  { key: 'top', label: 'Upper third' },
  { key: 'bottom', label: 'Lower third' },
]
```

and read it in `formFromBrand`, before the `return`:

```ts
  const bandsRaw = (brand.bands as Record<string, unknown>) ?? {}
  const band = (value: unknown) => (typeof value === 'number' ? value : 1)
  const bands: BrandBands = { top: band(bandsRaw.top), bottom: band(bandsRaw.bottom) }
```

and add `bands` to the returned object.

- [ ] **Step 5: Extend `eventBrand.ts`**

```ts
export const SECTIONS = ['colors', 'fonts', 'logo', 'output', 'subtitles', 'bands'] as const
```

- [ ] **Step 6: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass. Do NOT run `npm run build` — Task 8 owns the committed bundle.

- [ ] **Step 7: Commit**

```bash
git add src/yt_shorts/studio/web/src
git commit -m "feat(studio-web): band opacity and palette types in the shared brand client"
```

---

### Task 6: The channel Brand editor

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/BrandEditor.tsx`

**Interfaces:**
- Consumes: `getPalette`, `PaletteResponse`, `BrandBands`, `BAND_FIELDS`, `BrandEditorForm.bands` from Task 5.

- [ ] **Step 1: Add the band sliders**

Inside the existing Colors `Card`'s `Stack`, directly below the `COLOR_FIELDS.map(...)` block, add a divider and the two sliders:

```tsx
              <Divider my="xs" />
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Band opacity
              </Text>
              <Text size="xs" c="dimmed">
                How solid the upper and lower thirds are. At 0% only the clip's own
                blurred backdrop shows there — the hook, footer and logo stay.
              </Text>
              {BAND_FIELDS.map(({ key, label }) => (
                <Stack key={key} gap={2}>
                  <Text size="sm">{label}</Text>
                  <Slider
                    min={0}
                    max={1}
                    step={0.05}
                    value={form.bands[key]}
                    onChange={(value) => setBand(key, value)}
                    label={(value) => `${Math.round(value * 100)}%`}
                    marks={[
                      { value: 0, label: '0%' },
                      { value: 1, label: '100%' },
                    ]}
                  />
                </Stack>
              ))}
```

with the setter beside the other setters in the component:

```tsx
  function setBand(key: 'top' | 'bottom', value: number) {
    setForm((current) => ({ ...current, bands: { ...current.bands, [key]: value } }))
  }
```

`Slider` and `Divider` are already imported by this file; confirm rather than assume, and add them to the `@mantine/core` import if not.

- [ ] **Step 2: Send bands with the save and the preview**

Find every place this component builds a `BrandPatch` (the save handler and the debounced preview) and add `bands: form.bands` to each. If the two are built by one helper, change the helper once. Report which you found — a preview that ignores the sliders would show the operator the old picture while they drag.

- [ ] **Step 3: Add the derive button and the swatch chips**

Beside the Colors heading:

```tsx
  const [swatches, setSwatches] = useState<PaletteResponse['swatches']>([])
  const [deriving, setDeriving] = useState(false)
  const [paletteError, setPaletteError] = useState<string | null>(null)

  async function deriveFromLogo() {
    setDeriving(true)
    setPaletteError(null)
    try {
      const result = await getPalette(channel)
      setSwatches(result.swatches)
      // A PROPOSAL: it fills the fields and nothing more. Only the roles the
      // logo actually supports are set - a single-colour mark returns base
      // and text alone, and the other fields keep what the operator had
      // rather than being overwritten with an invented colour.
      setForm((current) => ({ ...current, colors: { ...current.colors, ...result.colors } }))
      notifications.show({
        message: 'Colours proposed from the logo. Nothing is saved yet.',
        color: 'green',
      })
    } catch (err) {
      setPaletteError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setDeriving(false)
    }
  }
```

rendered as:

```tsx
              <Group justify="space-between" align="center">
                <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                  Colors
                </Text>
                <Button size="xs" variant="light" loading={deriving} onClick={deriveFromLogo}>
                  Derive from logo
                </Button>
              </Group>
              {paletteError ? (
                <Alert color="red" title="Could not read the logo">
                  {paletteError}
                </Alert>
              ) : null}
              {swatches.length > 0 ? (
                <Group gap={6}>
                  {swatches.map((swatch) => (
                    <Tooltip key={swatch.hex} label={`${swatch.hex} · ${Math.round(swatch.share * 100)}%`}>
                      <Box
                        style={{
                          width: 28,
                          height: 28,
                          borderRadius: 4,
                          background: swatch.hex,
                          border: '1px solid rgba(128,128,128,0.4)',
                        }}
                      />
                    </Tooltip>
                  ))}
                </Group>
              ) : null}
```

- [ ] **Step 4: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass.

- [ ] **Step 5: Verify the pane still scrolls**

This adds a heading, a paragraph, a button, a chip row and two sliders to a pane that already has several cards. CLAUDE.md carries a standing requirement that every studio pane scroll to all of its elements at a short viewport. Trace the flex chain from this component's root to its scrolling region and confirm it still holds — exactly one `flex: 1 1 auto; minHeight: 0; overflowY: auto` region with every sibling `flex: 0 0 auto`, or, if this screen scrolls through its host instead, say which host and why nothing here breaks it. State in your report what you found and how you checked. You cannot check against a running studio: the committed bundle is rebuilt in Task 8.

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/BrandEditor.tsx
git commit -m "feat(studio-web): derive a palette from the logo, and two band-opacity sliders"
```

---

### Task 7: The event Brand editor

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/EventBrandEditor.tsx`

**Interfaces:**
- Consumes: `BAND_FIELDS`, `BrandEditorForm.bands`, `SECTIONS` (now including `'bands'`) from Task 5.

- [ ] **Step 1: Add the bands override section**

This editor already renders one card per overridable section with an override/inherit control. Add a `bands` card in exactly that shape — read how the neighbouring `subtitles` card does it and mirror it, since that is the simplest of the existing sections:

```tsx
        <Card padding="md">
          <Stack gap="sm">
            <Group justify="space-between" align="center">
              <Text fw={600} size="sm" tt="uppercase" c="dimmed">
                Band opacity
              </Text>
              <Switch
                label="Override"
                checked={overridden.has('bands')}
                onChange={(e) => toggleSection('bands', e.currentTarget.checked)}
                color="steel"
              />
            </Group>
            <Text size="xs" c="dimmed">
              How solid this event's upper and lower thirds are. At 0% only the clip's
              own blurred backdrop shows there — the hook, footer and logo stay.
            </Text>
            {BAND_FIELDS.map(({ key, label }) => (
              <Stack key={key} gap={2}>
                <Text size="sm">{label}</Text>
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  disabled={!overridden.has('bands')}
                  value={form.bands[key]}
                  onChange={(value) => setBand(key, value)}
                  label={(value) => `${Math.round(value * 100)}%`}
                  marks={[
                    { value: 0, label: '0%' },
                    { value: 1, label: '100%' },
                  ]}
                />
              </Stack>
            ))}
          </Stack>
        </Card>
```

with the setter beside this component's other setters:

```tsx
  function setBand(key: 'top' | 'bottom', value: number) {
    setForm((current) => ({ ...current, bands: { ...current.bands, [key]: value } }))
  }
```

Use whatever the existing section-toggle handler in this file is actually called — `toggleSection` above is a placeholder for it, and the real name must be read from the file, not assumed. Report the name you found.

- [ ] **Step 2: Confirm the payload and preview carry it**

`buildOverridePayload` (Task 5) already includes `bands` whenever it is in the overridden set, because it iterates `SECTIONS`. Verify by reading, then confirm the preview call for this editor also passes the section — a slider that does not move the preview is the same defect as in Task 6.

- [ ] **Step 3: Run every frontend gate**

```bash
cd src/yt_shorts/studio/web && npx tsc -b && npm run lint && npm test && cd -
```
Expected: tsc exit 0, oxlint clean, all Vitest pass.

- [ ] **Step 4: Verify the pane still scrolls**

Same standing requirement as Task 6, one more card in a pane that already has several. Trace the flex chain and state what you found.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/EventBrandEditor.tsx
git commit -m "feat(studio-web): per-event band opacity override"
```

---

### Task 8: Migration, docs, E2E, bundle, full verification

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `tests/test_studio_e2e.py`
- Modify: `src/yt_shorts/studio/static/**` (rebuilt)
- Workspace: `~/YT-Shorts-Data/channels/*/brand.json` — **the operator does this themselves; see Step 1**

- [ ] **Step 1: Print the proposals — but do not write to the workspace**

The migration itself is in scope and happens after this task, performed by the
controller together with the operator, who inspects each preview before it is
saved. It is not an implementer's step: a subagent cannot judge whether a
palette looks right, and `~/YT-Shorts-Data` is the operator's live data.

What this step delivers is the input for that conversation — what each channel
would be proposed:

```bash
PYTHONPATH=src .venv/bin/python -c "
import pathlib
from yt_shorts import palette
root = pathlib.Path.home() / 'YT-Shorts-Data/channels'
for channel in sorted(p.name for p in root.iterdir() if p.is_dir()):
    logo = root / channel / 'assets/logo.png'
    if logo.is_file():
        print(channel, palette.derive(logo)[0])
"
```
This is a read. Paste the real output into your report; the operator decides what to accept.

- [ ] **Step 1b: The migration itself (controller + operator, after this task)**

Present the derived palettes and a rendered overlay preview per channel, and
save only what the operator accepts. All five workspace channels are in scope
— including `erfofficial`, whose green was a first-pass choice from the
racecast NLS assets, correct while this was a single-channel tool and wrong
now (confirmed by the operator during brainstorming).

`tests/fixtures/channels/erf/brand.json` is **NOT** migrated. It is the
suite's own copy and the six pinned hashes are computed from it; changing it
breaks exactly the guard this feature is built around.

- [ ] **Step 2: Update `CLAUDE.md`**

In the Architecture section, directly after the paragraph beginning `**\`overlay.py\` knows nothing channel-specific.**`, add:

```markdown
**Band opacity is applied to the image, not passed into the drawing.**
`brand.json`'s optional `bands` (`{"top": 1.0, "bottom": 1.0}`, event-
overridable like every other section) scales the alpha of the overlay's two
band SURFACES - the veil, the channel decoration and the edge accents.
`overlay._fade_bands` does it at exactly one point in `build_overlay`: after
those three are drawn and BEFORE the logo, hook and footer, when the image
holds nothing but surfaces. That position is the design. Threading a factor
into `config["decorate"]` instead would push the responsibility into every
channel's `layout.py`, where the next one written would silently forget it -
whereas fading the image reaches a decoration this module has never seen.

A factor of `1.0` is SKIPPED rather than applied as an identity multiply.
Measured, not assumed: with the skip removed the six pinned hashes still
match, so Pillow's identity multiply happens to be lossless today - the skip
buys independence from that, not the byte-identity itself, and saves a crop
and a paste on every render. An absent `bands`, an absent key and `1.0` are
all the same request, which is why no existing profile needed migrating. The
video window's rows are never touched at any factor: they are alpha 0 and
must stay so.
`profile.load` normalises `config["bands"]` AFTER validation, because the
normaliser calls `float()` on values `_validate_brand` exists to report as
readable defects.

**`palette.py` proposes a channel palette from its logo** - quantise the
opaque pixels, then assign the darkest sufficiently-common swatch to `base`
(the veil sits behind white text, so darkness outranks prominence), the most
saturated other one to `edge`, and white or near-black to `text` by WCAG
contrast against the chosen base. It is pure and Pillow-only, like
`pathnames.py`; the studio calls in via `GET …/brand/palette` and never the
reverse. It is a PROPOSAL: the route writes nothing, the editor fills its
fields and offers the swatches as chips, and a mark with one colour yields
`base` and `text` alone rather than inventing the rest.
```

- [ ] **Step 3: Update `README.md`**

Find the brand/`brand.json` passage (search for `brand.json`) and add, in README's voice, a short paragraph: an optional `bands` section sets how solid the upper and lower thirds are (`1` as always, `0` for nothing but the clip's own blurred backdrop, with the hook and footer still on top), it can be set per channel and overridden per event, and the studio's Brand editor has a slider for each plus a "Derive from logo" button that proposes the four colours from the channel's own logo. Keep it to a paragraph — README documents the workflow, not the algorithm.

- [ ] **Step 4: Extend the E2E**

Append to `tests/test_studio_e2e.py` a test in the class that already covers the event brand editor (find it by searching for `EventBrandEditor` or `Event brand`; if none exists, add it beside the channel brand editor's own E2E class and say so in your report):

```python
    def test_an_event_overrides_band_opacity(
            self, studio_profile, event_dir, live_server, page):
        """The round trip this feature can most easily lose: a slider that
        does not reach the file, or a save that drops the section."""
        import json

        page.goto(f"{live_server}/erf/{event_dir.name}")
        page.get_by_role("tab", name="Brand").click()
        card = page.get_by_text("Band opacity", exact=True).locator("xpath=..")
        card.get_by_role("switch", name="Override").click()
        # Mantine renders a Slider as a role="slider" element; drive it with
        # the keyboard rather than a drag, which is both flaky and
        # resolution-dependent.
        upper = card.get_by_role("slider").first
        upper.click()
        for _ in range(20):
            upper.press("ArrowLeft")

        page.get_by_role("button", name="Save", exact=True).click()
        page.get_by_text("Saved.", exact=True).wait_for(timeout=5000)

        written = json.loads((event_dir / "brand.json").read_text(encoding="utf-8"))
        assert written["bands"]["top"] == 0
```

**Scope every locator to its card.** This file has already been caught once by a bare `.last` on a control name that two editors both render — it resolved to whichever had rendered, and the wrong one produced the very success message the test waited for. If a selector does not match the rendered DOM, fix the SELECTOR, never weaken the assertion, and report every selector you changed and why.

**Also add a CHANNEL-scope test, for a constraint nothing else guards.** The Task 6 review established by mutation that `bands: form.bands` can be deleted from `BrandEditor`'s `formToPatch` while `tsc`, oxlint and all 291 Vitest tests stay green — the project covers component behaviour by E2E rather than unit tests, so this E2E is the only place that constraint can live. Open a channel's Brand editor, capture the preview image, drag the "Upper third" slider to 0%, and assert the preview image CHANGES. A test that only asserts the slider moved would pass while the preview ignores it, which is exactly the defect this guards:

```python
    def test_a_channel_band_slider_reaches_the_preview(
            self, studio_profile, live_server, page):
        """The Task 6 review proved `bands` can be dropped from the editor's
        patch builder with every unit gate still green. This is the only
        guard: the picture itself must change when the slider moves."""
        page.goto(f"{live_server}/erf")
        page.get_by_role("tab", name="Brand").click()
        preview = page.get_by_role("img", name="Overlay preview")
        preview.wait_for(timeout=5000)
        before = preview.get_attribute("src")

        card = page.get_by_text("Band opacity", exact=True).locator("xpath=..")
        upper = card.get_by_role("slider").first
        upper.click()
        for _ in range(20):
            upper.press("ArrowLeft")

        # The preview is debounced, so poll rather than assert immediately.
        page.wait_for_function(
            "(old) => {"
            "  const img = document.querySelector('img[alt=\\"Overlay preview\\"]');"
            "  return img && img.src !== old;"
            "}",
            arg=before, timeout=5000)
```

Read the preview image's real accessible name and `alt` text out of `BrandEditor.tsx` rather than trusting `"Overlay preview"` above — if it differs, fix the selector to match the DOM and report what it actually is.

- [ ] **Step 5: Build and run every gate**

```bash
cd src/yt_shorts/studio/web && npm run lint && npm run build && npm test && npx tsc -b && cd -
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
```
Expected: oxlint clean, build exit 0, Vitest all pass, tsc exit 0, pytest ALL pass, `All checks passed!`. Build BEFORE pytest or the E2E exercises a stale page with no sliders in it. Paste the real output of each.

- [ ] **Step 6: Re-confirm the pinned hashes explicitly**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_event_layer_no_regression.py -q
```
Expected: PASS. Name this separately in your report even though Step 5 covers it — it is the guarantee the whole feature is built around.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md README.md tests/test_studio_e2e.py src/yt_shorts/studio/static
git commit -m "docs+build(brand): document band opacity and logo palettes; e2e; rebuild static"
```

- [ ] **Step 8: Operator smoke (not the implementer's job)**

Restart `bin/yt-shorts studio`, open a channel's Brand editor, press "Derive from logo", inspect the preview, adjust and save. Then open an event, override Band opacity, pull the upper third to 0% and confirm the preview shows the blurred backdrop with the hook still legible on it.

---

## Self-Review

**Spec coverage.** Optional `bands` section, floats in `[0,1]`, absent == 1.0 → Task 1. Layering through the existing deep-merge and `OVERRIDE_SECTIONS` → Tasks 1 and 4. Validation mirrored across `profile`/`brand_admin`/`event_brand_admin` → Task 1 plus Task 4's confirmation that both admins delegate rather than duplicate. Surfaces-only fading, one point in `build_overlay`, decoration included → Task 2. Byte-identical at 1.0, with the pinned hashes as proof → Tasks 1, 2 and 8, and mutation-checked in Task 2 Step 7. `palette.py` pure, the four assignment rules, the 5% floor, partial proposal for a one-colour mark → Task 3. `GET …/brand/palette` as a read returning 409 → Task 4. Both editors, sliders stepping 5% and labelled as percentages, derive button only on the channel editor → Tasks 6 and 7. Migration → Task 8 Steps 1 and 1b. It stays in scope, but is performed by the controller with the operator rather than by an implementer: the spec calls the palette a taste decision made against a preview, and a subagent can neither judge it nor be trusted with the operator's live workspace. The ERF fixture is untouched, which Task 8 Step 6 proves.

**Placeholder scan.** No TBD/TODO. Two steps name something the implementer must read from the file rather than assume — Task 7's section-toggle handler and Task 4's choice of brand-API test module — and both say so explicitly and require it in the report; that is a lookup, not a placeholder. Every code step carries its code and every command its expected output.

**Type consistency.** `bands` is the key everywhere: `brand.json`, `BrandPatchBody.bands`, `BrandPatch.bands`, `BrandEditorForm.bands`, `config["bands"]`, `SECTIONS`. `BAND_KEYS = ("top", "bottom")` matches `BAND_FIELDS`' `'top' | 'bottom'` and `BrandBands`' two fields. `palette.derive` returns `(dict[str, str], list[Swatch])` in Task 3 and is unpacked as `roles, found` in Task 4's route and Task 3's own measurement step. `Swatch.hex`/`Swatch.share` match the route's `{"hex", "share"}` and `PaletteResponse['swatches']`. `overlay.band_opacities` is defined in Task 1 and called in Task 2's `build_overlay` and nowhere else.

**One risk worth naming for the implementer of Task 3.** The role assignment is the only part of this plan whose *output quality* cannot be settled by a passing test — a palette can be valid and still ugly. That is why Task 3 Step 5 measures it against all five real logos and asks for the output rather than a verdict, and why the editor keeps the proposal editable with the swatches beside it.
