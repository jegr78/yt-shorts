from yt_shorts.upload_record import is_uploaded, load, record_path, save


class TestUploadRecord:
    def test_absent_means_not_uploaded(self, tmp_path):
        assert is_uploaded(tmp_path) is False
        assert load(tmp_path) is None

    def test_save_then_is_uploaded(self, tmp_path):
        save(tmp_path, "VID123", "https://youtu.be/VID123", "private",
             when="2026-07-22T10:00:00Z")
        assert is_uploaded(tmp_path) is True
        record = load(tmp_path)
        assert record["video_id"] == "VID123"
        assert record["privacy"] == "private"
        assert record["uploaded_at"] == "2026-07-22T10:00:00Z"

    def test_record_path_is_upload_json(self, tmp_path):
        assert record_path(tmp_path).name == "upload.json"


class TestTheWriteIsAtomic:
    def test_a_failed_save_leaves_the_clip_still_recorded_as_uploaded(
            self, tmp_path, monkeypatch):
        """The one place in this project where an empty read is worse than an
        error. `load` turns a JSONDecodeError into None and `is_uploaded`
        reads None as "not uploaded" - so a reader landing inside a
        truncating write would be told this clip may be uploaded AGAIN, which
        is irreversible, public and costs quota. Replacing the file whole
        removes that window; the record is either the old one or the new one.
        """
        import os

        import pytest

        save(tmp_path, "VID123", "https://youtu.be/VID123", "private",
             when="2026-07-22T10:00:00Z")
        before = record_path(tmp_path).read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            save(tmp_path, "VID999", "https://youtu.be/VID999", "public",
                 when="2026-07-22T11:00:00Z")

        assert record_path(tmp_path).read_bytes() == before
        assert is_uploaded(tmp_path) is True
        assert load(tmp_path)["video_id"] == "VID123"
