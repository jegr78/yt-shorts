"""What must stay true of docs/wiki/ and of the checker that guards it.

The checker decides whether the wiki is publishable, so it gets unit tests of
its own: one that cannot fail would let every broken link through silently.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "docs" / "wiki"


def _load():
    path = ROOT / "tools" / "check-wiki-links.py"
    spec = importlib.util.spec_from_file_location("check_wiki_links", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


def _wiki(tmp_path, pages, images=()):
    for name, text in pages.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    if images:
        (tmp_path / "images").mkdir(exist_ok=True)
        for name in images:
            (tmp_path / "images" / name).write_bytes(b"")
    return tmp_path


class TestTheAnchorAlgorithm:
    def test_it_lowercases_and_hyphenates(self):
        assert check.github_anchor("Where the data lives") == "where-the-data-lives"

    def test_it_drops_punctuation_and_keeps_the_gap(self):
        assert check.github_anchor("The editorial layer (`edit.json`)") == "the-editorial-layer-editjson"

    def test_duplicate_headings_get_github_s_suffixes(self):
        seen = {}
        assert check.github_anchor("Setup", seen) == "setup"
        assert check.github_anchor("Setup", seen) == "setup-1"

    def test_a_fenced_code_block_provides_no_anchors(self):
        assert check.page_anchors("```\n# Not a heading\n```\n# Real\n") == {"real"}


class TestIntraWikiLinks:
    def test_a_clean_wiki_reports_nothing(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[Layout](Layout)\n", "Layout.md": "# Layout\n"})
        assert check.check_wiki(tmp_path) == []

    def test_a_link_to_a_missing_page_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[Gone](Nowhere)\n"})
        assert any("Nowhere" in problem for problem in check.check_wiki(tmp_path))

    def test_a_link_to_a_missing_anchor_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[X](Layout#no-such)\n", "Layout.md": "# Layout\n"})
        assert any("no-such" in problem for problem in check.check_wiki(tmp_path))

    def test_a_same_page_anchor_resolves_against_its_own_page(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n## Deeper\n[jump](#deeper)\n"})
        assert check.check_wiki(tmp_path) == []


class TestRepoReferences:
    """The class the reference checker cannot see: it skips anything with a
    scheme, and a wiki cannot link to a repo file any other way."""

    def test_a_reference_to_an_existing_file_is_accepted(self, tmp_path):
        _wiki(tmp_path, {"Home.md":
                         "# Home\n[rules](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md)\n"})
        assert check.check_wiki(tmp_path, repo_root=ROOT) == []

    def test_a_reference_to_a_missing_file_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md":
                         "# Home\n[x](https://github.com/jegr78/yt-shorts/blob/main/NOPE.md)\n"})
        assert any("NOPE.md" in problem
                   for problem in check.check_wiki(tmp_path, repo_root=ROOT))

    def test_a_missing_anchor_in_a_repo_markdown_file_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md":
                         "# Home\n[x](https://github.com/jegr78/yt-shorts/blob/main/CLAUDE.md#no-such-heading)\n"})
        assert any("no-such-heading" in problem
                   for problem in check.check_wiki(tmp_path, repo_root=ROOT))

    def test_a_foreign_url_is_ignored(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[docs](https://example.invalid/whatever#anchor)\n"})
        assert check.check_wiki(tmp_path, repo_root=ROOT) == []


class TestFileAndImageTargets:
    def test_an_existing_image_is_accepted(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n![frame](images/frame.png)\n"}, images=["frame.png"])
        assert check.check_wiki(tmp_path) == []

    def test_a_missing_image_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n![frame](images/gone.png)\n"})
        assert any("gone.png" in problem for problem in check.check_wiki(tmp_path))


class TestTheRealWiki:
    def test_the_directory_exists_and_has_pages(self):
        assert WIKI.is_dir(), f"no {WIKI}"
        assert list(WIKI.glob("*.md")), "no wiki pages - every check below would be vacuous"

    def test_it_has_no_broken_links(self):
        problems = check.check_wiki(WIKI, repo_root=ROOT)
        assert not problems, "\n  " + "\n  ".join(problems)

    def test_every_page_is_reachable_from_the_sidebar_or_home(self):
        # Not part of the checker: a page nobody links to still publishes fine,
        # it is only invisible. Worth failing the suite over, not the sync.
        linked = set()
        for name in ("_Sidebar.md", "Home.md"):
            text = (WIKI / name).read_text(encoding="utf-8")
            linked |= {target.partition("#")[0]
                       for _, target in check.extract_links(text)}
        orphans = sorted(page.stem for page in WIKI.glob("*.md")
                         if page.stem not in linked
                         and page.name not in ("_Sidebar.md", "Home.md"))
        assert not orphans, f"wiki pages nothing links to: {orphans}"
