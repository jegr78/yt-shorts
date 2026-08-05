import pytest

from yt_shorts import pathnames


class TestValidateSegment:
    @pytest.mark.parametrize("good", ["race-1", "Round_2", "a", "a.b-c_d", "erf", "2026-07"])
    def test_accepts_a_safe_segment(self, good):
        pathnames.validate_segment(good, what="event name")   # must not raise

    @pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "/abs", "a b",
                                     "a" * 101, "-x", "x\n", "a\nb"])
    def test_rejects_unsafe_segments(self, bad):
        with pytest.raises(ValueError) as error:
            pathnames.validate_segment(bad, what="channel name")
        assert "channel name" in str(error.value)   # the label appears in the message

    @pytest.mark.parametrize("bad", [None, 5, 0, ["a"], [], {"a": 1}, b"a", 1.5, True])
    def test_a_non_string_is_a_ValueError_like_any_other_bad_segment(self, bad):
        # Not pedantry about types: the falsy ones (None, 0, [], False) already
        # answered ValueError while the TRUTHY ones (5, ["a"], b"a") reached
        # len()/match() and came back out as TypeError - so the same defect got
        # two different answers depending on the value. Every caller documents
        # ValueError, and `detect._has` CATCHES it to keep one odd id from
        # sinking a whole catalogue; a TypeError walked straight past that
        # handler. One kind of bad segment, one kind of refusal.
        with pytest.raises(ValueError) as error:
            pathnames.validate_segment(bad, what="video id")
        assert "video id" in str(error.value)
