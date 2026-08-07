# Block C — Wiki and README Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the bulk of `README.md` into a generated GitHub wiki, leaving a ~180-line shop window, and make every link and image in that wiki checkable.

**Architecture:** The wiki source lives in the repo at `docs/wiki/`, one Markdown file per page. `tools/check-wiki-links.py` validates it offline and is enforced twice — by `tests/test_wiki.py` in the suite and by `tools/sync-wiki.py` before it pushes. `tools/sync-wiki.py` mirrors `docs/wiki/` into `<origin>.wiki.git`; `.github/workflows/wiki.yml` runs the same thing on manual dispatch. `tools/build-wiki-images.py` regenerates the images from the test fixture so no third-party frame and no real channel data reach a public wiki.

**Tech Stack:** Python 3.12+ stdlib (the three tools), pytest (the gate), Pillow and ffmpeg (image generation, already dependencies), Playwright (studio screenshots, already a dev dependency), GitHub Actions.

**Design:** `docs/superpowers/specs/2026-08-07-wiki-and-readme-split-design.md`

## Global Constraints

- **The project is English** — code, docs, folder names, commit messages, wiki pages.
- **`PYTHONPATH=src .venv/bin/pytest -q`** runs the suite. **`python3 tools/lint.py`** must exit 0 before every commit; it needs no `PYTHONPATH`.
- **`tools/check-wiki-links.py` imports nothing from this project** and uses the stdlib only — `sync-wiki.py` loads it by file path and both run outside the venv, exactly like `logsetup.py`'s rule.
- **Comments in checked-in files are one to three lines.** Reasoning belongs in the commit message or this plan.
- **No rule from `CLAUDE.md` or a skill is restated in the wiki.** Where a rule reaches the operator, the wiki states its *effect* and links to the source.
- **A wiki page cannot link to a repo file relatively.** The wiki is a separate git repository; every repo reference is a full `https://github.com/jegr78/yt-shorts/blob/main/<path>` URL.
- **Never run `npm run build` (or anything that triggers it) while a test suite is in flight** — a build deletes `src/yt_shorts/studio/static/` before rewriting it.
- **Every new test is proven by mutation:** break the thing it claims to check, watch it go red, restore.
- **No change to rendering, overlay, detection or upload behaviour.** This block moves prose and adds three tools plus one workflow.

## File Structure

| File | Responsibility |
|---|---|
| `tools/check-wiki-links.py` | Create. The checker: intra-wiki links and anchors, repo references, relative file and image targets. Stdlib only, exit 1 on breakage. |
| `tools/sync-wiki.py` | Create. Mirrors `docs/wiki/` into `<origin>.wiki.git`. Runs the checker first. |
| `tools/build-wiki-images.py` | Create. Regenerates `docs/wiki/images/`. |
| `tests/test_wiki.py` | Create. Unit tests of the checker plus the integration run over the real `docs/wiki/`. |
| `.github/workflows/wiki.yml` | Create. `workflow_dispatch` only. |
| `docs/wiki/*.md` | Create. The pages. |
| `docs/wiki/images/*.png` | Create (generated). |
| `README.md` | Modify: 1573 lines → ~180. |
| `CONTRIBUTING.md` | Modify: point at the wiki where it currently points into the README's moved sections. |
| `.gitignore` | Modify: add `/.wiki-clone/`. |
| `tests/test_workflows.py` | Modify: `wiki.yml` joins the pinned-SHA and no-`master` checks automatically; add the dispatch-only assertion. |

**README line ranges as they stand today** (used verbatim by Tasks 3–6; re-read the file before cutting, because each completed task shifts what follows it):

| Section | Lines |
|---|---|
| `Installing` | 12–39 |
| `Requirements` | 40–62 |
| `Workflow after a race weekend` | 63–112 |
| `What the tool guarantees` | 113–152 |
| `Layout` | 153–241 |
| `Where the data lives` | 242–347 |
| `The editorial layer (edit.json)` | 348–422 |
| `Studio` | 423–694 |
| `Development` | 695–768 |
| `Setting up a new channel` | 769–848 |
| `Subtitles` | 849–963 |
| `Whole-stream transcription` | 964–1006 |
| `Moment detection` | 1007–1105 |
| `Model providers` | 1106–1475 |
| `Upload` | 1476–1563 |
| `Not built yet (later)` | 1564–1573 |

---

### Task 1: The link checker and its gate

