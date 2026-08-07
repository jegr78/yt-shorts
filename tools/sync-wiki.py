#!/usr/bin/env python3
"""Publish docs/wiki/ to the GitHub wiki repo.

Maintainer script (NOT shipped). The wiki is generated from the repo: edit the
Markdown under docs/wiki/, then run this to mirror it into the GitHub wiki.

It derives the wiki remote from the repo's `origin` (`<origin>.wiki.git`), clones
it into .wiki-clone/ (gitignored) or pulls if already there, mirrors the pages
(add/update/delete), commits and pushes.

Usage:
  python3 tools/sync-wiki.py                 # mirror + commit + push
  python3 tools/sync-wiki.py --dry-run       # show what would change, push nothing
  python3 tools/sync-wiki.py -m "msg"        # custom commit message
  python3 tools/sync-wiki.py --remote URL    # override the wiki remote URL

First-time bootstrap: GitHub only creates the wiki Git repo after the FIRST page is
saved via the web UI. If the clone fails with "repository not found", open the repo's
Wiki tab, create+save any page once, then re-run this script.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIKI_SRC = os.path.join(ROOT, "docs", "wiki")
CLONE = os.path.join(ROOT, ".wiki-clone")


def git(args, cwd=None, check=True, capture=True):
    """Run a git command; return stdout (stripped) when capturing."""
    r = subprocess.run(["git", *args], cwd=cwd, text=True,
                       capture_output=capture)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return (r.stdout or "").strip() if capture else ""


def wiki_remote_from_origin():
    """`<origin>.wiki.git` derived from the repo's origin (https or ssh)."""
    try:
        url = git(["remote", "get-url", "origin"], cwd=ROOT)
    except RuntimeError:
        sys.exit("ERROR: no 'origin' remote on this repo. Pass --remote <wiki url>.")
    if url.endswith(".git"):
        url = url[:-4]
    return url + ".wiki.git"


def run_link_check():
    """Abort the sync when tools/check-wiki-links.py finds broken links.
    `repo_root` is what checks the links from a wiki page INTO this repo; drop
    it and that whole class goes unchecked while the sync still says OK."""
    import importlib.util
    path = os.path.join(ROOT, "tools", "check-wiki-links.py")
    spec = importlib.util.spec_from_file_location("check_wiki_links", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    errors = mod.check_wiki(WIKI_SRC, repo_root=ROOT)
    if errors:
        sys.exit("ERROR: broken wiki links - fix before publishing:\n  "
                 + "\n  ".join(errors))


def ensure_clone(remote):
    """Clone the wiki repo into .wiki-clone, or fetch+reset if already present."""
    os.makedirs(os.path.dirname(CLONE), exist_ok=True)
    if os.path.isdir(os.path.join(CLONE, ".git")):
        git(["remote", "set-url", "origin", remote], cwd=CLONE)
        git(["fetch", "origin"], cwd=CLONE)
        head = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
                   cwd=CLONE, check=False) or "origin/master"
        branch = head.split("/")[-1]
        git(["checkout", branch], cwd=CLONE, check=False)
        git(["reset", "--hard", f"origin/{branch}"], cwd=CLONE)
        git(["clean", "-fd"], cwd=CLONE)  # drop leftovers (e.g. from a prior --dry-run)
        return
    try:
        git(["clone", remote, CLONE], cwd=ROOT)
    except RuntimeError as e:
        if "not found" in str(e).lower():
            sys.exit(
                "ERROR: the wiki Git repo does not exist yet.\n"
                "GitHub creates it only after the FIRST page is saved via the web UI.\n"
                "  1. Open the repo's Wiki tab and create + save any page once.\n"
                "  2. Re-run: python3 tools/sync-wiki.py\n"
                f"(wiki remote: {remote})")
        raise


def _rel_files(base):
    """Files to mirror, as paths relative to `base`: top-level *.md plus everything
    under images/ (the wiki's binary assets, e.g. screenshots)."""
    files = []
    if os.path.isdir(base):
        for f in os.listdir(base):
            if f.endswith(".md") and os.path.isfile(os.path.join(base, f)):
                files.append(f)
        img = os.path.join(base, "images")
        if os.path.isdir(img):
            for f in os.listdir(img):
                if os.path.isfile(os.path.join(img, f)):
                    files.append(os.path.join("images", f))
    return files


def mirror_pages():
    """Make the clone's pages + images/ match docs/wiki exactly. Returns (added,
    updated, removed) relative-path lists. Bytes are compared, so binary assets
    (PNG/JPG/SVG) sync correctly alongside the Markdown."""
    src = _rel_files(WIKI_SRC)
    dst = set(_rel_files(CLONE))
    added, updated, removed = [], [], []
    for rel in src:
        s, d = os.path.join(WIKI_SRC, rel), os.path.join(CLONE, rel)
        with open(s, "rb") as fh:
            new = fh.read()
        if rel not in dst:
            added.append(rel)
        else:
            with open(d, "rb") as fh:
                old = fh.read()
            if old != new:
                updated.append(rel)
            else:
                continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(d, "wb") as fh:
            fh.write(new)
    for rel in sorted(set(dst) - set(src)):
        os.remove(os.path.join(CLONE, rel))
        removed.append(rel)
    return sorted(added), sorted(updated), sorted(removed)


def main():
    ap = argparse.ArgumentParser(description="Publish docs/wiki/ to the GitHub wiki.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change, commit/push nothing")
    ap.add_argument("-m", "--message", default="Sync wiki from docs/wiki",
                    help="commit message")
    ap.add_argument("--remote", default=None, help="override the wiki remote URL")
    a = ap.parse_args()

    if not os.path.isdir(WIKI_SRC):
        sys.exit(f"ERROR: wiki source not found: {WIKI_SRC}")
    if not any(f.endswith(".md") for f in os.listdir(WIKI_SRC)):
        sys.exit(f"ERROR: no Markdown pages under {WIKI_SRC}")

    run_link_check()

    remote = a.remote or wiki_remote_from_origin()
    print(f"Wiki remote: {remote}")
    ensure_clone(remote)

    added, updated, removed = mirror_pages()
    changes = [("added", added), ("updated", updated), ("removed", removed)]
    if not any(lst for _, lst in changes):
        print("Wiki already up to date - nothing to do.")
        return
    for label, lst in changes:
        for f in lst:
            print(f"  {label:8} {f}")

    if a.dry_run:
        print("\n--dry-run: not committing or pushing.")
        return

    git(["add", "-A"], cwd=CLONE)
    git(["commit", "-m", a.message], cwd=CLONE)
    git(["push", "origin", "HEAD"], cwd=CLONE)
    n = len(added) + len(updated) + len(removed)
    print(f"\nPushed {n} page change(s) to the wiki.")


if __name__ == "__main__":
    main()
