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