**Files:**
- Create: `tools/check-wiki-links.py`
- Create: `tests/test_wiki.py`
- Create: `docs/wiki/Home.md`, `docs/wiki/_Sidebar.md`

**Interfaces:**
- Produces: `check_wiki(directory, repo_root=None) -> list[str]` (problem strings, empty when clean), `github_anchor(heading, seen=None) -> str`, `page_anchors(markdown) -> set[str]`, `main(argv=None)`. `sync-wiki.py` (Task 7) loads this module by path and calls `check_wiki`.
- Consumes: `pyproject.toml`'s `[project.urls] Repository`, to learn which `https://github.com/<owner>/<repo>/…` URLs are its own.

`Home.md` and `_Sidebar.md` are created here rather than in a later task because the integration test needs a real wiki to run over; a test that skips when the directory is missing would pass for the wrong reason.

- [ ] **Step 1: Write the failing test**

Create `tests/test_wiki.py`:

```python
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
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q`
Expected: collection error — `tools/check-wiki-links.py` does not exist.

- [ ] **Step 3: Write the checker**

Create `tools/check-wiki-links.py`:

```python
#!/usr/bin/env python3
"""Check the links in docs/wiki/ - pages, anchors, repo references and images.

Heading renames break `[text](Page#anchor)` silently, and a wiki cannot link to
a repo file relatively (it is a separate git repository), so those links are
full github.com URLs that a scheme-skipping checker would pass without looking.

Usage:
  python3 tools/check-wiki-links.py             # checks docs/wiki/
  python3 tools/check-wiki-links.py some/dir

Exit 1 when something is broken. Gates: tests/test_wiki.py runs it in the
suite; tools/sync-wiki.py runs it before pushing.
"""
import html
import os
import re
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI = os.path.join(ROOT, "docs", "wiki")

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LINK_RE = re.compile(r'(?<!!)\[[^\]]*\]\(([^)]*?)(?:\s+"[^"]*")?\)')
IMAGE_RE = re.compile(r'!\[[^\]]*\]\(([^)]*?)(?:\s+"[^"]*")?\)')
SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*:")
_MD_DECOR = re.compile(r"[*`]")           # not "_": GitHub's own anchors keep it
_ANCHOR_DROP = re.compile(r"[^\w\- ]")


def github_anchor(heading, seen=None):
    """GitHub's heading -> anchor id; `seen` makes duplicates -1, -2, ...
    Entities are unescaped first, because GitHub anchors the RENDERED text."""
    text = _MD_DECOR.sub("", html.unescape(heading.strip())).lower()
    text = _ANCHOR_DROP.sub("", text).replace(" ", "-")
    if seen is None:
        return text
    n = seen.get(text)
    seen[text] = (n or 0) + 1
    return text if n is None else f"{text}-{n}"


def _content_lines(markdown):
    """(line number, line) outside fenced code blocks."""
    fence = None
    for i, line in enumerate(markdown.splitlines(), 1):
        marker = line.lstrip()[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            yield i, line


def page_anchors(markdown):
    """Every anchor id a page provides."""
    seen = {}
    return {github_anchor(match.group(2), seen)
            for _, line in _content_lines(markdown)
            if (match := HEADING_RE.match(line))}


def extract_links(markdown):
    """(line, target) for every inline link outside code fences."""
    return [(i, m.group(1))
            for i, line in _content_lines(markdown)
            for m in LINK_RE.finditer(line)]


def extract_images(markdown):
    """(line, target) for every image embed outside code fences."""
    return [(i, m.group(1))
            for i, line in _content_lines(markdown)
            for m in IMAGE_RE.finditer(line)]


def repo_slug(repo_root):
    """`owner/name` from pyproject.toml, so the checker knows which github.com
    URLs are its own. Returns None when it cannot tell."""
    path = os.path.join(repo_root, "pyproject.toml")
    try:
        with open(path, "rb") as fh:
            url = tomllib.load(fh)["project"]["urls"]["Repository"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        print(f"warning: could not determine repo slug from {path}", file=sys.stderr)
        return None
    return url.rstrip("/").removeprefix("https://github.com/") or None


def _check_repo_reference(target, slug, repo_root):
    """None when the target is not one of our own blob URLs, otherwise a
    problem string or ''."""
    prefix = f"https://github.com/{slug}/blob/"
    if slug is None or not target.startswith(prefix):
        return None
    rest = target[len(prefix):]
    _, _, rest = rest.partition("/")            # drop the ref (main, a tag, a sha)
    path, _, anchor = rest.partition("#")
    full = os.path.join(repo_root, path)
    if not os.path.exists(full):
        return f"repo file does not exist: {path}"
    if anchor and path.endswith(".md"):
        with open(full, encoding="utf-8") as fh:
            if anchor not in page_anchors(fh.read()):
                return f"anchor '{anchor}' not in {path}"
    return ""


def check_wiki(directory, repo_root=None):
    """Problems as '<page>:<line>: ...' strings; empty when the wiki is clean."""
    directory = str(directory)
    repo_root = str(repo_root) if repo_root else ROOT
    slug = repo_slug(repo_root)
    docs = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith(".md"):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                docs[name] = fh.read()
    anchors = {name[:-3]: page_anchors(md) for name, md in docs.items()}

    problems = []
    for name, md in docs.items():
        for line, target in extract_links(md) + extract_images(md):
            if not target:
                problems.append(f"{name}:{line}: empty link target")
                continue
            if SCHEME_RE.match(target):
                verdict = _check_repo_reference(target, slug, repo_root)
                if verdict:
                    problems.append(f"{name}:{line}: {verdict} ({target})")
                continue
            if "/" in target:
                if not os.path.exists(os.path.join(directory, target.partition("#")[0])):
                    problems.append(f"{name}:{line}: file does not exist ({target})")
                continue
            page, _, anchor = target.partition("#")
            if page and page not in anchors:
                # a same-directory file (an image, a text file) keeps its
                # extension, unlike a page link, so a page-name miss checks here
                if os.path.exists(os.path.join(directory, page)):
                    continue
                problems.append(f"{name}:{line}: link to missing page '{page}' ({target})")
                continue
            if anchor and anchor not in anchors[page or name[:-3]]:
                problems.append(f"{name}:{line}: broken anchor '{target}'")
    return problems


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    directory = args[0] if args else WIKI
    if not os.path.isdir(directory):
        sys.exit(f"not a directory: {directory}")
    problems = check_wiki(directory)
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(1)
    print(f"wiki links OK ({os.path.relpath(directory, ROOT)})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Write the two pages the integration test needs**

Create `docs/wiki/Home.md`. It is the wiki's landing page: one paragraph on what the tool does, then a linked list of every page. Write it with only `Home` and `_Sidebar` existing so far, and extend it in each later task as pages appear — the sidebar test in Task 6 is what finally pins that no page was forgotten.

Create `docs/wiki/_Sidebar.md` with the same list, no prose.

Both must satisfy the checker: every link points at a page that exists at the time of the commit.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q`
Expected: PASS.

- [ ] **Step 6: Prove the tests can fail**

Break one thing at a time, confirm the named test goes red, restore:

Restore by copy, never with `git checkout --` — that reverts to the INDEX, not
to `HEAD`, and it has destroyed uncommitted work on this project twice:

```bash
# a) an intra-wiki link the checker must catch
cp docs/wiki/Home.md /tmp/Home.md.bak
printf '\n[nope](No-Such-Page)\n' >> docs/wiki/Home.md
PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q      # test_it_has_no_broken_links FAILS
cp /tmp/Home.md.bak docs/wiki/Home.md && rm /tmp/Home.md.bak

# b) the repo-reference rule (edit the test's own fixture, not the tool)
#    change CLAUDE.md to CLAUDE-GONE.md in test_a_reference_to_an_existing_file_is_accepted
#    -> that test FAILS. Restore.

# c) the anchor rule
#    make github_anchor return the heading unchanged
#    -> TestTheAnchorAlgorithm FAILS. Restore.
```

- [ ] **Step 7: Lint and commit**

```bash
python3 tools/lint.py
git add tools/check-wiki-links.py tests/test_wiki.py docs/wiki/
git commit -m "docs: a link checker for the wiki, and the wiki's first two pages"
```

---

### Task 2: The image builder

**Files:**
- Create: `tools/build-wiki-images.py`
- Create (generated): `docs/wiki/images/overlay.png`, `docs/wiki/images/frame.png`, `docs/wiki/images/studio-editor.png`

**Interfaces:**
- Consumes: `yt_shorts.profile.load`, `yt_shorts.overlay.build_overlay`, the `erf` fixture under `tests/fixtures/channels/`, `ffmpeg`, Playwright.
- Produces: three PNGs under `docs/wiki/images/`, referenced by pages in Tasks 3–6.

Run with `PYTHONPATH=src`. The script needs `profile.CHANNELS_DIR` pointed at `tests/fixtures/channels/` — that is the fixture the suite owns, and using it is what keeps a real channel's data out of a public wiki.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Regenerate docs/wiki/images/ from the test fixture.

Nothing here borrows a frame of somebody's race stream or a real channel's
data: the overlay is this project's own Pillow output, the frame's picture is
ffmpeg's `testsrc`, and the studio screenshot is driven against a temporary
workspace seeded from tests/fixtures/channels/.

Usage:  PYTHONPATH=src .venv/bin/python tools/build-wiki-images.py

Do not run this while a test suite is in flight - the studio step serves
src/yt_shorts/studio/static/, and a frontend build deletes it before rewriting.
"""
```

The three producers, in order:

1. **`overlay.png`** — `profile.CHANNELS_DIR` to the fixture, `profile.load("erf/community-clips-back-catalogue")`, `overlay.build_overlay("WHAT IS HAPPENING?!?", profile.channel["footer"], profile.config)`, flattened onto a mid-grey background so the transparency is visible, saved at half size (540x960) to keep the file small.

2. **`frame.png`** — a synthetic source, then the real filter chain, then an extraction that applies the sample aspect ratio:

```python
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                "-i", "testsrc=size=1920x1080:rate=10:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True)
```

   Build the short from it with `render.build_short` (subtitles off), then:

```python
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(short),
                "-vf", "scale=iw*sar:ih", "-frames:v", "1", str(out)], check=True)
