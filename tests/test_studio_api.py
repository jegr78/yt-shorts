"""Tests for yt_shorts.studio.api - the studio's HTTP surface.

Every route gets a success case and a failure case (see CLAUDE.md's own
history of tests that could not fail for the right reason - the goal here
is assertions tied to real behaviour, not tautologies).

The FastAPI app is exercised only through fastapi.testclient.TestClient:
no server is started, nothing is downloaded. Preview tests do run real
ffmpeg against a locally-generated color source, same technique as
tests/test_preview.py - no network involved.

``create_app()`` is workspace-level (no bound profile - see api.py's own
docstring): every scoped route resolves its Profile from the URL's
channel/event path params via ``profile.load``, so the event must exist
for real under ``profile.CHANNELS_DIR``. The ``studio_profile`` fixture
copies the tiny checked-in ``erf`` fixture channel (tests/fixtures/channels/erf)
into a fresh tmp_path, repoints ``profile.CHANNELS_DIR`` at that copy for
the duration of the test, and creates an empty ``studio-test`` event
under it - so every test in this module operates on channel ``erf``,
event ``studio-test``, a real profile that ``profile.load`` can resolve,
without ever touching the checked-in fixture itself or the operator's
real workspace.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops

from yt_shorts import ownermode
from yt_shorts import clipstore, editorial
from yt_shorts import font_admin
from yt_shorts import job_queue as job_queue_module
from yt_shorts import profile as profile_module
from yt_shorts import workspace as workspace_module
from yt_shorts.job_queue import JobQueue
from yt_shorts.overlay import _footer_top
from yt_shorts import providers
from yt_shorts.providers import gemini_api, openai_api
from yt_shorts.profile import load as profile_load
from yt_shorts.studio import api as studio_api
from yt_shorts.studio import jobs as jobs_module
from yt_shorts.studio import worker as worker_module
from yt_shorts.studio.api import create_app
from yt_shorts.studio import jobs
from yt_shorts.studio import api

CLIP_URL = "https://www.youtube.com/clip/UgkxSpeedy123"

FIXTURE_CHANNELS = Path(__file__).parent / "fixtures" / "channels"

CHANNEL = "erf"
EVENT = "studio-test"
EVENT_PREFIX = f"/api/channels/{CHANNEL}/events/{EVENT}"
EV = EVENT_PREFIX  # short alias used by the stream-analysis route tests


def clip_entry(url=CLIP_URL, hook="Speedy!", duration=60.0):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 10.0 + duration, "duration": duration,
            "error": None}


@pytest.fixture
def studio_profile(tmp_path, monkeypatch):
    channels = tmp_path / "channels"
    shutil.copytree(FIXTURE_CHANNELS / "erf", channels / "erf")
    monkeypatch.setattr(profile_module, "CHANNELS_DIR", channels)
    (channels / "erf" / "events" / "studio-test").mkdir(parents=True)
    return profile_load("erf/studio-test")


@pytest.fixture
def event_dir(studio_profile):
    # The real event dir under the repointed CHANNELS_DIR; seed clips here,
    # and reach them via /api/channels/erf/events/studio-test/... URLs.
    return studio_profile.event_dir


@pytest.fixture
def client(studio_profile):
    return TestClient(create_app())


@pytest.fixture
def workspace_root(_fixed_workspace_root):
    # Must be the SAME session-scoped root that tests/conftest.py's autouse
    # _isolated_resolved_workspace fixture pins workspace.resolve() (and
    # studio.api._resolve_workspace) to. A fresh tmp_path here would make
    # the route under test read one workspace while this fixture writes
    # streams/<video_id>/... into a different, unrelated directory - the
    # test would then fail looking like a routing bug rather than what it
    # actually is: a workspace mismatch between test and route.
    return _fixed_workspace_root


@pytest.fixture
def manual_client(studio_profile, tmp_path):
    # studio_profile already copied erf into the repointed CHANNELS_DIR and
    # made the studio-test event; make that copy render-only by editing its
    # brand.json, so the channel-scoped auth routes see upload.mode=manual.
    channels = profile_module.CHANNELS_DIR
    brand_path = channels / "erf" / "brand.json"
    brand = json.loads(brand_path.read_text(encoding="utf-8"))
    brand["upload"] = {"mode": "manual"}
    brand_path.write_text(json.dumps(brand), encoding="utf-8")
    return TestClient(create_app())


def _solid_video(path: Path, seconds: float = 2.0) -> None:
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x336699:s=640x360:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ], check=True)


class TestListClips:
    def test_lists_every_clip_with_summary_fields(self, event_dir, client):
        untouched = clipstore.write_clip(event_dir, clip_entry(url=CLIP_URL, hook="Speedy!"))
        edited_dir = clipstore.write_clip(
            event_dir, clip_entry(url="https://www.youtube.com/clip/UgkxBarbie456",
                                   hook="Barbie", duration=30.0))
        editorial.save(edited_dir, editorial.Edit(title="Jegr and the Barbie",
                                                   status=editorial.KEPT, transcript=None))
        clipstore.short_path(edited_dir).write_bytes(b"fake mp4 bytes")

        response = client.get(f"{EVENT_PREFIX}/clips")
        assert response.status_code == 200
        by_name = {row["name"]: row for row in response.json()}
        assert set(by_name) == {untouched.name, edited_dir.name}

        plain = by_name[untouched.name]
        assert plain["harvested_title"] == "Speedy!"
        assert plain["effective_title"] == "Speedy!"
        assert plain["status"] == editorial.CANDIDATE
        assert plain["has_edit"] is False
        assert plain["has_short"] is False
        assert plain["duration"] == 60.0

        edited = by_name[edited_dir.name]
        assert edited["harvested_title"] == "Barbie"
        assert edited["effective_title"] == "Jegr and the Barbie"
        assert edited["status"] == editorial.KEPT
        assert edited["has_edit"] is True
        assert edited["has_short"] is True
        assert edited["duration"] == 30.0

    def test_a_clip_with_unreadable_clip_json_is_skipped_not_500d(self, event_dir, client):
        good = clipstore.write_clip(event_dir, clip_entry())
        broken = event_dir / "clips" / "broken--deadbeef"
        broken.mkdir(parents=True)
        (broken / clipstore.CLIP_FILENAME).write_text("{not json", encoding="utf-8")

        response = client.get(f"{EVENT_PREFIX}/clips")
        assert response.status_code == 200
        names = {row["name"] for row in response.json()}
        assert names == {good.name}


class TestGetClip:
    def test_unknown_clip_is_404(self, client):
        response = client.get(f"{EVENT_PREFIX}/clips/does-not-exist")
        assert response.status_code == 404
        assert "does-not-exist" in response.json()["detail"]

    def test_returns_effective_words_with_no_conflict_when_current(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}),
            encoding="utf-8")
        corrected = [{"start": 0.0, "end": 0.5, "text": " Speedy"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(derived), "words": corrected}))

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}")
        assert response.status_code == 200
        body = response.json()
        assert body["words"] == corrected
        assert body["conflict"] is False

    def test_a_corrupt_edit_json_500s_without_leaking_the_absolute_path(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        (directory / editorial.EDIT_FILENAME).write_text("{not json", encoding="utf-8")
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}")
        assert response.status_code == 500
        # The 500 body must not disclose the absolute workspace path of the file.
        assert str(directory) not in response.text
        assert "edit state" in response.json()["detail"]

    def test_reports_conflict_when_derived_transcript_changed_underneath(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        corrected = [{"start": 0.0, "end": 0.5, "text": " Speedy"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(derived), "words": corrected}))
        # The derived transcript changes underneath the correction (a re-run
        # of transcribe(), say) - not the same words the correction was
        # made against.
        changed = derived + [{"start": 0.5, "end": 1.0, "text": " overtakes"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": changed}),
            encoding="utf-8")

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}")
        assert response.status_code == 200
        body = response.json()
        assert body["words"] == corrected  # hand work still wins
        assert body["conflict"] is True


class TestPatchClip:
    def test_unknown_clip_is_404(self, client):
        response = client.patch(f"{EVENT_PREFIX}/clips/does-not-exist", json={"title": "X"})
        assert response.status_code == 404

    def test_a_bad_status_is_422(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"status": "maybe"})
        assert response.status_code == 422
        assert not (directory / editorial.EDIT_FILENAME).exists()

    def test_sets_title_and_status_and_writes_only_edit_json(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        before = {p.name for p in event_dir.rglob("*") if p.is_file()}

        response = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                                json={"title": "Jegr Tunes", "status": "kept"})
        assert response.status_code == 200
        body = response.json()
        assert body["effective_title"] == "Jegr Tunes"
        assert body["status"] == "kept"

        after = {p.name for p in event_dir.rglob("*") if p.is_file()}
        assert after - before == {editorial.EDIT_FILENAME}

        saved = editorial.load(directory)
        assert saved.title == "Jegr Tunes"
        assert saved.status == "kept"

    def test_a_null_title_clears_an_existing_override(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"title": "Custom"})
        assert editorial.load(directory).title == "Custom"

        response = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"title": None})
        assert response.status_code == 200
        assert response.json()["effective_title"] == "Speedy!"
        assert editorial.load(directory).title is None

    def test_setting_words_records_based_on_the_current_derived_transcript(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": derived}),
            encoding="utf-8")

        new_words = [{"start": 0.0, "end": 0.5, "text": " Speedy"}]
        response = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"words": new_words})
        assert response.status_code == 200
        body = response.json()
        assert body["words"] == new_words
        assert body["conflict"] is False

        saved = editorial.load(directory)
        assert saved.transcript["based_on"] == editorial.checksum(derived)
        assert saved.transcript["words"] == new_words

    def test_a_new_words_patch_supersedes_a_stale_correction_rather_than_reconciling_it(
            self, event_dir, client):
        """Decision under test: when a correction already exists AND the
        derived transcript has since changed (a conflict - see
        TestGetClip.test_reports_conflict_when_derived_transcript_changed_underneath),
        a fresh PATCH that sets `words` does not try to merge the stale
        correction with the new derived transcript, and does not refuse the
        write. It simply records a brand new correction, stamped with
        whatever is CURRENTLY cached at transcript.json - the conflict is
        gone afterwards because there is nothing left to be stale against."""
        directory = clipstore.write_clip(event_dir, clip_entry())
        old_derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        old_correction = [{"start": 0.0, "end": 0.5, "text": " Speedy"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(old_derived), "words": old_correction}))

        new_derived = old_derived + [{"start": 0.5, "end": 1.0, "text": " overtakes"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": new_derived}),
            encoding="utf-8")
        # Confirm the conflict exists before the PATCH under test.
        pre = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert pre["conflict"] is True

        fresh_correction = [{"start": 0.0, "end": 0.5, "text": " Speedy"},
                            {"start": 0.5, "end": 1.0, "text": " overtakes!"}]
        response = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                                json={"words": fresh_correction})
        assert response.status_code == 200
        body = response.json()
        assert body["words"] == fresh_correction
        assert body["conflict"] is False

        saved = editorial.load(directory)
        assert saved.transcript["based_on"] == editorial.checksum(new_derived)
        assert saved.transcript["words"] == fresh_correction


class TestPreview:
    def test_missing_raw_mp4_is_409(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/preview", params={"at": 0.1})
        assert response.status_code == 409
        assert directory.name in response.json()["detail"]

    def test_unknown_clip_is_404(self, client):
        response = client.get(f"{EVENT_PREFIX}/clips/does-not-exist/preview", params={"at": 0.1})
        assert response.status_code == 404

    def test_returns_a_png_frame_when_raw_mp4_exists(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        _solid_video(clipstore.raw_path(directory))

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/preview", params={"at": 0.5})
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


class TestPreviewPost:
    """POST /api/channels/erf/events/studio-test/clips/{name}/preview - the
    live-preview route: it renders words the CLIENT is holding but has not
    saved, so an operator sees a correction land before clicking Save (see
    the studio's own gap: the GET route only ever reflects edit.json, which
    is why the page used to refresh the preview after Save instead of
    during typing). The one rule that must hold here even more than
    elsewhere in this module: this is a READ. It must never touch
    edit.json, and an unsaved preview must leave no trace on disk at all -
    see test_post_preview_writes_nothing_to_disk.
    """

    def test_unknown_clip_is_404(self, client):
        response = client.post(
            f"{EVENT_PREFIX}/clips/does-not-exist/preview",
            json={"at": 0.1, "words": []},
        )
        assert response.status_code == 404

    def test_missing_raw_mp4_is_409(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.post(
            f"{EVENT_PREFIX}/clips/{directory.name}/preview",
            json={"at": 0.1, "words": []},
        )
        assert response.status_code == 409
        assert directory.name in response.json()["detail"]

    def test_altered_words_render_different_caption_pixels_than_the_saved_state(
            self, event_dir, client, studio_profile):
        """The test the bug fix is actually FOR: a client holding an
        edited word that was never saved must see that edit reflected in
        the PNG - not the last-saved caption. A test that only checked for
        200-and-some-bytes would pass against a route that silently
        ignored the posted words (the exact bug being closed here), so
        this measures the actual caption pixels in the band between the
        video window and the footer, the same band tests/test_preview.py
        already trusts for this purpose."""
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        _solid_video(clipstore.raw_path(directory))
        saved_words = [{"start": 0.0, "end": 3.0, "text": " very"}]
        clipstore.transcript_path(directory).write_text(
            json.dumps({"model": "small", "source": CLIP_URL, "words": saved_words}),
            encoding="utf-8")

        at = 0.5
        saved_response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/preview", params={"at": at})
        assert saved_response.status_code == 200

        edited_words = [{"start": 0.0, "end": 3.0, "text": " COMPLETELY DIFFERENT CAPTION"}]
        edited_response = client.post(
            f"{EVENT_PREFIX}/clips/{directory.name}/preview",
            json={"at": at, "words": edited_words},
        )
        assert edited_response.status_code == 200
        assert edited_response.headers["content-type"] == "image/png"

        # Whole-image bytes must differ...
        assert edited_response.content != saved_response.content

        # ...and specifically within the caption band, not merely somewhere
        # in the frame (which a flaky ffmpeg extraction could also cause).
        saved_image = Image.open(io.BytesIO(saved_response.content)).convert("RGB")
        edited_image = Image.open(io.BytesIO(edited_response.content)).convert("RGB")
        output = studio_profile.config["output"]
        band_top = output["video_y"] + output["video_height"]
        band_bottom = _footer_top(output["height"])
        saved_band = saved_image.crop((0, band_top, saved_image.width, band_bottom))
        edited_band = edited_image.crop((0, band_top, edited_image.width, band_bottom))

        diff = ImageChops.difference(saved_band, edited_band)
        bbox = diff.getbbox()
        assert bbox is not None, "caption band is pixel-identical between saved and edited previews"

        diff_pixels = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0))
        # Reported to make the measured difference legible in CI output, and
        # to make sure the difference is a real caption swap, not a couple
        # of stray antialiased pixels.
        print(f"caption band differing pixels (post vs. saved preview): {diff_pixels}")
        assert diff_pixels > 200, f"only {diff_pixels} differing pixels in the caption band"

    def test_altered_title_renders_different_hook_pixels_than_the_saved_state(
            self, event_dir, client, studio_profile):
        """The title's own version of the caption test above: an unsaved
        title edit must show up in the hook the PNG draws, not just the
        caption. A test that only checked for 200-and-some-bytes would
        pass against a route that accepted `title` and silently dropped
        it - exactly the bug this field closes - so this measures the
        actual hook pixels in the band above the video window (see
        overlay.build_overlay: the hook is drawn into
        [0, window_top), i.e. y < output.video_y)."""
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        _solid_video(clipstore.raw_path(directory))

        at = 0.5
        saved_response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/preview", params={"at": at})
        assert saved_response.status_code == 200

        edited_response = client.post(
            f"{EVENT_PREFIX}/clips/{directory.name}/preview",
            json={"at": at, "words": [], "title": "A COMPLETELY DIFFERENT HOOK"},
        )
        assert edited_response.status_code == 200
        assert edited_response.headers["content-type"] == "image/png"

        # Whole-image bytes must differ...
        assert edited_response.content != saved_response.content

        # ...and specifically within the hook band (above the video
        # window), not merely somewhere in the frame.
        saved_image = Image.open(io.BytesIO(saved_response.content)).convert("RGB")
        edited_image = Image.open(io.BytesIO(edited_response.content)).convert("RGB")
        window_top = studio_profile.config["output"]["video_y"]
        saved_band = saved_image.crop((0, 0, saved_image.width, window_top))
        edited_band = edited_image.crop((0, 0, edited_image.width, window_top))

        diff = ImageChops.difference(saved_band, edited_band)
        bbox = diff.getbbox()
        assert bbox is not None, "hook band is pixel-identical between saved and edited previews"

        diff_pixels = sum(1 for pixel in diff.getdata() if pixel != (0, 0, 0))
        print(f"hook band differing pixels (post vs. saved preview): {diff_pixels}")
        assert diff_pixels > 200, f"only {diff_pixels} differing pixels in the hook band"

    def test_title_omitted_falls_back_to_the_saved_effective_title(self, event_dir, client):
        """The other half of the contract: when `title` is left out of
        the POST body (the studio's own behaviour whenever the title
        field itself has not been touched), the hook must be pixel-
        identical to the GET route's - not blank, not the harvested
        title, the SAVED one. Guards against a fix that always overrides
        the hook once the field exists on the model."""
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        editorial.save(directory, editorial.Edit(
            title="Saved Override Title", status=editorial.CANDIDATE, transcript=None))
        _solid_video(clipstore.raw_path(directory))

        at = 0.5
        saved_response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/preview", params={"at": at})
        no_title_response = client.post(
            f"{EVENT_PREFIX}/clips/{directory.name}/preview",
            json={"at": at, "words": []},
        )
        assert no_title_response.status_code == 200
        assert no_title_response.content == saved_response.content

    def test_post_preview_writes_nothing_to_disk(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        _solid_video(clipstore.raw_path(directory))
        edit_path = directory / editorial.EDIT_FILENAME
        assert not edit_path.exists()
        before = {p: p.stat().st_mtime_ns for p in event_dir.rglob("*") if p.is_file()}

        response = client.post(
            f"{EVENT_PREFIX}/clips/{directory.name}/preview",
            json={"at": 0.5, "words": [{"start": 0.0, "end": 1.0, "text": "ALTERED"}]},
        )
        assert response.status_code == 200
        assert not edit_path.exists()

        after = {p: p.stat().st_mtime_ns for p in event_dir.rglob("*") if p.is_file()}
        # Not just "edit.json is still absent" - literally nothing on disk
        # was created, deleted or modified by a route that is supposed to
        # be a pure read.
        assert after == before


class TestShort:
    def test_missing_short_is_404(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short")
        assert response.status_code == 404

    def test_unknown_clip_is_404(self, client):
        response = client.get(f"{EVENT_PREFIX}/clips/does-not-exist/short")
        assert response.status_code == 404

    def test_streams_the_short_when_present(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        payload = b"pretend this is an mp4"
        clipstore.short_path(directory).write_bytes(payload)

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short")
        assert response.status_code == 200
        assert response.content == payload


class TestStreamsRoute:
    def _catalogue(self, **overrides):
        from yt_shorts.youtube import Catalogue, Playlist, Video
        base = {
            "videos": [Video("aaa", "Race Part 1", 29975, 2200, ["PLaaa"]),
                       Video("bbb", "Special", 8983, 400, [])],
            "playlists": [Playlist("PLaaa", "2026 Season", 1, 2)],
            "failed_playlists": [],
        }
        base.update(overrides)
        return Catalogue(**base)

    def test_videos_carry_their_playlists_and_what_exists(self, client, monkeypatch):
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.status_code == 200
        assert r.json()["videos"][0] == {
            "video_id": "aaa", "title": "Race Part 1",
            "duration_seconds": 29975, "view_count": 2200,
            "playlist_ids": ["PLaaa"],
            "has_transcript": False, "has_analysis": False}

    def test_playlists_carry_their_size_and_what_is_unavailable(self, client, monkeypatch):
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.json()["playlists"] == [
            {"id": "PLaaa", "title": "2026 Season", "count": 1, "unavailable": 2}]

    def test_a_failed_playlist_is_reported_rather_than_hidden(self, client, monkeypatch):
        from yt_shorts.youtube import FailedPlaylist
        monkeypatch.setattr(
            api, "channel_catalogue",
            lambda url, **k: self._catalogue(
                failed_playlists=[FailedPlaylist("Bathurst", "HTTP 404")]))
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.json()["failed_playlists"] == [
            {"title": "Bathurst", "reason": "HTTP 404"}]

    def test_what_exists_is_read_fresh_not_cached_with_the_yt_dlp_answer(
            self, client, monkeypatch, workspace_root):
        """The expensive half (yt-dlp) is cached for the session; these two
        flags are not. Caching them would leave the list saying "no
        transcript" after a transcription finished, until someone pressed
        refresh - and the whole point of the marker is to be true."""
        monkeypatch.setattr(api, "channel_catalogue",
                            lambda url, **k: self._catalogue())
        first = client.get(f"{EVENT_PREFIX}/streams")
        assert first.json()["videos"][0]["has_transcript"] is False

        directory = workspace_root / "streams" / "aaa"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "transcript.json").write_text("{}", encoding="utf-8")

        second = client.get(f"{EVENT_PREFIX}/streams")   # NO refresh
        assert second.json()["videos"][0]["has_transcript"] is True

    def test_the_catalogue_is_cached_within_a_session(self, client, monkeypatch):
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return self._catalogue()

        monkeypatch.setattr(api, "channel_catalogue", fake)
        client.get(f"{EVENT_PREFIX}/streams")
        client.get(f"{EVENT_PREFIX}/streams")
        assert calls["n"] == 1

    def test_a_refresh_re_fetches(self, client, monkeypatch):
        calls = {"n": 0}

        def fake(url, **k):
            calls["n"] += 1
            return self._catalogue()

        monkeypatch.setattr(api, "channel_catalogue", fake)
        client.get(f"{EVENT_PREFIX}/streams")
        client.get(f"{EVENT_PREFIX}/streams?refresh=true")
        assert calls["n"] == 2

    def test_a_youtube_error_is_a_502_with_a_message(self, client, monkeypatch):
        from yt_shorts.youtube import YouTubeError

        def fail(url, **k):
            raise YouTubeError("yt-dlp is not installed")

        monkeypatch.setattr(api, "channel_catalogue", fail)
        r = client.get(f"{EVENT_PREFIX}/streams")
        assert r.status_code == 502
        assert "yt-dlp" in r.json()["detail"]


# The real `start_detect_job`, with `_STUDIO_DETECT_FN` stubbed inside it -
# so the route's own wiring into the starter is exercised. Opts out of
# tests/conftest.py's autouse `_no_real_job_starter` guard by name.
@pytest.mark.usefixtures("real_job_starters")
class TestDetectRoute:
    def _real_shaped_analysis(self, tmp_path, name="moments.json"):
        """Writes a file on disk shaped exactly like detect.detect_moments's
        real payload (see detect.py) and returns its Path. A stand-in that
        instead returned a bare list of clip names (the OLD contract, from
        when detect wrote clip directories) makes `_run_detect` fail with
        `TypeError: argument should be a str or an os.PathLike object ...
        not 'list'` the moment it calls `Path(path).read_text(...)` - a
        failure neither of these two tests would have caught, since both
        only assert the synchronous HTTP response and never poll the job."""
        path = tmp_path / name
        path.write_text(json.dumps({
            "video_id": "vid123",
            "engine": "lexicon",
            "created_at": "2026-07-27T10:00:00+00:00",
            "duration_seconds": 600.0,
            "activity": [],
            "moments": [],
            "missing_windows": [],
            "missing_chunks": [],
        }), encoding="utf-8")
        return path

    def test_starts_a_detect_job(self, client, monkeypatch, tmp_path):
        """The route starts a detect job that RUNS - not merely a route that
        answers with an id.

        Patched at `jobs._STUDIO_DETECT_FN`, which is what the route now
        reaches: it stopped passing `detect_fn=detect_moments` (Task 6), so
        patching `api.detect_moments` - what this test used to do - would
        now patch a name no route reads and let the real, transcript-
        requiring default run instead.

        That is also why this asserts what it does. It used to check only
        `status_code in (200, 202)` and `"job_id" in r.json()`, and PASSED
        under a mutation that made its own patch inert: the route answered
        exactly the same while the real default ran and the job failed with
        TranscriptNotCached. A test whose whole point is which function the
        route reaches has to observe that function being reached.
        """
        import time

        analysis = self._real_shaped_analysis(tmp_path)
        seen = []

        def fake_detect(*args, **kwargs):
            seen.append((args, kwargs))
            return analysis

        monkeypatch.setattr(jobs_module, "_STUDIO_DETECT_FN", fake_detect)
        r = client.post(f"{EVENT_PREFIX}/streams/vid123/detect")
        assert r.status_code in (200, 202)
        job_id = r.json()["job_id"]

        deadline = time.monotonic() + 5.0
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        while snapshot["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = client.get(f"/api/jobs/{job_id}").json()

        assert seen, "the patched detect function was never called"
        assert "vid123" in repr(seen[0]), seen[0]
        assert snapshot["status"] == "done", snapshot

    def test_refuses_a_second_job_for_the_event(self, client, monkeypatch, tmp_path):
        import threading
        gate = threading.Event()
        analysis = self._real_shaped_analysis(tmp_path)

        def fake_detect(*a, **k):
            gate.wait(5)
            return analysis

        monkeypatch.setattr(jobs_module, "_STUDIO_DETECT_FN", fake_detect)
        first = client.post(f"{EVENT_PREFIX}/streams/vid123/detect")
        assert first.status_code in (200, 202)
        second = client.post(f"{EVENT_PREFIX}/streams/vid999/detect")
        assert second.status_code == 409
        gate.set()

    def test_the_route_no_longer_transcribes_on_demand(self, client, workspace_root):
        """Task 4 split transcription out of detection and Task 6 finished
        the change here: `post_detect` no longer overrides
        `start_detect_job`'s default, so the studio's detect button runs
        `require_cached_transcript` and REFUSES a stream nobody has
        transcribed - rather than silently starting an hour of Whisper decode
        nobody asked for. Driven with nothing patched at all: the override
        being gone is the whole thing under test.

        The way to get that transcript is a queued `transcribe` job - see
        TestJobQueueRoutes.
        test_a_transcribe_job_is_how_an_operator_gets_what_detect_now_needs.
        """
        import time

        video_id = "vid-task6-never-transcribed"
        started = client.post(f"{EVENT_PREFIX}/streams/{video_id}/detect")
        assert started.status_code in (200, 202)
        job_id = started.json()["job_id"]

        deadline = time.monotonic() + 5.0
        snapshot = client.get(f"/api/jobs/{job_id}").json()
        while snapshot["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = client.get(f"/api/jobs/{job_id}").json()

        assert snapshot["status"] == "failed", snapshot
        assert "TranscriptNotCached" in snapshot["results"]["detect"]["reason"]
        assert not (workspace_root / "streams" / video_id / "moments.json").exists()


class TestCreateClipFromWindow:
    def _body(self, **over):
        body = {"start": 90.0, "end": 104.0, "hook": "BIG ONE",
                "source_title": "ERF Race Part 1"}
        body.update(over)
        return body

    def test_creates_a_clip_directory(self, client, event_dir):
        response = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        assert response.status_code == 200
        name = response.json()["name"]
        assert (event_dir / "clips" / name / "clip.json").is_file()

    def test_the_same_window_twice_is_idempotent(self, client):
        first = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        second = client.post(f"{EV}/streams/vid123/clips", json=self._body())
        assert second.status_code == 200
        assert second.json()["name"] == first.json()["name"]

    def test_a_same_window_repick_with_a_different_hook_rewrites_clip_json(
            self, client, event_dir):
        # IMPORTANT 2 / the sixth write-boundary contradiction: an exact
        # re-pick of the SAME window is an ordinary, idempotent correction -
        # it updates the existing directory's clip.json in place rather than
        # minting a second directory or being refused. This is exactly the
        # case CLAUDE.md's and api.py's "the studio never edits an existing
        # clip.json" claims were false about; pinning it here is what keeps
        # that claim honest going forward.
        first = client.post(f"{EV}/streams/vid123/clips", json=self._body(hook="FIRST HOOK"))
        assert first.status_code == 200
        name = first.json()["name"]

        second = client.post(f"{EV}/streams/vid123/clips", json=self._body(hook="SECOND HOOK"))
        assert second.status_code == 200
        assert second.json()["name"] == name, "a same-window re-pick must reuse the directory"

        entry = clipstore.read_clip(event_dir / "clips" / name)
        assert entry["hook"] == "SECOND HOOK"
        assert len(list((event_dir / "clips").iterdir())) == 1, \
            "no second directory should exist for the same identity"

    def test_a_colliding_different_window_is_a_409_naming_both(self, client):
        client.post(f"{EV}/streams/vid123/clips", json=self._body())
        # Rounds to the same identity, but is a different window.
        response = client.post(f"{EV}/streams/vid123/clips",
                               json=self._body(start=90.4, end=104.4, hook="OTHER"))
        assert response.status_code == 409
        assert "90" in response.json()["detail"]

    def test_an_inverted_window_is_a_400(self, client):
        response = client.post(f"{EV}/streams/vid123/clips",
                               json=self._body(start=104.0, end=90.0))
        assert response.status_code == 400

    def test_a_traversing_video_id_is_refused(self, client):
        # ..%2F..%2Fauth carries an encoded slash, which the ASGI layer
        # decodes to a literal '/' before Starlette ever matches a route -
        # the single-segment {video_id} pattern then doesn't match at all,
        # so this 404s from routing before validate_segment gets a chance to
        # run and return its own 400. Same framework quirk
        # TestStreamAnalysisRoutes.test_a_traversing_video_id_is_refused
        # already hedges around for the GET streams routes. Either way
        # nothing outside streams/<video_id>/ is ever read or written - that
        # is what actually matters, and the guard itself is proven below by
        # test_a_traversing_video_id_without_a_slash_is_rejected_400, whose
        # id reaches the handler.
        response = client.post(f"{EV}/streams/..%2F..%2Fauth/clips", json=self._body())
        assert response.status_code in (400, 404)

    def test_a_traversing_video_id_without_a_slash_is_rejected_400(self, client):
        # %2e%2e decodes to a literal ".." with no slash, so it DOES match
        # the single-segment {video_id} pattern and reaches the handler -
        # this is a real assertion about validate_segment's guard, unlike
        # the %2F case above.
        response = client.post(f"{EV}/streams/%2e%2e/clips", json=self._body())
        assert response.status_code == 400

    def test_it_is_refused_while_the_event_lock_is_held(self, client, event_dir):
        from yt_shorts.lock import EventLock
        lock = EventLock(event_dir)
        lock.acquire()
        try:
            response = client.post(f"{EV}/streams/vid123/clips", json=self._body())
            assert response.status_code == 409
        finally:
            lock.release()


class TestWindowEdit:
    def _moment(self, event_dir):
        from yt_shorts import clipstore
        return clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/92-104", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 92.0, "end": 104.0,
            "duration": 12.0, "error": None})

    def test_patching_a_window_persists_to_edit_json(self, event_dir, client):
        from yt_shorts import editorial
        directory = self._moment(event_dir)
        r = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"window": [95.0, 108.0]})
        assert r.status_code == 200
        assert editorial.load(directory).window == (95.0, 108.0)

    def test_clearing_a_window(self, event_dir, client):
        from yt_shorts import editorial
        directory = self._moment(event_dir)
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"window": [95.0, 108.0]})
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"window": None})
        assert editorial.load(directory).window is None

    def test_get_clip_exposes_detected_and_effective_window(self, event_dir, client):
        directory = self._moment(event_dir)
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"window": [95.0, 108.0]})
        body = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert body["detected_window"] == [92.0, 104.0]
        assert body["effective_window"] == [95.0, 108.0]


class TestUploadAndAuthRoutes:
    def _kept_rendered(self, event_dir, status="kept"):
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        editorial.save(directory, editorial.Edit(title=None, status=status, transcript=None))
        return directory

    def test_upload_starts_a_job_for_a_kept_rendered_clip(self, event_dir, client, monkeypatch):
        directory = self._kept_rendered(event_dir)
        monkeypatch.setattr(api.jobs, "start_upload_job",
                            lambda *a, **k: type("J", (), {"id": "job1"})())
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload")
        assert r.status_code in (200, 202)
        assert r.json()["job_id"] == "job1"

    def test_upload_refuses_a_clip_that_is_not_kept(self, event_dir, client):
        directory = self._kept_rendered(event_dir, status="candidate")
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload")
        assert r.status_code == 409

    def test_upload_refuses_an_already_uploaded_clip_without_force(self, event_dir, client):
        from yt_shorts import upload_record
        directory = self._kept_rendered(event_dir)
        upload_record.save(directory, "OLD", "https://youtu.be/OLD", "private",
                           when="2026-07-22T00:00:00Z")
        assert client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload").status_code == 409

    def test_auth_status_reports_disconnected_without_a_token(self, client, monkeypatch):
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)
        body = client.get(f"/api/channels/{CHANNEL}/auth").json()
        assert body["connected"] is False
        assert "remaining_uploads" in body


class TestUploadVisibility:
    def _kept_rendered(self, event_dir, status="kept"):
        from yt_shorts import clipstore, editorial
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        editorial.save(directory, editorial.Edit(title=None, status=status, transcript=None))
        return directory

    def _clip(self, event_dir, hook="CRASH at Turn 1"):
        from yt_shorts import clipstore
        return clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": hook, "source_title": "ERF 24h Part 1", "start": 0.0,
            "end": 12.0, "duration": 12.0, "error": None})

    def _fake_start(self, seen):
        def fake_start(profile, job_store, name, *, force=False, visibility="private",
                       publish_at=None, uploader=None, when=None):
            seen["visibility"] = visibility
            seen["publish_at"] = publish_at
            seen["force"] = force
            job = job_store.create()
            job.finish("done")
            return job
        return fake_start

    def test_public_without_confirm_is_refused(self, event_dir, client):
        directory = self._kept_rendered(event_dir)
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload",
                        json={"visibility": "public"})
        assert r.status_code == 400

    def test_public_with_confirm_threads_visibility(self, event_dir, client, monkeypatch):
        directory = self._kept_rendered(event_dir)
        seen = {}
        monkeypatch.setattr(api.jobs, "start_upload_job", self._fake_start(seen))
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload",
                        json={"visibility": "public", "confirm": True})
        assert r.status_code == 200
        assert seen["visibility"] == "public"
        assert seen["publish_at"] is None

    def test_scheduled_without_confirm_is_refused(self, event_dir, client):
        directory = self._kept_rendered(event_dir)
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload",
                        json={"publish_at": "2099-01-01T00:00:00Z"})
        assert r.status_code == 400

    def test_scheduled_with_confirm_threads_publish_at(self, event_dir, client, monkeypatch):
        directory = self._kept_rendered(event_dir)
        seen = {}
        monkeypatch.setattr(api.jobs, "start_upload_job", self._fake_start(seen))
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload",
                        json={"publish_at": "2099-01-01T00:00:00Z", "confirm": True})
        assert r.status_code == 200
        assert seen["visibility"] == "private"
        assert seen["publish_at"] == "2099-01-01T00:00:00Z"

    def test_private_upload_needs_no_confirm(self, event_dir, client, monkeypatch):
        directory = self._kept_rendered(event_dir)
        seen = {}
        monkeypatch.setattr(api.jobs, "start_upload_job", self._fake_start(seen))
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload", json={})
        assert r.status_code == 200
        assert seen["visibility"] == "private"

    def test_force_travels_in_the_body_not_a_query_param(self, event_dir, client, monkeypatch):
        from yt_shorts import upload_record
        directory = self._kept_rendered(event_dir)
        upload_record.save(directory, "OLD", "https://youtu.be/OLD", "private",
                           when="2026-07-22T00:00:00Z")
        seen = {}
        monkeypatch.setattr(api.jobs, "start_upload_job", self._fake_start(seen))
        # A bare force=true QUERY param (the old contract) is no longer honored...
        refused = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload?force=true")
        assert refused.status_code == 409
        # ...only a body force does.
        r = client.post(f"{EVENT_PREFIX}/clips/{directory.name}/upload", json={"force": True})
        assert r.status_code == 200
        assert seen["force"] is True

    def test_patch_saves_per_clip_upload_override(self, event_dir, client):
        directory = self._clip(event_dir)
        r = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                         json={"upload": {"description": "hand", "tags": ["gt7"]}})
        assert r.status_code == 200
        # reflected in upload-preview
        pv = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/upload-preview").json()
        assert pv["description"] == "hand" and pv["tags"] == ["gt7"]

    def test_patch_omitting_upload_leaves_existing_override_untouched(self, event_dir, client):
        directory = self._clip(event_dir)
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                    json={"upload": {"description": "hand"}})
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"title": "New title"})
        from yt_shorts import editorial
        assert editorial.load(directory).upload == {"description": "hand"}

    def test_patch_clearing_upload_with_null(self, event_dir, client):
        directory = self._clip(event_dir)
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                    json={"upload": {"description": "hand"}})
        client.patch(f"{EVENT_PREFIX}/clips/{directory.name}", json={"upload": None})
        from yt_shorts import editorial
        assert editorial.load(directory).upload is None

    def test_patch_bad_upload_shape_is_422_and_does_not_persist(self, event_dir, client):
        directory = self._clip(event_dir)
        r = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                         json={"upload": {"tags": "not-a-list"}})
        assert r.status_code == 422
        # Not written: edit.json still holds no upload override, so every
        # other clip route (not just this PATCH) keeps working - a bad
        # shape must not brick the clip.
        from yt_shorts import editorial
        assert editorial.load(directory).upload is None
        get_r = client.get(f"{EVENT_PREFIX}/clips/{directory.name}")
        assert get_r.status_code == 200
        pv = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/upload-preview")
        assert pv.status_code == 200
        # A follow-up PATCH with a good shape still works against this clip.
        r2 = client.patch(f"{EVENT_PREFIX}/clips/{directory.name}",
                          json={"upload": {"description": "hand"}})
        assert r2.status_code == 200
        assert editorial.load(directory).upload == {"description": "hand"}

    def test_actual_recorded_privacy_reflects_youtubes_response(
            self, event_dir, real_job_starters):
        # Unit-level, against the real start_upload_job: an injected uploader
        # that behaves like _default_uploader (records upload_record.save with
        # whatever privacy it was actually given) must see the ACTUAL privacy
        # YouTube reported, threaded straight through - not a hard "private".
        import time

        from yt_shorts import upload_record
        from yt_shorts.profile import load as profile_load
        from yt_shorts.studio import jobs as jobs_module

        profile = profile_load("erf/studio-test")
        directory = self._kept_rendered(event_dir)

        def fake_uploader(profile, directory, clip, edit, when, *,
                          visibility="private", publish_at=None):
            # Simulates YouTube reporting back exactly the requested
            # visibility, the same way _default_uploader forwards
            # result.privacy_status.
            upload_record.save(directory, "VID1", "https://youtu.be/VID1",
                               visibility, when=when)
            return {"video_id": "VID1", "url": "https://youtu.be/VID1"}

        job = jobs_module.start_upload_job(
            profile, jobs_module.JobStore(), directory.name,
            uploader=fake_uploader, visibility="public",
            when="2026-07-23T00:00:00Z")
        for _ in range(50):
            if job.status != "running":
                break
            time.sleep(0.05)
        assert job.status == "done"
        assert upload_record.load(directory)["privacy"] == "public"


class TestConnectRoute:
    def _fake_connect(self, seen, job_id="job1"):
        def fake(profile, store, channel_id, *, force=False):
            seen["cid"] = channel_id
            seen["force"] = force
            return type("J", (), {"id": job_id})()
        return fake

    def test_connect_starts_a_job_with_the_given_channel_id(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        monkeypatch.setattr(api.jobs, "start_connect_job", self._fake_connect(seen))
        r = client.post(f"/api/channels/{CHANNEL}/auth/connect", json={"channel_id": "UCentered"})
        assert r.status_code in (200, 202)
        assert r.json()["job_id"] == "job1"
        assert seen["cid"] == "UCentered"

    def test_connect_defaults_to_the_profile_channel_id_when_omitted(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        monkeypatch.setattr(api.jobs, "start_connect_job", self._fake_connect(seen, "j"))
        client.post(f"/api/channels/{CHANNEL}/auth/connect", json={})
        assert seen["cid"] == "UCb3S2oA7lANdg5IS0QtF46w"   # the ERF fixture's id

    def test_connect_passes_force_through(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        monkeypatch.setattr(api.jobs, "start_connect_job", self._fake_connect(seen))
        client.post(f"/api/channels/{CHANNEL}/auth/connect", json={"channel_id": "UCx", "force": True})
        assert seen["force"] is True
        # default is False when omitted
        client.post(f"/api/channels/{CHANNEL}/auth/connect", json={"channel_id": "UCx"})
        assert seen["force"] is False

    def test_connect_without_the_google_libraries_is_503(self, client, monkeypatch):
        from yt_shorts._google import GoogleUnavailable
        def boom(feature):
            raise GoogleUnavailable("install it")
        monkeypatch.setattr(api, "google_require", boom)
        r = client.post(f"/api/channels/{CHANNEL}/auth/connect", json={"channel_id": "UCx"})
        assert r.status_code == 503
        assert "install" in r.json()["detail"]


class TestUploadStateInSummary:
    def test_clip_summary_exposes_upload_state(self, event_dir, client):
        from yt_shorts import clipstore, upload_record
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "x", "source_title": "y", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        before = next(c for c in client.get(f"{EVENT_PREFIX}/clips").json() if c["name"] == directory.name)
        assert before["has_upload"] is False
        assert before["upload_url"] is None
        upload_record.save(directory, "VID1", "https://youtu.be/VID1", "private",
                           when="2026-07-22T00:00:00Z")
        after = next(c for c in client.get(f"{EVENT_PREFIX}/clips").json() if c["name"] == directory.name)
        assert after["has_upload"] is True
        assert after["upload_url"] == "https://youtu.be/VID1"


class TestUploadPreviewRoute:
    def _clip(self, event_dir, hook="CRASH at Turn 1"):
        from yt_shorts import clipstore
        return clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": hook, "source_title": "ERF 24h Part 1", "start": 0.0,
            "end": 12.0, "duration": 12.0, "error": None})

    def test_preview_returns_the_computed_metadata(self, event_dir, client):
        directory = self._clip(event_dir)
        body = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/upload-preview").json()
        assert body["title"] == "CRASH at Turn 1"          # effective hook
        assert body["made_for_kids"] is False
        assert "category_id" in body
        assert "description" in body and "tags" in body

    def test_preview_reflects_an_edited_title(self, event_dir, client):
        from yt_shorts import editorial
        directory = self._clip(event_dir)
        editorial.save(directory, editorial.Edit(
            title="Massive shunt!", status="kept", transcript=None))
        body = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/upload-preview").json()
        assert body["title"] == "Massive shunt!"

    def test_preview_of_a_too_long_title_is_409_not_500(self, event_dir, client):
        # build_metadata rejects a title over YouTube's 100-character limit
        # (a hook this long is common) - the route must map that UploadError
        # to a 409, not let it escape as an opaque 500 that would hide the
        # whole metadata editor in UploadPanel.
        directory = self._clip(event_dir, hook="X" * 101)
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/upload-preview")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "title" in detail and "100" in detail

    def test_preview_of_an_unknown_clip_is_404(self, client):
        assert client.get(f"{EVENT_PREFIX}/clips/nope--00000000/upload-preview").status_code == 404


class TestTrimRoutes:
    def _clip_with_short(self, event_dir, duration=60.0, status=editorial.CANDIDATE):
        directory = clipstore.write_clip(event_dir, clip_entry(duration=duration))
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        if status != editorial.CANDIDATE:
            editorial.save(directory, editorial.Edit(
                title=None, status=status, transcript=None))
        return directory

    def _clip_without_short(self, event_dir, duration=60.0):
        return clipstore.write_clip(
            event_dir, clip_entry(url="https://www.youtube.com/clip/UgkxNoShort999",
                                   duration=duration))

    def test_summary_reports_desired_and_applied(self, event_dir, client):
        # A clip with a rendered short and no trim: both null.
        directory = self._clip_with_short(event_dir)
        body = client.get(f"{EV}/clips/{directory.name}").json()
        assert body["trim"] is None and body["trim_applied"] is None

    def test_patching_a_trim_stores_it(self, event_dir, client):
        directory = self._clip_with_short(event_dir)
        body = client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]}).json()
        assert body["trim"] == [3.0, 2.0]
        assert body["trim_applied"] is None          # set, not applied

    def test_a_trim_leaving_less_than_the_floor_is_422(self, event_dir, client):
        directory = self._clip_with_short(event_dir)
        response = client.patch(f"{EV}/clips/{directory.name}", json={"trim": [999.0, 999.0]})
        assert response.status_code == 422

    def test_a_bool_in_the_trim_pair_is_refused_not_coerced_to_a_float(
            self, event_dir, client):
        # Confirmed live: with PatchClipBody.trim typed list[float], Pydantic
        # coerces a JSON `true` to `1.0` before patch_clip ever runs, so
        # editorial.validate_trim's explicit bool rejection never got the
        # chance to fire and {"trim": [true, 0]} returned 200, storing
        # [1.0, 0.0]. It must 422 instead, and edit.json must stay untouched.
        directory = self._clip_with_short(event_dir)
        response = client.patch(f"{EV}/clips/{directory.name}",
                                json={"trim": [True, 0]})
        assert response.status_code == 422
        body = client.get(f"{EV}/clips/{directory.name}").json()
        assert body["trim"] is None, "the rejected trim must not be written"

    def test_null_clears_the_trim(self, event_dir, client):
        directory = self._clip_with_short(event_dir)
        client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        assert client.patch(f"{EV}/clips/{directory.name}",
                            json={"trim": None}).json()["trim"] is None

    def test_upload_is_refused_while_a_trim_is_pending(self, event_dir, client):
        directory = self._clip_with_short(event_dir, status=editorial.KEPT)
        client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        response = client.post(f"{EV}/clips/{directory.name}/upload")
        assert response.status_code == 409
        assert "trim" in response.json()["detail"].lower()

    def test_the_download_form_is_refused_while_pending(self, event_dir, client):
        directory = self._clip_with_short(event_dir)
        client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        assert client.get(f"{EV}/clips/{directory.name}/short?as=download").status_code == 409

    def test_the_plain_short_url_still_streams_while_pending(self, event_dir, client):
        # The player previews the trim; blocking this would kill the very
        # thing the operator needs in order to choose the values.
        directory = self._clip_with_short(event_dir)
        client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        assert client.get(f"{EV}/clips/{directory.name}/short").status_code == 200

    def test_applying_starts_a_job(self, event_dir, client, monkeypatch):
        directory = self._clip_with_short(event_dir)
        client.patch(f"{EV}/clips/{directory.name}", json={"trim": [3.0, 2.0]})
        monkeypatch.setattr(api.jobs, "start_trim_job",
                            lambda *a, **k: type("J", (), {"id": "job1"})())
        response = client.post(f"{EV}/clips/{directory.name}/trim")
        assert response.status_code == 200 and response.json()["job_id"] == "job1"

    def test_applying_without_a_short_is_409(self, event_dir, client):
        directory = self._clip_without_short(event_dir)
        response = client.post(f"{EV}/clips/{directory.name}/trim")
        assert response.status_code == 409

    def test_trim_validates_against_the_effective_window_not_clip_json(
            self, event_dir, client):
        # clip.json's own duration is the DETECTED span (20.0s here); nudging
        # the window writes edit.window and never rewrites clip.json, and the
        # render uses the EFFECTIVE window - a real 60s short. Before the
        # fix, patch_clip validated a trim against clip["duration"] (20.0)
        # and 422'd "leaves less than 3.0s of a 20.0s clip" for a trim the
        # operator's actual 60-second video easily accommodates.
        directory = self._clip_with_short(event_dir, duration=20.0)
        window_response = client.patch(
            f"{EV}/clips/{directory.name}", json={"window": [10.0, 70.0]})
        assert window_response.status_code == 200
        response = client.patch(
            f"{EV}/clips/{directory.name}", json={"trim": [10.0, 10.0]})
        assert response.status_code == 200, response.json()
        assert response.json()["trim"] == [10.0, 10.0]

    def test_trim_still_validates_against_clip_json_with_no_window_override(
            self, event_dir, client):
        # No edit.window set: the old behaviour (validate against
        # clip["duration"]) must still hold - a 20s clip cannot absorb a
        # trim that would leave less than the floor.
        directory = self._clip_with_short(event_dir, duration=20.0)
        response = client.patch(
            f"{EV}/clips/{directory.name}", json={"trim": [10.0, 10.0]})
        assert response.status_code == 422

    def test_an_inverted_window_is_refused_not_turned_into_a_negative_duration(
            self, event_dir, client):
        # MINOR 1: patch_clip used to accept {"window": [70, 10]} unvalidated,
        # and the trim block's own validation (window[1] - window[0]) then
        # computed a NEGATIVE floor duration from it - so even a bare
        # {"trim": [0, 0]} sent only to CLEAR a trim 422'd with "leaves less
        # than 3.0s of a -60.0s clip" instead of succeeding trivially. Reject
        # the cause (the inverted window) at the source.
        directory = self._clip_with_short(event_dir, duration=60.0)
        response = client.patch(
            f"{EV}/clips/{directory.name}", json={"window": [70.0, 10.0]})
        assert response.status_code == 422
        assert "window" in response.json()["detail"].lower()
        # And the clip must still be able to clear/set a trim afterward -
        # the window override was never saved.
        clear = client.patch(f"{EV}/clips/{directory.name}", json={"trim": [0.0, 0.0]})
        assert clear.status_code == 200, clear.json()


class TestTrimUnknownState:
    """THE BLOCKER: short.full.mp4 exists (a real master) beside a cut
    short.mp4, and short.trim.json is missing or corrupt - reachable by a
    crash between scratch.replace(short) and the state write, or a deleted/
    corrupted sidecar. Before the fix, is_pending's own comparison never
    looked at the master and reported "nothing pending" here, so all three
    delivery paths (this route, post_upload, cmd_upload) handed out the CUT
    file as if it were the full render."""

    def _unknown_clip(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry(duration=60.0))
        clipstore.short_path(directory).write_bytes(b"CUT-BUT-LOOKS-FINE")
        clipstore.short_master_path(directory).write_bytes(b"FULL-MASTER")
        # No short.trim.json at all: the crash-window case.
        return directory

    def test_summary_reports_trim_unknown(self, event_dir, client):
        directory = self._unknown_clip(event_dir)
        body = client.get(f"{EV}/clips/{directory.name}").json()
        assert body["trim_unknown"] is True
        assert body["trim"] is None and body["trim_applied"] is None

    def test_an_ordinary_clip_is_not_trim_unknown(self, event_dir, client):
        directory = self._clip_with_short_helper(event_dir)
        body = client.get(f"{EV}/clips/{directory.name}").json()
        assert body["trim_unknown"] is False

    def _clip_with_short_helper(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry(duration=60.0))
        clipstore.short_path(directory).write_bytes(b"ORDINARY")
        return directory

    def test_the_download_form_is_refused(self, event_dir, client):
        directory = self._unknown_clip(event_dir)
        response = client.get(f"{EV}/clips/{directory.name}/short?as=download")
        assert response.status_code == 409

    def test_upload_is_refused(self, event_dir, client):
        directory = self._unknown_clip(event_dir)
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.KEPT, transcript=None))
        response = client.post(f"{EV}/clips/{directory.name}/upload")
        assert response.status_code == 409
        assert "trim" in response.json()["detail"].lower()


class TestRenderOnly:
    def test_auth_reports_manual_mode(self, manual_client):
        body = manual_client.get(f"/api/channels/{CHANNEL}/auth").json()
        assert body["upload_mode"] == "manual"

    def test_auth_reports_api_mode_by_default(self, client):
        body = client.get(f"/api/channels/{CHANNEL}/auth").json()
        assert body["upload_mode"] == "api"

    def test_connect_is_refused_for_a_render_only_channel(self, manual_client):
        response = manual_client.post(f"/api/channels/{CHANNEL}/auth/connect", json={})
        assert response.status_code == 409
        assert "render-only" in response.json()["detail"]

    def test_upload_is_refused_for_a_render_only_channel(self, manual_client, event_dir):
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        clipstore.short_path(directory).write_bytes(b"mp4")
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        response = manual_client.post(f"/api/channels/{CHANNEL}/events/{EVENT}/clips/{directory.name}/upload")
        assert response.status_code == 409
        assert "render-only" in response.json()["detail"]

    def test_upload_preview_still_works_for_a_render_only_channel(self, manual_client, event_dir):
        directory = clipstore.write_clip(event_dir, {
            "url": "https://www.youtube.com/watch/vid/0-12", "video_id": "vid",
            "hook": "CRASH", "source_title": "ERF", "start": 0.0, "end": 12.0,
            "duration": 12.0, "error": None})
        editorial.save(directory, editorial.Edit(title=None, status="kept", transcript=None))
        response = manual_client.get(
            f"/api/channels/{CHANNEL}/events/{EVENT}/clips/{directory.name}/upload-preview")
        assert response.status_code == 200
        assert response.json()["title"] == "CRASH"


class TestWorkspaceShell:
    """The start screen's own two listing routes, and the two failure
    surfaces every scoped route beneath them shares (an unknown channel or
    an unknown event both 404, from _load_profile/_load_channel), plus the
    SPA fallback that lets a deep link into the built React page survive a
    reload without shadowing anything under /api."""

    def test_lists_channels_including_erf(self, client):
        response = client.get("/api/channels")
        assert response.status_code == 200
        names = {row["name"] for row in response.json()}
        assert "erf" in names

    def test_get_channel_returns_identity_fields(self, client, studio_profile):
        response = client.get(f"/api/channels/{CHANNEL}")
        assert response.status_code == 200
        body = response.json()
        assert body["handle"] and body["display_name"] and body["footer"]

    def test_get_channel_unknown_404(self, client):
        assert client.get("/api/channels/ghost").status_code == 404

    def test_lists_events_including_studio_test(self, client, studio_profile):
        response = client.get(f"/api/channels/{CHANNEL}/events")
        assert response.status_code == 200
        names = {row["name"] for row in response.json()}
        # community-clips-back-catalogue is copied in along with the erf
        # fixture channel - present alongside studio-test, not instead of it.
        assert "studio-test" in names

    def test_unknown_channel_is_404(self, client):
        response = client.get("/api/channels/nope/events/x/clips")
        assert response.status_code == 404

    def test_unknown_event_is_404(self, client):
        response = client.get(f"/api/channels/{CHANNEL}/events/nope/clips")
        assert response.status_code == 404

    def test_spa_fallback_serves_html_for_a_deep_route(self, client):
        response = client.get("/some/deep/route")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_spa_fallback_does_not_shadow_the_api(self, client):
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404


class TestEventAdmin:
    def test_create_makes_an_empty_event_and_returns_its_entry(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR   # the tmp copy the fixture set
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": "round-9"})
        assert response.status_code == 201
        assert response.json()["name"] == "round-9"
        assert (channels / "erf" / "events" / "round-9").is_dir()

    def test_create_rejects_a_bad_name_400(self, client):
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": "../escape"})
        assert response.status_code == 400

    def test_create_on_existing_event_409(self, client):
        response = client.post(f"/api/channels/{CHANNEL}/events", json={"name": EVENT})
        assert response.status_code == 409

    def test_create_on_unknown_channel_404(self, client):
        response = client.post("/api/channels/nope/events", json={"name": "round-9"})
        assert response.status_code == 404

    def test_rename_moves_the_event(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.patch(f"/api/channels/{CHANNEL}/events/{EVENT}",
                                json={"name": "renamed"})
        assert response.status_code == 200
        assert response.json()["name"] == "renamed"
        assert (channels / "erf" / "events" / "renamed").is_dir()
        assert not (channels / "erf" / "events" / EVENT).exists()

    def test_rename_unknown_event_404(self, client):
        response = client.patch(f"/api/channels/{CHANNEL}/events/ghost", json={"name": "x"})
        assert response.status_code == 404

    def test_delete_removes_the_event(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.delete(f"/api/channels/{CHANNEL}/events/{EVENT}")
        assert response.status_code == 200
        assert response.json()["deleted"] == EVENT
        assert not (channels / "erf" / "events" / EVENT).exists()

    def test_delete_unknown_event_404(self, client):
        response = client.delete(f"/api/channels/{CHANNEL}/events/ghost")
        assert response.status_code == 404

    def test_event_scoped_read_rejects_traversal_event_segment_400(self, client, studio_profile):
        # The event-scoped read routes go through _load_profile, which used to
        # skip validate_segment. A traversal {event} is now a 400, not a path
        # that reaches profile.load / exec_module of a layout.py.
        assert client.get(f"/api/channels/{CHANNEL}/events/%2e%2e/clips").status_code == 400
        assert client.get(f"/api/channels/%2e%2e/events/{EVENT}/clips").status_code == 400

    def test_clip_name_traversal_is_a_404_not_a_path_read(self, client, studio_profile):
        # An unsafe clip {name} must be refused before building clips_dir/name.
        assert client.get(f"{EVENT_PREFIX}/clips/%2e%2e").status_code in (400, 404)

    def test_mutating_request_with_foreign_origin_is_403(self, client, studio_profile):
        # A cross-origin browser POST (CSRF, or a DNS-rebound page whose Origin is
        # still the attacker's domain) is refused before it can act.
        r = client.post("/api/channels", json={"slug": "x", "display_name": "X",
                                               "handle": "@x"},
                        headers={"origin": "http://evil.example"})
        assert r.status_code == 403

    def test_mutating_request_with_local_origin_is_allowed(self, client, studio_profile):
        # The operator's own studio page (Origin 127.0.0.1/localhost) is fine.
        r = client.delete(f"/api/channels/{CHANNEL}/events/ghost",
                          headers={"origin": "http://127.0.0.1:8765"})
        assert r.status_code != 403

    def test_read_with_foreign_origin_is_unaffected(self, client, studio_profile):
        r = client.get("/api/channels", headers={"origin": "http://evil.example"})
        assert r.status_code == 200

    def test_a_traversal_channel_segment_is_rejected_not_escaped(self, client, studio_profile):
        # A percent-encoded '..' channel must not let create/delete act outside
        # channels/ (it once could - the channel segment was unvalidated). Both
        # are refused (400 bad channel), and nothing is created/removed outside.
        channels = profile_module.CHANNELS_DIR
        outside = channels.parent / "events" / "victim"
        outside.mkdir(parents=True)
        (outside / "precious.txt").write_text("keep me")

        assert client.post("/api/channels/%2e%2e/events",
                           json={"name": "pwned"}).status_code == 400
        assert client.delete("/api/channels/%2e%2e/events/victim").status_code == 400
        assert (outside / "precious.txt").read_text() == "keep me"
        assert not (channels.parent / "events" / "pwned").exists()

    def test_a_live_lock_makes_delete_and_rename_409(self, client, studio_profile):
        from yt_shorts.lock import LOCK_NAME
        channels = profile_module.CHANNELS_DIR
        (channels / "erf" / "events" / EVENT / LOCK_NAME).write_text(str(os.getpid()))
        assert client.delete(f"/api/channels/{CHANNEL}/events/{EVENT}").status_code == 409
        assert client.patch(f"/api/channels/{CHANNEL}/events/{EVENT}",
                            json={"name": "x"}).status_code == 409


class TestChannelAdmin:
    FIELDS = {"id": "UCnew", "channel_url": "https://www.youtube.com/channel/UCnew",
              "handle": "@new", "display_name": "New League", "language": "en",
              "footer": "NEW | @new"}

    def test_create_makes_a_channel_and_returns_its_entry(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.post("/api/channels", json={"slug": "newchan", **self.FIELDS})
        assert response.status_code == 201
        assert response.json()["name"] == "newchan"
        assert response.json()["display_name"] == "New League"
        assert (channels / "newchan" / "channel.json").is_file()
        assert (channels / "newchan" / "brand.json").is_file()

    def test_create_bad_slug_400(self, client):
        response = client.post("/api/channels", json={"slug": "../x", **self.FIELDS})
        assert response.status_code == 400

    def test_create_missing_field_400(self, client):
        response = client.post("/api/channels", json={"slug": "newchan", **{**self.FIELDS, "footer": ""}})
        assert response.status_code == 400

    def test_create_existing_channel_409(self, client):
        response = client.post("/api/channels", json={"slug": CHANNEL, **self.FIELDS})
        assert response.status_code == 409

    def test_a_traversal_channel_segment_is_400_not_escaped(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        outside = channels.parent / "victim"
        outside.mkdir()
        (outside / "keep").write_text("x")
        assert client.delete("/api/channels/%2e%2e").status_code == 400
        assert (outside / "keep").read_text() == "x"

    def test_edit_updates_channel_json(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.patch(f"/api/channels/{CHANNEL}", json={"display_name": "ERF Renamed"})
        assert response.status_code == 200
        assert json.loads((channels / "erf" / "channel.json").read_text())["display_name"] == "ERF Renamed"

    def test_rename_moves_the_channel(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.post(f"/api/channels/{CHANNEL}/rename", json={"name": "erf2"})
        assert response.status_code == 200
        assert response.json()["name"] == "erf2"
        assert (channels / "erf2").is_dir()
        assert not (channels / "erf").exists()

    def test_delete_removes_the_channel(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        response = client.delete(f"/api/channels/{CHANNEL}")
        assert response.status_code == 200
        assert response.json()["deleted"] == CHANNEL
        assert not (channels / "erf").exists()

    def test_a_live_event_lock_makes_rename_and_delete_409(self, client, studio_profile):
        from yt_shorts.lock import LOCK_NAME
        channels = profile_module.CHANNELS_DIR
        (channels / "erf" / "events" / EVENT / LOCK_NAME).write_text(str(os.getpid()))
        assert client.post(f"/api/channels/{CHANNEL}/rename", json={"name": "erf2"}).status_code == 409
        assert client.delete(f"/api/channels/{CHANNEL}").status_code == 409


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

    def test_upload_over_size_limit_is_413_not_buffered(self, client, studio_profile):
        # A body over the 10 MB cap is refused (413) via the Content-Length /
        # streamed-read guard, before font_admin ever sees the full bytes.
        oversized = b"\x00" * (font_admin.MAX_FONT_BYTES + 1)
        r = client.post(f"/api/channels/{CHANNEL}/fonts/Huge.ttf", content=oversized)
        assert r.status_code == 413

    def test_upload_traversal_filename_is_rejected_not_escaped(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        # A ".." filename reaches save_font and is rejected by validate_segment (400).
        r = client.post(f"/api/channels/{CHANNEL}/fonts/%2e%2e", content=self._erf_font_bytes())
        assert r.status_code == 400
        # An encoded-slash filename never matches the single-segment {filename}
        # route, so it also cannot reach save_font - it falls through to the
        # unconditional POST/PUT/PATCH/DELETE /api/{full_path} catch-all
        # instead, which answers 404 rather than the 405 Starlette would give
        # a partial (path matches, method doesn't) route match. Either way
        # nothing is written outside the channel's fonts/ dir.
        r2 = client.post(f"/api/channels/{CHANNEL}/fonts/%2e%2e%2fescape.ttf",
                         content=self._erf_font_bytes())
        assert r2.status_code >= 400
        assert not (channels / "escape.ttf").exists()
        assert not (channels / "erf" / "escape.ttf").exists()

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

    def test_preview_absolute_font_path_rejected_409(self, client, studio_profile):
        # A client-supplied absolute font ref must be rejected before it can
        # reach ImageFont.truetype - it must never open a file outside the
        # channel's fonts/ dir (an existence oracle / traversal). The ref goes
        # through the same fonts/<safe-segment> guard PUT /brand uses.
        colors = {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"}
        r = client.post(f"/api/channels/{CHANNEL}/brand/preview", json={
            "colors": colors,
            "fonts": {"hook": "/etc/passwd", "small": "/etc/passwd"}})
        assert r.status_code == 409

    def test_preview_traversal_font_ref_rejected_409(self, client, studio_profile):
        colors = {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"}
        r = client.post(f"/api/channels/{CHANNEL}/brand/preview", json={
            "colors": colors,
            "fonts": {"hook": "fonts/../../../../etc/hosts",
                      "small": "fonts/../../../../etc/hosts"}})
        assert r.status_code == 409

    _FONTS = {"hook": "fonts/BarlowCondensed-Bold.ttf", "small": "fonts/BarlowCondensed-Bold.ttf"}

    def _seed_logo(self):
        """Give the tmp erf copy an assets/logo.png (the fixture has none)."""
        from PIL import Image
        assets = profile_module.CHANNELS_DIR / "erf" / "assets"
        assets.mkdir(exist_ok=True)
        Image.new("RGBA", (120, 120), (255, 255, 0, 255)).save(assets / "logo.png")

    def test_put_upload_mode_toggles_manual_and_api(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        r = client.put(f"/api/channels/{CHANNEL}/upload", json={"mode": "manual"})
        assert r.status_code == 200 and r.json()["mode"] == "manual"
        assert json.loads((channels / "erf" / "brand.json").read_text())["upload"]["mode"] == "manual"
        r = client.put(f"/api/channels/{CHANNEL}/upload", json={"mode": "api"})
        assert r.status_code == 200
        assert json.loads((channels / "erf" / "brand.json").read_text())["upload"]["mode"] == "api"

    def test_put_upload_mode_rejects_unknown_400(self, client):
        r = client.put(f"/api/channels/{CHANNEL}/upload", json={"mode": "public"})
        assert r.status_code == 400

    def test_put_brand_applies_output_geometry(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        output = json.loads((channels / "erf" / "brand.json").read_text())["output"]
        output = {**output, "video_y": output["video_y"] + 8}
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={"fonts": self._FONTS, "output": output})
        assert r.status_code == 200
        assert json.loads((channels / "erf" / "brand.json").read_text())["output"]["video_y"] == output["video_y"]

    def test_put_brand_rejects_window_outside_frame_400(self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        output = json.loads((channels / "erf" / "brand.json").read_text())["output"]
        output = {**output, "video_y": output["height"] + 100}  # window past the bottom edge
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={"fonts": self._FONTS, "output": output})
        assert r.status_code == 400

    def test_put_brand_applies_logo(self, client, studio_profile):
        self._seed_logo()
        channels = profile_module.CHANNELS_DIR
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={
            "fonts": self._FONTS,
            "logo": {"file": "assets/logo.png", "position": "bottom-right",
                     "variant": "color", "opacity": 0.7}})
        assert r.status_code == 200
        logo = json.loads((channels / "erf" / "brand.json").read_text())["logo"]
        assert logo["position"] == "bottom-right" and logo["variant"] == "color"

    def test_put_brand_rejects_bad_logo_position_400(self, client, studio_profile):
        self._seed_logo()
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={
            "fonts": self._FONTS,
            "logo": {"file": "assets/logo.png", "position": "middle"}})
        assert r.status_code == 400

    def test_preview_includes_the_logo(self, client, studio_profile):
        self._seed_logo()
        r = client.post(f"/api/channels/{CHANNEL}/brand/preview", json={
            "colors": {"text": "#FFFFFF", "base": "#004625", "accent": "#144E53", "edge": "#B8F5CA"},
            "fonts": self._FONTS,
            "logo": {"file": "assets/logo.png", "position": "bottom-right", "variant": "color"}})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"


class TestBrandDetectRoute:
    """detect (which model provider scores moments) accepted by PUT .../brand
    - see profile._validate_detect (Task 4) for the validation rules this
    borrows, and brand_admin.TestDetectSection for the same coverage one
    layer down."""

    def test_put_stores_a_valid_detect_section(self, client, studio_profile):
        r = client.put(f"/api/channels/{CHANNEL}/brand",
                        json={"detect": {"provider": "gemini", "model": "x"}})
        assert r.status_code == 200
        assert r.json()["brand"]["detect"]["provider"] == "gemini"

    def test_put_refuses_an_unknown_provider_400(self, client, studio_profile):
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={"detect": {"provider": "nope"}})
        assert r.status_code == 400

    def test_put_rejects_a_hostile_provider_shape_rather_than_coercing_it(self, client, studio_profile):
        # A list where a string provider is expected must be REJECTED, not
        # silently coerced into something profile._validate_detect would
        # then accept - the same trap PatchClipBody's trim field hit with a
        # JSON `true` coerced into `1.0` before validate_trim ever saw it
        # (see that field's own comment). detect is typed as a plain dict,
        # so a list value here reaches profile._validate_detect unconverted
        # and is refused there.
        r = client.put(f"/api/channels/{CHANNEL}/brand",
                        json={"detect": {"provider": ["anthropic"]}})
        assert r.status_code == 400

    def test_put_rejects_a_bare_bool_detect_value(self, client, studio_profile):
        # pydantic itself refuses a non-object/non-null 'detect' before
        # brand_admin ever runs - a 422, not the mapped 400 a value that
        # reaches profile._validate_detect gets.
        r = client.put(f"/api/channels/{CHANNEL}/brand", json={"detect": True})
        assert r.status_code == 422


class TestPaletteRoute:
    """GET .../brand/palette - the channel logo's own colours, proposed as a
    palette (yt_shorts.palette). tests/test_studio_brand_api.py does not
    exist in this repo; these classes live here, beside TestBrandFonts
    (the other channel-brand route tests) instead - see the task report."""

    def _assign_logo(self):
        # A real opaque PNG under assets/, assigned in brand.json directly
        # (same technique manual_client already uses to hand-edit brand.json
        # in this file) - the palette route reads brand.json's own logo, it
        # is not given one in the request body the way preview is.
        from PIL import Image
        channels = profile_module.CHANNELS_DIR
        assets = channels / "erf" / "assets"
        assets.mkdir(exist_ok=True)
        Image.new("RGBA", (120, 120), (255, 200, 0, 255)).save(assets / "logo.png")
        brand_path = channels / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["logo"] = {"file": "assets/logo.png", "position": "bottom-right", "variant": "color"}
        brand_path.write_text(json.dumps(brand), encoding="utf-8")

    def test_returns_roles_and_swatches(self, client, studio_profile):
        self._assign_logo()
        body = client.get(f"/api/channels/{CHANNEL}/brand/palette").json()
        assert "base" in body["colors"]
        assert body["swatches"]
        assert set(body["swatches"][0]) == {"hex", "share"}

    def test_every_returned_colour_is_a_hex_string(self, client, studio_profile):
        self._assign_logo()
        body = client.get(f"/api/channels/{CHANNEL}/brand/palette").json()
        for value in body["colors"].values():
            assert value.startswith("#") and len(value) == 7

    def test_a_channel_with_no_logo_is_409(self, client, studio_profile):
        """A read that cannot be performed, reported the way the brand
        preview route reports the same class of failure. The erf fixture
        brand.json has no 'logo' key by default - no need to remove one."""
        assert client.get(f"/api/channels/{CHANNEL}/brand/palette").status_code == 409

    def test_an_unsafe_channel_segment_is_refused(self, client, studio_profile):
        assert client.get("/api/channels/..%2F../brand/palette").status_code in (400, 404)

    def test_an_unknown_channel_is_404(self, client, studio_profile):
        assert client.get("/api/channels/nope/brand/palette").status_code == 404


class TestBandsThroughTheRoutes:
    """bands accepted by PUT .../brand and POST .../brand/preview - see
    TestPaletteRoute's own note on why these live here."""

    def test_put_stores_bands(self, client, studio_profile):
        response = client.put(f"/api/channels/{CHANNEL}/brand", json={"bands": {"top": 0.25}})
        assert response.status_code == 200
        assert response.json()["brand"]["bands"]["top"] == 0.25

    def test_put_refuses_an_out_of_range_band(self, client, studio_profile):
        assert client.put(
            f"/api/channels/{CHANNEL}/brand", json={"bands": {"top": 9}}).status_code == 400

    def test_preview_accepts_bands(self, client, studio_profile):
        """The preview must honour an UNSAVED band value, or the slider
        would show the operator the old picture while they drag it. A
        status-only assertion here passes even when the route's config dict
        never carries 'bands' through to build_overlay at all - dropping the
        key left every other band/palette test in this file green while the
        preview silently stopped reacting to the slider. So this asserts the
        rendered PNG bytes actually differ between two opposite band values,
        which only a route that truly threads 'bands' into the overlay can
        satisfy."""
        opaque = client.post(
            f"/api/channels/{CHANNEL}/brand/preview",
            json={"bands": {"top": 1.0, "bottom": 1.0}})
        clear = client.post(
            f"/api/channels/{CHANNEL}/brand/preview",
            json={"bands": {"top": 0.0, "bottom": 0.0}})
        assert opaque.status_code == 200
        assert clear.status_code == 200
        assert opaque.content != clear.content


