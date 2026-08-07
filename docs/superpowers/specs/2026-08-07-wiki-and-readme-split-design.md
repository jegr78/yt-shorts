# Block C — the wiki and the README split

Date: 2026-08-07
Status: approved (design), ready for implementation plan

Block C of three. Block A (packaging and binaries) and Block B (CI, release and
public readiness) are merged. This block gives the project a reader-facing
manual, moves the bulk of the README into it, and makes both checkable.

`<racecast-repo>` (racecast) is the reference again, and this time it is close
to transplantable: its wiki mechanism is four files and one workflow, and the
deviations below are named where they occur.

## Motivation

`README.md` is **1573 lines / 87.7 kB**. It is the first thing a stranger sees
on a public repository and the last thing they will read. The reference's is
192 lines, and everything else lives in its GitHub wiki, generated from
`src/docs/wiki/` in the repo.

Nothing about this is cosmetic. The README currently carries four sections that
are reference material rather than an introduction — `Model providers` (370
lines), `Studio` (272), `Subtitles` (115), `Where the data lives` (106) — and
three of them describe the same subjects as `CLAUDE.md` and the skills, which
is where this design's hardest constraint comes from.

## What was measured, not assumed

| Question | Answer | Consequence |
|---|---|---|
| How big is the README, section by section? | 17 sections, 1573 lines; the largest four are 370, 272, 115 and 106 | The split is worth doing and the boundary has to be a reader decision, not a size one |
| How does the reference publish its wiki? | `src/docs/wiki/` → `tools/sync-wiki.py` → `<origin>.wiki.git`; `tools/check-wiki-links.py` gates it; `.github/workflows/wiki.yml` is dispatch-only | The mechanism is portable as is; only paths change |
| What does the reference's link checker cover? | Intra-wiki pages and anchors. It **ignores** any target containing `/` and any scheme | A wiki→repo link is exactly a target with `/` or a scheme, so the class this design creates is the class that checker cannot see |
| Can a wiki page link to a repo file relatively? | **No.** A GitHub wiki is a separate git repository; `../../CLAUDE.md` resolves to nothing | Repo references must be full `https://github.com/<owner>/<repo>/blob/<ref>/<path>` URLs — which a scheme-skipping checker silently passes |
| Who links into the README today? | `CONTRIBUTING.md` → `README.md#installing` and `#requirements` | Both sections stay in the README; the split must not move them |

## Decided requirements

- **The README becomes a shop window, ~180 lines.** It keeps what the tool is,
  `Installing`, `Requirements`, `Workflow after a race weekend`, `What the tool
  guarantees`, and a pointer to the wiki. Everything else moves.
- **The wiki belongs to the operator.** It answers *how do I use this*.
  `CLAUDE.md` and the skills belong to the contributor and to Claude, and
  answer *why is it built this way and what must not break*. No rule is
  restated in the wiki: where a rule affects the operator, the wiki states its
  **effect** and links to the source.
- **The link checker covers intra-wiki links, repo references and image
  targets.** Repo references are recognised by URL shape, not skipped as
  schemes. No network is touched.
- **Images are generated, not pasted.** `tools/build-wiki-images.py` produces
  them from what the suite already has.
- **Sync is dispatch-only.** Pushing to a public wiki is a deliberate
  maintainer action, never a side effect of a merge.

## Components

| File | Responsibility |
|---|---|
| `docs/wiki/*.md` | The pages, one per file, GitHub wiki naming (`Model-providers.md` → "Model providers"). Sibling of the existing `docs/superpowers/`. |
| `docs/wiki/_Sidebar.md`, `docs/wiki/Home.md` | Navigation and landing page |
| `docs/wiki/images/*.png` | Generated images, checked in |
| `tools/check-wiki-links.py` | The checker. Pure stdlib, no project imports, exit 1 on breakage |
| `tools/sync-wiki.py` | Mirrors `docs/wiki/` into `<origin>.wiki.git`. Runs the checker first and refuses to push if it fails |
| `tools/build-wiki-images.py` | Regenerates `docs/wiki/images/` |
| `tests/test_wiki.py` | Runs the checker over the real wiki, plus unit tests of the checker itself |
| `.github/workflows/wiki.yml` | `workflow_dispatch` only, `contents: write`, built-in `GITHUB_TOKEN` |

The wiki clone lives in `.wiki-clone/` at the repo root, gitignored. The
reference uses `runtime/wiki/`; this repository has no `runtime/`.

## The page set

README keeps its opening, `Installing`, `Requirements`, `Workflow after a race
weekend` and `What the tool guarantees`, and gains a short pointer to the wiki.
These move, one page each:

| README section | Wiki page |
|---|---|
| `Layout` | Layout |
| `Where the data lives` | Where-the-data-lives |
| `The editorial layer (edit.json)` | The-editorial-layer |
| `Studio` | Studio |
| `Setting up a new channel` | Setting-up-a-new-channel |
| `Subtitles` | Subtitles |
| `Whole-stream transcription` | Whole-stream-transcription |
| `Moment detection` | Moment-detection |
| `Model providers` | Model-providers |
| `Upload` | Upload |
| `Development` | folded into Building-from-source, minus what `CONTRIBUTING.md` already says |
| `Not built yet (later)` | folded into Home |

