from yt_shorts.gallery import build_page


class TestBuildPage:
    def test_contains_every_draft(self):
        html = build_page([
            {"file": "a.mp4", "hook": "REI GOT SLICED"},
            {"file": "b.mp4", "hook": "WHAT IS HAPPENING?!?"},
        ], "24h Nuerburgring 2026")
        assert "a.mp4" in html and "b.mp4" in html
        assert "REI GOT SLICED" in html

    def test_escapes_angle_brackets_in_hook(self):
        html = build_page([{"file": "a.mp4", "hook": "<script>boom</script>"}], "T")
        assert "<script>boom" not in html
        assert "&lt;script&gt;" in html

    def test_empty_list_produces_valid_page(self):
        html = build_page([], "Empty")
        assert "<html" in html and "</html>" in html
