import pytest

from yt_shorts.timecode import to_seconds, to_text, with_padding


class TestToSeconds:
    def test_hours_minutes_seconds(self):
        assert to_seconds("03:18:42") == 11922.0

    def test_minutes_seconds(self):
        assert to_seconds("18:42") == 1122.0

    def test_seconds_only(self):
        assert to_seconds("42") == 42.0

    def test_number_stays_number(self):
        assert to_seconds(42.5) == 42.5

    def test_fractional_seconds(self):
        assert to_seconds("00:00:02.5") == 2.5

    def test_nonsense_raises(self):
        with pytest.raises(ValueError):
            to_seconds("three o'clock")


class TestToText:
    def test_rounds_to_whole_seconds(self):
        assert to_text(11922.4) == "03:18:42"

    def test_pads_with_zeros(self):
        assert to_text(5) == "00:00:05"

    def test_over_ten_hours(self):
        assert to_text(36000) == "10:00:00"

    def test_negative_seconds_raises(self):
        with pytest.raises(ValueError):
            to_text(-5)


class TestWithPadding:
    def test_extends_both_sides(self):
        assert with_padding(100.0, 120.0, 2.0, 3.0, 500.0) == (98.0, 123.0)

    def test_clamps_at_video_start(self):
        assert with_padding(1.0, 20.0, 5.0, 0.0, 500.0) == (0.0, 20.0)

    def test_clamps_at_video_end(self):
        assert with_padding(480.0, 499.0, 0.0, 10.0, 500.0) == (480.0, 500.0)

    def test_unknown_length_only_clamps_the_front(self):
        assert with_padding(1.0, 20.0, 5.0, 10.0, None) == (0.0, 30.0)

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError):
            with_padding(50.0, 40.0, 0.0, 0.0, 500.0)

    def test_length_less_than_start_raises(self):
        with pytest.raises(ValueError):
            with_padding(100.0, 120.0, 0.0, 0.0, 50.0)

    def test_negative_lead_in_raises(self):
        with pytest.raises(ValueError):
            with_padding(10.0, 20.0, -5.0, 0.0, 500.0)

    def test_negative_lead_out_raises(self):
        with pytest.raises(ValueError):
            with_padding(10.0, 20.0, 0.0, -5.0, 500.0)
