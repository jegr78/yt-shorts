from datetime import datetime, timezone

from yt_shorts.quota import INSERT_COST, QuotaTracker


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class TestQuota:
    def test_starts_empty(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        assert q.spent_today(utc(2026, 7, 22, 18)) == 0

    def test_booking_an_insert_spends_the_cost(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        q.book_insert(utc(2026, 7, 22, 18))
        assert q.spent_today(utc(2026, 7, 22, 18)) == INSERT_COST

    def test_write_is_atomic_leaving_no_temp_file(self, tmp_path):
        import json
        q = QuotaTracker(tmp_path, "UCabc")
        q.book_insert(utc(2026, 7, 22, 18))
        assert list(tmp_path.glob(".quota-*.tmp")) == []
        # The persisted file is complete/valid JSON, not a torn write.
        json.loads((tmp_path / "quota.json").read_text())

    def test_remaining_uploads_reflects_the_default_quota(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        assert q.remaining_uploads(utc(2026, 7, 22, 18)) == 10000 // INSERT_COST  # 6
        q.book_insert(utc(2026, 7, 22, 18))
        assert q.remaining_uploads(utc(2026, 7, 22, 18)) == 5

    def test_resets_at_the_pacific_day_boundary(self, tmp_path):
        q = QuotaTracker(tmp_path, "UCabc")
        # 2026-07-22 06:00 UTC is 2026-07-21 23:00 Pacific (PDT, UTC-7)
        q.book_insert(utc(2026, 7, 22, 6))
        assert q.spent_today(utc(2026, 7, 22, 6)) == INSERT_COST
        # 2026-07-22 08:00 UTC is 2026-07-22 01:00 Pacific - a new Pacific day
        assert q.spent_today(utc(2026, 7, 22, 8)) == 0

    def test_two_channels_track_separately(self, tmp_path):
        a = QuotaTracker(tmp_path, "UCaaa")
        b = QuotaTracker(tmp_path, "UCbbb")
        a.book_insert(utc(2026, 7, 22, 18))
        assert b.spent_today(utc(2026, 7, 22, 18)) == 0
