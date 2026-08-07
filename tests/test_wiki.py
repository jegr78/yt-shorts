"""What must stay true of docs/wiki/ and of the checker that guards it.

The checker decides whether the wiki is publishable, so it gets unit tests of
its own: one that cannot fail would let every broken link through silently.
"""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = ROOT / "docs" / "wiki"
REPO = "https://github.com/jegr78/yt-shorts"
BLOB, TREE, WIKI = f"{REPO}/blob/main", f"{REPO}/tree/main", f"{REPO}/wiki"


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

    def test_it_keeps_underscores_unlike_asterisks_and_backticks(self):
        assert check.github_anchor("snake_case_name") == "snake_case_name"


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
    """Links to files in this repo, addressed by full github.com URL because a
    wiki page cannot reach a repo file by a relative path - it is a separate
    git repository."""

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

    def test_an_existing_image_without_a_slash_is_accepted(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n![frame](frame.png)\n"})
        (tmp_path / "frame.png").write_bytes(b"")
        assert check.check_wiki(tmp_path) == []

    def test_an_existing_file_link_without_a_slash_is_accepted(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[notes](notes.txt)\n"})
        (tmp_path / "notes.txt").write_bytes(b"")
        assert check.check_wiki(tmp_path) == []


class TestLinkTargetSyntax:
    def test_a_target_containing_a_space_is_extracted(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[X](Some Page)\n", "Some Page.md": "# Some Page\n"})
        assert check.check_wiki(tmp_path) == []

    def test_a_missing_page_with_a_space_in_its_name_is_reported(self, tmp_path):
        # A target the old [^)\s]+ regex could not match at all was DROPPED,
        # not flagged - this is the case that tells the two apart.
        _wiki(tmp_path, {"Home.md": "# Home\n[X](Some Missing Page)\n"})
        assert any("Missing" in problem for problem in check.check_wiki(tmp_path))

    def test_a_target_with_a_title_and_a_space_still_stops_before_the_title(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[X](Some Page \"a title\")\n", "Some Page.md": "# Some Page\n"})
        assert check.check_wiki(tmp_path) == []

    def test_an_empty_target_is_reported(self, tmp_path):
        _wiki(tmp_path, {"Home.md": "# Home\n[x]()\n"})
        assert any("empty" in problem for problem in check.check_wiki(tmp_path))


class TestTheRealWiki:
    def test_the_directory_exists_and_has_pages(self):
        assert WIKI_DIR.is_dir(), f"no {WIKI_DIR}"
        assert list(WIKI_DIR.glob("*.md")), "no wiki pages - every check below would be vacuous"

    def test_it_has_no_broken_links(self):
        problems = check.check_wiki(WIKI_DIR, repo_root=ROOT)
        assert not problems, "\n  " + "\n  ".join(problems)

    def test_every_page_is_reachable_from_the_sidebar_or_home(self):
        # Not part of the checker: a page nobody links to still publishes fine,
        # it is only invisible. Worth failing the suite over, not the sync.
        linked = set()
        for name in ("_Sidebar.md", "Home.md"):
            text = (WIKI_DIR / name).read_text(encoding="utf-8")
            linked |= {target.partition("#")[0]
                       for _, target in check.extract_links(text)}
        orphans = sorted(page.stem for page in WIKI_DIR.glob("*.md")
                         if page.stem not in linked
                         and page.name not in ("_Sidebar.md", "Home.md"))
        assert not orphans, f"wiki pages nothing links to: {orphans}"


def _sync():
    path = ROOT / "tools" / "sync-wiki.py"
    spec = importlib.util.spec_from_file_location("sync_wiki", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _sync()


class TestCredentialsNeverReachAnOutput:
    """In CI the wiki remote carries a token, and it is printed and embedded in
    every git error."""

    def test_it_strips_userinfo(self):
        assert sync.without_credentials(
            "https://x-access-token:ghp_SECRET@github.com/a/b.wiki.git"
        ) == "https://github.com/a/b.wiki.git"

    def test_a_url_without_credentials_is_unchanged(self):
        url = "https://github.com/a/b.wiki.git"
        assert sync.without_credentials(url) == url

    def test_an_ssh_remote_is_unchanged(self):
        url = "git@github.com:a/b.wiki.git"
        assert sync.without_credentials(url) == url

    def test_it_reaches_a_git_failure_message(self, monkeypatch, tmp_path):
        secret = "https://x-access-token:ghp_SECRET@github.com/a/b.wiki.git"
        with pytest.raises(RuntimeError) as error:
            sync.git(["clone", secret, str(tmp_path / "nope")], cwd=str(tmp_path))
        assert "ghp_SECRET" not in str(error.value)


class TestMirroringThePages:
    """A mirror, not a copy: it also deletes. And it compares BYTES, or the
    three PNGs under images/ do not sync."""

    def _both(self, tmp_path, monkeypatch):
        source, clone = tmp_path / "src", tmp_path / "clone"
        (source / "images").mkdir(parents=True)
        (clone / "images").mkdir(parents=True)
        monkeypatch.setattr(sync, "WIKI_SRC", str(source))
        monkeypatch.setattr(sync, "CLONE", str(clone))
        return source, clone

    def test_a_png_that_changed_without_changing_size_is_copied(self, tmp_path, monkeypatch):
        source, clone = self._both(tmp_path, monkeypatch)
        (source / "Home.md").write_text("# Home\n", encoding="utf-8")
        (clone / "Home.md").write_text("# Home\n", encoding="utf-8")
        (source / "images" / "frame.png").write_bytes(b"\x89PNG" + b"\x01" * 60)
        (clone / "images" / "frame.png").write_bytes(b"\x89PNG" + b"\x02" * 60)

        added, updated, removed = sync.mirror_pages()

        assert updated == [os.path.join("images", "frame.png")]
        assert (added, removed) == ([], [])
        assert (clone / "images" / "frame.png").read_bytes() == \
            (source / "images" / "frame.png").read_bytes()

    def test_a_page_the_source_no_longer_has_is_removed(self, tmp_path, monkeypatch):
        source, clone = self._both(tmp_path, monkeypatch)
        (source / "Home.md").write_text("# Home\n", encoding="utf-8")
        (clone / "Home.md").write_text("# Home\n", encoding="utf-8")
        (clone / "Gone.md").write_text("# Gone\n", encoding="utf-8")

        added, updated, removed = sync.mirror_pages()

        assert removed == ["Gone.md"]
        assert not (clone / "Gone.md").exists()

    def test_an_identical_file_is_in_none_of_the_three_lists(self, tmp_path, monkeypatch):
        source, clone = self._both(tmp_path, monkeypatch)
        (source / "Home.md").write_text("# Home\n", encoding="utf-8")
        (clone / "Home.md").write_text("# Home\n", encoding="utf-8")

        assert sync.mirror_pages() == ([], [], [])


class TestTheWikiRemote:
    def _origin(self, tmp_path, monkeypatch, url):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url],
                       check=True)
        monkeypatch.setattr(sync, "ROOT", str(tmp_path))

    def test_it_derives_the_wiki_repo_from_an_https_origin(self, tmp_path, monkeypatch):
        self._origin(tmp_path, monkeypatch, "https://github.com/jegr78/yt-shorts.git")
        assert sync.wiki_remote_from_origin() == \
            "https://github.com/jegr78/yt-shorts.wiki.git"

    def test_it_derives_it_from_an_ssh_origin_too(self, tmp_path, monkeypatch):
        self._origin(tmp_path, monkeypatch, "git@github.com:jegr78/yt-shorts.git")
        assert sync.wiki_remote_from_origin() == "git@github.com:jegr78/yt-shorts.wiki.git"


class TestRepoReferenceShapes:
    """Issue #15: /tree/ was unrecognised, a query string was read as part of
    the filename, and a line anchor on a non-Markdown file went unchecked."""

    def _wiki(self, tmp_path, target):
        (tmp_path / "Home.md").write_text(f"# Home\n[x]({target})\n", encoding="utf-8")
        return check.check_wiki(tmp_path, repo_root=ROOT)

    def test_a_query_string_is_not_part_of_the_filename(self, tmp_path):
        assert self._wiki(tmp_path, f"{BLOB}/CLAUDE.md?plain=1") == []

    def test_a_tree_url_to_a_real_directory_is_accepted(self, tmp_path):
        assert self._wiki(tmp_path, f"{TREE}/tools") == []

    def test_a_tree_url_naming_a_file_is_reported(self, tmp_path):
        assert any("use /blob/" in p for p in self._wiki(tmp_path, f"{TREE}/CLAUDE.md"))

    def test_a_blob_url_naming_a_directory_is_reported(self, tmp_path):
        assert any("use /tree/" in p for p in self._wiki(tmp_path, f"{BLOB}/tools"))

    def test_a_line_anchor_past_the_end_is_reported(self, tmp_path):
        assert any("lines" in p for p in self._wiki(tmp_path, f"{BLOB}/ruff.toml#L99999"))

    def test_a_line_anchor_inside_the_file_is_accepted(self, tmp_path):
        assert self._wiki(tmp_path, f"{BLOB}/ruff.toml#L1") == []

    def test_a_foreign_host_is_still_ignored(self, tmp_path):
        assert self._wiki(tmp_path, "https://example.invalid/tree/main/nope") == []


class TestLinksPointingIntoTheWiki:
    """Issue #14: check_wiki resolves links OUT of docs/wiki/. Nothing looked
    the other way, so renaming a page broke every pointer at it silently."""

    def test_the_real_tree_has_none_broken(self):
        problems = check.check_inbound()
        assert not problems, "\n  " + "\n  ".join(problems)

    def test_it_actually_scans_something(self):
        # Guards the guard: a scan that finds no URL would pass above forever.
        slug = check.repo_slug(str(ROOT))
        urls = [t for _, full in check._tracked_text_files(str(ROOT))
                for t in check.URL_RE.findall(
                    Path(full).read_text(encoding="utf-8", errors="replace"))
                if f"/{slug}/wiki" in t]
        assert len(urls) > 10, f"only {len(urls)} wiki URLs seen - is the scan working?"

    def test_a_link_to_a_missing_page_is_reported(self, tmp_path):
        (tmp_path / "Home.md").write_text("# Home\n", encoding="utf-8")
        problems = check._check_wiki_reference(
            f"{WIKI}/No-Such-Page", check.repo_slug(str(ROOT)),
            check._wiki_pages(tmp_path))
        assert "No-Such-Page" in problems

    def test_a_link_to_a_missing_anchor_is_reported(self, tmp_path):
        (tmp_path / "Home.md").write_text("# Home\n## Real\n", encoding="utf-8")
        problems = check._check_wiki_reference(
            f"{WIKI}/Home#nope", check.repo_slug(str(ROOT)),
            check._wiki_pages(tmp_path))
        assert "nope" in problems

    def test_the_frozen_design_documents_are_not_scanned(self):
        # They name pages that were true when they were written.
        names = [n for n, _ in check._tracked_text_files(str(ROOT))]
        assert not [n for n in names if n.startswith("docs/superpowers/")]
        assert any(n == "README.md" for n in names)