Two pages exist nowhere today and are written for this block:

- **Building-from-source** — the wheel, the binary, and what each needs. Takes
  over the README's `Development` section where `CONTRIBUTING.md` does not
  already cover it.
- **If-something-goes-wrong** — the operator's failure catalogue. Block B's
  design explicitly deferred one item here: the macOS binaries are unsigned and
  stay quarantined, so the `xattr` workaround is documented in this block and
  nowhere else.

## The boundary that has to hold

This project's own record is that restating a rule is how it becomes false —
`CLAUDE.md` says so about its own subsystem chapters, which were moved to
skills *word for word* rather than rewritten. The wiki now creates a third
place for the same subjects, and the rule that keeps it honest is a difference
in **level**, not in wording:

> The wiki says what the operator does and what happens. The skill says why,
> what was measured, and what must not change.

Worked example, `Model providers`. The wiki page carries: which providers
exist, what goes in the settings, what a key costs, where the key is stored,
what the operator sees when a scan fails. It does **not** carry the reasoning
behind "one engine per run" — that stays in the `detection-and-providers`
skill, and the wiki page links to it. Where the rule reaches the operator (a
window that fails is recorded in `missing_windows` rather than silently
falling back), the wiki states that **outcome**, because the operator sees it.

## The link checker

Three link classes, all resolved offline:

1. **Intra-wiki** — `[text](Page)`, `[text](Page#anchor)`, `[text](#anchor)`.
   The page must exist in `docs/wiki/`; the anchor must exist in that page,
   computed with GitHub's anchor algorithm.
2. **Repo references** — `https://github.com/<owner>/<repo>/blob/<ref>/<path>`
   and `#anchor`, where `<owner>/<repo>` is this repository. The path must
   exist in the working tree, and for a Markdown target the anchor must exist
   in it. Any other host, and any other path shape, is a foreign URL and is
   ignored.
3. **Image targets** — `![alt](images/x.png)`. The file must exist. The
   reference ignores image embeds entirely; with images in scope, a renamed
   file would otherwise be exactly the silent breakage the checker exists to
   catch.

`mailto:` and foreign `https:` targets are ignored. Nothing resolves over the
network — the suite must stay offline.

Exit 1 on breakage, listing every offender with page and line. Enforced twice:
`tests/test_wiki.py` in the suite (so CI fails), and `tools/sync-wiki.py`
before it pushes (so a local publish cannot bypass CI).

## The images

`tools/build-wiki-images.py` regenerates `docs/wiki/images/` from sources the
repository already owns. No third-party pixels and no real channel data reach a
public wiki:

- **The overlay** — `overlay.build_overlay` with the `erf` test fixture, which
  is pure Pillow output and this project's own artwork.
- **The frame geometry** — the real filter chain over a *synthetic* 16:9 source
  generated by ffmpeg's `testsrc`, the same generator `tests/test_transcribe.py`
  already uses. It shows the blurred background, the window and the overlay
  without borrowing a single frame of somebody's race stream. The extraction
  applies the sample aspect ratio (`scale=iw*sar:ih`), because an extracted
  frame otherwise lies about what a player shows — `CLAUDE.md`'s "Verifying
  changes" says so.
- **The studio** — Playwright against a studio serving a temporary workspace
  seeded from the same fixture, so the screenshots show the real UI with
  invented content.

The script must not run while a test suite is in flight, for the reason
`CLAUDE.md` already records: a frontend build deletes `static/` before
rewriting it.

## Bootstrap and hand-off

GitHub creates `<repo>.wiki.git` only after the **first page is saved through
the repository's Wiki tab**. No workflow and no script can do this; until it
happens every sync fails with "repository not found". The wiki is already
enabled on the repository (Block B set `--enable-wiki`).

Hand-off items for the operator:

1. Save one page through the Wiki tab, once, to create the wiki repository.
2. Run the sync (or dispatch the workflow) and confirm the result reads the way
   it should.

## Testing

- `tests/test_wiki.py` runs the checker over `docs/wiki/` and must report
  nothing.
- The checker gets its own unit tests: a broken page link, a broken intra-wiki
  anchor, a broken repo path, a broken repo anchor, a missing image, and a
  valid document that must stay clean. Each is proven by mutation — a checker
  that cannot fail is the failure mode this project has recorded three times.
- `CONTRIBUTING.md`'s two links into the README are verified to still resolve
  after the split.

## Out of scope

- **GitHub Pages.** The wiki is the manual; there is no site.
- **Translating the wiki.** The project is English.
- **Any change to rendering, overlay, detection or upload behaviour.** This
  block moves and writes prose, and adds three tools and one workflow.
- **Automatic sync on merge.** Deliberate: see the dispatch-only requirement.
