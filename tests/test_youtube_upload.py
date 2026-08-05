import pytest

from yt_shorts.editorial import Edit
from yt_shorts.youtube_upload import (
    UploadError, UploadResult, build_metadata, upload_short)


def _edit(title=None):
    return Edit(title=title, status="kept", transcript=None)


class TestBuildMetadata:
    def test_title_is_the_effective_hook(self):
        clip = {"hook": "harvested", "source_title": "ERF 24h"}
        body = build_metadata(clip, _edit(title="Corrected title"), {})
        assert body["snippet"]["title"] == "Corrected title"

    def test_title_falls_back_to_the_hook(self):
        clip = {"hook": "CRASH at Turn 1", "source_title": "ERF 24h"}
        assert build_metadata(clip, _edit(), {})["snippet"]["title"] == "CRASH at Turn 1"

    def test_privacy_is_always_private(self):
        # config cannot override the private-by-default invariant
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(),
                              {"upload": {"privacy": "public"}})
        assert body["status"]["privacyStatus"] == "private"

    def test_made_for_kids_is_always_present_and_defaults_false(self):
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})
        assert body["status"]["selfDeclaredMadeForKids"] is False

    def test_description_template_interpolates_source_title_and_title(self):
        clip = {"hook": "Speedy!", "source_title": "ERF 24h Part 1"}
        config = {"upload": {"description": "From {source_title}. {title}"}}
        body = build_metadata(clip, _edit(), config)
        assert body["snippet"]["description"] == "From ERF 24h Part 1. Speedy!"

    def test_an_invalid_description_template_raises_a_clear_upload_error(self):
        clip = {"hook": "Speedy!", "source_title": "ERF"}
        for bad in ("Clip of {event}", "Clip of {", "Clip of {0}"):
            config = {"upload": {"description": bad}}
            with pytest.raises(UploadError, match="template is invalid"):
                build_metadata(clip, _edit(), config)

    def test_category_defaults_to_gaming(self):
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})
        assert body["snippet"]["categoryId"] == "20"

    def test_tags_come_from_config_and_default_empty(self):
        assert build_metadata({"hook": "x", "source_title": "y"}, _edit(), {})["snippet"]["tags"] == []
        body = build_metadata({"hook": "x", "source_title": "y"}, _edit(),
                              {"upload": {"tags": ["simracing", "erf"]}})
        assert body["snippet"]["tags"] == ["simracing", "erf"]


class TestVisibilityAndScheduling:
    """build_metadata's new contract: visibility/publish_at are an explicit
    operator choice (default stays private), effective metadata comes from
    editorial.effective_upload, and YouTube's own limits are guarded here
    rather than surfacing as an opaque API error at upload time."""

    def _clip(self):
        return {"hook": "CRASH", "source_title": "ERF R3"}

    def _edit(self, title=None, upload=None):
        return Edit(title=title, status="kept", transcript=None, upload=upload)

    def test_default_visibility_is_private(self):
        meta = build_metadata(self._clip(), self._edit(), {})
        assert meta["status"]["privacyStatus"] == "private"

    def test_explicit_public(self):
        meta = build_metadata(self._clip(), self._edit(), {}, visibility="public")
        assert meta["status"]["privacyStatus"] == "public"

    def test_scheduled_is_private_plus_publishat(self):
        meta = build_metadata(self._clip(), self._edit(), {}, visibility="private",
                              publish_at="2099-01-01T10:00:00Z")
        assert meta["status"]["privacyStatus"] == "private"
        assert meta["status"]["publishAt"] == "2099-01-01T10:00:00Z"

    def test_publishat_with_nonprivate_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(), {}, visibility="public",
                           publish_at="2099-01-01T10:00:00Z")

    def test_bad_visibility_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(), {}, visibility="friends")

    def test_per_clip_metadata_wins(self):
        meta = build_metadata(
            self._clip(),
            self._edit(upload={"description": "hand written", "tags": ["gt7"],
                               "category_id": "17"}),
            {"upload": {"description": "tmpl {title}", "tags": ["x"],
                       "category_id": "20"}})
        assert meta["snippet"]["description"] == "hand written"  # free text, no templating
        assert meta["snippet"]["tags"] == ["gt7"]
        assert meta["snippet"]["categoryId"] == "17"

    def test_per_clip_description_with_literal_brace_is_verbatim(self):
        # A per-clip description is final free text, used as-is - never
        # passed through str.format - so a literal '{' in it is safe.
        meta = build_metadata(self._clip(),
                              self._edit(upload={"description": "score {200}"}), {})
        assert meta["snippet"]["description"] == "score {200}"

    def test_channel_template_still_formats_when_no_clip_override(self):
        meta = build_metadata(self._clip(), self._edit(),
                              {"upload": {"description": "Clip from {source_title}"}})
        assert meta["snippet"]["description"] == "Clip from ERF R3"

    def test_title_too_long_rejected(self):
        long_title = "x" * 101
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(title=long_title), {})

    def test_tags_too_long_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(),
                           self._edit(upload={"tags": ["x" * 300, "y" * 300]}), {})

    def test_empty_tag_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(upload={"tags": ["ok", ""]}), {})

    def test_description_too_long_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(), {"upload": {"description": "x" * 5001}})

    def test_past_publish_at_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(), {}, visibility="private",
                           publish_at="2000-01-01T10:00:00Z")

    def test_naive_publish_at_rejected(self):
        with pytest.raises(UploadError):
            build_metadata(self._clip(), self._edit(), {}, visibility="private",
                           publish_at="2099-01-01T10:00:00")


class FakeInsert:
    def __init__(self, result=None, error=None):
        self._result = result or {"id": "VID123"}
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._result


class FakeVideos:
    def __init__(self, insert):
        self._insert = insert
        self.called_with = None

    def insert(self, part, body, media_body):
        self.called_with = {"part": part, "body": body, "media": media_body}
        return self._insert


class FakeService:
    def __init__(self, insert):
        self._videos = FakeVideos(insert)

    def videos(self):
        return self._videos


def _meta():
    return {"snippet": {"title": "t"}, "status": {"privacyStatus": "private"}}


class TestUploadShort:
    def test_returns_the_video_id_and_url(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert({"id": "VID123"}))
        result = upload_short(short, _meta(), service=service,
                             media_factory=lambda p: f"media:{p}")
        assert isinstance(result, UploadResult)
        assert result.video_id == "VID123"
        assert result.url == "https://www.youtube.com/watch?v=VID123"

    def test_sends_snippet_and_status_parts(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert())
        upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert "snippet" in service._videos.called_with["part"]
        assert "status" in service._videos.called_with["part"]

    def test_a_quota_error_becomes_an_upload_error(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert(error=RuntimeError("quotaExceeded")))
        with pytest.raises(UploadError) as error:
            upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert "quota" in str(error.value).lower()

    def test_returns_the_actual_privacy_status_from_the_response(self, tmp_path):
        # YouTube is the authority on what actually happened - not the request.
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(
            FakeInsert({"id": "VID123", "status": {"privacyStatus": "public"}}))
        result = upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert result.privacy_status == "public"

    def test_privacy_status_falls_back_to_the_requested_value_when_absent(self, tmp_path):
        short = tmp_path / "short.mp4"
        short.write_bytes(b"x")
        service = FakeService(FakeInsert({"id": "VID123"}))  # no status in the response
        result = upload_short(short, _meta(), service=service, media_factory=lambda p: p)
        assert result.privacy_status == "private"  # from _meta()'s requested status
