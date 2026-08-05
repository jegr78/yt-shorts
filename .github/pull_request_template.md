## What changed and why

<!-- Briefly describe the change and the problem it solves. -->

## How this was verified

<!-- The suite (`PYTHONPATH=src .venv/bin/pytest -q`), `python3 tools/lint.py`,
     and anything else run by hand (a rendered short, a manual studio check,
     `npm run build` / `npm test` for a frontend change). -->

---

**PR title must be a [Conventional Commit](https://www.conventionalcommits.org/)**
(`fix:`, `feat:`, `docs:`, ...) — PRs are squash-merged, and the title becomes
the squash commit's subject, which release-please parses to decide the next
version and changelog entry.