```

   `scale=iw*sar:ih` is not optional. An extracted frame ignores the sample aspect ratio, which is exactly how the SAR bug shipped once — `CLAUDE.md`'s "Verifying changes" records it.

3. **`studio-editor.png`** — start `studio.api.create_app()` against a `tempfile.TemporaryDirectory()` workspace seeded from the fixture, serve it with uvicorn on an ephemeral port in a thread, drive Playwright's sync API to the editor screen, `page.screenshot()`, then shut down. Set a fixed viewport (1440x900) so the image does not change with whoever runs the script.

   **Do not invent this wiring — read it.** `tests/test_studio_e2e.py` already starts a real server against a seeded temporary workspace and drives it with Playwright; its `live_studio` fixture and the `_serving`/`_AppSwitch` machinery around it are the working reference for `create_app`'s arguments, how the workspace is seeded from `tests/fixtures/channels/`, and how the server is shut down. That file uses pytest fixtures and the async-free sync API differs slightly, so this is a source to copy the *calls* from, not a module to import.

Each producer prints what it wrote. A missing `ffmpeg`, a missing Playwright browser or a missing Pillow must exit with a message naming what to install — this is a maintainer tool, so failing loudly is right and degrading is not.

- [ ] **Step 2: Run it**

Run: `PYTHONPATH=src .venv/bin/python tools/build-wiki-images.py`
Expected: three PNGs under `docs/wiki/images/`, and the script says where.

- [ ] **Step 3: Verify the frame is not lying**

```bash
ffprobe -v error -select_streams v \
  -show_entries stream=width,height,sample_aspect_ratio,display_aspect_ratio \
  -of csv=p=0 <the short the script built>