class TestSettings:
    def _fake_workspace(self, root):
        from yt_shorts.workspace import Workspace
        return Workspace(root=root, channels_dir=root / "channels", origin="YT_SHORTS_DATA")

    def test_settings_lists_channels_with_connection_state(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        (root / "auth").mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: object())   # connected
        monkeypatch.setattr(api, "google_require", lambda feature: None)          # libs present
        body = client.get("/api/settings").json()
        assert body["workspace"]["origin"] == "YT_SHORTS_DATA"
        assert body["workspace"]["channel_count"] == 1
        assert body["workspace"]["google_upload_available"] is True
        erf = next(r for r in body["channels"] if r["channel"] == "erf")
        assert erf["connected"] is True
        assert erf["upload_mode"] == "api"
        assert erf["channel_id"]                       # the fixture's real id, non-empty
        assert isinstance(erf["remaining_uploads"], int)
        assert erf["error"] is None

    def test_settings_reports_disconnected_and_missing_google(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)        # not connected
        def no_google(feature):
            raise api.GoogleUnavailable("install the libs")
        monkeypatch.setattr(api, "google_require", no_google)
        body = client.get("/api/settings").json()
        assert body["workspace"]["google_upload_available"] is False
        assert body["channels"][0]["connected"] is False

    def test_settings_marks_a_manual_channel(self, manual_client, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        monkeypatch.setattr(api, "load_credentials", lambda *a, **k: None)
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        body = manual_client.get("/api/settings").json()
        modes = {r["channel"]: r["upload_mode"] for r in body["channels"]}
        assert "manual" in modes.values()

    def test_disconnect_removes_only_the_token(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        auth_dir = root / "auth"
        auth_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        channel_id = json.loads((profile_module.CHANNELS_DIR / "erf" / "channel.json").read_text())["id"]
        (auth_dir / f"token-{channel_id}.json").write_text("{}", encoding="utf-8")
        secret = auth_dir / "client_secret.json"
        secret.write_text("SECRET", encoding="utf-8")
        r = client.delete(f"/api/channels/{CHANNEL}/auth")
        assert r.status_code == 200
        assert r.json()["disconnected"] == channel_id
        assert not (auth_dir / f"token-{channel_id}.json").exists()
        assert secret.exists()

    def test_disconnect_without_a_token_is_404(self, client, studio_profile, monkeypatch):
        root = profile_module.CHANNELS_DIR.parent
        (root / "auth").mkdir(exist_ok=True)
        monkeypatch.setattr(api, "_resolve_workspace", lambda: self._fake_workspace(root))
        r = client.delete(f"/api/channels/{CHANNEL}/auth")
        assert r.status_code == 404

    def test_disconnect_rejects_a_bad_channel_segment(self, client, studio_profile):
        r = client.delete("/api/channels/%2e%2e/auth")
        assert r.status_code in (400, 404, 405)

    def test_disconnect_on_channel_json_without_id_is_404_not_500(self, client, studio_profile):
        # A well-formed channel.json missing its "id" must be a clean 404, never
        # a KeyError 500 on this destructive route.
        path = profile_module.CHANNELS_DIR / "erf" / "channel.json"
        data = json.loads(path.read_text())
        data.pop("id", None)
        path.write_text(json.dumps(data))
        r = client.delete(f"/api/channels/{CHANNEL}/auth")
        assert r.status_code == 404

    def test_connect_on_channel_json_without_id_is_404_not_500(self, client, studio_profile, monkeypatch):
        monkeypatch.setattr(api, "google_require", lambda feature: None)
        path = profile_module.CHANNELS_DIR / "erf" / "channel.json"
        data = json.loads(path.read_text())
        data.pop("id", None)
        path.write_text(json.dumps(data))
        r = client.post(f"/api/channels/{CHANNEL}/auth/connect", json={})
        assert r.status_code == 404


class TestWorkspaces:
    """GET/switch/create for the multi-workspace shell, plus the FS browser.

    ``client``/``studio_profile`` already repoint ``profile.CHANNELS_DIR`` at
    a tmp copy of the erf fixture (see the module docstring), but the new
    routes here resolve the *workspace* (``workspace.resolve``/``_resolve_workspace``),
    a separate axis from that channels-dir repoint - so every test that
    switches or creates a workspace also repoints ``api._config_home`` at a
    tmp dir, never the operator's real ``~/.config``.
    """

    NEW_CHANNEL_FIELDS = {
        "slug": "demo", "id": "UCx", "channel_url": "https://youtube.com/channel/UCx",
        "handle": "@d", "display_name": "D", "language": "en", "footer": "D | @d",
    }

    def test_get_workspaces_reports_current(self, client, studio_profile):
        body = client.get("/api/workspaces").json()
        assert "current" in body and body["current"]["path"]
        assert "recent" in body

    def _use_tmp_home(self, monkeypatch, tmp_path):
        # get_workspaces()/_guard_reroot() call the real workspace.resolve()
        # bare (no config_home override - see api.py), which independently
        # falls back to Path.home()/".config" when its own config_home
        # param is None. Patch Path.home() itself, so BOTH that internal
        # default AND api._config_home() (patched to match) land on the
        # same tmp file - the studio's own config write and workspace.resolve()'s
        # read of "current" agree, and neither ever touches the operator's
        # real ~/.config.
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / ".config")

    def test_switch_to_a_created_workspace(self, client, studio_profile, tmp_path, monkeypatch):
        self._use_tmp_home(monkeypatch, tmp_path)
        parent = tmp_path / "parent"
        parent.mkdir()
        made = client.post("/api/workspaces/create",
                           json={"parent": str(parent), "name": "ws-new"})
        assert made.status_code == 200
        assert made.json()["current"]["name"] == "ws-new"
        assert made.json()["current"]["path"] == str(parent / "ws-new")
        # A channel created NOW lands in the NEW workspace, proving the
        # closed-over channels_dir (and profile.CHANNELS_DIR) really re-rooted.
        created = client.post("/api/channels", json=self.NEW_CHANNEL_FIELDS)
        assert created.status_code == 201
        assert (parent / "ws-new" / "channels" / "demo" / "channel.json").is_file()

    def test_switch_refused_while_a_job_runs(self, client, studio_profile, tmp_path, monkeypatch):
        from yt_shorts.studio.jobs import JobStore
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        target = api._workspaces.create_workspace(tmp_path, "wsX", "2026-07-24T00:00:00")
        monkeypatch.setattr(JobStore, "any_running", lambda self: True)
        r = client.post("/api/workspaces/switch", json={"path": str(target)})
        assert r.status_code == 409

    def test_switch_to_a_non_workspace_is_400(self, client, studio_profile, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        not_a_workspace = tmp_path / "plain-dir"
        not_a_workspace.mkdir()
        r = client.post("/api/workspaces/switch", json={"path": str(not_a_workspace)})
        assert r.status_code == 400

    def test_create_refused_while_a_job_runs(self, client, studio_profile, tmp_path, monkeypatch):
        from yt_shorts.studio.jobs import JobStore
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        monkeypatch.setattr(JobStore, "any_running", lambda self: True)
        r = client.post("/api/workspaces/create",
                        json={"parent": str(tmp_path), "name": "ws-busy"})
        assert r.status_code == 409

    def test_create_with_a_bad_name_is_400(self, client, studio_profile, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        r = client.post("/api/workspaces/create",
                        json={"parent": str(tmp_path), "name": "../escape"})
        assert r.status_code == 400

    def test_switch_refused_when_env_locked(self, client, studio_profile, tmp_path, monkeypatch):
        from yt_shorts.workspace import Workspace
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        locked = Workspace(root=tmp_path, channels_dir=tmp_path / "channels",
                           origin="YT_SHORTS_DATA")
        monkeypatch.setattr(api, "_resolve_workspace", lambda: locked)
        target = api._workspaces.create_workspace(tmp_path / "elsewhere", "wsY",
                                                    "2026-07-24T00:00:00")
        r = client.post("/api/workspaces/switch", json={"path": str(target)})
        assert r.status_code == 409

    def test_switch_repoints_the_central_log_to_the_new_workspace(
            self, client, studio_profile, tmp_path, monkeypatch):
        """`logsetup.configure_logging` is idempotent by LOGGER NAME, not by
        path (see its own docstring), and create_app() calls it exactly once
        - without the fix in `_switch_to`, the 'ytshorts' central logger
        would keep writing to whichever workspace was current when the
        process/app started, forever, even after a switch. See api.py's
        _switch_to for the re-point this pins."""
        import logging

        from yt_shorts import logsetup
        from yt_shorts import workspace as workspace_module

        self._use_tmp_home(monkeypatch, tmp_path)
        parent = tmp_path / "parent"
        parent.mkdir()
        try:
            made = client.post("/api/workspaces/create",
                               json={"parent": str(parent), "name": "ws-log"})
            assert made.status_code == 200

            logger = logging.getLogger("ytshorts")
            logger.info("after switch marker")
            for handler in logger.handlers:
                handler.flush()

            new_log = (workspace_module.logs_dir(parent / "ws-log")
                      / workspace_module.CENTRAL_LOG_NAME)
            assert new_log.exists()
            assert "after switch marker" in new_log.read_text(encoding="utf-8")
        finally:
            logsetup.close_logging("ytshorts")   # don't leak this test's log destination

    def test_fs_lists_directories(self, client, tmp_path):
        (tmp_path / "sub").mkdir()
        r = client.get("/api/fs", params={"path": str(tmp_path)})
        assert r.status_code == 200
        assert "sub" in {e["name"] for e in r.json()["entries"]}

    def test_fs_defaults_to_home_when_no_path_given(self, client):
        r = client.get("/api/fs")
        assert r.status_code == 200
        assert r.json()["path"] == str(Path.home())

    def test_copy_starts_a_job_and_clones(self, client, studio_profile, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        # run the copy synchronously so the test is deterministic
        monkeypatch.setattr(jobs, "_spawn", lambda fn: fn())
        r = client.post("/api/workspaces/copy",
                        json={"parent": str(tmp_path), "name": "clone"})
        assert r.status_code == 200 and "job_id" in r.json()
        job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
        assert job["status"] == "done"
        assert (tmp_path / "clone" / "channels").is_dir()   # erf fixture cloned

    def test_copy_refused_on_the_repository_fallback(self, client, studio_profile, tmp_path,
                                                       monkeypatch):
        # If workspace.resolve() fell all the way through to the repository
        # fallback (no YT_SHORTS_DATA, no ~/YT-Shorts-Data), the current
        # workspace root IS the git repo - a copy would shutil.copytree the
        # whole repository (.git, src/, everything). _guard_reroot only
        # refuses origin == "YT_SHORTS_DATA", so this needs its own guard.
        from yt_shorts.workspace import Workspace
        monkeypatch.setattr(api, "_config_home", lambda: tmp_path / "cfg")
        on_repo = Workspace(root=tmp_path, channels_dir=tmp_path / "channels",
                            origin="repository")
        monkeypatch.setattr(api, "_resolve_workspace", lambda: on_repo)
        r = client.post("/api/workspaces/copy",
                        json={"parent": str(tmp_path), "name": "clone-of-repo"})
        assert r.status_code == 409
        assert not (tmp_path / "clone-of-repo").exists()


class TestEventBrand:
    """GET/PUT .../brand, POST/DELETE .../fonts/{filename} and POST
    .../brand/preview at event scope - mirrors TestBrandFonts (channel scope)
    but against the event's OWN brand.json override, layered over the
    channel's (see yt_shorts.event_brand_admin). ``studio_profile`` already
    creates an empty ``erf/events/studio-test`` under the repointed
    CHANNELS_DIR, so the event exists for real with no extra seeding."""

    def _erf_font_bytes(self):
        return (FIXTURE_CHANNELS / "erf" / "fonts" / "BarlowCondensed-Bold.ttf").read_bytes()

    def test_get_event_brand_reports_channel_and_effective(self, client, studio_profile):
        r = client.get(f"{EVENT_PREFIX}/brand")
        assert r.status_code == 200
        body = r.json()
        assert "override" in body and "channel" in body and "effective" in body
        assert body["override"] == {}   # nothing overridden yet
        assert "channel" in body["fonts"] and "event" in body["fonts"]
        assert "BarlowCondensed-Bold.ttf" in body["fonts"]["channel"]
        assert body["fonts"]["event"] == []

    def test_get_event_brand_unknown_event_404(self, client, studio_profile):
        r = client.get(f"/api/channels/{CHANNEL}/events/no-such-event/brand")
        assert r.status_code == 404

    def test_put_event_brand_stores_only_overridden_section(self, client, studio_profile):
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        colors = {**eff["colors"], "accent": "#FF0000"}
        r = client.put(f"{EVENT_PREFIX}/brand", json={"colors": colors})
        assert r.status_code == 200
        assert r.json()["effective"]["colors"]["accent"] == "#FF0000"
        # override holds ONLY colors - the channel brand.json is untouched
        again = client.get(f"{EVENT_PREFIX}/brand").json()
        assert set(again["override"]) == {"colors"}
        assert again["channel"]["colors"]["accent"] != "#FF0000"

    def test_put_event_brand_rejects_upload(self, client, studio_profile):
        r = client.put(f"{EVENT_PREFIX}/brand", json={"upload": {"mode": "api"}})
        assert r.status_code == 400

    def test_put_event_brand_rejects_detect(self, client, studio_profile):
        # 'detect' is not in event_brand_admin.OVERRIDE_SECTIONS, so the patch
        # filter drops it: without an explicit guard this answered 200 and
        # wrote nothing - a silent no-op, which is this project's named
        # recurring failure shape. The provider choice is ACCOUNT-scoped (whose
        # key and whose quota get spent), which puts it with 'upload', not with
        # colors/fonts.
        r = client.put(f"{EVENT_PREFIX}/brand",
                       json={"detect": {"provider": "gemini"}})
        assert r.status_code == 400
        assert "detect" in r.text
        assert not (profile_module.CHANNELS_DIR / "erf" / "events" / EVENT
                    / "brand.json").exists()

    def test_put_event_brand_rejects_detect_even_beside_a_valid_section(
            self, client, studio_profile):
        # The mixed body is the dangerous one: the colors half would be written
        # and the detect half silently discarded, so the operator sees a
        # successful save that half-happened.
        r = client.put(f"{EVENT_PREFIX}/brand",
                       json={"colors": {"accent": "#FF0000"},
                             "detect": {"provider": "gemini"}})
        assert r.status_code == 400
        assert not (profile_module.CHANNELS_DIR / "erf" / "events" / EVENT
                    / "brand.json").exists()

    def _seed_channel_logo(self):
        """Give the tmp erf copy an assets/logo.png (the fixture's channel
        brand.json has logo: null, so a logo override needs a real file to
        resolve against - mirrors TestBrandFonts._seed_logo)."""
        from PIL import Image
        assets = profile_module.CHANNELS_DIR / "erf" / "assets"
        assets.mkdir(exist_ok=True)
        Image.new("RGBA", (120, 120), (255, 255, 0, 255)).save(assets / "logo.png")

    def test_put_event_brand_logo_none_is_stored_not_dropped(self, client, studio_profile):
        # The erf fixture channel brand has logo: null (no channel-level logo
        # at all), so first establish a real logo override to overturn -
        # otherwise "override to null" would be indistinguishable from
        # "never overridden" and would prove nothing.
        self._seed_channel_logo()
        r = client.put(f"{EVENT_PREFIX}/brand", json={
            "logo": {"file": "assets/logo.png", "position": "bottom-right",
                     "variant": "color"}})
        assert r.status_code == 200
        again = client.get(f"{EVENT_PREFIX}/brand").json()
        assert "logo" in again["override"] and again["override"]["logo"] is not None

        # Now override to explicit "no logo". Before the fix, the
        # `v is not None` filter dropped `logo: null` from the patch entirely,
        # silently re-inheriting the channel logo instead of removing it.
        r = client.put(f"{EVENT_PREFIX}/brand", json={"logo": None})
        assert r.status_code == 200
        body = r.json()
        assert "logo" in body["override"] and body["override"]["logo"] is None
        assert body["effective"]["logo"] is None

        # And it survives a reload (GET), so the UI toggle stays "override"
        # rather than snapping back to "inherit".
        reloaded = client.get(f"{EVENT_PREFIX}/brand").json()
        assert "logo" in reloaded["override"] and reloaded["override"]["logo"] is None
        assert reloaded["effective"]["logo"] is None

    def test_event_brand_preview_logo_none_renders_without_logo(self, client, studio_profile):
        self._seed_channel_logo()
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        r = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"], "fonts": eff["fonts"], "output": eff["output"],
            "logo": None})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_put_event_brand_bad_color_400(self, client, studio_profile):
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        colors = {**eff["colors"], "accent": "nope"}
        r = client.put(f"{EVENT_PREFIX}/brand", json={"colors": colors})
        assert r.status_code == 400

    def test_upload_event_font_saves_and_lists_it_separately_from_channel(
            self, client, studio_profile):
        channels = profile_module.CHANNELS_DIR
        r = client.post(f"{EVENT_PREFIX}/fonts/EventOnly.ttf", content=self._erf_font_bytes())
        assert r.status_code == 201
        assert "EventOnly.ttf" in r.json()["fonts"]
        assert (channels / "erf" / "events" / EVENT / "fonts" / "EventOnly.ttf").is_file()
        # Does not leak into the channel's own font list.
        assert "EventOnly.ttf" not in font_admin.list_fonts(channels, CHANNEL)

    def test_upload_event_font_rejects_non_font_bytes_400(self, client, studio_profile):
        r = client.post(f"{EVENT_PREFIX}/fonts/broken.ttf", content=b"nope")
        assert r.status_code == 400

    def test_delete_event_font_removes_it(self, client, studio_profile):
        client.post(f"{EVENT_PREFIX}/fonts/Removable.ttf", content=self._erf_font_bytes())
        r = client.delete(f"{EVENT_PREFIX}/fonts/Removable.ttf")
        assert r.status_code == 200
        assert "Removable.ttf" not in r.json()["fonts"]

    def test_delete_event_font_refuses_when_assigned_409(self, client, studio_profile):
        client.post(f"{EVENT_PREFIX}/fonts/Assigned.ttf", content=self._erf_font_bytes())
        r = client.put(f"{EVENT_PREFIX}/brand", json={
            "fonts": {"hook": "fonts/Assigned.ttf", "small": "fonts/Assigned.ttf"}})
        assert r.status_code == 200
        r = client.delete(f"{EVENT_PREFIX}/fonts/Assigned.ttf")
        assert r.status_code == 409

    def test_event_brand_preview_returns_png(self, client, studio_profile):
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        r = client.post(f"{EVENT_PREFIX}/brand/preview",
                        json={"colors": eff["colors"], "fonts": eff["fonts"],
                              "output": eff["output"]})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert len(r.content) > 100

    def test_event_brand_preview_prefers_event_only_font(self, client, studio_profile):
        # An event font, referenced only by the PATCH body (never saved), must
        # resolve event-first - proving the preview uses
        # event_brand_admin.resolve_event_font_ref rather than the channel-only
        # brand_admin.resolve_font_ref.
        client.post(f"{EVENT_PREFIX}/fonts/EventPreview.ttf", content=self._erf_font_bytes())
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        r = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"],
            "fonts": {"hook": "fonts/EventPreview.ttf", "small": "fonts/EventPreview.ttf"},
            "output": eff["output"]})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_event_brand_preview_missing_font_409(self, client, studio_profile):
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        r = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"],
            "fonts": {"hook": "fonts/absent.ttf", "small": "fonts/absent.ttf"},
            "output": eff["output"]})
        assert r.status_code == 409

    def test_event_brand_preview_accepts_bands(self, client, studio_profile):
        """Same guarantee as TestBandsThroughTheRoutes.test_preview_accepts_bands,
        against the EVENT-scoped preview route - it merges its patch's 'bands'
        over the channel's own and must thread the merged value into
        build_overlay too. A status-only assertion would pass even if the
        route's config dict never carried 'bands' at all, so this asserts the
        rendered PNG actually changes between two opposite band values."""
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        opaque = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"], "fonts": eff["fonts"], "output": eff["output"],
            "bands": {"top": 1.0, "bottom": 1.0}})
        clear = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"], "fonts": eff["fonts"], "output": eff["output"],
            "bands": {"top": 0.0, "bottom": 0.0}})
        assert opaque.status_code == 200
        assert clear.status_code == 200
        assert opaque.content != clear.content

    def test_event_brand_preview_traversal_font_ref_rejected_409(self, client, studio_profile):
        eff = client.get(f"{EVENT_PREFIX}/brand").json()["effective"]
        r = client.post(f"{EVENT_PREFIX}/brand/preview", json={
            "colors": eff["colors"],
            "fonts": {"hook": "fonts/../../../../etc/hosts",
                      "small": "fonts/../../../../etc/hosts"},
            "output": eff["output"]})
        assert r.status_code == 409


