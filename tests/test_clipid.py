from yt_shorts.clipid import canonical_url, clip_id, directory_name, slug

CLIP = "https://www.youtube.com/clip/UgkxSpeedy123"


class TestCanonicalUrl:
    def test_a_query_string_does_not_change_identity(self):
        assert canonical_url(f"{CLIP}?si=abc") == canonical_url(CLIP)

    def test_a_fragment_does_not_change_identity(self):
        assert canonical_url(f"{CLIP}#t=10") == canonical_url(CLIP)

    def test_a_trailing_slash_does_not_change_identity(self):
        assert canonical_url(f"{CLIP}/") == canonical_url(CLIP)

    def test_surrounding_whitespace_does_not_change_identity(self):
        assert canonical_url(f"  {CLIP}  ") == canonical_url(CLIP)

    def test_host_case_does_not_change_identity(self):
        assert canonical_url("https://WWW.YOUTUBE.COM/clip/UgkxSpeedy123") == canonical_url(CLIP)

    def test_a_different_clip_is_a_different_url(self):
        assert canonical_url(CLIP) != canonical_url(CLIP + "X")


class TestClipId:
    def test_the_id_is_stable_for_the_same_url(self):
        assert clip_id(CLIP) == clip_id(CLIP)

    def test_the_id_has_the_documented_shape(self):
        value = clip_id(CLIP)
        assert len(value) == 8
        assert all(c in "0123456789abcdef" for c in value)

    def test_different_urls_get_different_ids(self):
        assert clip_id(CLIP) != clip_id(CLIP + "X")

    def test_query_variants_share_one_id(self):
        assert clip_id(f"{CLIP}?si=abc") == clip_id(CLIP)

    def test_empty_input_raises_error(self):
        import pytest
        with pytest.raises(ValueError, match="empty.*"):
            clip_id("")

    def test_query_only_input_raises_error(self):
        import pytest
        with pytest.raises(ValueError, match="empty.*"):
            clip_id("?si=abc")


class TestSlug:
    def test_a_title_becomes_a_readable_slug(self):
        assert slug("Jegr and the Barbie") == "jegr-and-the-barbie"

    def test_punctuation_collapses(self):
        assert slug("WHAT IS HAPPENING?!?") == "what-is-happening"

    def test_a_slug_is_capped_at_fifty_characters(self):
        assert slug("a" * 60) == "a" * 50

    def test_the_cap_never_leaves_a_trailing_separator(self):
        # "word word word..." caps exactly on a separator; it must be
        # stripped, so this is 49 characters, not 50. Verified by running it.
        capped = slug("word " * 40)
        assert not capped.endswith("-")
        assert len(capped) == 49

    def test_a_title_with_nothing_usable_yields_an_empty_slug(self):
        assert slug("!!! ???") == ""

    def test_umlauts_are_not_silently_dropped_into_an_empty_slug(self):
        # YouTube titles carry them; the slug is only a label, so any
        # non-ascii run collapses to a separator rather than vanishing.
        assert slug("Nürburgring") == "n-rburgring"


class TestDirectoryName:
    def test_the_name_pairs_the_slug_with_the_id(self):
        name = directory_name(CLIP, "Speedy!")
        assert name == f"speedy--{clip_id(CLIP)}"

    def test_two_clips_with_the_same_title_get_different_directories(self):
        assert directory_name(CLIP, "Speedy!") != directory_name(CLIP + "X", "Speedy!")

    def test_the_directory_name_always_ends_with_the_clip_id(self):
        # Renamed from "a title change does not change the directory" -
        # this never actually retitles anything and calls directory_name()
        # only once; the real "retitling keeps the same directory"
        # guarantee is exercised end to end in
        # test_clipstore.py::TestWritingAClip::test_a_retitled_clip_keeps_its_original_directory.
        # What THIS test actually pins is the name's shape: the id suffix
        # is always present, regardless of the label in front of it.
        name = directory_name(CLIP, "Speedy!")
        assert name.endswith(clip_id(CLIP))

    def test_an_unusable_title_falls_back_to_the_bare_id(self):
        assert directory_name(CLIP, "!!!") == clip_id(CLIP)