```

Expected: `1080,1920,1:1,9:16`. Look at `docs/wiki/images/frame.png` and confirm it shows the blurred background, the sharp 16:9 window and the overlay — not a stretched picture.

- [ ] **Step 4: Prove it is reproducible**

Run the script twice and confirm the second run overwrites with the same bytes for `overlay.png` and `frame.png`:

```bash
shasum -a 256 docs/wiki/images/*.png > /tmp/before
PYTHONPATH=src .venv/bin/python tools/build-wiki-images.py
shasum -a 256 -c /tmp/before
```

The studio screenshot may differ (font rendering, cursor); if it does, say so in the script's docstring rather than pretending otherwise.

- [ ] **Step 5: Lint and commit**

```bash
python3 tools/lint.py
git add tools/build-wiki-images.py docs/wiki/images/
git commit -m "docs: generate the wiki's images from the test fixture"
```

---

### Task 3: The pages about structure and data

**Files:**
- Create: `docs/wiki/Layout.md`, `docs/wiki/Where-the-data-lives.md`, `docs/wiki/The-editorial-layer.md`, `docs/wiki/Setting-up-a-new-channel.md`
- Modify: `README.md` (remove the moved sections), `docs/wiki/Home.md`, `docs/wiki/_Sidebar.md`

**Interfaces:**
- Consumes: the checker from Task 1, `images/frame.png` from Task 2.

Move `README.md`'s `Layout` (153–241), `Where the data lives` (242–347), `The editorial layer (edit.json)` (348–422) and `Setting up a new channel` (769–848). **Re-read the file before each cut** — every removal shifts the ranges below it.

- [ ] **Step 1: Move the text**

One page per section. The section's `##` becomes the page's `#`; every heading below it moves up one level. Text moves **as written** — this is a move, not a rewrite. Two edits are required and are the only ones allowed:

1. Any link that pointed at another README section becomes a wiki link (`[Studio](Studio)`) or, if the target stayed in the README, a repo reference (`https://github.com/jegr78/yt-shorts/blob/main/README.md#requirements`).
2. Where a passage restates a rule that lives in `CLAUDE.md` or a skill, cut the reasoning and keep the effect, then link the source. `Where the data lives` is where this bites: the "derived data is never edited in place" rule belongs to `CLAUDE.md`, and the wiki says what the operator sees (a correction goes to `edit.json`, a re-render never loses it) and links there.

- [ ] **Step 2: Add the frame image to Layout**

`Layout.md` opens with `![A rendered short: blurred background, the 16:9 window, the overlay](images/frame.png)` before its first paragraph.

- [ ] **Step 3: Link the four pages**

Add them to `Home.md` and `_Sidebar.md`.

- [ ] **Step 4: Run the gate**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q`
Expected: PASS — including `test_every_page_is_reachable_from_the_sidebar_or_home`.

- [ ] **Step 5: Commit**

```bash
python3 tools/lint.py
git add -A
git commit -m "docs: move layout, data and editorial-layer reference into the wiki"
```

---

### Task 4: The pipeline pages

**Files:**
- Create: `docs/wiki/Subtitles.md`, `docs/wiki/Whole-stream-transcription.md`, `docs/wiki/Moment-detection.md`, `docs/wiki/Model-providers.md`, `docs/wiki/Upload.md`
- Modify: `README.md`, `docs/wiki/Home.md`, `docs/wiki/_Sidebar.md`

Same mechanics as Task 3, for `Subtitles` (849–963), `Whole-stream transcription` (964–1006), `Moment detection` (1007–1105), `Model providers` (1106–1475) and `Upload` (1476–1563) — ranges as of the original file; re-read before cutting.

This is where the boundary rule does most of its work, because all five subjects also live in a skill.

- [ ] **Step 1: Move the text, then cut what belongs to a skill**

For each page, keep what the operator does and sees; move nothing that explains why. Concretely:

- **Model-providers**: keep which providers exist, what goes in the settings, what a key costs, where keys are stored, what happens when a scan fails. Cut the reasoning behind one-engine-per-run and the `ModelError`-from-type-name rule; link `.claude/skills/detection-and-providers/SKILL.md`. Keep the *effect* the operator sees: a window that fails is recorded in `missing_windows` and is not silently filled from the lexicon.
- **Upload**: keep the steps, the fields, and that privacy defaults to `private` and a non-private upload takes a per-upload confirmation. Cut the enforcement design; link `.claude/skills/upload-to-youtube/SKILL.md`.
- **Subtitles**: keep how corrections work in the transcript editor and what a lost caption looks like in the render panel. Cut the pipeline internals; link `CLAUDE.md`.
- **Moment-detection** and **Whole-stream-transcription**: keep the operator's path; link the detection skill for everything else.

- [ ] **Step 2: Link the pages, run the gate, commit**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q` — must pass, and every repo reference added above is now verified by it.

```bash
python3 tools/lint.py
git add -A
git commit -m "docs: move the subtitle, detection, provider and upload reference into the wiki"
```

---

### Task 5: Studio, and the two pages that did not exist

**Files:**
- Create: `docs/wiki/Studio.md`, `docs/wiki/Building-from-source.md`, `docs/wiki/If-something-goes-wrong.md`
- Modify: `README.md`, `docs/wiki/Home.md`, `docs/wiki/_Sidebar.md`

- [ ] **Step 1: Move `Studio` (423–694)**

Same mechanics. `Studio.md` embeds `![The studio's editor](images/studio-editor.png)`. The studio's own rules live in `src/yt_shorts/studio/CLAUDE.md`; link it rather than repeating it.

- [ ] **Step 2: Write `Building-from-source`**

New page. It takes over `README.md`'s `Development` section (695–768) for the parts `CONTRIBUTING.md` does not already carry, and answers: how to get a wheel, how to get a binary for one OS, what each needs (Node for the frontend, `pyinstaller` for the binary), and what is deliberately not bundled (ffmpeg, yt-dlp, the Whisper models — `yt-shorts install-tools` fetches them). Anything about the test suite stays in `CONTRIBUTING.md`; link it.

- [ ] **Step 3: Write `If-something-goes-wrong`**

New page, the operator's failure catalogue. It must contain at least:

- **macOS refuses to run the binary.** The release binaries are unsigned and stay quarantined; Block B's design deferred this workaround to Block C and it is documented nowhere else:

```bash
xattr -dr com.apple.quarantine /path/to/yt-shorts
```

- **A render appears stuck.** The Whisper decode has no timeout — the one place the "one failed clip never aborts a run" guarantee does not hold. Say what to check and that the recovery is Ctrl-C, then `kill -9` if that does not answer; link `src/yt_shorts/transcribe.py`'s module docstring for why it is left this way.
- **A clip rendered without subtitles.** The render panel shows the reason; a lost caption track degrades rather than failing.
- **The studio refuses to start.** One studio per workspace; the refusal names the process holding the lock.
- **A render refuses to start.** The event is locked by another render.
- **`yt-shorts doctor`** — name it as the first thing to run.

- [ ] **Step 4: Link the pages, run the gate, commit**

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_wiki.py -q
python3 tools/lint.py
git add -A
git commit -m "docs: the studio, building from source, and what to do when it breaks"
```

---

### Task 6: The README becomes a shop window

**Files:**
- Modify: `README.md`, `CONTRIBUTING.md`, `docs/wiki/Home.md`, `docs/wiki/_Sidebar.md`

**Interfaces:**
- Consumes: every page created in Tasks 3–5.

- [ ] **Step 1: Cut what is left**

Remove `Development` (whatever Task 5 did not take) and `Not built yet (later)`; the latter moves into `Home.md`. What remains is the opening, `Installing`, `Requirements`, `Workflow after a race weekend` and `What the tool guarantees`.

- [ ] **Step 2: Add the pointer**

A `## Documentation` section after `What the tool guarantees`, linking the wiki and naming the four or five pages a new operator wants first. Wiki links from the README are full URLs: `https://github.com/jegr78/yt-shorts/wiki/Studio`.

- [ ] **Step 3: Check the inbound links**

`CONTRIBUTING.md` links `README.md#installing` and `README.md#requirements`; both sections stay, so both must still resolve. Verify by hand, then update anything in `CONTRIBUTING.md` that pointed at a section that moved.

```bash
grep -n "README.md#" CONTRIBUTING.md
```

For each anchor found, confirm the heading still exists in `README.md`.

- [ ] **Step 4: Confirm the size**

```bash
wc -l README.md
```

Expected: roughly 180 lines. Materially more means a section that should have moved did not; materially fewer means something was dropped rather than moved — check the wiki has it.

- [ ] **Step 5: Full suite, lint, commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
git add -A
git commit -m "docs: the README becomes an introduction, the wiki carries the manual"
```

---

### Task 7: Publishing

**Files:**
- Create: `tools/sync-wiki.py`, `.github/workflows/wiki.yml`
- Modify: `.gitignore`, `tests/test_workflows.py`

**Interfaces:**
- Consumes: `check_wiki` from Task 1.

- [ ] **Step 1: Write `tools/sync-wiki.py`**

A working implementation of this exists in the racecast reference project
(`tools/sync-wiki.py`, 176 lines); the controller supplies its path in the
dispatch, because no path to that repository is committed here. Port it, with
three changes: the source is `docs/wiki/` not `src/docs/wiki/`, the clone is
`.wiki-clone/` not `runtime/wiki/`, and `run_link_check` passes
`repo_root=ROOT` so the repo-reference class from Task 1 is actually checked.

Structure, all of it stdlib:

- `WIKI_SRC = <root>/docs/wiki`, `CLONE = <root>/.wiki-clone`.
- `wiki_remote_from_origin()` — `git remote get-url origin`, strip a trailing `.git`, append `.wiki.git`. Exit with a message naming `--remote` when there is no origin.
- `run_link_check()` — load `tools/check-wiki-links.py` by path with `importlib.util`, call `check_wiki(WIKI_SRC, repo_root=ROOT)`, and `sys.exit` listing every problem when it is not empty. This runs **before** anything is cloned or written.
- `ensure_clone(remote)` — clone into `CLONE`, or fetch/reset/clean when it is already there. A clone failure whose message contains "not found" exits with the bootstrap instruction: GitHub creates the wiki repository only after the first page is saved through the Wiki tab, so open it, save any page once, and re-run.
- `mirror_pages()` — make the clone's top-level `*.md` and everything under `images/` match `WIKI_SRC` exactly, comparing **bytes** so the PNGs sync correctly. Returns `(added, updated, removed)`.
- `main()` — flags `--dry-run`, `-m/--message`, `--remote`. Print the remote and every change; with `--dry-run` stop there. Otherwise `git add -A`, `commit`, `push origin HEAD`.

- [ ] **Step 2: Prove the gate fires before anything is published**

```bash
cp docs/wiki/Home.md /tmp/Home.md.bak
printf '\n[nope](No-Such-Page)\n' >> docs/wiki/Home.md
python3 tools/sync-wiki.py --dry-run
# Expected: exits non-zero naming Home.md's broken link, and NEVER reaches "Wiki remote:"
cp /tmp/Home.md.bak docs/wiki/Home.md && rm /tmp/Home.md.bak
python3 tools/sync-wiki.py --dry-run
# Expected: prints the remote and the changes it would make, pushes nothing
```

- [ ] **Step 3: Add the clone directory to `.gitignore`**

```
# tools/sync-wiki.py clones the wiki repo here; it is not part of this one.
/.wiki-clone/
```

- [ ] **Step 4: Write `.github/workflows/wiki.yml`**

`workflow_dispatch` only, with `dry-run` (boolean) and `message` (string) inputs. `permissions: contents: write` — the wiki shares the repository's permissions, so the built-in `GITHUB_TOKEN` can push to `<repo>.wiki.git` and no PAT is needed. `concurrency: {group: wiki-sync, cancel-in-progress: false}`. Steps: checkout, setup-python 3.13, configure `github-actions[bot]` as the git identity, then run the script with `--remote https://x-access-token:${GH_TOKEN}@github.com/${{ github.repository }}.wiki.git`. The remote is passed explicitly because `actions/checkout`'s auth header is repo-local and does not carry into the fresh wiki clone.

Pin every action to the SHA already used elsewhere in `.github/workflows/` — `tests/test_workflows.py::TestEveryActionIsPinnedToASha` covers this file automatically once it exists.

- [ ] **Step 5: Pin the dispatch-only rule**

Add to `tests/test_workflows.py`:

```python
class TestTheWikiIsNeverPublishedByAMerge:
    """Pushing to a public wiki is a maintainer's decision. A `push:` trigger
    here would publish every merge to main, silently."""

    def test_it_triggers_on_dispatch_only(self):
        doc = yaml.safe_load((WORKFLOW_DIR / "wiki.yml").read_text(encoding="utf-8"))
        triggers = doc.get(True, doc.get("on"))
        assert set(triggers) == {"workflow_dispatch"}, (
            f"wiki.yml triggers on {sorted(triggers)}, not dispatch alone")
```

Note for the implementer: `wiki.yml` does **not** trigger on pull requests, so it adds no entry to `_pr_check_names()` and `TestEveryPullRequestCheckIsInTheRuleset.EXPECTED` stays at twelve. Confirm that by running that test rather than assuming it.

- [ ] **Step 6: Prove the new test can fail**

Add `push: {branches: [main]}` to `wiki.yml`'s `on:`, run `PYTHONPATH=src .venv/bin/pytest tests/test_workflows.py -q`, watch `test_it_triggers_on_dispatch_only` fail, remove it again.

- [ ] **Step 7: Full suite, lint, actionlint, commit**

```bash
PYTHONPATH=src .venv/bin/pytest -q
python3 tools/lint.py
actionlint .github/workflows/wiki.yml
git add -A
git commit -m "docs: publish the wiki on dispatch, never on a merge"
```

---

## After the last task

The operator has two things to do that no script can:

1. Open the repository's Wiki tab and save any page once. GitHub creates `<repo>.wiki.git` only then; until it exists, every sync fails with "repository not found".
2. Run `python3 tools/sync-wiki.py` (or dispatch **Publish Wiki**) and read the result.