class TestWordBoundariesThroughTheRoutes:
    """The studio is the only way a human types word text, so these two call
    sites are the whole fix. They are tested separately because they fail
    separately: normalising only on save would show IT'SREIRACING in the live
    preview while the rendered short said IT'S REI RACING.
    """

    def test_patch_stores_the_boundary(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.patch(
            f"{EVENT_PREFIX}/clips/{directory.name}",
            json={"words": [{"start": 0.0, "end": 1.0, "text": "Rei"},
                            {"start": 1.0, "end": 2.0, "text": "Racing"}]})
        assert response.status_code == 200
        assert [w["text"] for w in response.json()["words"]] == [" Rei", " Racing"]
        saved = editorial.load(directory)
        assert [w["text"] for w in saved.transcript["words"]] == [" Rei", " Racing"]

    def test_patch_leaves_a_continuation_alone(self, event_dir, client):
        """The other half of the rule, through the route: a punctuation-led
        token is a continuation and must stay glued to what precedes it."""
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.patch(
            f"{EVENT_PREFIX}/clips/{directory.name}",
            json={"words": [{"start": 0.0, "end": 1.0, "text": "C"},
                            {"start": 1.0, "end": 2.0, "text": ".L"}]})
        assert response.status_code == 200
        assert [w["text"] for w in response.json()["words"]] == [" C", ".L"]

    def test_the_preview_renders_the_same_picture_either_way(self, event_dir, client):
        """The assertion that makes the preview and the render agree: a
        spaceless word and the same word carrying its boundary must produce
        IDENTICAL bytes, because the route normalises before drawing.
        Byte-comparing two preview responses is already trusted in this file
        (TestPreviewPost.test_title_omitted_falls_back_to_the_saved_effective_title
        does it), and asserting only a 200 here would pass against a preview
        that ignored the fix entirely.
        """
        directory = clipstore.write_clip(event_dir, clip_entry(hook="Speedy!"))
        _solid_video(clipstore.raw_path(directory))

        def preview(text):
            response = client.post(
                f"{EVENT_PREFIX}/clips/{directory.name}/preview",
                json={"at": 0.5,
                      "words": [{"start": 0.0, "end": 3.0, "text": " it's"},
                                {"start": 0.0, "end": 3.0, "text": text}]})
            assert response.status_code == 200
            return response.content

        assert preview("Rei") == preview(" Rei")
        # Guards the equality above from passing for the wrong reason: if the
        # route ignored `words` altogether, every preview would be identical
        # and that assertion would be worthless.
        assert preview("Rei") != preview("Racing")


class TestShortVersion:
    """The rendered short's URL is otherwise a constant, so a re-render leaves
    the studio's <video> pointing at the same src, React never touches the
    attribute, and the element keeps the resource it already loaded - the
    stale-player bug. This token is what makes the src change when the file
    does, whoever re-rendered it.
    """

    def test_an_absent_short_has_no_version(self, event_dir, studio_profile):
        directory = clipstore.write_clip(event_dir, clip_entry())
        assert studio_api._short_version(directory) is None

    def test_two_calls_with_no_change_agree(self, event_dir, studio_profile):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(b"one render")
        assert studio_api._short_version(directory) == studio_api._short_version(directory)

    def test_replacing_the_bytes_changes_the_token(self, event_dir, studio_profile):
        """Pins that it CHANGED, never what it equals - the shape is an
        implementation detail the client never parses. The two payloads
        deliberately differ in LENGTH as well as content, so this cannot
        depend on filesystem timestamp resolution.
        """
        directory = clipstore.write_clip(event_dir, clip_entry())
        path = clipstore.short_path(directory)
        path.write_bytes(b"old render")
        before = studio_api._short_version(directory)
        path.write_bytes(b"a new render of a completely different length")
        assert studio_api._short_version(directory) != before

    def test_the_list_and_the_detail_both_carry_it(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(b"pretend mp4")
        listed = client.get(f"{EVENT_PREFIX}/clips").json()[0]
        detail = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert listed["has_short"] is True
        assert listed["short_version"]
        assert detail["short_version"] == listed["short_version"]

    def test_a_clip_with_no_short_reports_null(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        detail = client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()
        assert detail["has_short"] is False
        assert detail["short_version"] is None


class TestShortCachePolicy:
    """`v` is a cache KEY, not a precondition. Every case serves the same
    bytes; only the policy differs, and the hard policy is handed out only
    when the token still identifies those bytes."""

    PAYLOAD = b"pretend this is an mp4"

    def _seed(self, event_dir):
        directory = clipstore.write_clip(event_dir, clip_entry())
        clipstore.short_path(directory).write_bytes(self.PAYLOAD)
        return directory

    def _version(self, client, directory):
        return client.get(f"{EVENT_PREFIX}/clips/{directory.name}").json()["short_version"]

    def test_a_matching_token_may_be_cached_hard(self, event_dir, client):
        directory = self._seed(event_dir)
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": self._version(client, directory)})
        assert response.status_code == 200
        assert response.content == self.PAYLOAD
        assert response.headers["cache-control"] == "private, max-age=31536000, immutable"

    @pytest.mark.parametrize("params", [None, {"v": "0-0"}, {"v": "not-a-token"}])
    def test_absent_stale_and_garbage_all_revalidate_and_all_serve_the_file(
            self, event_dir, client, params):
        directory = self._seed(event_dir)
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short", params=params)
        assert response.status_code == 200, "a token is never a precondition"
        assert response.content == self.PAYLOAD
        assert response.headers["cache-control"] == "private, no-cache"

    def test_a_token_that_was_valid_before_a_re_render_must_revalidate(
            self, event_dir, client):
        """The reason the match check exists: without it we would hand a
        client `immutable` for a year while serving bytes that had already
        changed under the token it asked for."""
        directory = self._seed(event_dir)
        stale = self._version(client, directory)
        replacement = b"a completely different render, longer than the first"
        clipstore.short_path(directory).write_bytes(replacement)

        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": stale})
        assert response.status_code == 200
        assert response.content == replacement
        assert response.headers["cache-control"] == "private, no-cache"

    def test_a_missing_short_is_still_a_404_even_with_a_token(self, event_dir, client):
        directory = clipstore.write_clip(event_dir, clip_entry())
        response = client.get(f"{EVENT_PREFIX}/clips/{directory.name}/short",
                              params={"v": "0-0"})
        assert response.status_code == 404


class TestStreamAnalysisRoutes:
    def _write(self, root, video_id, name, payload):
        directory = root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_serves_a_written_analysis(self, client, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, video_id, "moments.json",
                    {"video_id": video_id, "engine": "lexicon", "moments": [],
                     "activity": [0.5], "missing_windows": [], "missing_chunks": [],
                     "duration_seconds": 60.0, "stream_title": "Race",
                     "created_at": "2026-07-29T10:00:00+00:00"})
        response = client.get(f"{EV}/streams/{video_id}/moments")
        assert response.status_code == 200
        assert response.json()["engine"] == "lexicon"

    def test_no_analysis_yet_is_an_empty_analysis_not_a_404(self, client):
        # The screen works before detection has ever run; a 404 here would
        # make the client render a failure for an ordinary state.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        response = client.get(f"{EV}/streams/{video_id}/moments")
        assert response.status_code == 200
        body = response.json()
        assert body["moments"] == [] and body["engine"] is None

    def test_every_analysis_shape_carries_configured_provider(
            self, client, workspace_root):
        # detect.detect_moments writes `configured_provider` beside `engine`,
        # and the stream screen's own StreamAnalysis type declares it - so it
        # must be present in BOTH shapes this route can answer with, not only
        # in a freshly written moments.json. The never-analysed synthesis and
        # an analysis written before the field existed would otherwise hand
        # the client `undefined` for a non-optional field.
        never = f"vid-{uuid.uuid4().hex[:8]}"
        body = client.get(f"{EV}/streams/{never}/moments").json()
        assert "configured_provider" in body and body["configured_provider"] is None

        old = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, old, "moments.json",
                    {"video_id": old, "engine": "lexicon", "moments": [],
                     "activity": [], "missing_windows": [], "missing_chunks": [],
                     "duration_seconds": 60.0, "stream_title": "Race",
                     "created_at": "2026-07-29T10:00:00+00:00"})
        body = client.get(f"{EV}/streams/{old}/moments").json()
        assert "configured_provider" in body and body["configured_provider"] is None

        fresh = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, fresh, "moments.json",
                    {"video_id": fresh, "engine": "model:gemini-3.6-flash",
                     "configured_provider": "gemini", "moments": [],
                     "activity": [], "missing_windows": [], "missing_chunks": [],
                     "duration_seconds": 60.0, "stream_title": "Race",
                     "created_at": "2026-07-29T10:00:00+00:00"})
        body = client.get(f"{EV}/streams/{fresh}/moments").json()
        assert body["configured_provider"] == "gemini"

    def test_no_analysis_but_a_transcript_still_yields_a_real_activity_curve(
            self, client, workspace_root):
        # IMPORTANT 1: the plan's own promise is that the curve works with no
        # model call - "a task that makes any of the five [transcript,
        # search, curve, manual window selection, clip creation] depend on
        # moments.json existing is wrong". Before this fix the "not_found"
        # branch always answered with an empty activity/duration_seconds
        # regardless of a transcript on disk, so the overview strip was
        # empty on the screen's own flagship (no key, no detection) state.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, video_id, "transcript.json",
                    {"video_id": video_id, "duration_seconds": 90.0,
                     "words": [{"start": t, "end": t + 0.4, "text": " word"}
                              for t in range(0, 90, 2)],
                     "missing_chunks": []})
        response = client.get(f"{EV}/streams/{video_id}/moments")
        assert response.status_code == 200
        body = response.json()
        assert body["engine"] is None and body["moments"] == []
        assert body["duration_seconds"] == 90.0
        assert body["activity"] != [] and any(v > 0 for v in body["activity"])

    def test_a_transcript_with_no_words_key_still_gives_a_200_empty_curve(
            self, client, workspace_root):
        # Same fault class as a corrupt (unparsable) transcript.json, which
        # the "not_found" branch already swallows to a 200 with an empty
        # curve (`except AnalysisError: pass`, above) - a transcript that
        # PARSES but has no "words" key used to reach `transcript["words"]`
        # unguarded and 500. Malformed shape is not a more serious fault
        # than malformed JSON; it gets the same degrade.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, video_id, "transcript.json",
                    {"video_id": video_id, "duration_seconds": 90.0,
                     "missing_chunks": []})
        response = client.get(f"{EV}/streams/{video_id}/moments")
        assert response.status_code == 200
        assert response.json()["activity"] == []

    def test_a_word_missing_end_still_gives_a_200_empty_curve(
            self, client, workspace_root):
        # activity_curve itself does `word["end"]` with no guard, so a word
        # dict missing "end" (or "start") raised KeyError inside the curve
        # computation, past the transcript["words"] lookup above - reproduced
        # through this route the same way. Same degrade as the sibling test.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, video_id, "transcript.json",
                    {"video_id": video_id, "duration_seconds": 90.0,
                     "words": [{"start": 0.0, "text": " go"}],
                     "missing_chunks": []})
        response = client.get(f"{EV}/streams/{video_id}/moments")
        assert response.status_code == 200
        assert response.json()["activity"] == []

    def test_a_corrupt_analysis_is_a_500_not_an_empty_one(self, client, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        directory = workspace_root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "moments.json").write_text("{not json", encoding="utf-8")
        assert client.get(f"{EV}/streams/{video_id}/moments").status_code == 500

    def test_serves_the_transcript(self, client, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write(workspace_root, video_id, "transcript.json",
                    {"video_id": video_id, "duration_seconds": 60.0,
                     "words": [{"start": 0.0, "end": 0.5, "text": " go"}],
                     "missing_chunks": []})
        response = client.get(f"{EV}/streams/{video_id}/transcript")
        assert response.status_code == 200
        assert response.json()["words"][0]["text"] == " go"

    def test_a_missing_transcript_is_a_404(self, client):
        # Unlike the analysis: without a transcript the screen's main pane has
        # nothing to show, and rendering an empty document would look like a
        # silent stream rather than a missing file.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        assert client.get(f"{EV}/streams/{video_id}/transcript").status_code == 404

    def test_a_traversing_video_id_is_refused(self, client):
        # ..%2F..%2Fauth carries an encoded slash, which the ASGI layer
        # decodes to a literal '/' before Starlette ever matches a route -
        # the single-segment {video_id} pattern then doesn't match at all,
        # so this 404s from the SPA catch-all before validate_segment gets a
        # chance to run and return its own 400. Same framework quirk the
        # channel-segment traversal test above (TestPaletteRoute) already
        # hedges around for an identical reason. Either way nothing outside
        # streams/<video_id>/ is ever read - that is what actually matters.
        #
        # This test exercises ROUTING, not validate_segment - the handler is
        # never called, so it proves nothing about the guard itself. The
        # guard is covered below, by
        # test_a_traversing_video_id_without_a_slash_is_rejected_400, which
        # uses an id that DOES reach the handler.
        assert client.get(f"{EV}/streams/..%2F..%2Fauth/moments").status_code in (400, 404)
        assert client.get(f"{EV}/streams/..%2F..%2Fauth/transcript").status_code in (400, 404)

    def test_a_traversing_video_id_without_a_slash_is_rejected_400(self, client):
        # %2e%2e decodes to a literal ".." with no slash, so it DOES match
        # the single-segment {video_id} pattern and reaches the handler -
        # unlike the %2F case above, this is a real assertion about
        # validate_segment's guard, not about Starlette's routing. See
        # test_event_scoped_read_rejects_traversal_event_segment_400 and
        # test_upload_traversal_filename_is_rejected_not_escaped for the
        # same idiom elsewhere in this file.
        assert client.get(f"{EV}/streams/%2e%2e/moments").status_code == 400
        assert client.get(f"{EV}/streams/%2e%2e/transcript").status_code == 400


class TestEstimateRoute:
    def _write_transcript(self, root, video_id, *, seconds=600):
        directory = root / "streams" / video_id
        directory.mkdir(parents=True, exist_ok=True)
        words = [{"start": float(t), "end": t + 0.5, "text": " word"}
                 for t in range(seconds)]
        (directory / "transcript.json").write_text(
            json.dumps({"video_id": video_id, "duration_seconds": float(seconds),
                       "words": words, "missing_chunks": []}),
            encoding="utf-8")

    def test_estimates_a_written_transcript(self, client, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        response = client.post(f"{EV}/streams/{video_id}/estimate", json={"model": None})
        assert response.status_code == 200
        body = response.json()
        assert body["estimated"] is True
        assert body["windows"] >= 1
        assert body["model"] == "claude-opus-5"  # anthropic_api.DEFAULT_MODEL

    def test_a_model_override_is_honoured(self, client, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        response = client.post(f"{EV}/streams/{video_id}/estimate",
                               json={"model": "claude-haiku-4-5"})
        assert response.status_code == 200
        assert response.json()["model"] == "claude-haiku-4-5"

    def _client_for_provider(self, provider_id):
        # studio_profile has already copied erf into the repointed
        # CHANNELS_DIR; select a provider on that copy, same technique
        # manual_client uses for upload.mode.
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["detect"] = {"provider": provider_id}
        brand_path.write_text(json.dumps(brand), encoding="utf-8")
        return TestClient(create_app())

    def test_the_default_model_comes_from_the_selected_provider(
            self, studio_profile, workspace_root):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        response = self._client_for_provider("gemini").post(
            f"{EV}/streams/{video_id}/estimate", json={"model": None})
        assert response.status_code == 200
        assert response.json()["model"] == gemini_api.DEFAULT_MODEL

    def test_the_cost_is_priced_against_the_selected_providers_table(
            self, studio_profile, workspace_root):
        # gemini-3.5-flash is in gemini_api.PRICES and in no other provider's,
        # so a route still passing anthropic's table answers priced=False here
        # - the quiet way to quote one vendor's rates for another's bill.
        # It is also NOT gemini_api.DEFAULT_MODEL, deliberately: a route that
        # ignored the requested model and priced the provider's default would
        # otherwise pass both assertions below. (It used to be gemini-2.5-flash,
        # which satisfied both conditions until that id was dropped from PRICES
        # for not being servable at all - see gemini_api.PRICES' own comment.)
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        response = self._client_for_provider("gemini").post(
            f"{EV}/streams/{video_id}/estimate", json={"model": "gemini-3.5-flash"})
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "gemini-3.5-flash"
        assert body["model"] != gemini_api.DEFAULT_MODEL
        assert body["priced"] is True
        assert body["usd"] > 0.0

    def test_a_null_provider_falls_back_to_the_default_instead_of_500(
            self, studio_profile, workspace_root):
        # I1: {"detect": {"provider": null}} passes profile.load (an explicit
        # null is treated as absent), and this route used to hand that None
        # straight to providers.get and 500.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["detect"] = {"provider": None}
        brand_path.write_text(json.dumps(brand), encoding="utf-8")
        response = TestClient(create_app()).post(
            f"{EV}/streams/{video_id}/estimate", json={"model": None})
        assert response.status_code == 200
        assert response.json()["model"] == "claude-opus-5"  # anthropic_api.DEFAULT_MODEL

    def test_a_null_model_falls_back_to_the_providers_default_instead_of_none(
            self, studio_profile, workspace_root):
        # I2: {"detect": {"provider": "gemini", "model": null}} used to reach
        # estimate_run with model=None.
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        self._write_transcript(workspace_root, video_id)
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["detect"] = {"provider": "gemini", "model": None}
        brand_path.write_text(json.dumps(brand), encoding="utf-8")
        response = TestClient(create_app()).post(
            f"{EV}/streams/{video_id}/estimate", json={"model": None})
        assert response.status_code == 200
        assert response.json()["model"] == gemini_api.DEFAULT_MODEL

    def test_no_transcript_is_a_404(self, client):
        video_id = f"vid-{uuid.uuid4().hex[:8]}"
        response = client.post(f"{EV}/streams/{video_id}/estimate", json={"model": None})
        assert response.status_code == 404

    def test_a_traversing_video_id_without_a_slash_is_rejected_400(self, client):
        response = client.post(f"{EV}/streams/%2e%2e/estimate", json={"model": None})
        assert response.status_code == 400


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A per-test workspace root for the provider-key routes.

    Deliberately NOT the session-scoped ``workspace_root``: these tests write
    real key files into ``<root>/auth/``, and a key left behind in the shared
    root would make some later test's ``key_present is False`` assertion pass
    or fail depending on collection order. Patching ``api._resolve_workspace``
    (not ``workspace.resolve``) is what the routes actually read - the
    from-import in ``studio/api.py`` binds its own name, exactly as
    ``tests/conftest.py`` explains.
    """
    from yt_shorts.workspace import Workspace
    root = tmp_path / "provider-workspace"
    (root / "channels").mkdir(parents=True)
    monkeypatch.setattr(
        studio_api, "_resolve_workspace",
        lambda: Workspace(root=root, channels_dir=root / "channels", origin="test"))
    return root


def _raw_asgi_request(app, method: str, path: str, body: bytes = b"") -> tuple[int, str]:
    """One request straight into the ASGI app, with the path taken LITERALLY.

    TestClient goes through httpx, which normalises a URL's dot segments
    before sending - so `/api/providers/../key` leaves as `/api/key` and a
    test written against it silently stops exercising the route it names.
    Handing the app a scope directly is the only way to deliver a path
    segment that is a real filesystem relative reference, which is the shape
    the provider-key routes' registry guard exists to refuse.

    The scope describes a LOOPBACK request (host, client and server all
    127.0.0.1), because that is what a real request to the studio is: it
    binds 127.0.0.1 and `api._local_origin_guard` refuses any other Host as a
    DNS-rebound page. This helper used to say `testserver`, which was
    invisible while nothing read the header and became a 403 the moment
    something did - and a 403 here would have hidden the 404 this test is
    actually about.
    """
    import asyncio

    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8765"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765),
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in messages
                       if m["type"] == "http.response.body")
    return status, payload.decode("utf-8", "replace")


class TestProviderKeys:
    """PUT/DELETE /api/providers/{id}/key, and the settings block that reports
    each provider's state.

    An API key passes through these routes, so the assertions here are about
    what does NOT come back out: no response body, no error detail and no
    settings payload may ever carry the key, and no request-supplied string
    may ever become a filename under ``auth/``.
    """

    def test_settings_lists_every_provider_with_its_state(self, client, workspace):
        body = client.get("/api/settings").json()
        ids = [row["id"] for row in body["providers"]]
        assert ids[0] == "anthropic"          # the default sorts first
        assert set(ids) == {"anthropic", "gemini", "openai"}
        row = body["providers"][0]
        assert row["key_present"] is False and isinstance(row["sdk_installed"], bool)
        assert row["verified"] is True
        assert row["default_model"] == "claude-opus-5"
        assert row["install"]

    def test_the_verified_flag_is_each_modules_own(self, client, workspace,
                                                   monkeypatch):
        # This used to assert `rows["openai"]["verified"] is False` - the one
        # shipped provider that had never been measured. All three have been
        # since (2026-07-31), so a hardcoded `True` in the route would now pass
        # that assertion's replacement trivially. Patching one module False is
        # what still catches it: the row must follow the module.
        #
        # The loop derives from `providers.ordered()` rather than naming the
        # three modules, for the same reason
        # `test_importing_the_package_pulls_in_no_vendor_sdk` does: a hardcoded
        # tuple leaves a FOURTH provider silently uncovered by exactly the
        # check that is meant to catch a hardcoded flag in the route.
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        for module in providers.ordered():
            assert rows[module.PROVIDER_ID]["verified"] is module.VERIFIED
        assert all(r["verified"] is True for r in rows.values())

        monkeypatch.setattr(openai_api, "VERIFIED", False)
        patched = {r["id"]: r
                   for r in client.get("/api/settings").json()["providers"]}
        assert patched["openai"]["verified"] is False
        assert patched["anthropic"]["verified"] is True   # only the patched one

    def test_each_provider_row_carries_its_own_price_table(self, client, workspace):
        # Added for the provider picker's cost disclosure (Task 7 step 6): the
        # brand editor must show the selected model's rate AND that provider's
        # cheapest priced model at the moment of choosing, and PRICES lives
        # only in the provider modules. Shipped constants, like `install` and
        # `default_model` beside it - nothing here is derived from a key.
        #
        # Derived from `providers.ordered()`, never a hardcoded tuple: a fourth
        # provider must inherit this check the moment it enters `_MODULES`,
        # exactly as it inherits the whole conformance suite.
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        for module in providers.ordered():
            expected = {model: list(rate) for model, rate in module.PRICES.items()}
            assert rows[module.PROVIDER_ID]["prices"] == expected
        # The concrete pair the disclosure's own wording quotes.
        assert rows["openai"]["prices"]["gpt-5.6-terra"] == [2.00, 12.00]

    def test_saving_a_key_makes_it_present(self, client, workspace):
        response = client.put("/api/providers/gemini/key", json={"api_key": "abc123"})
        assert response.status_code == 200
        assert response.json() == {"provider": "gemini", "key_present": True}
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        assert rows["gemini"]["key_present"] is True
        assert rows["anthropic"]["key_present"] is False   # only the named one

    def test_the_key_lands_in_the_provider_modules_own_file(self, client, workspace):
        # The filename comes from the resolved module, never from the URL:
        # gemini_api.KEY_FILENAME, in auth/, and nowhere else.
        client.put("/api/providers/gemini/key", json={"api_key": "abc123"})
        assert (workspace / "auth" / gemini_api.KEY_FILENAME).is_file()
        assert sorted(p.name for p in (workspace / "auth").iterdir()) == \
            [gemini_api.KEY_FILENAME]

    def test_the_key_is_never_returned_anywhere(self, client, workspace):
        secret = "sk-DO-NOT-LEAK-987654321"
        put = client.put("/api/providers/gemini/key", json={"api_key": secret})
        assert put.status_code == 200
        assert secret not in put.text
        settings = client.get("/api/settings")
        assert secret not in settings.text
        # And not through the route that reports one provider's state either.
        assert secret not in client.delete("/api/providers/gemini/key").text

    def test_the_key_is_never_logged_either(self, client, workspace):
        # Everything else about the key-secrecy rule already has a killing
        # test (see test_the_key_is_never_returned_anywhere and the
        # rejection-path tests below) - this is the "never logged" half,
        # proven by actually capturing every record the route's execution
        # produces rather than by reading the route's source (F2 in the
        # task-6 report). Two loggers are watched: the `ytshorts` tree
        # (`logsetup.configure_logging` sets `propagate = False` on it, so a
        # record logged there would never reach the root's own handlers) and
        # the root logger itself, in case a future line calls the stdlib
        # `logging` module directly rather than through that tree.
        import logging

        class _Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records: list[logging.LogRecord] = []

            def emit(self, record):
                self.records.append(record)

        capture = _Capture()
        ytshorts_logger = logging.getLogger("ytshorts")
        root_logger = logging.getLogger()
        ytshorts_logger.addHandler(capture)
        root_logger.addHandler(capture)
        try:
            marker = "sk-MARKER-DO-NOT-LOG-13579"
            put = client.put("/api/providers/gemini/key", json={"api_key": marker})
            assert put.status_code == 200

            # The rejection path: a key that fails save_api_key's own validation.
            rejected = client.put(
                "/api/providers/openai/key", json={"api_key": "bad\n" + marker})
            assert rejected.status_code == 400

            delete = client.delete("/api/providers/gemini/key")
            assert delete.status_code == 200
        finally:
            ytshorts_logger.removeHandler(capture)
            root_logger.removeHandler(capture)

        for record in capture.records:
            assert marker not in record.getMessage()
            assert marker not in repr(record.args)

    def test_the_key_file_is_owner_only(self, client, workspace):
        client.put("/api/providers/gemini/key", json={"api_key": "abc123"})
        path = workspace / "auth" / gemini_api.KEY_FILENAME
        assert ownermode.is_owner_only(path)

    def test_an_empty_key_is_400(self, client, workspace):
        assert client.put("/api/providers/gemini/key",
                          json={"api_key": "   "}).status_code == 400
        assert not (workspace / "auth" / gemini_api.KEY_FILENAME).exists()

    def test_a_key_with_a_newline_is_400(self, client, workspace):
        assert client.put("/api/providers/gemini/key",
                          json={"api_key": "one\ntwo"}).status_code == 400

    def test_an_over_long_key_is_400(self, client, workspace):
        from yt_shorts.providers import MAX_KEY_LENGTH
        response = client.put("/api/providers/gemini/key",
                              json={"api_key": "k" * (MAX_KEY_LENGTH + 1)})
        assert response.status_code == 400
        assert str(MAX_KEY_LENGTH) in response.text     # the constraint, named

    def test_a_rejected_key_is_not_echoed_in_the_error(self, client, workspace):
        secret = "bad\nkey-SECRET-123"
        response = client.put("/api/providers/gemini/key", json={"api_key": secret})
        assert response.status_code == 400 and "SECRET" not in response.text

    def test_an_over_long_key_is_not_echoed_in_the_error(self, client, workspace):
        # The other 400 path: a key that is well-formed but too long is the one
        # most likely to be quoted back by a lazy f-string.
        from yt_shorts.providers import MAX_KEY_LENGTH
        secret = "LEAKME" * ((MAX_KEY_LENGTH // 6) + 1)
        response = client.put("/api/providers/gemini/key", json={"api_key": secret})
        assert response.status_code == 400 and "LEAKME" not in response.text

    @pytest.mark.parametrize("wrapped", [
        ["sk-LEAKME-in-a-list"],
        {"value": "sk-LEAKME-in-an-object"},
        12345,
        None,
    ])
    def test_a_wrong_typed_key_is_400_and_is_not_echoed(self, client, workspace, wrapped):
        # ProviderKeyBody types api_key as `Any`, not `str`, precisely for
        # this: pydantic would reject the wrong type itself and FastAPI's 422
        # body quotes the offending `input` straight back - so a key sent
        # inside a list would come back out in the error. Typed `Any`, every
        # shape reaches save_api_key and every rejection is a 400 naming a
        # constraint and quoting nothing.
        response = client.put("/api/providers/gemini/key", json={"api_key": wrapped})
        assert response.status_code == 400, response.text
        assert "LEAKME" not in response.text
        assert "12345" not in response.text
        assert not (workspace / "auth" / gemini_api.KEY_FILENAME).exists()

    def test_a_body_with_no_api_key_at_all_is_400(self, client, workspace):
        # Not a 422 either: the missing-field case goes through the same
        # single validator as every other unusable key.
        response = client.put("/api/providers/gemini/key", json={})
        assert response.status_code == 400
        assert not (workspace / "auth").exists()

    @pytest.mark.parametrize("raw", [
        b'"sk-LEAKME-a-bare-string"',
        b'["sk-LEAKME-in-a-bare-list"]',
        b'{"api_key": "sk-LEAKME-unterminated"',
    ])
    def test_a_body_that_is_not_a_json_object_is_400_and_is_not_echoed(
            self, client, workspace, raw):
        # The last hole in the key-secrecy rule (F3 in the task-6 report):
        # every WRONG-TYPED api_key already reaches save_api_key and comes
        # back as a 400 naming a constraint (see the parametrised test
        # above), but a body that is not a JSON OBJECT at all never reached
        # the route - pydantic refused to build ProviderKeyBody from it and
        # FastAPI's own 422 handler quoted the offending `input` straight
        # back. A bare `"sk-..."` string IS a plausible thing for a client
        # to send, and it would have come back out verbatim. The route now
        # reads and shapes the body itself (the same `request: Request`
        # idiom upload_font uses) and answers 400 with a fixed sentence that
        # interpolates nothing - which also covers unparsable JSON, whose
        # decoder message would otherwise quote a fragment of the input.
        response = client.put("/api/providers/gemini/key", content=raw,
                              headers={"Content-Type": "application/json"})
        assert response.status_code == 400, response.text
        assert "LEAKME" not in response.text
        assert not (workspace / "auth").exists()

    def test_an_unknown_provider_is_404_and_touches_no_file(self, client, workspace):
        assert client.put("/api/providers/nope/key",
                          json={"api_key": "x"}).status_code == 404
        assert not (workspace / "auth").exists()

    @pytest.mark.parametrize("provider_id", [
        "../../etc/passwd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "%2e%2e%2ftoken-UC123.json",
        "..",
        "anthropic.json",
        "gemini.json",
        ".hidden",
        "GEMINI",
    ])
    def test_a_traversal_shaped_provider_id_writes_nothing(
            self, client, workspace, provider_id):
        # The registry lookup is the guard: the set of providers is CLOSED, so
        # no request-supplied string can become a path segment under auth/.
        # A route that built the filename from the URL instead would write
        # (or unlink) outside the auth dir for at least one of these.
        #
        # NOTE what this can and cannot reach. httpx (under TestClient)
        # NORMALISES dot segments before sending, so '../../etc/passwd' and
        # '..' never arrive as a path param at all - they are 405/404 from
        # routing, which proves nothing about the route's own guard. The rows
        # that genuinely exercise it are the filename-shaped ones
        # ('anthropic.json', '.hidden', 'GEMINI'), and
        # test_a_dot_segment_provider_id_reaches_the_route_and_is_refused
        # below covers the normalised cases by driving ASGI directly.
        put = client.put(f"/api/providers/{provider_id}/key", json={"api_key": "x"})
        assert put.status_code in (404, 405), put.text
        delete = client.delete(f"/api/providers/{provider_id}/key")
        assert delete.status_code in (404, 405), delete.text
        assert not (workspace / "auth").exists()
        assert not (workspace.parent / "passwd").exists()

    @pytest.mark.parametrize("provider_id", ["..", "."])
    def test_a_dot_segment_provider_id_reaches_the_route_and_is_refused(
            self, client, workspace, provider_id):
        # The case the parametrised test above CANNOT express: httpx resolves
        # '/api/providers/../key' to '/api/key' before it is ever sent, so a
        # dot segment can only be delivered by handing the ASGI app a scope
        # whose path already contains it. Starlette's own path converter is
        # `[^/]+`, which happily matches '..' - so this is the shortest string
        # that reaches provider_id as a literal filesystem-relative segment.
        # With the registry lookup in place both verbs are a clean 404 and the
        # auth dir is never even created. Without it, `auth/..` IS the
        # workspace root: save_api_key would try to replace a directory (a 500
        # and a stray scratch file) and forget_api_key would unlink one.
        for method in ("PUT", "DELETE"):
            status, text = _raw_asgi_request(
                client.app, method, f"/api/providers/{provider_id}/key",
                body=b'{"api_key": "x"}')
            assert status == 404, (method, status, text)
        assert not (workspace / "auth").exists()

    def test_deleting_forgets_the_key(self, client, workspace):
        client.put("/api/providers/gemini/key", json={"api_key": "abc123"})
        response = client.delete("/api/providers/gemini/key")
        assert response.status_code == 200
        assert response.json() == {"provider": "gemini", "key_present": False}
        assert not (workspace / "auth" / gemini_api.KEY_FILENAME).exists()
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        assert rows["gemini"]["key_present"] is False

    def test_deleting_touches_no_other_file_in_auth(self, client, workspace):
        # Same discipline as disconnect_auth: it removes one file and nothing
        # else in auth/ - not the client secret, not another provider's key.
        (workspace / "auth").mkdir(parents=True)
        secret = workspace / "auth" / "client_secret.json"
        secret.write_text("SECRET", encoding="utf-8")
        client.put("/api/providers/gemini/key", json={"api_key": "abc123"})
        client.put("/api/providers/openai/key", json={"api_key": "def456"})
        assert client.delete("/api/providers/gemini/key").status_code == 200
        assert secret.read_text(encoding="utf-8") == "SECRET"
        assert (workspace / "auth" / "openai.json").is_file()

    def test_deleting_a_key_that_is_not_there_is_404(self, client, workspace):
        assert client.delete("/api/providers/gemini/key").status_code == 404

    def test_deleting_an_unknown_provider_is_404(self, client, workspace):
        assert client.delete("/api/providers/nope/key").status_code == 404

    def test_deleting_a_key_that_is_a_directory_is_a_controlled_500(
            self, client, workspace):
        # F4: a directory placed at the key's path makes forget_api_key's
        # unlink() raise IsADirectoryError. PUT already has an OSError branch
        # for the mirror case (save_api_key's os.replace onto a directory);
        # DELETE must behave the same way rather than let the exception
        # escape uncaught - a 500 with a stack trace naming the path in the
        # log. The message names only the exception's TYPE, never its own
        # text, which is what would carry the path.
        key_path = workspace / "auth" / gemini_api.KEY_FILENAME
        key_path.mkdir(parents=True)
        response = client.delete("/api/providers/gemini/key")
        assert response.status_code == 500
        # The concrete OSError subclass unlink() raises on a directory is
        # platform-dependent (IsADirectoryError on Linux, PermissionError on
        # macOS) - either is fine, since the branch names the TYPE only.
        assert "Error" in response.text
        assert str(key_path) not in response.text

    def test_a_malformed_key_file_reports_absent_rather_than_500(self, client, workspace):
        # has_api_key READS the file rather than stat()ing it, so a file an
        # operator half-pasted must render the settings page as "no key", not
        # break it.
        (workspace / "auth").mkdir(parents=True)
        (workspace / "auth" / gemini_api.KEY_FILENAME).write_text("{}", encoding="utf-8")
        rows = {r["id"]: r for r in client.get("/api/settings").json()["providers"]}
        assert rows["gemini"]["key_present"] is False


class TestSettingsProviderRows:
    """Each channel row in GET /api/settings reports which provider and model
    that channel's brand.json selects - read-only, and never a 500."""

    def _settings(self, monkeypatch, client=None):
        from yt_shorts.workspace import Workspace
        root = profile_module.CHANNELS_DIR.parent
        monkeypatch.setattr(
            api, "_resolve_workspace",
            lambda: Workspace(root=root, channels_dir=root / "channels", origin="test"))
        return (client or TestClient(create_app())).get("/api/settings").json()

    def test_a_channel_with_no_detect_section_reports_the_default(
            self, client, studio_profile, monkeypatch):
        row = self._settings(monkeypatch, client)["channels"][0]
        assert row["detect_provider"] == "anthropic"
        assert row["detect_model"] == ""

    def test_each_channel_row_reports_its_provider_and_model(
            self, studio_profile, monkeypatch):
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["detect"] = {"provider": "gemini", "model": "gemini-2.5-flash"}
        brand_path.write_text(json.dumps(brand), encoding="utf-8")
        row = self._settings(monkeypatch)["channels"][0]
        assert row["detect_provider"] == "gemini"
        assert row["detect_model"] == "gemini-2.5-flash"

    @pytest.mark.parametrize("detect", ["gemini", ["gemini"], 7,
                                        {"provider": None, "model": None},
                                        {"provider": ["gemini"], "model": 7}])
    def test_a_malformed_detect_section_does_not_500_the_page(
            self, studio_profile, monkeypatch, detect):
        # brand.json is hand-editable, and this read is not behind
        # profile.load's validation - a nonsense value must degrade to the
        # default in one row, never take the whole Settings screen down.
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand = json.loads(brand_path.read_text(encoding="utf-8"))
        brand["detect"] = detect
        brand_path.write_text(json.dumps(brand), encoding="utf-8")
        row = self._settings(monkeypatch)["channels"][0]
        assert row["detect_provider"] == "anthropic"
        assert row["detect_model"] == ""

    def test_an_unparseable_brand_json_does_not_500_the_page(
            self, studio_profile, monkeypatch):
        brand_path = profile_module.CHANNELS_DIR / "erf" / "brand.json"
        brand_path.write_text("{ not json", encoding="utf-8")
        body = self._settings(monkeypatch)
        assert body["channels"][0]["detect_provider"] == "anthropic"


def test_rendering_the_settings_page_pulls_in_no_vendor_sdk(tmp_path):
    """sdk_installed answers with find_spec precisely so that REPORTING three
    providers' state does not import three vendor SDKs into the studio
    process. Same shape (and same reasoning) as
    test_provider_contract.test_importing_the_package_pulls_in_no_vendor_sdk,
    one layer up: this exercises create_app() AND a real GET /api/settings,
    which is where has_api_key/sdk_installed are actually called.

    It matches on the FULL PACKAGE name, not on its top-level package the way
    the contract test does, and that difference is load-bearing rather than
    stylistic. gemini's PACKAGE is ``google.genai``, whose top level -
    ``google`` - is a NAMESPACE package shared with google-auth/google-api-
    python-client, and this route legitimately imports those: ``get_settings``
    calls ``google_require("upload")`` and ``GoogleOAuth`` to report each
    channel's YouTube connection state. A top-level match therefore fails on
    the upload stack while proving nothing about the Gemini SDK. ``google``
    alone being present is also EXPECTED for a second reason - find_spec
    imports the parent namespace of a dotted PACKAGE as a side effect, which
    _shared.sdk_installed documents. What must stay absent is the vendor SDK
    itself.
    """
    import subprocess as _subprocess
    import sys as _sys

    from yt_shorts import providers as _providers
    repo_root = Path(__file__).resolve().parent.parent
    vendors = sorted(module.PACKAGE for module in _providers.ordered())
    root = tmp_path / "ws"
    (root / "channels").mkdir(parents=True)
    code = (
        "import sys;"
        "from fastapi.testclient import TestClient;"
        "from yt_shorts.studio.api import create_app;"
        # base_url explicitly: this runs in a SUBPROCESS, so
        # tests/conftest.py's `_test_client_speaks_from_loopback` is not in
        # force and TestClient's own default host would be refused by
        # api._local_origin_guard.
        "r=TestClient(create_app(), base_url='http://127.0.0.1')"
        ".get('/api/settings');"
        "assert r.status_code == 200, r.status_code;"
        "assert r.json()['providers'], 'no providers reported';"
        f"bad=[n for n in sys.modules for p in {vendors!r} "
        "if n == p or n.startswith(p + '.')];"
        "print(sorted(bad))"
    )
    result = _subprocess.run(
        [_sys.executable, "-c", code], capture_output=True, text=True, cwd=repo_root,
        env={**os.environ, "PYTHONPATH": "src", "YT_SHORTS_DATA": str(root)}, check=True)
    assert result.stdout.strip() == "[]", result.stdout


class TestJobQueueRoutes:
    """The seven routes over `yt_shorts.job_queue`, plus the pool limits.

    Nothing here starts a real job: the queue is driven directly (claim,
    finish) where a state is all a test needs, and through
    `Worker.drain_once()` with the one relevant `jobs.start_*_job` replaced
    where the route must reach a RUNNING job's cancel token. No network, no
    ffmpeg, no Whisper decode, no key and no money - the same rule
    tests/test_studio_worker.py keeps.

    Each test gets its OWN queue file (`tmp_path/jobs.json`), not the
    session-scoped workspace root's: `tests/conftest.py` pins one workspace
    for the whole session, so a plan left behind in it would leak into
    whichever test collected next.
    """

    HERE = {"channel": CHANNEL, "event": EVENT}

    @pytest.fixture
    def app(self, studio_profile, tmp_path):
        built = create_app()
        queue = JobQueue(tmp_path / "jobs.json", jobs_module.KINDS,
                         dict(worker_module.DEFAULT_LIMITS))
        built.state.job_queue = queue
        built.state.worker = worker_module.Worker(queue, built.state.job_store)
        return built

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    @pytest.fixture
    def queue(self, app):
        return app.state.job_queue

    @staticmethod
    def _state(queue, entry_id):
        return next(e.state for e in queue.list() if e.id == entry_id)

    def _running(self, app, queue, monkeypatch, kind, params=None):
        """One entry of `kind`, actually RUNNING, with its starter replaced.

        The fake records the cancel token on the Job exactly as the real
        starters do - that is what a stop has to reach, and a fake that
        dropped it would make the stop route look broken for the wrong
        reason.
        """
        made = {}

        def fake_start(profile, job_store, *args, **kwargs):
            job = jobs_module.Job(f"fake-{kind}", kind=kind)
            job.cancel = kwargs.get("cancel")
            made["job"] = job
            return job

        monkeypatch.setattr(jobs_module, f"start_{kind}_job", fake_start)
        entry = queue.enqueue(kind, dict(self.HERE, **(params or {})))
        app.state.worker.drain_once()
        assert self._state(queue, entry.id) == "running", "the entry never started"
        return entry, made["job"]

    # -- the listing ------------------------------------------------------

    def test_the_listing_shows_queued_running_and_recent(self, client, queue):
        finished = queue.enqueue("render", dict(self.HERE))
        running = queue.enqueue("detect", dict(self.HERE, video_id="vid1"))
        waiting = queue.enqueue("trim", dict(self.HERE, clip="clip-a"))
        queue.claim_next()                                  # the render
        queue.mark_finished(finished.id, "done", reason="1 clip")
        queue.claim_next()                                  # the detect (net pool)

        body = client.get("/api/jobs").json()

        assert [row["id"] for row in body["finished"]] == [finished.id]
        assert [row["id"] for row in body["running"]] == [running.id]
        assert [row["id"] for row in body["queued"]] == [waiting.id]
        assert body["finished"][0]["reason"] == "1 clip"
        assert body["queued"][0]["kind"] == "trim"
        # What the screen needs to label a stop button honestly, from KINDS.
        assert body["queued"][0]["stop_point"] == jobs_module.KINDS["trim"].stop_point
        assert body["running"][0]["hard_stop_allowed"] is True
        assert body["running"][0]["pool"] == "net"
        assert body["running"][0]["stoppable"] is True
        # Positions are indexes into the WHOLE plan - the same index space
        # POST …/move takes, so a client can compute a target from them.
        assert [row["position"] for row in
                body["finished"] + body["running"] + body["queued"]] == [0, 1, 2]
        assert body["limits"] == {"cpu": 1, "net": 3}
        assert body["worker_running"] is False
        assert body["load_error"] is None

    def test_every_row_says_whether_a_stop_can_reach_it_at_all(self, client, queue):
        """`stoppable` is a FACT on the wire, and it replaced a bet on prose.

        The screen used to infer "this kind cannot be stopped" by comparing
        `stop_point` against the literal phrase `'cannot be stopped'`. A
        reviewer changed `KINDS["upload"].stop_point` to `"Cannot be
        stopped"` - a case-only edit - and 389 Python tests stayed green
        while the Jobs screen would then have offered a Stop button on a
        running upload, which `KINDS["upload"]` exists to forbid.

        Enumerated over every queueable kind rather than spot-checked on
        upload, and read from `worker.STARTERS[kind].stoppable` (whether the
        starter TAKES a cancel token) rather than restated here - that is
        precisely what `Worker.request_stop` refuses on, so a starter that
        stopped taking a token would flip this field instead of leaving the
        screen offering a button the server answers 409 to.
        """
        expected = {}
        for kind, spec in jobs_module.KINDS.items():
            if not spec.queueable:
                continue
            params = dict(self.HERE)
            if kind in ("transcribe", "detect"):
                params["video_id"] = f"vid-{kind}"
            if kind in ("trim", "upload"):
                params["clip"] = f"clip-{kind}"
            queue.enqueue(kind, params)
            expected[kind] = worker_module.STARTERS[kind].stoppable

        rows = client.get("/api/jobs").json()["queued"]

        assert {row["kind"]: row["stoppable"] for row in rows} == expected
        # And the one that matters most, named rather than only enumerated.
        assert expected["upload"] is False

    def test_an_entry_of_a_kind_this_build_cannot_start_is_not_stoppable(
            self, client, queue, tmp_path):
        # A jobs.json written by another version can name a kind this build
        # has no starter for. There is nothing to stop, so the row must say
        # so - the same safe default `stop_point`/`hard_stop_allowed` take.
        path = tmp_path / "jobs.json"
        entry = queue.enqueue("trim", dict(self.HERE, clip="clip-a"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw["entries"]:
            if row["id"] == entry.id:
                row["kind"] = "sonar"
        path.write_text(json.dumps(raw), encoding="utf-8")
        queue.load()

        row = client.get("/api/jobs").json()["queued"][0]

        assert row["kind"] == "sonar"
        assert row["stoppable"] is False
        assert row["stop_point"] is None
        assert row["hard_stop_allowed"] is False

    def test_every_queue_state_lands_in_exactly_one_section(self):
        # Enumerated against the queue's own STATES, not spot-checked: a
        # state added later (and a route that forgot it) would otherwise
        # simply vanish from the screen with nothing failing.
        sections = [studio_api._RUNNING_STATES, studio_api._PENDING_STATES,
                    studio_api._FINISHED_STATES]
        seen = [state for section in sections for state in section]
        assert sorted(seen) == sorted(job_queue_module.STATES)
        assert len(seen) == len(set(seen)), "a state appears in two sections"

    # -- enqueue ----------------------------------------------------------

    def test_enqueue_returns_the_entry_and_its_place(self, client, queue):
        first = client.post("/api/jobs",
                            json={"kind": "render", "params": dict(self.HERE)})
        assert first.status_code == 200, first.text
        assert first.json()["position"] == 0
        assert first.json()["queued_ahead"] == 0
        entry = first.json()["entry"]
        assert entry["kind"] == "render" and entry["state"] == "queued"
        assert entry["pool"] == "cpu" and entry["job_id"] is None

        second = client.post(
            "/api/jobs",
            json={"kind": "detect", "params": dict(self.HERE, video_id="vid1")})
        assert second.json()["position"] == 1
        assert second.json()["queued_ahead"] == 1
        assert [e.id for e in queue.list()] == [entry["id"],
                                                second.json()["entry"]["id"]]

    def test_enqueue_refuses_an_unknown_kind_with_400(self, client, queue):
        response = client.post("/api/jobs",
                               json={"kind": "polish", "params": dict(self.HERE)})
        assert response.status_code == 400
        assert "polish" in response.json()["detail"]
        assert queue.list() == []

    def test_enqueue_refuses_connect_with_400(self, client, queue):
        # `connect` opens the operator's browser and waits for a consent only
        # they can give (jobs.KINDS marks it not queueable) - queueing it
        # behind a two-hour transcription is meaningless.
        response = client.post("/api/jobs",
                               json={"kind": "connect", "params": dict(self.HERE)})
        assert response.status_code == 400
        assert queue.list() == []
        # …and the other non-queueable kind, for the same reason.
        assert client.post("/api/jobs",
                           json={"kind": "copy", "params": dict(self.HERE)}
                           ).status_code == 400

    def test_enqueue_refuses_a_non_private_or_scheduled_upload(self, client, queue):
        # Privacy is an explicit, CONFIRMED, per-upload operator choice, and a
        # queue entry runs later from a state file that can carry no
        # confirmation - so this is refused at ENQUEUE, not at run time.
        for extra in ({"visibility": "public"}, {"visibility": "unlisted"},
                      {"publish_at": "2026-08-01T10:00:00Z"}):
            response = client.post(
                "/api/jobs",
                json={"kind": "upload",
                      "params": dict(self.HERE, clip="clip-a", **extra)})
            assert response.status_code == 400, extra
            assert "confirm" in response.json()["detail"], extra
        assert queue.list() == [], "a refused upload was written to the plan"

        ok = client.post("/api/jobs",
                         json={"kind": "upload",
                               "params": dict(self.HERE, clip="clip-a",
                                              visibility="private")})
        assert ok.status_code == 200
        assert len(queue.list()) == 1

    def test_enqueue_refuses_a_traversal_channel_or_event(self, client, queue):
        # These are BODY values, not URL path segments, so httpx's dot-segment
        # normalisation (which defeats the obvious traversal test - see
        # CLAUDE.md) never touches them: the literal string reaches the route.
        # They still go through pathnames.validate_segment, because the worker
        # turns them into a directory name when it resolves the profile.
        for params in ({"channel": "../../etc", "event": EVENT},
                       {"channel": CHANNEL, "event": "../secrets"},
                       {"channel": CHANNEL, "event": ""},
                       {"channel": CHANNEL}):
            response = client.post("/api/jobs",
                                   json={"kind": "render", "params": params})
            assert response.status_code == 400, params
        assert queue.list() == []

    def test_enqueue_refuses_a_traversal_clip_name(self, client, queue):
        # A clip name becomes a DIRECTORY under <event>/clips/ exactly the way
        # channel/event become one - `trim`/`upload` take one as `clip`,
        # `render` takes a list of them as `clips`. Measured before this guard
        # existed: a `trim` entry naming "../../../OUTSIDE" was accepted here,
        # claimed by the worker, and handed to `trim.ensure_applied` - the one
        # function that renames short.mp4 aside and re-encodes it - with a
        # directory OUTSIDE the event's clips/ dir.
        for kind, params in (("trim", {"clip": "../../../OUTSIDE"}),
                             ("upload", {"clip": "../../../OUTSIDE"}),
                             ("trim", {"clip": ".hidden"}),
                             ("render", {"clips": ["ok", "../../../OUTSIDE"]})):
            response = client.post(
                "/api/jobs", json={"kind": kind, "params": dict(self.HERE, **params)})
            assert response.status_code == 400, (kind, params)
        assert queue.list() == [], "a refused entry was written to the plan"

    def test_enqueue_still_accepts_ordinary_clip_names(self, client, queue):
        # The control for the guard above: the shapes a real operator sends
        # must keep working - a single clip name, a list of them, and `render`
        # with no `clips` at all (which means "every clip in the event").
        for kind, params in (("trim", {"clip": "speedy--1a2b3c4d"}),
                             ("render", {"clips": ["speedy--1a2b3c4d"]}),
                             ("render", {})):
            response = client.post(
                "/api/jobs", json={"kind": kind, "params": dict(self.HERE, **params)})
            assert response.status_code == 200, (kind, params)
        assert len(queue.list()) == 3

    def test_a_transcribe_job_is_how_an_operator_gets_what_detect_now_needs(
            self, client, queue):
        # Detection refuses without a cached transcript (see TestDetectRoute),
        # so this route is the answer to "then how do I get one?". No dedicated
        # transcribe route exists or is needed.
        response = client.post(
            "/api/jobs",
            json={"kind": "transcribe",
                  "params": dict(self.HERE, video_id="vid-needs-words")})
        assert response.status_code == 200, response.text
        assert response.json()["entry"]["kind"] == "transcribe"
        assert response.json()["entry"]["pool"] == "cpu"
        assert queue.list()[0].params["video_id"] == "vid-needs-words"

    # -- stop -------------------------------------------------------------

    def test_stop_is_202_and_moves_the_entry_to_stopping(
            self, app, client, queue, monkeypatch):
        # 202, not 200: the stop was ACCEPTED, not completed - the work
        # stops at its own safe point. And the entry only says `stopping`
        # because the token the work is checking was actually asked to stop:
        # a route that called queue.mark_stopping() would leave that token
        # untouched and the button would be a lie.
        entry, job = self._running(app, queue, monkeypatch, "render")
        assert job.cancel.stop_requested is False

        response = client.post(f"/api/jobs/{entry.id}/stop")

        assert response.status_code == 202, response.text
        assert response.json()["entry"]["state"] == "stopping"
        assert job.cancel.stop_requested is True, (
            "the stop never reached the token the work is actually checking")
        assert job.cancel.kill_requested is False, "a plain stop must not kill"
        assert self._state(queue, entry.id) == "stopping"

    def test_a_hard_stop_is_a_kill_where_the_kind_allows_it(
            self, app, client, queue, monkeypatch):
        entry, job = self._running(app, queue, monkeypatch, "render")

        response = client.post(f"/api/jobs/{entry.id}/stop?force=true")

        assert response.status_code == 202
        assert job.cancel.kill_requested is True
        assert self._state(queue, entry.id) == "stopping"

    def test_escalating_an_already_stopping_entry_to_a_hard_stop_is_202(
            self, app, client, queue, monkeypatch):
        """The escalation the Jobs screen offers on a `stopping` row.

        It used to answer 409 for a kill it had actually performed:
        `Worker.request_stop` requested the kill and only THEN called
        `mark_stopping`, which refuses any state but `running`. The operator
        was told the escalation was refused while the subprocess was being
        terminated. Over HTTP, because that is where the operator sees it.
        """
        entry, job = self._running(app, queue, monkeypatch, "render")
        assert client.post(f"/api/jobs/{entry.id}/stop").status_code == 202
        assert self._state(queue, entry.id) == "stopping"
        assert job.cancel.kill_requested is False

        response = client.post(f"/api/jobs/{entry.id}/stop?force=true")

        assert response.status_code == 202, response.text
        assert response.json()["entry"]["state"] == "stopping"
        assert job.cancel.kill_requested is True
        assert self._state(queue, entry.id) == "stopping"

    def test_a_second_graceful_stop_on_a_stopping_entry_is_409(
            self, app, client, queue, monkeypatch):
        entry, _job = self._running(app, queue, monkeypatch, "render")
        assert client.post(f"/api/jobs/{entry.id}/stop").status_code == 202

        response = client.post(f"/api/jobs/{entry.id}/stop")

        assert response.status_code == 409, response.text
        assert self._state(queue, entry.id) == "stopping"

    def test_force_stop_on_upload_is_409(self, app, client, queue, monkeypatch):
        # KINDS["upload"] has no stop at any level, and a hard stop is never
        # quietly downgraded to a graceful one: the entry must stay running.
        entry, _job = self._running(app, queue, monkeypatch, "upload",
                                    {"clip": "clip-a"})

        response = client.post(f"/api/jobs/{entry.id}/stop?force=true")

        assert response.status_code == 409, response.text
        assert "hard stop" in response.json()["detail"]
        assert self._state(queue, entry.id) == "running"

    def test_a_graceful_stop_on_upload_is_409_too(
            self, app, client, queue, monkeypatch):
        entry, _job = self._running(app, queue, monkeypatch, "upload",
                                    {"clip": "clip-a"})
        response = client.post(f"/api/jobs/{entry.id}/stop")
        assert response.status_code == 409
        assert self._state(queue, entry.id) == "running"

    def test_stopping_an_entry_that_is_not_running_is_409(self, client, queue):
        entry = queue.enqueue("render", dict(self.HERE))
        response = client.post(f"/api/jobs/{entry.id}/stop")
        assert response.status_code == 409
        assert self._state(queue, entry.id) == "queued"

    # -- pause / resume / move / delete / retry -----------------------------

    def test_pause_and_resume_keep_the_entry_in_its_place(self, client, queue):
        first = queue.enqueue("render", dict(self.HERE))
        second = queue.enqueue("trim", dict(self.HERE, clip="clip-a"))

        paused = client.post(f"/api/jobs/{first.id}/pause")
        assert paused.status_code == 200
        assert paused.json()["entry"]["state"] == "paused"
        assert paused.json()["entry"]["position"] == 0
        # A paused entry is not claimable, and it has NOT lost its place.
        assert queue.claim_next().id == second.id
        assert [e.id for e in queue.list()] == [first.id, second.id]

        resumed = client.post(f"/api/jobs/{first.id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["entry"]["state"] == "queued"
        assert self._state(queue, first.id) == "queued"
        # Resuming something that was never paused is refused, not a no-op.
        assert client.post(f"/api/jobs/{first.id}/resume").status_code == 409

    def test_move_reorders(self, client, queue):
        first = queue.enqueue("render", dict(self.HERE))
        second = queue.enqueue("trim", dict(self.HERE, clip="clip-a"))
        third = queue.enqueue("detect", dict(self.HERE, video_id="vid1"))

        response = client.post(f"/api/jobs/{third.id}/move", json={"index": 0})

        assert response.status_code == 200, response.text
        assert response.json()["entry"]["position"] == 0
        assert [e.id for e in queue.list()] == [third.id, first.id, second.id]

    def test_delete_refuses_a_running_entry_with_409_naming_stop(
            self, app, client, queue, monkeypatch):
        entry, _job = self._running(app, queue, monkeypatch, "render")

        response = client.delete(f"/api/jobs/{entry.id}")

        assert response.status_code == 409, response.text
        assert "stop" in response.json()["detail"], (
            "the refusal must point at the other button, not just say no")
        assert [e.id for e in queue.list()] == [entry.id]

    def test_delete_removes_a_queued_entry(self, client, queue):
        entry = queue.enqueue("render", dict(self.HERE))
        response = client.delete(f"/api/jobs/{entry.id}")
        assert response.status_code == 200
        assert response.json()["removed"] == entry.id
        assert queue.list() == []

    def test_retry_re_enqueues(self, client, queue):
        entry = queue.enqueue("render", dict(self.HERE))
        queue.claim_next()
        queue.mark_finished(entry.id, "failed", reason="ffmpeg exploded")

        response = client.post(f"/api/jobs/{entry.id}/retry")

        assert response.status_code == 200, response.text
        assert response.json()["entry"]["state"] == "queued"
        assert response.json()["entry"]["reason"] is None
        assert self._state(queue, entry.id) == "queued"

    def test_retry_resumes_a_stopped_entry(self, client, queue):
        """The route side of the promise the stop dialog already makes.

        `jobs.ts`'s `costSentence` tells the operator, BEFORE they click
        stop, that "a retry resumes at the first window nobody reached"
        (detect) or "from the first missing chunk" (transcribe) - and this
        route used to answer 409 for a stopped entry, so the confirmation
        named a control that did not exist. Making the promise true was the
        operator's own decision, over rewording it: the per-window and
        per-chunk caches that make a stop cheap are real.
        """
        entry = queue.enqueue("detect", dict(self.HERE, video_id="v"))
        queue.claim_next()
        queue.mark_finished(entry.id, "stopped", reason="stopped by the operator")

        response = client.post(f"/api/jobs/{entry.id}/retry")

        assert response.status_code == 200, response.text
        assert response.json()["entry"]["state"] == "queued"
        assert response.json()["entry"]["reason"] is None
        assert self._state(queue, entry.id) == "queued"

    def test_retry_refuses_an_entry_that_succeeded(self, client, queue):
        # `done` is the one terminal state with no retry: re-running work
        # that succeeded is a new request, and for a paid kind it spends the
        # money again on a click that means "try again".
        entry = queue.enqueue("render", dict(self.HERE))
        queue.claim_next()
        queue.mark_finished(entry.id, "done")
        assert client.post(f"/api/jobs/{entry.id}/retry").status_code == 409
        assert self._state(queue, entry.id) == "done"

    def test_an_unknown_entry_id_is_a_404_on_every_route(self, client, queue):
        # The PERCENT-ENCODED form, deliberately: httpx normalises a literal
        # '..' before sending, so `/api/jobs/../x` never reaches the route at
        # all, while `%2e%2e` arrives as a literal '..' in the path param and
        # does (see CLAUDE.md's three cases). Nothing here touches the
        # filesystem either way - an id is looked up in the plan, a closed
        # set, the same shape the provider routes' registry guard has.
        # The detail is asserted, not just the status: this file's /api
        # catch-all answers 404 for any unclaimed (path, method) pair, so a
        # status-only assertion here would pass with no routes registered at
        # all - and would keep passing if one were registered AFTER the SPA
        # fallback, which is the exact defect the ordering rule exists for.
        for entry_id in ("nosuch", "%2e%2e"):
            calls = [
                client.post(f"/api/jobs/{entry_id}/stop"),
                client.post(f"/api/jobs/{entry_id}/pause"),
                client.post(f"/api/jobs/{entry_id}/resume"),
                client.post(f"/api/jobs/{entry_id}/retry"),
                client.post(f"/api/jobs/{entry_id}/move", json={"index": 0}),
                client.delete(f"/api/jobs/{entry_id}"),
            ]
            for response in calls:
                assert response.status_code == 404, response.text
                assert "queue entry" in response.json()["detail"], response.text
        assert queue.list() == []

    # -- secrets ------------------------------------------------------------

    def test_no_route_ever_returns_a_stored_parameter_that_looks_like_a_key(
            self, client, queue, tmp_path):
        secret = "sk-ant-DO-NOT-LEAK-0123456789"

        refused = client.post(
            "/api/jobs",
            json={"kind": "detect",
                  "params": dict(self.HERE, video_id="v", api_key=secret)})
        assert refused.status_code == 400
        assert secret not in refused.text
        assert queue.list() == []

        # The other half: `jobs.json` is a plain file. One written by hand -
        # or by a version before the queue refused such params - can still
        # carry one, and the ROUTE is the last thing between it and a
        # browser, so it redacts on the way out too.
        (tmp_path / "jobs.json").write_text(json.dumps({"entries": [{
            "id": "planted", "kind": "detect",
            "params": dict(self.HERE, video_id="v", api_key=secret),
            "state": "queued", "reason": None, "progress": None,
            "created_at": 1.0, "after": None, "job_id": None}]}),
            encoding="utf-8")
        queue.load()

        listing = client.get("/api/jobs")
        assert secret not in listing.text
        assert listing.json()["queued"][0]["params"]["api_key"] == "[redacted]"
        assert listing.json()["queued"][0]["params"]["video_id"] == "v"

    # -- the workspace is not always there ----------------------------------

    @staticmethod
    def _registered_queue_routes(app) -> set:
        """Every queue route on the app, as (method, path template).

        Derived from the ROUTER rather than written out, so a route added
        later cannot quietly stay out of the enumeration below - which is
        exactly how `PUT /api/settings/limits` came to be the one route
        that answered 500 in this state while the other eight answered 503.

        The filter has to be as wide as that claim. It used to be
        `/api/jobs*` PLUS the one literal `/api/settings/limits`, which
        would have let the very next `/api/settings/<something>` queue route
        slip past exactly the way the limits route already had - the
        docstring said "a route added later cannot quietly stay out" while
        the code only knew about the one route that had already caught it
        out. So the whole `/api/settings` prefix is swept, and anything
        under it that is NOT about the queue is excluded BY NAME below,
        which is the direction that fails safe: a new route is included
        until someone deliberately says it does not belong.

        `{job_id}` paths are excluded on purpose and the exclusion is the
        point, not a convenience: `GET /api/jobs/{job_id}` and its `…/log`
        address a `studio.jobs.Job`, a completely different id space from
        the queue `Entry` every route here takes (see list_queue's own
        docstring). They have no queue to be unavailable.
        """
        # Routes under /api/settings that have nothing to do with the queue.
        # `GET /api/settings` aggregates auth, providers and the workspace
        # and answers 200 with `queue.available: false` rather than 503 -
        # that IS how it says why (see get_settings).
        not_about_the_queue = {("GET", "/api/settings")}
        found = set()
        for route in app.routes:
            path = getattr(route, "path", "")
            if "{job_id}" in path:
                continue
            if path.startswith("/api/jobs") or path.startswith("/api/settings"):
                for method in getattr(route, "methods", ()):
                    if method in ("HEAD", "OPTIONS"):
                        continue
                    if (method, path) in not_about_the_queue:
                        continue
                    found.add((method, path))
        return found

    def test_every_route_says_why_when_the_queue_is_unavailable(
            self, app, client, monkeypatch):
        # _build_queue_and_worker is deliberately best-effort: a workspace
        # that will not resolve leaves both None, and the studio still starts.
        # Every route must then say something an operator can act on - never a
        # 500, and never an empty listing that reads as "no jobs".
        #
        # The CAUSE is reproduced here, not only its symptom. Nulling the two
        # app.state attributes is what a queue-reading route sees, but PUT
        # /api/settings/limits reads neither - it resolves the workspace
        # itself, to write settings.json - so against the symptom alone it
        # answered 200 and its real behaviour (an unhandled WorkspaceError,
        # i.e. a 500) stayed invisible.
        def no_workspace():
            raise workspace_module.WorkspaceError("no workspace in this test")

        monkeypatch.setattr(studio_api, "_resolve_workspace", no_workspace)
        app.state.job_queue = None
        app.state.worker = None
        calls = [
            ("GET", "/api/jobs", None),
            ("POST", "/api/jobs", {"kind": "render", "params": dict(self.HERE)}),
            ("POST", "/api/jobs/{entry_id}/stop", None),
            ("POST", "/api/jobs/{entry_id}/pause", None),
            ("POST", "/api/jobs/{entry_id}/resume", None),
            ("POST", "/api/jobs/{entry_id}/move", {"index": 0}),
            ("POST", "/api/jobs/{entry_id}/retry", None),
            ("DELETE", "/api/jobs/{entry_id}", None),
            ("PUT", "/api/settings/limits", {"limits": {"cpu": 2}}),
        ]
        assert {(method, path) for method, path, _ in calls} == \
            self._registered_queue_routes(app), (
                "a queue route was added or renamed and this enumeration was "
                "not extended - which is how one of them slipped out before")
        for method, template, body in calls:
            path = template.replace("{entry_id}", "x")
            response = client.request(method, path, json=body)
            assert response.status_code == 503, f"{method} {path}: {response.text}"
            assert "workspace" in response.json()["detail"].lower()

    # -- the pool limits ----------------------------------------------------

    def test_the_pool_limits_round_trip_through_settings(self, client, workspace_root):
        from yt_shorts import workspace as workspace_module
        settings = workspace_module.settings_path(workspace_root)
        settings.unlink(missing_ok=True)
        try:
            before = client.get("/api/settings").json()["queue"]
            assert before["limits"] == {"cpu": 1, "net": 3}
            assert before["pools"] == ["cpu", "net"]
            assert before["available"] is True

            written = client.put("/api/settings/limits",
                                 json={"limits": {"cpu": 3}})
            assert written.status_code == 200, written.text
            assert written.json()["limits"] == {"cpu": 3, "net": 3}

            assert client.get("/api/settings").json()["queue"]["limits"] == {
                "cpu": 3, "net": 3}
            # The LIVE queue, not just the file: a limit that only lands on
            # disk changes nothing until the next restart.
            assert client.app.state.job_queue.limits()["cpu"] == 3
            # …and the file, not just the live queue: a fresh app must come up
            # with what the operator chose.
            assert create_app().state.job_queue.limits() == {"cpu": 3, "net": 3}
        finally:
            settings.unlink(missing_ok=True)

    @pytest.mark.parametrize("limits", [{"cpu": 0}, {"cpu": -1}, {"cpu": True},
                                        {"cpu": 1.5}, {"cpu": "2"},
                                        {"gpu": 2}])
    def test_an_unusable_pool_limit_is_refused(self, client, workspace_root, limits):
        # `True` in particular: pydantic coerces a JSON true into 1 for an int
        # field, so a dict[str, int] body would have accepted it silently as
        # "one job at a time" - the same trap PatchClipBody's `trim` documents.
        from yt_shorts import workspace as workspace_module
        settings = workspace_module.settings_path(workspace_root)
        settings.unlink(missing_ok=True)
        try:
            response = client.put("/api/settings/limits", json={"limits": limits})
            assert response.status_code == 400, response.text
            assert not settings.exists(), "a refused limit was still written"
        finally:
            settings.unlink(missing_ok=True)

    # -- what the stored settings file may say (I-1) -------------------------
    # `_stored_limits` is the read side of the pool limits and it runs on the
    # path that BUILDS the queue at startup. Every one of these shapes was
    # driven by a review and answered correctly, with nothing pinning it:
    # replacing the whole per-value guard with a bare `limits[pool] = value`
    # left the suite green.

    @staticmethod
    def _write_settings(root, raw: str):
        from yt_shorts import workspace as ws
        path = ws.settings_path(root)
        path.write_text(raw, encoding="utf-8")
        return path

    @pytest.mark.parametrize("raw", [
        '{"limits": {"cpu": "lots"}}',      # a string where a count belongs
        '{"limits": {"cpu": 0}}',           # 0 stalls the pool forever
        '{"limits": {"cpu": -2}}',
        '{"limits": {"cpu": true}}',        # bool is an int in Python; not a count
        '{"limits": {"cpu": 1.5}}',
        '{"limits": {"cpu": null}}',
        '{"limits": {"cpu": [3]}}',
        '{"limits": {"cpu": {"n": 3}}}',
        '{"limits": {"gpu": 4}}',           # a pool this workspace does not have
        '{"limits": null}',
        '{"limits": [1, 2, 3]}',
        '{"limits": "cpu=3"}',
        '[1, 2, 3]',                        # valid JSON, wrong top-level shape
        '{ not json at all',
        '',
    ])
    def test_an_unusable_stored_limit_falls_back_to_the_default(
            self, workspace_root, raw):
        # Not merely "does not raise": the DEFAULTS have to be what comes
        # back. Two of these are the ones that actually hurt if they get
        # through - "lots" reaches JobQueue._pool_has_room's `count < limit`
        # as int < str and raises TypeError on the WORKER thread, and 0
        # leaves every cpu job queued forever with nothing saying why. Both
        # are exactly what _validated_limits refuses on the WRITE side.
        path = self._write_settings(workspace_root, raw)
        try:
            got = studio_api._stored_limits(workspace_root)
            assert got == dict(worker_module.DEFAULT_LIMITS)
            # …and by TYPE, not only by value: `True == 1` in Python, so the
            # equality above alone passes for a stored `true` that got
            # through - measured, it is the one shape of this parametrize
            # that survived the mutation this test exists to kill.
            assert all(type(value) is int for value in got.values()), got
        finally:
            path.unlink(missing_ok=True)

    def test_one_unusable_pool_costs_only_that_pool_its_stored_limit(
            self, workspace_root):
        # The guard is per VALUE, not per file: a garbage `net` must not
        # discard a perfectly good `cpu` beside it. This is the half a
        # whole-file check would get wrong in the other direction.
        path = self._write_settings(
            workspace_root, '{"limits": {"cpu": 4, "net": "lots"}}')
        try:
            assert studio_api._stored_limits(workspace_root) == {"cpu": 4, "net": 3}
        finally:
            path.unlink(missing_ok=True)

    def test_a_usable_stored_limit_is_honoured(self, workspace_root):
        # The other side of the guard: it must not be so strict that a
        # legitimate saved limit never survives a restart.
        path = self._write_settings(workspace_root, '{"limits": {"cpu": 2, "net": 7}}')
        try:
            assert studio_api._stored_limits(workspace_root) == {"cpu": 2, "net": 7}
        finally:
            path.unlink(missing_ok=True)

    def test_an_unreadable_settings_file_still_leaves_the_studio_a_queue(
            self, workspace_root):
        # The reason the read degrades at all: this runs while the app is
        # being built, so a stray character in a hand-editable file must cost
        # one pool its custom limit, never the studio its whole queue.
        path = self._write_settings(workspace_root, '{"limits": {"cpu": "lots"}}')
        try:
            built = create_app()
            assert built.state.job_queue is not None
            assert built.state.job_queue.limits() == dict(worker_module.DEFAULT_LIMITS)
        finally:
            path.unlink(missing_ok=True)

    # -- a params value carries no names of its own (I-3) --------------------

    def test_a_nested_key_shaped_param_never_reaches_the_plan(self, client, queue):
        # `looks_like_a_secret_name` tests a NAME and only sees the top
        # level, so this shape - proven against the real route by a review -
        # used to be enqueued, written to jobs.json and served straight back
        # out by GET /api/jobs: "creds" looks like nothing and nothing ever
        # looked inside it.
        marker = "sk-ant-NESTED-DO-NOT-PERSIST-9f3c1"
        response = client.post("/api/jobs", json={
            "kind": "render",
            "params": dict(self.HERE, creds={"api_key": marker}),
        })
        assert response.status_code == 400, response.text
        assert "nested" in response.json()["detail"].lower()

        assert queue.list() == [], "a refused entry was still added to the plan"
        on_disk = (queue.path.read_text(encoding="utf-8")
                   if queue.path.exists() else "")
        assert marker not in on_disk, "the key reached jobs.json"
        assert marker not in json.dumps(client.get("/api/jobs").json())

    def test_a_key_shaped_name_deeper_still_is_refused_too(self, client, queue):
        # One level further down, and inside a LIST - the shape a check
        # written as "refuse a dict at the top of a value" would let past.
        response = client.post("/api/jobs", json={
            "kind": "render",
            "params": dict(self.HERE, blobs=[{"inner": {"token": "t"}}]),
        })
        assert response.status_code == 400, response.text
        assert queue.list() == []

    def test_a_list_of_strings_is_still_a_usable_param(self, client, queue):
        # The refusal must not be "no non-scalar values": `render` ships with
        # `clips: [str]` (see studio/worker.py's table), and a rule that
        # refused it would break the one shipped kind that needs a list.
        response = client.post("/api/jobs", json={
            "kind": "render", "params": dict(self.HERE, clips=["a", "b"]),
        })
        assert response.status_code == 200, response.text
        assert queue.list()[0].params["clips"] == ["a", "b"]

    def test_a_nested_key_shaped_param_already_on_disk_is_redacted_on_the_way_out(
            self, app, client, queue):
        # jobs.json is a plain file: one written by hand, or by a version
        # before enqueue refused this, can still hold the shape above. This
        # route is the last thing between that file and a browser.
        marker = "sk-ant-NESTED-FROM-DISK-4a7b2"
        queue.path.write_text(json.dumps({"entries": [{
            "id": "planted", "kind": "render",
            "params": {"channel": CHANNEL, "event": EVENT,
                       "creds": {"api_key": marker},
                       "blobs": [{"session_token": marker}]},
            "state": "queued", "reason": None, "progress": None,
            "created_at": 0.0, "after": None, "job_id": None,
        }]}), encoding="utf-8")
        queue.load()

        payload = client.get("/api/jobs").json()
        assert marker not in json.dumps(payload)
        params = payload["queued"][0]["params"]
        assert params["creds"]["api_key"] == "[redacted]"
        assert params["blobs"][0]["session_token"] == "[redacted]"
        # The NAME is kept, at every depth, so the operator can see which
        # entry to delete - only the value goes.
        assert params["channel"] == CHANNEL

    # -- a dependency that names nothing (M-7) -------------------------------

    def test_an_after_that_names_no_entry_is_refused(self, client, queue):
        # The queue treats an unknown `after` as satisfied on purpose (a
        # long-since-done dependency is aged out of the plan by
        # _trim_finished), so without this the constraint an operator typed
        # would be silently dropped and the entry would run immediately.
        response = client.post("/api/jobs", json={
            "kind": "render", "params": dict(self.HERE), "after": "typo",
        })
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "typo" in detail
        assert queue.list() == [], "a refused entry was still added to the plan"

    def test_an_after_that_names_a_real_entry_is_accepted(self, client, queue):
        first = client.post("/api/jobs", json={
            "kind": "render", "params": dict(self.HERE)})
        assert first.status_code == 200, first.text
        depends_on = first.json()["entry"]["id"]

        second = client.post("/api/jobs", json={
            "kind": "render", "params": dict(self.HERE), "after": depends_on})
        assert second.status_code == 200, second.text
        assert second.json()["entry"]["after"] == depends_on
        assert [e.after for e in queue.list()] == [None, depends_on]


class TestTheRenderRouteValidatesItsClipNames:
    """`POST …/render` takes its clip names in a BODY list, not as a URL path
    segment - so httpx's own dot-segment normalisation never touches them and
    the literal string reaches the route (see CLAUDE.md on why the obvious
    traversal test is otherwise a tautology).

    Measured before this guard existed: `{"clips": ["../../../../../OUTSIDE"]}`
    returned 200, and `render.build_short` was handed
    `<event>/clips/../../../../../OUTSIDE/short.mp4` as its target and that
    same directory as its work dir - so a short, a raw download and an overlay
    PNG were written outside the event's own clips/ directory.
    """

    def test_a_clip_name_that_escapes_the_clips_dir_is_refused(
            self, client, studio_profile, event_dir, real_job_starters):
        # `real_job_starters`, deliberately: the guard lives INSIDE
        # `jobs.start_render_job` (before the event lock and before the
        # thread), so a test that stubbed the starter would stub the very
        # thing under test and pass against a broken build. Nothing expensive
        # runs - the starter raises before it acquires anything.
        response = client.post(f"{EVENT_PREFIX}/render",
                               json={"clips": ["../../../../../OUTSIDE"]})
        assert response.status_code == 400, response.text
        assert "clip name" in response.json()["detail"]
        # No lock was taken and no thread was started: the refusal happens
        # ahead of both.
        assert not (event_dir / ".render.lock").exists()
        assert "job_id" not in response.json()


class TestTheHostGuard:
    """The studio serves 127.0.0.1 and nothing else, so a request naming any
    other host did not come from a page the operator opened at the studio's
    own address.

    This is the read half of the CSRF/DNS-rebinding defence the Origin check
    above covers for mutations. Origin cannot close it: a rebound page's
    Origin IS the attacker's domain (which the mutation guard refuses), but a
    plain GET carries no Origin the guard could act on, and after the rebind
    the page is same-origin as far as the browser is concerned. The Host
    header is the one thing the attacker cannot change without giving up the
    origin they need, so it is what this checks.

    Measured before this guard existed: `GET /api/fs?path=$HOME` with
    `Host: evil.example.com` answered 200 with the operator's home directory
    listing.
    """

    def test_a_read_with_a_foreign_host_is_refused(self, client, studio_profile):
        response = client.get("/api/channels", headers={"host": "evil.example.com"})
        assert response.status_code == 403

    def test_the_filesystem_browser_is_unreachable_from_a_rebound_page(
            self, client, studio_profile):
        # The worst of the reads: an arbitrary directory listing anywhere the
        # studio process can read.
        response = client.get("/api/fs", params={"path": str(Path.home())},
                              headers={"host": "evil.example.com"})
        assert response.status_code == 403

    def test_a_mutating_request_with_a_foreign_host_is_refused(
            self, client, studio_profile):
        response = client.delete(f"/api/channels/{CHANNEL}/events/ghost",
                                 headers={"host": "evil.example.com"})
        assert response.status_code == 403

    def test_every_loopback_spelling_is_allowed(self, client, studio_profile):
        # With and without a port, and both IP families - all four are what a
        # real studio is reached by.
        for host in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            response = client.get("/api/channels", headers={"host": host})
            assert response.status_code == 200, host
