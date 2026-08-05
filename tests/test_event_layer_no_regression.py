"""Byte-identical regression guard for the ERF channel reference overlays.

The event-level profile layer must not change the output for a channel/event
that carries none of the new optional files (brand.json/fonts/assets/
layout.py at the event level) by a single byte - channels/erf's own event
folder is exactly such a case today.

The six hashes below were captured from build_overlay() BEFORE the
event-layer feature existed (see .superpowers/sdd/event-layer-report.md for
how). No reference PNGs are committed to the repo; the hash is enough to
catch a regression, and is cheap to keep in a test.
"""

from __future__ import annotations

import hashlib
import io

from yt_shorts.overlay import build_overlay
from yt_shorts.profile import load as profile_load

EXPECTED_SHA256 = {
    "WHAT IS HAPPENING?!?": "659536954f4fa4988b7102957ebe809c2ce35a1aecf8274625463be05fbea3b9",
    "Jegr and the Barbie": "e83285dc29ce717ada5da5f2d538fa44b409ed686bf29b8965c8636fdd9458a3",
    "rei got sliced": "6868a8fefcd2664413cf7249fad7eb5ce93ba29f8141e453d79c34a3de83a82b",
    "Forcing a SC": "c1f96f3a6c1b0c83f6b7c60aa9b57fe6fa6726b9dbd2259170b272e12dd13e0a",
    "Speedy!": "a8ca198d5e1eb387d92ca1a2cbf480cb4b4ae9284c312013e328b7f378831b30",
    "Jegr Tunes": "b3098f591e6ec8366f5c65b63cd3c2fc04f834412d563774c30c45d32348a1f2",
}


def _sha256_of_png(image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return hashlib.sha256(buffer.getvalue()).hexdigest()


class TestSixReferenceOverlaysStayByteIdentical:
    def test_each_reference_hook_matches_its_pre_feature_hash(self):
        p = profile_load("erf/community-clips-back-catalogue")
        mismatches = []
        for hook, expected in EXPECTED_SHA256.items():
            image = build_overlay(hook, p.channel["footer"], p.config)
            actual = _sha256_of_png(image)
            if actual != expected:
                mismatches.append(f"{hook!r}: expected {expected}, got {actual}")
        assert not mismatches, "\n".join(mismatches)
