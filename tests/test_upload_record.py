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
