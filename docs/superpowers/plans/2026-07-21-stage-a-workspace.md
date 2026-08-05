# Stage A — Workspace, Clip Identity and Editorial Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the data store out of the code repository, key every clip to its source URL instead of its title, and introduce an editorial layer that hand corrections live in — without changing a single byte of rendered output.

**Architecture:** Four new pure-logic modules (`workspace`, `clipid`, `editorial`, `clipstore`) define where data lives, what a clip is called, and how hand corrections are stored and compared. `profile.py` and `bin/yt-shorts` are then rewired onto them, and a `migrate` command copies the existing repository data into the new workspace. Derived data (raw clip, transcript, short) and editorial data (title, corrections, status) never write into each other.

**Tech Stack:** Python 3, standard library only (`hashlib`, `json`, `pathlib`, `shutil`, `os`). Pillow, faster-whisper, ffmpeg and yt-dlp are already present and unchanged by this stage.

## Global Constraints

- `PYTHONPATH=src` is mandatory for every Python invocation. Full suite: `PYTHONPATH=src .venv/bin/pytest -q`. 286 tests pass at the start of this plan; all must still pass.
- **No new dependencies.** Standard library only for everything in this plan.
- **Rendered output must stay byte-identical.** The acceptance check is old code against new code on the *same local raw material*, comparing the ffmpeg filter-chain string and the output SHA-256. Never re-download to compare — yt-dlp may return a differently encoded copy and the bytes will differ for reasons unrelated to the code.
- **`setsar=1` must remain the final step of the filter chain.** Without it, files are 1080x1920 pixels carrying a non-square sample aspect ratio and every player stretches them back to 16:9.
- **Never crop the picture.** No `crop`, no `force_original_aspect_ratio=increase`.
- **One failed clip must never abort a run.** Failures isolate per entry, are recorded with their exception type, reported at the end, exit code 1.
- **Any failure inside the subtitle pipeline degrades to "no subtitles"**, reported on stderr with its exception type. Only a failure of the render itself fails the clip.
- **Derived data is never edited in place; editorial data is never written by a derivation step.** Harvesting, transcribing and rendering never write `edit.json`. Editorial actions never write `clip.json` or `transcript.json`.
- **An untouched clip has no `edit.json` at all.** The file is created by the first editorial action. Its existence means a human touched the clip.
- **On an editorial/derived conflict the editorial version is used**, the clip is reported, and the run does not abort.
- ffmpeg here is built without `libfreetype` and `libass`: no `drawtext`, no `subtitles` filter. Do not reinstall or modify ffmpeg — the separate `racecast` project depends on that exact binary. `<racecast-runtime>/` is read-only.
- English only: code, comments, docstrings, tests, commit messages. Commit messages are imperative mood.
- **Migration copies, never moves, and verifies every copied file by checksum.** The original is left in place for the operator to delete.
- Never write to `channels/erf/events/community-clips-back-catalogue/drafts/` or `raw/` during development or verification. Use a scratch directory.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/yt_shorts/workspace.py` (create) | Resolve where the data lives. Nothing else. |
| `src/yt_shorts/clipid.py` (create) | Canonical URL, stable clip id, directory name. Pure functions. |
| `src/yt_shorts/editorial.py` (create) | `edit.json`: read, write, checksum, conflict detection, effective title/words. |
| `src/yt_shorts/clipstore.py` (create) | The per-clip directory layout: where each file of a clip lives, iterating clips. |
| `src/yt_shorts/migrate.py` (create) | Copy an old-layout event into the workspace, verified. |
| `src/yt_shorts/profile.py` (modify) | Take its channels directory from `workspace` instead of a repo-relative constant. |
| `bin/yt-shorts` (modify) | Report the resolved workspace; harvest, render and gallery through `clipstore` + `editorial`; new `migrate` command. |
| `tests/test_workspace.py`, `tests/test_clipid.py`, `tests/test_editorial.py`, `tests/test_clipstore.py`, `tests/test_migrate.py` (create) | One test file per new module. |

---

## Task 1: Workspace resolution

**Files:**
- Create: `src/yt_shorts/workspace.py`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `workspace.Workspace` — dataclass with `root: Path`, `channels_dir: Path`, `origin: str`
  - `workspace.WorkspaceError` — raised when `YT_SHORTS_DATA` is set to a path that does not exist
  - `workspace.resolve(env: dict | None = None, home: Path | None = None, repo_channels: Path | None = None) -> Workspace`
  - `workspace.DEFAULT_DIR_NAME = "YT-Shorts-Data"`

- [ ] **Step 1: Write the failing test**

`tests/test_workspace.py`:

```python
import pytest

from yt_shorts.workspace import DEFAULT_DIR_NAME, Workspace, WorkspaceError, resolve


class TestResolutionOrder:
    def test_the_environment_variable_wins(self, tmp_path):
        chosen = tmp_path / "chosen"
        (chosen / "channels").mkdir(parents=True)
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={"YT_SHORTS_DATA": str(chosen)}, home=home,
                         repo_channels=repo)

        assert result.root == chosen
        assert result.channels_dir == chosen / "channels"
        assert result.origin == "YT_SHORTS_DATA"

    def test_the_default_directory_is_used_when_it_exists(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert result.root == home / DEFAULT_DIR_NAME
        assert result.origin == "default"

    def test_the_repository_is_the_last_resort(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert result.channels_dir == repo
        assert result.root == repo.parent
        assert result.origin == "repository"


class TestErrors:
    def test_a_set_but_missing_path_is_an_error_not_a_fallback(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)
        missing = tmp_path / "nope"

        with pytest.raises(WorkspaceError) as error:
            resolve(env={"YT_SHORTS_DATA": str(missing)}, home=home,
                    repo_channels=repo)

        assert str(missing) in str(error.value)

    def test_an_empty_environment_variable_is_treated_as_unset(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={"YT_SHORTS_DATA": ""}, home=home, repo_channels=repo)

        assert result.origin == "default"


class TestDescription:
    def test_the_workspace_describes_itself_for_the_startup_line(self, tmp_path):
        home = tmp_path / "home"
        (home / DEFAULT_DIR_NAME / "channels").mkdir(parents=True)
        repo = tmp_path / "repo" / "channels"
        repo.mkdir(parents=True)

        result = resolve(env={}, home=home, repo_channels=repo)

        assert str(result.root) in result.describe()
        assert "default" in result.describe()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.workspace'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/workspace.py`:

```python
"""Where the data lives.

The tool's repository holds code; a workspace holds channels, events, clips
and everything derived from them. Keeping them apart means a workspace can
be backed up as a unit and a checkout can be replaced without touching data.

Resolution deliberately has no flag and no cutover date: creating the default
directory is the entire migration switch. What it must not have is ambiguity,
which is why a set-but-missing YT_SHORTS_DATA is an error rather than a quiet
fall back to a different dataset, and why the caller is expected to report
`describe()` once at startup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR_NAME = "YT-Shorts-Data"
ENV_VAR = "YT_SHORTS_DATA"

ROOT = Path(__file__).resolve().parent.parent.parent
REPO_CHANNELS = ROOT / "channels"


class WorkspaceError(Exception):
    """Understandable error message about an unusable workspace."""


@dataclass(frozen=True)
class Workspace:
    root: Path
    channels_dir: Path
    origin: str

    def describe(self) -> str:
        return f"data: {self.root} ({self.origin})"


def resolve(env: dict | None = None, home: Path | None = None,
            repo_channels: Path | None = None) -> Workspace:
    """Returns the workspace to use, in this order:

    1. ``YT_SHORTS_DATA`` if set to a non-empty value. A path that does not
       exist raises WorkspaceError - falling back silently would mean the
       operator asked for one dataset and quietly got another.
    2. ``~/YT-Shorts-Data`` if it exists.
    3. the repository's own ``channels/`` - the layout that predates this
       module.

    The parameters exist for testing; production callers pass nothing.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    repo_channels = REPO_CHANNELS if repo_channels is None else repo_channels

    named = (env.get(ENV_VAR) or "").strip()
    if named:
        root = Path(named).expanduser()
        if not root.is_dir():
            raise WorkspaceError(
                f"{ENV_VAR} points at {root}, which does not exist.\n"
                f"Create it, or unset {ENV_VAR} to use "
                f"~/{DEFAULT_DIR_NAME} or the repository's channels/."
            )
        return Workspace(root=root, channels_dir=root / "channels",
                         origin=ENV_VAR)

    default_root = home / DEFAULT_DIR_NAME
    if default_root.is_dir():
        return Workspace(root=default_root,
                         channels_dir=default_root / "channels",
                         origin="default")

    return Workspace(root=repo_channels.parent, channels_dir=repo_channels,
                     origin="repository")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_workspace.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 292 passed (286 + 6)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/workspace.py tests/test_workspace.py
git commit -m "Resolve where the data lives, separately from the code"
```

---

## Task 2: Stable clip identity

**Files:**
- Create: `src/yt_shorts/clipid.py`
- Test: `tests/test_clipid.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `clipid.canonical_url(url: str) -> str`
  - `clipid.clip_id(url: str) -> str` — 8 lowercase hex characters
  - `clipid.slug(title: str) -> str` — at most 50 characters, `""` if nothing usable
  - `clipid.directory_name(url: str, title: str) -> str` — `"<slug>--<id>"`, or just `"<id>"` when the title yields no slug
  - `clipid.ID_LENGTH = 8`

- [ ] **Step 1: Write the failing test**

`tests/test_clipid.py`:

```python
from yt_shorts.clipid import ID_LENGTH, canonical_url, clip_id, directory_name, slug

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

    def test_a_different_clip_is_a_different_url(self):
        assert canonical_url(CLIP) != canonical_url(CLIP + "X")


class TestClipId:
    def test_the_id_is_stable_for_the_same_url(self):
        assert clip_id(CLIP) == clip_id(CLIP)

    def test_the_id_has_the_documented_shape(self):
        value = clip_id(CLIP)
        assert len(value) == ID_LENGTH
        assert all(c in "0123456789abcdef" for c in value)

    def test_different_urls_get_different_ids(self):
        assert clip_id(CLIP) != clip_id(CLIP + "X")

    def test_query_variants_share_one_id(self):
        assert clip_id(f"{CLIP}?si=abc") == clip_id(CLIP)


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

    def test_a_title_change_does_not_change_the_directory(self):
        # The whole point: the directory is keyed to the clip, not its label.
        first = directory_name(CLIP, "Speedy!")
        assert first.endswith(clip_id(CLIP))

    def test_an_unusable_title_falls_back_to_the_bare_id(self):
        assert directory_name(CLIP, "!!!") == clip_id(CLIP)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clipid.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.clipid'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/clipid.py`:

```python
"""What a clip is called on disk, and why that name never changes.

A clip's identity is its source URL - that is what the clip *is*. Everything
else about it, the title above all, is an attribute a human may edit at any
time. The previous layout derived every filename from the title, so renaming
a clip orphaned its transcript, its raw download and its rendered short under
the old name, and a collision suffix shifted as soon as the order of the
source list changed.

The directory name therefore pairs a readable slug, frozen once at creation
from the harvested title, with a short hash of the canonical URL. The slug is
a label for humans browsing the workspace; the hash is the identity.
"""

from __future__ import annotations

import hashlib
import re

ID_LENGTH = 8
SLUG_MAX = 50


def canonical_url(url: str) -> str:
    """Strips the parts of a URL that do not change which clip it names.

    A query string (YouTube appends share parameters), a fragment and a
    trailing slash all address the same clip; treating them as different
    would give one clip several identities and several directories.
    """
    cleaned = url.strip()
    for separator in ("#", "?"):
        cleaned = cleaned.split(separator, 1)[0]
    return cleaned.rstrip("/")


def clip_id(url: str) -> str:
    """A short, stable identity for a clip, derived from its URL."""
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()
    return digest[:ID_LENGTH]


def slug(title: str) -> str:
    """A readable, filesystem-safe label. May be empty."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SLUG_MAX].strip("-")


def directory_name(url: str, title: str) -> str:
    """The clip's directory name: '<slug>--<id>', or '<id>' with no slug."""
    label = slug(title)
    identity = clip_id(url)
    return f"{label}--{identity}" if label else identity
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clipid.py -q`
Expected: PASS, 17 tests

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 309 passed (292 + 17)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/clipid.py tests/test_clipid.py
git commit -m "Key a clip to its source URL instead of its title"
```

---

## Task 3: The editorial layer

**Files:**
- Create: `src/yt_shorts/editorial.py`
- Test: `tests/test_editorial.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `editorial.Edit` — dataclass with `title: str | None`, `status: str`, `transcript: dict | None` (the transcript dict has keys `based_on: str` and `words: list[dict]`)
  - `editorial.EditError` — raised for an unreadable or schema-invalid `edit.json`
  - `editorial.CANDIDATE`, `editorial.KEPT`, `editorial.DISCARDED`, `editorial.STATUSES`
  - `editorial.EDIT_FILENAME = "edit.json"`
  - `editorial.checksum(words: list[dict]) -> str` — `"sha256:<hex>"`
  - `editorial.load(clip_dir) -> Edit`
  - `editorial.save(clip_dir, edit: Edit) -> None`
  - `editorial.effective_title(edit: Edit, harvested_title: str) -> str`
  - `editorial.effective_words(edit: Edit, derived_words: list[dict] | None) -> tuple[list[dict], bool]` — returns `(words, conflict)`

- [ ] **Step 1: Write the failing test**

`tests/test_editorial.py`:

```python
import json

import pytest

from yt_shorts.editorial import (
    CANDIDATE, DISCARDED, EDIT_FILENAME, Edit, EditError,
    checksum, effective_title, effective_words, load, save)


def w(start, end, text):
    return {"start": start, "end": end, "text": text}


DERIVED = [w(0.0, 0.5, " very"), w(0.5, 1.0, " very")]
CORRECTED = [w(0.0, 1.0, " Rei Racing")]


class TestAnUntouchedClip:
    def test_no_file_means_defaults(self, tmp_path):
        edit = load(tmp_path)
        assert edit.title is None
        assert edit.status == CANDIDATE
        assert edit.transcript is None

    def test_loading_does_not_create_the_file(self, tmp_path):
        load(tmp_path)
        assert not (tmp_path / EDIT_FILENAME).exists()


class TestSaving:
    def test_saving_writes_only_what_was_set(self, tmp_path):
        save(tmp_path, Edit(title="Abschied von Speedy", status=CANDIDATE,
                            transcript=None))
        payload = json.loads((tmp_path / EDIT_FILENAME).read_text(encoding="utf-8"))
        assert payload == {"title": "Abschied von Speedy", "status": CANDIDATE}

    def test_a_saved_edit_reloads_unchanged(self, tmp_path):
        original = Edit(title="Titel", status=DISCARDED,
                        transcript={"based_on": checksum(DERIVED),
                                    "words": CORRECTED})
        save(tmp_path, original)
        assert load(tmp_path) == original


class TestRejectingBrokenFiles:
    def test_invalid_json_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text("{not json", encoding="utf-8")
        with pytest.raises(EditError):
            load(tmp_path)

    def test_an_unknown_status_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({"status": "maybe"}), encoding="utf-8")
        with pytest.raises(EditError) as error:
            load(tmp_path)
        assert "maybe" in str(error.value)

    def test_a_transcript_without_based_on_is_reported(self, tmp_path):
        (tmp_path / EDIT_FILENAME).write_text(
            json.dumps({"transcript": {"words": CORRECTED}}), encoding="utf-8")
        with pytest.raises(EditError):
            load(tmp_path)


class TestChecksum:
    def test_the_same_words_give_the_same_checksum(self):
        assert checksum(DERIVED) == checksum(list(DERIVED))

    def test_different_words_give_different_checksums(self):
        assert checksum(DERIVED) != checksum(CORRECTED)

    def test_the_checksum_names_its_algorithm(self):
        assert checksum(DERIVED).startswith("sha256:")


class TestEffectiveTitle:
    def test_without_an_override_the_harvested_title_is_used(self):
        assert effective_title(load_default(), "Speedy!") == "Speedy!"

    def test_an_override_wins(self):
        assert effective_title(Edit(title="Neu", status=CANDIDATE,
                                    transcript=None), "Speedy!") == "Neu"


def load_default():
    return Edit(title=None, status=CANDIDATE, transcript=None)


class TestEffectiveWords:
    def test_without_corrections_the_derived_words_are_used(self):
        words, conflict = effective_words(load_default(), DERIVED)
        assert words == DERIVED
        assert conflict is False

    def test_corrections_win_and_do_not_conflict_when_still_current(self):
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        words, conflict = effective_words(edit, DERIVED)
        assert words == CORRECTED
        assert conflict is False

    def test_a_changed_derived_transcript_is_a_conflict_and_corrections_still_win(self):
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        changed = DERIVED + [w(1.0, 1.4, " more")]
        words, conflict = effective_words(edit, changed)
        assert words == CORRECTED     # hand work is never dropped
        assert conflict is True

    def test_corrections_without_any_derived_transcript_are_not_a_conflict(self):
        # Nothing exists to contradict them.
        edit = Edit(title=None, status=CANDIDATE,
                    transcript={"based_on": checksum(DERIVED), "words": CORRECTED})
        words, conflict = effective_words(edit, None)
        assert words == CORRECTED
        assert conflict is False

    def test_no_corrections_and_no_derived_transcript_yields_nothing(self):
        words, conflict = effective_words(load_default(), None)
        assert words == []
        assert conflict is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.editorial'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/editorial.py`:

```python
"""Hand-made data, kept strictly apart from derived data.

Everything else on disk is derived: harvested timecodes, cached transcripts,
rendered shorts. All of it may be deleted and recomputed, which is what makes
caching safe. A corrected transcript is not like that - losing it destroys
work no amount of compute recreates.

So corrections live here, in their own additive layer, and the two never
write into each other: harvesting, transcribing and rendering never touch
edit.json, and editorial actions never touch clip.json or transcript.json.
Rendering is `derived + editorial -> short`.

An untouched clip has NO edit.json. The file is created by the first
editorial action, so its mere existence means a human touched this clip -
which is exactly the question backup and cleanup need answered.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

EDIT_FILENAME = "edit.json"

CANDIDATE = "candidate"
KEPT = "kept"
DISCARDED = "discarded"
STATUSES = (CANDIDATE, KEPT, DISCARDED)


class EditError(Exception):
    """Understandable error message about an unusable edit.json."""


@dataclass
class Edit:
    title: str | None
    status: str
    transcript: dict | None


def checksum(words: list[dict]) -> str:
    """Identifies a word list, so a correction can record what it was made
    against. Serialised with sorted keys and no whitespace so the same words
    always hash the same way regardless of how they were written out."""
    canonical = json.dumps(words, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load(clip_dir: str | Path) -> Edit:
    """Reads a clip's editorial layer. A missing file means 'untouched' and
    is not an error; an unreadable or schema-invalid one is."""
    path = Path(clip_dir) / EDIT_FILENAME
    if not path.exists():
        return Edit(title=None, status=CANDIDATE, transcript=None)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise EditError(f"Editorial file is unreadable: {path}\n{error}") from error

    if not isinstance(payload, dict):
        raise EditError(f"Editorial file must hold an object: {path}")

    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise EditError(f"'title' must be a string or absent: {path}")

    status = payload.get("status", CANDIDATE)
    if status not in STATUSES:
        raise EditError(
            f"Unknown status {status!r} in {path}. "
            f"Expected one of: {', '.join(STATUSES)}"
        )

    transcript = payload.get("transcript")
    if transcript is not None:
        if not isinstance(transcript, dict):
            raise EditError(f"'transcript' must be an object or absent: {path}")
        if not isinstance(transcript.get("based_on"), str):
            raise EditError(f"'transcript.based_on' is missing or not a string: {path}")
        if not isinstance(transcript.get("words"), list):
            raise EditError(f"'transcript.words' is missing or not a list: {path}")

    return Edit(title=title, status=status, transcript=transcript)


def save(clip_dir: str | Path, edit: Edit) -> None:
    """Writes the editorial layer, omitting everything that was not set -
    so the file stays a record of decisions rather than a dump of defaults."""
    payload: dict = {}
    if edit.title is not None:
        payload["title"] = edit.title
    payload["status"] = edit.status
    if edit.transcript is not None:
        payload["transcript"] = edit.transcript

    path = Path(clip_dir) / EDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def effective_title(edit: Edit, harvested_title: str) -> str:
    return edit.title if edit.title is not None else harvested_title


def effective_words(edit: Edit,
                    derived_words: list[dict] | None) -> tuple[list[dict], bool]:
    """Returns (words to use, whether the correction is out of date).

    The editorial version ALWAYS wins - hand work is never dropped
    automatically. When the derived transcript has changed underneath it, the
    caller is told so it can report the clip; it is information, not a
    failure, and must not abort a run. Auto-merging was the third option and
    is the one that silently produces a wrong caption.

    With no derived transcript at all there is nothing to contradict a
    correction, so that is not a conflict.
    """
    if edit.transcript is None:
        return (list(derived_words) if derived_words else [], False)
    if derived_words is None:
        return (edit.transcript["words"], False)
    conflict = edit.transcript["based_on"] != checksum(derived_words)
    return (edit.transcript["words"], conflict)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q`
Expected: PASS, 17 tests

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 326 passed (309 + 17)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/editorial.py tests/test_editorial.py
git commit -m "Store hand corrections in a layer of their own"
```

---

## Task 4: The clip store

**Files:**
- Create: `src/yt_shorts/clipstore.py`
- Test: `tests/test_clipstore.py`

**Interfaces:**
- Consumes: `clipid.directory_name`
- Produces:
  - `clipstore.CLIPS_DIRNAME = "clips"`, `clipstore.CLIP_FILENAME = "clip.json"`
  - `clipstore.ClipStoreError`
  - `clipstore.clips_dir(event_dir) -> Path`
  - `clipstore.clip_dir(event_dir, url: str, harvested_title: str) -> Path`
  - `clipstore.write_clip(event_dir, entry: dict) -> Path` — entry has the `ClipEntry` field names (`url`, `hook`, `source_title`, `start`, `end`, `duration`, `error`); returns the clip directory
  - `clipstore.read_clip(clip_dir) -> dict`
  - `clipstore.iter_clip_dirs(event_dir) -> list[Path]` — sorted by directory name
  - `clipstore.transcript_path(clip_dir) -> Path`, `raw_path`, `short_path`, `subs_track_path`, `subs_work_dir` — all `(clip_dir) -> Path`

- [ ] **Step 1: Write the failing test**

`tests/test_clipstore.py`:

```python
import json

import pytest

from yt_shorts.clipid import clip_id
from yt_shorts.clipstore import (
    ClipStoreError, clip_dir, clips_dir, iter_clip_dirs, raw_path, read_clip,
    short_path, subs_track_path, subs_work_dir, transcript_path, write_clip)

CLIP = "https://www.youtube.com/clip/UgkxSpeedy123"
OTHER = "https://www.youtube.com/clip/UgkxBarbie456"


def entry(url=CLIP, hook="Speedy!", error=None):
    return {"url": url, "hook": hook, "source_title": "ERF Round 3",
            "start": 10.0, "end": 70.0, "duration": 60.0, "error": error}


class TestWritingAClip:
    def test_the_directory_is_named_after_url_and_title(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert directory.name == f"speedy--{clip_id(CLIP)}"
        assert directory.parent == clips_dir(tmp_path)

    def test_the_derived_data_round_trips(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert read_clip(directory) == entry()

    def test_writing_twice_updates_rather_than_duplicates(self, tmp_path):
        write_clip(tmp_path, entry())
        write_clip(tmp_path, entry(hook="Speedy!!"))
        assert len(iter_clip_dirs(tmp_path)) == 1

    def test_a_retitled_clip_keeps_its_original_directory(self, tmp_path):
        first = write_clip(tmp_path, entry(hook="Speedy!"))
        second = write_clip(tmp_path, entry(hook="Abschied von Speedy"))
        assert second == first

    def test_an_entry_without_a_url_is_refused(self, tmp_path):
        broken = entry()
        del broken["url"]
        with pytest.raises(ClipStoreError):
            write_clip(tmp_path, broken)


class TestReading:
    def test_an_unreadable_clip_file_is_reported(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        (directory / "clip.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ClipStoreError):
            read_clip(directory)

    def test_clips_are_listed_in_a_stable_order(self, tmp_path):
        write_clip(tmp_path, entry(url=OTHER, hook="Barbie"))
        write_clip(tmp_path, entry())
        names = [d.name for d in iter_clip_dirs(tmp_path)]
        assert names == sorted(names)

    def test_an_event_without_clips_lists_nothing(self, tmp_path):
        assert iter_clip_dirs(tmp_path) == []


class TestPaths:
    def test_every_file_of_a_clip_lives_in_its_directory(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        for path in (transcript_path(directory), raw_path(directory),
                     short_path(directory), subs_track_path(directory),
                     subs_work_dir(directory)):
            assert path.parent == directory

    def test_the_filenames_are_the_documented_ones(self, tmp_path):
        directory = write_clip(tmp_path, entry())
        assert transcript_path(directory).name == "transcript.json"
        assert raw_path(directory).name == "raw.mp4"
        assert short_path(directory).name == "short.mp4"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clipstore.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.clipstore'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/clipstore.py`:

```python
"""One clip, one directory.

The previous layout scattered a clip across three folders - drafts/x.mp4,
raw/x.raw.mp4, transcripts/x.json - all keyed on a slug derived from the
title. Backing up, deleting or inspecting one clip meant touching three
places and hoping the names still lined up; they stopped lining up as soon
as a title changed or the source list was reordered.

Here everything belonging to a clip sits under one directory named by
clipid.directory_name, so those operations become one operation on one path.
The cost, deliberately accepted: listing "all finished shorts" is no longer
`ls drafts/` and needs the tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from .clipid import clip_id, directory_name

CLIPS_DIRNAME = "clips"
CLIP_FILENAME = "clip.json"


class ClipStoreError(Exception):
    """Understandable error message about the clip store on disk."""


def clips_dir(event_dir: str | Path) -> Path:
    return Path(event_dir) / CLIPS_DIRNAME


def clip_dir(event_dir: str | Path, url: str, harvested_title: str) -> Path:
    return clips_dir(event_dir) / directory_name(url, harvested_title)


def _existing_dir(event_dir: str | Path, url: str) -> Path | None:
    """Finds a clip's directory by its IDENTITY, not by its current name.

    This is the whole point of the id suffix. Looking a clip up by
    recomputing its name from the title would fail for exactly the case the
    layout exists to handle: a retitled clip's recomputed name is new, so the
    lookup misses and a second directory appears, orphaning everything under
    the old one. Verified against that case before this plan was written.
    """
    identity = clip_id(url)
    directory = clips_dir(event_dir)
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.iterdir()):
        if candidate.is_dir() and (candidate.name == identity
                                   or candidate.name.endswith(f"--{identity}")):
            return candidate
    return None


def write_clip(event_dir: str | Path, entry: dict) -> Path:
    """Writes the derived facts about a clip and returns its directory.

    The directory is named from the URL and the title *as harvested*. Calling
    this again with a different title updates the file in place and keeps the
    directory, because the URL - not the label - is the identity.
    """
    url = entry.get("url")
    if not url:
        raise ClipStoreError(f"Clip entry has no url: {entry!r}")

    directory = _existing_dir(event_dir, url) or clip_dir(
        event_dir, url, entry.get("hook", ""))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CLIP_FILENAME).write_text(
        json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return directory


def read_clip(clip_dir_: str | Path) -> dict:
    path = Path(clip_dir_) / CLIP_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ClipStoreError(f"Clip file is unreadable: {path}\n{error}") from error
    if not isinstance(payload, dict):
        raise ClipStoreError(f"Clip file must hold an object: {path}")
    return payload


def iter_clip_dirs(event_dir: str | Path) -> list[Path]:
    """Every clip directory of an event, in a stable order."""
    directory = clips_dir(event_dir)
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir()
                   if p.is_dir() and (p / CLIP_FILENAME).exists()),
                  key=lambda p: p.name)


def transcript_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "transcript.json"


def raw_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "raw.mp4"


def short_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "short.mp4"


def subs_track_path(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "subs.mov"


def subs_work_dir(clip_dir_: str | Path) -> Path:
    return Path(clip_dir_) / "subs"
```

**Note on `write_clip`'s directory lookup:** the second call re-derives the
directory from the *stored* harvested title, so a retitled entry lands on the
existing directory instead of creating a second one. That is the mechanism
behind `test_a_retitled_clip_keeps_its_original_directory`; without it the
slug would follow the new title and orphan everything under the old name.
The first lookup can only hit an existing directory when the title is
unchanged, which is why the stored title is consulted before writing.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_clipstore.py -q`
Expected: PASS, 11 tests

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 337 passed (326 + 11)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/clipstore.py tests/test_clipstore.py
git commit -m "Give every clip a directory of its own"
```

---

## Task 5: Profile resolves through the workspace

**Files:**
- Modify: `src/yt_shorts/profile.py:43-44` (the `ROOT` / `CHANNELS_DIR` constants)
- Test: `tests/test_profile.py` (add one class)

**Interfaces:**
- Consumes: `workspace.resolve`
- Produces: `profile.CHANNELS_DIR` keeps its name and meaning, but is now resolved through `workspace`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_profile.py`:

```python
class TestChannelsComeFromTheWorkspace:
    def test_the_channels_directory_is_the_workspaces(self):
        from yt_shorts import profile, workspace
        assert profile.CHANNELS_DIR == workspace.resolve().channels_dir

    def test_the_repository_fallback_still_finds_the_shipped_channel(self):
        # With no workspace configured, the repo layout must keep working -
        # that is the whole point of the fallback. Once Task 8 has created a
        # real workspace on this machine, resolution no longer lands on the
        # repository, and this assertion would be testing the wrong thing.
        import pytest

        from yt_shorts import profile, workspace
        space = workspace.resolve()
        if space.origin != "repository":
            pytest.skip(f"workspace resolves to {space.origin}, not the repository")
        assert (profile.CHANNELS_DIR / "erf" / "brand.json").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q -k Workspace`
Expected: FAIL — `AssertionError` on the first test, because `CHANNELS_DIR` is still `ROOT / "channels"` rather than the resolved workspace. (If a `~/YT-Shorts-Data` happens to exist on the machine, this fails; if not, the two paths coincide and the test passes vacuously. **Before running, create a temporary `~/YT-Shorts-Data/channels` so the failure is real, then remove it again after step 4.**)

- [ ] **Step 3: Write the implementation**

In `src/yt_shorts/profile.py`, replace lines 43-44:

```python
ROOT = Path(__file__).resolve().parent.parent.parent
CHANNELS_DIR = ROOT / "channels"
```

with:

```python
from .workspace import resolve as _resolve_workspace

ROOT = Path(__file__).resolve().parent.parent.parent
# Resolved once at import: which dataset a process works on must not change
# halfway through a run. Tests override this attribute directly.
CHANNELS_DIR = _resolve_workspace().channels_dir
```

Place the `from .workspace import ...` line with the other relative imports
at the top of the file, next to `from .merge import deep_merge`, not inline.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q`
Expected: PASS (all profile tests, including the two new ones)

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 339 passed (337 + 2)

- [ ] **Step 6: Commit**

```bash
git add src/yt_shorts/profile.py tests/test_profile.py
git commit -m "Take the channels directory from the workspace"
```

---

## Task 6: Harvest writes into the clip store

**Files:**
- Modify: `bin/yt-shorts` — `cmd_harvest` and the `main` block
- Test: `tests/test_cli.py` (add one class)

**Interfaces:**
- Consumes: `clipstore.write_clip`, `clipstore.iter_clip_dirs`, `clipstore.read_clip`, `workspace.resolve`
- Produces: `cmd_harvest(dir_: Path, ytdlp: str = "yt-dlp") -> int` — unchanged signature, new storage

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
class TestHarvestWritesTheClipStore:
    def test_each_clip_gets_its_own_directory(self, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
            {"url": "https://www.youtube.com/clip/BBB", "hook": "Barbie"},
        ]), encoding="utf-8")

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        assert cli.cmd_harvest(tmp_path) == 0

        directories = clipstore.iter_clip_dirs(tmp_path)
        assert len(directories) == 2
        assert {clipstore.read_clip(d)["hook"] for d in directories} == {
            "Speedy!", "Barbie"}

    def test_a_good_entry_is_not_re_queried_on_a_second_run(self, tmp_path, monkeypatch):
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
        ]), encoding="utf-8")

        calls = []

        def fake_harvest(entries, ytdlp="yt-dlp"):
            calls.append(list(entries))
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        cli.cmd_harvest(tmp_path)
        cli.cmd_harvest(tmp_path)
        # harvest() is not called at all when there is nothing to query, so
        # the second run leaves the call list untouched.
        assert len(calls) == 1

    def test_a_hand_edited_title_is_not_overwritten_by_a_second_harvest(
            self, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        import yt_shorts.editorial as editorial
        (tmp_path / "clip_urls.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!"},
        ]), encoding="utf-8")

        def fake_harvest(entries, ytdlp="yt-dlp"):
            for e in entries:
                yield cli.ClipEntry(url=e["url"], hook=e["hook"],
                                    source_title="ERF", start=1.0, end=2.0,
                                    duration=1.0, error=None)

        monkeypatch.setattr(cli, "harvest", fake_harvest)
        cli.cmd_harvest(tmp_path)
        directory = clipstore.iter_clip_dirs(tmp_path)[0]
        editorial.save(directory, editorial.Edit(
            title="Abschied von Speedy", status=editorial.CANDIDATE,
            transcript=None))

        cli.cmd_harvest(tmp_path)

        assert editorial.load(directory).title == "Abschied von Speedy"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q -k ClipStore`
Expected: FAIL — `cmd_harvest` still writes `clips.json`, so `iter_clip_dirs` returns `[]`

- [ ] **Step 3: Write the implementation**

Replace `cmd_harvest` in `bin/yt-shorts` with:

```python
def cmd_harvest(dir_: Path, ytdlp: str = "yt-dlp") -> int:
    """Queries clip_urls.json and writes one directory per clip.

    An entry already resolved without error is NOT queried again, but kept
    as it stands. Reason: a temporary disruption (rate limiting, network
    down) must never replace good timecodes with a failure - the subsequent
    render needs exactly this data, and a second harvest run should only
    ever IMPROVE the state, never make it worse. Only missing or previously
    failed entries are (re-)queried.

    Only clip.json is ever written here. edit.json belongs to the operator
    and is never touched by a derivation step, so a hand-edited title
    survives any number of harvest runs.

    A human can force a re-resolve for one clip by deleting its clip.json.
    """
    inputs = json.loads((dir_ / "clip_urls.json").read_text(encoding="utf-8"))

    existing: dict[str, dict] = {}
    for directory in clipstore.iter_clip_dirs(dir_):
        try:
            stored = clipstore.read_clip(directory)
        except clipstore.ClipStoreError:
            continue
        if stored.get("url") and not stored.get("error"):
            existing[stored["url"]] = stored

    to_harvest = [e for e in inputs if e.get("url") not in existing]
    harvest_iter = iter(harvest(to_harvest, ytdlp=ytdlp) if to_harvest else [])

    entries: list[ClipEntry] = []
    for input_ in inputs:
        stored = existing.get(input_.get("url"))
        if stored is not None:
            entries.append(ClipEntry(
                url=stored["url"], hook=stored["hook"],
                source_title=stored["source_title"], start=stored["start"],
                end=stored["end"], duration=stored["duration"], error=None))
        else:
            entries.append(next(harvest_iter))

    for entry in entries:
        clipstore.write_clip(dir_, asdict(entry))
        print(f"ERROR: {entry.hook}: {entry.error}" if entry.error
              else f"{entry.duration:6.1f}s  {entry.hook}")
    return 1 if any(e.error for e in entries) else 0
```

Update the imports at the top of `bin/yt-shorts`:

```python
from dataclasses import asdict                                # noqa: E402

from yt_shorts import clipstore                               # noqa: E402
from yt_shorts import editorial                               # noqa: E402
from yt_shorts import workspace                               # noqa: E402
```

Note that the harvested title is deliberately **not** refreshed from
`clip_urls.json` on a second run for entries that already exist: the title
that names the directory is frozen at creation, and the operator's own title
lives in `edit.json`. Re-reading it from the source list would fight both.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q`
Expected: PASS. Existing `cmd_harvest` tests that assert on `clips.json` will
fail — **rewrite them to assert on the clip store, do not delete them.** Each
one describes a behaviour that must survive: carry-over of good entries,
error reporting, exit code 1 on failure.

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: 342 passed

- [ ] **Step 6: Commit**

```bash
git add bin/yt-shorts tests/test_cli.py
git commit -m "Harvest into one directory per clip"
```

---

## Task 7: Render and gallery read the clip store and apply the editorial layer

**Files:**
- Modify: `bin/yt-shorts` — `cmd_render`, `cmd_gallery`, `main`
- Test: `tests/test_cli.py` (add one class)

**Interfaces:**
- Consumes: `clipstore.*`, `editorial.*`
- Produces: `cmd_render(dir_, config, footer) -> int` and `cmd_gallery(dir_) -> int` — unchanged signatures

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
class TestRenderUsesTheEditorialLayer:
    def _event(self, tmp_path):
        import yt_shorts.clipstore as clipstore
        clipstore.write_clip(tmp_path, {
            "url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        return clipstore.iter_clip_dirs(tmp_path)[0]

    def test_the_overridden_title_is_used_as_the_hook(self, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        editorial.save(directory, editorial.Edit(
            title="Abschied von Speedy", status=editorial.CANDIDATE,
            transcript=None))

        seen = []

        def stub(source, hook, footer, target, config, work_dir,
                 subtitle_provider=None):
            seen.append(hook)
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 0
        assert seen == ["Abschied von Speedy"]

    def test_a_discarded_clip_is_not_rendered(self, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.DISCARDED, transcript=None))

        def stub(*a, **k):
            raise AssertionError("a discarded clip must not be rendered")

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 0

    def test_the_short_lands_in_the_clips_directory(self, tmp_path, monkeypatch):
        import yt_shorts.clipstore as clipstore
        directory = self._event(tmp_path)

        def stub(source, hook, footer, target, config, work_dir,
                 subtitle_provider=None):
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER")
        assert clipstore.short_path(directory).exists()

    def test_a_broken_editorial_file_fails_only_that_clip(self, tmp_path, monkeypatch):
        import yt_shorts.editorial as editorial
        directory = self._event(tmp_path)
        (directory / editorial.EDIT_FILENAME).write_text("{not json",
                                                         encoding="utf-8")

        def stub(*a, **k):
            raise AssertionError("must not render a clip whose edit is unreadable")

        monkeypatch.setattr(cli, "build_short", stub)
        assert cli.cmd_render(tmp_path, {"subtitles": {}}, "FOOTER") == 1


class TestSubtitleConflictIsReported:
    def test_a_stale_correction_is_used_and_reported(self, tmp_path, monkeypatch, capsys):
        import yt_shorts.clipstore as clipstore
        import yt_shorts.editorial as editorial
        clipstore.write_clip(tmp_path, {
            "url": "https://www.youtube.com/clip/AAA", "hook": "Speedy!",
            "source_title": "ERF", "start": 1.0, "end": 2.0, "duration": 1.0,
            "error": None})
        directory = clipstore.iter_clip_dirs(tmp_path)[0]

        derived = [{"start": 0.0, "end": 0.5, "text": " very"}]
        corrected = [{"start": 0.0, "end": 0.5, "text": " Rei Racing"}]
        editorial.save(directory, editorial.Edit(
            title=None, status=editorial.CANDIDATE,
            transcript={"based_on": editorial.checksum(derived),
                        "words": corrected}))

        changed = derived + [{"start": 0.5, "end": 1.0, "text": " more"}]
        monkeypatch.setattr(cli, "transcribe", lambda *a, **k: changed)

        used = []
        monkeypatch.setattr(cli, "group_words",
                            lambda words, **k: used.append(words) or [])

        def stub(source, hook, footer, target, config, work_dir,
                 subtitle_provider=None):
            if subtitle_provider is not None:
                subtitle_provider(str(work_dir) + "/raw.mp4")
            Path(target).write_bytes(b"x")
            return target

        monkeypatch.setattr(cli, "build_short", stub)
        cli.cmd_render(tmp_path, {"subtitles": {"enabled": True}}, "FOOTER")

        assert used == [corrected]                     # hand work wins
        assert "conflict" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q -k "EditorialLayer or Conflict"`
Expected: FAIL — `cmd_render` still reads `clips.json` and writes into `drafts/`

- [ ] **Step 3: Write the implementation**

Replace the body of `cmd_render` in `bin/yt-shorts`. The structure below keeps
every existing guarantee; only the sources of the hook, the paths and the
transcript words change.

```python
def cmd_render(dir_: Path, config: dict, footer: str) -> int:
    lock = EventLock(dir_)
    lock.acquire()
    try:
        failed = []
        for directory in clipstore.iter_clip_dirs(dir_):
            hook_display = directory.name
            # Every candidate is handled ENTIRELY within this try block. A
            # broken entry must only fail THIS one candidate, never the whole
            # run - same approach as harvest.harvest() for the same problem.
            try:
                clip = clipstore.read_clip(directory)
                if clip.get("error"):
                    failed.append((hook_display, clip["error"]))
                    continue
                edit = editorial.load(directory)
                if edit.status == editorial.DISCARDED:
                    print(f"skipped (discarded): {directory.name}")
                    continue

                hook = editorial.effective_title(edit, clip["hook"])
                hook_display = hook
                target = clipstore.short_path(directory)

                provider = None
                if config.get("subtitles", {}).get("enabled"):
                    def provider(raw_path: str, _dir: Path = directory,
                                 _edit=edit, _clip=clip, _name: str = hook):
                        """Turns the downloaded raw clip into a subtitle track.

                        Returns None whenever there is nothing to show. A clip
                        without speech is a normal outcome, not a failure, and
                        must not cost the short its render - and neither may
                        any OTHER failure in this pipeline. Subtitles are the
                        optional layer, so any exception raised past this
                        point degrades to "no subtitles", reported with its
                        exception type. KeyboardInterrupt and SystemExit are
                        deliberately not caught.
                        """
                        subtitles = config.get("subtitles", {})
                        try:
                            derived = None
                            try:
                                derived = transcribe(
                                    raw_path,
                                    str(clipstore.transcript_path(_dir)),
                                    source=_clip["url"], display_name=_name)
                            except Exception:
                                # A correction may still carry the clip; only
                                # re-raise when there is nothing to fall back
                                # on.
                                if _edit.transcript is None:
                                    raise
                            words, conflict = editorial.effective_words(
                                _edit, derived)
                            if conflict:
                                print(f"NOTE: {_name}: subtitle conflict - the "
                                      f"correction was made against an older "
                                      f"transcript; using the correction",
                                      file=sys.stderr)
                            groups = group_words(
                                words,
                                max_words=subtitles.get("max_words",
                                                        DEFAULT_MAX_WORDS),
                                max_seconds=subtitles.get("max_seconds",
                                                          DEFAULT_MAX_SECONDS),
                            )
                            if not groups:
                                print(f"NOTE: {_name}: no speech detected, "
                                      f"no subtitles", file=sys.stderr)
                                return None
                            work_dir = clipstore.subs_work_dir(_dir)
                            track = build_track(
                                groups, config,
                                str(clipstore.subs_track_path(_dir)),
                                str(work_dir))
                            try:
                                work_dir.rmdir()
                            except OSError:
                                pass
                            return track
                        except (KeyboardInterrupt, SystemExit):
                            raise
                        except Exception as error:
                            print(f"NOTE: {_name}: no subtitles "
                                  f"({type(error).__name__}: {error})",
                                  file=sys.stderr)
                            return None

                build_short(Source(clip_url=clip["url"]), hook, footer,
                            str(target), config, str(directory),
                            subtitle_provider=provider)
                print("done:", directory.name)
            except Exception as error:
                failed.append((hook_display, f"{type(error).__name__}: {error}"))
                print("ERROR:", hook_display, file=sys.stderr)
        if failed:
            print(f"\n{len(failed)} candidate(s) failed:", file=sys.stderr)
            for hook, reason in failed:
                print(f"  {hook}: {reason.splitlines()[0]}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        lock.release()
```

Replace `cmd_gallery`:

```python
def cmd_gallery(dir_: Path) -> int:
    entries = []
    for directory in clipstore.iter_clip_dirs(dir_):
        short = clipstore.short_path(directory)
        if not short.exists():
            continue
        try:
            clip = clipstore.read_clip(directory)
        except clipstore.ClipStoreError:
            continue
        edit = editorial.load(directory)
        if edit.status == editorial.DISCARDED:
            continue
        entries.append({
            "file": f"{clipstore.CLIPS_DIRNAME}/{directory.name}/{short.name}",
            "hook": editorial.effective_title(edit, clip.get("hook", directory.name)),
        })
    target = dir_ / "index.html"
    target.write_text(build_page(entries, dir_.name), encoding="utf-8")
    print("written:", target)
    return 0
```

In the `main` block, report the resolved workspace once, before anything else
runs, and add `migrate` to the command set (its implementation arrives in
Task 8):

```python
COMMANDS = {"harvest", "render", "gallery", "migrate"}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)

    command, identifier = sys.argv[1], sys.argv[2]
    try:
        space = workspace.resolve()
    except workspace.WorkspaceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    print(space.describe(), file=sys.stderr)
```

Keep the existing `profile_load` block that follows it unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_cli.py -q`
Expected: PASS. Existing render tests referring to `drafts/` and `clips.json`
must be rewritten to the clip store — **rewrite, do not delete**: each covers
a guarantee (failure isolation, exit code 1, the lock, name collisions) that
must survive. The collision test is the one exception: unique naming by
counter no longer exists, because two clips can no longer collide. Replace it
with a test that two clips sharing a title get separate directories.

- [ ] **Step 5: Prove the rendered bytes did not change**

This is the acceptance criterion of the whole stage. Do it with a scratch
worktree and no network:

Use the commit this plan started from — `cabf827` ("Enable subtitles for the
ERF channel") — not a `HEAD~N` offset, which shifts with every commit made
along the way and would silently compare against the wrong tree.

```bash
cd <repo>
git worktree add /tmp/stage-a-before cabf827
PYTHONPATH=src .venv/bin/python - <<'PY'
import subprocess, hashlib, sys
from pathlib import Path
scratch = Path("/private/tmp/claude-501/-Users-jegr/e9e10c75-78bd-42da-8e0a-f7b1c6c2395d/scratchpad/stage-a")
scratch.mkdir(parents=True, exist_ok=True)
raw = scratch / "src.mp4"
subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",
                "testsrc=size=1280x720:rate=30:duration=2","-f","lavfi","-i",
                "sine=frequency=440:duration=2","-c:v","libx264","-pix_fmt",
                "yuv420p","-c:a","aac","-shortest",str(raw)], check=True)
print("raw ready:", raw)
PY
```

Then, for each of the two trees (`/tmp/stage-a-before` and the working tree),
run `overlay.build_overlay` + `render.compose` on that same `src.mp4` with
subtitles off, writing to two different output paths, and compare
`shasum -a 256` of the two outputs **and** the `-filter_complex` strings.
They must be identical. Remove the worktree afterwards:

```bash
git worktree remove /tmp/stage-a-before
```

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: all tests pass, no collection errors

- [ ] **Step 7: Commit**

```bash
git add bin/yt-shorts tests/test_cli.py
git commit -m "Render and list clips through the store and the editorial layer"
```

---

## Task 8: The migrate command

**Files:**
- Create: `src/yt_shorts/migrate.py`
- Modify: `bin/yt-shorts` — add `cmd_migrate`
- Test: `tests/test_migrate.py`

**Interfaces:**
- Consumes: `clipstore.write_clip`, `clipstore.transcript_path`, `clipstore.short_path`, `workspace.resolve`
- Produces:
  - `migrate.MigrationError`
  - `migrate.Report` — dataclass with `copied: list[str]`, `clips: int`, `unmapped: list[str]`
  - `migrate.migrate_event(old_event_dir, new_event_dir) -> Report`

- [ ] **Step 1: Write the failing test**

`tests/test_migrate.py`:

```python
import hashlib
import json

import pytest

from yt_shorts import clipstore
from yt_shorts.migrate import MigrationError, migrate_event

CLIP_A = "https://www.youtube.com/clip/AAA"
CLIP_B = "https://www.youtube.com/clip/BBB"


def old_event(tmp_path):
    old = tmp_path / "old"
    (old / "drafts").mkdir(parents=True)
    (old / "transcripts").mkdir()
    (old / "clip_urls.json").write_text(json.dumps(
        [{"url": CLIP_A, "hook": "Speedy!"}]), encoding="utf-8")
    (old / "clips.json").write_text(json.dumps([
        {"url": CLIP_A, "hook": "Speedy!", "source_title": "ERF",
         "start": 1.0, "end": 2.0, "duration": 1.0, "error": None},
        {"url": CLIP_B, "hook": "Barbie", "source_title": "ERF",
         "start": 3.0, "end": 4.0, "duration": 1.0, "error": None},
    ]), encoding="utf-8")
    (old / "drafts" / "speedy.mp4").write_bytes(b"speedy-short")
    (old / "drafts" / "barbie.mp4").write_bytes(b"barbie-short")
    (old / "transcripts" / "speedy.json").write_text(json.dumps(
        {"source": CLIP_A, "words": [{"start": 0.0, "end": 0.5, "text": " hi"}]}),
        encoding="utf-8")
    return old


class TestMigration:
    def test_every_clip_gets_a_directory(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        report = migrate_event(old, new)
        assert report.clips == 2
        assert len(clipstore.iter_clip_dirs(new)) == 2

    def test_a_transcript_is_mapped_by_its_recorded_source(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        directory = clipstore.clip_dir(new, CLIP_A, "Speedy!")
        payload = json.loads(
            clipstore.transcript_path(directory).read_text(encoding="utf-8"))
        assert payload["source"] == CLIP_A

    def test_a_draft_is_copied_byte_for_byte(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        directory = clipstore.clip_dir(new, CLIP_A, "Speedy!")
        assert clipstore.short_path(directory).read_bytes() == b"speedy-short"

    def test_the_source_list_is_carried_over(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        migrate_event(old, new)
        assert (new / "sources.json").exists()

    def test_the_original_is_left_untouched(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        before = {p.name: p.read_bytes() for p in (old / "drafts").iterdir()}
        migrate_event(old, new)
        after = {p.name: p.read_bytes() for p in (old / "drafts").iterdir()}
        assert before == after

    def test_an_unmappable_transcript_is_reported_not_dropped_silently(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        (old / "transcripts" / "ghost.json").write_text(json.dumps(
            {"source": "https://www.youtube.com/clip/ZZZ", "words": []}),
            encoding="utf-8")
        report = migrate_event(old, new)
        assert any("ghost.json" in item for item in report.unmapped)

    def test_a_corrupted_copy_is_detected(self, tmp_path, monkeypatch):
        old, new = old_event(tmp_path), tmp_path / "new"
        import yt_shorts.migrate as migrate_module

        real = migrate_module._digest
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return "wrong" if calls["n"] % 2 == 0 else real(path)

        monkeypatch.setattr(migrate_module, "_digest", flaky)
        with pytest.raises(MigrationError):
            migrate_event(old, new)

    def test_migrating_onto_itself_is_refused(self, tmp_path):
        old = old_event(tmp_path)
        with pytest.raises(MigrationError):
            migrate_event(old, old)

    def test_the_events_own_overrides_are_carried_over(self, tmp_path):
        old, new = old_event(tmp_path), tmp_path / "new"
        (old / "brand.json").write_text(json.dumps(
            {"colors": {"accent": "#FF3355"}}), encoding="utf-8")
        (old / "layout.py").write_text("def decorate(*a, **k): pass\n",
                                       encoding="utf-8")
        (old / "assets").mkdir()
        (old / "assets" / "logo.png").write_bytes(b"png-bytes")

        migrate_event(old, new)

        assert json.loads((new / "brand.json").read_text(encoding="utf-8")) == {
            "colors": {"accent": "#FF3355"}}
        assert (new / "layout.py").exists()
        assert (new / "assets" / "logo.png").read_bytes() == b"png-bytes"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_migrate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'yt_shorts.migrate'`

- [ ] **Step 3: Write the implementation**

`src/yt_shorts/migrate.py`:

```python
"""Copy an old-layout event into the workspace, verified.

This copies and never moves, and verifies every copied file by checksum
before reporting success. The original is left in place for the operator to
delete once satisfied.

That is not caution for its own sake: four reference shorts were destroyed
during development by a run that wrote into the directory holding them, and
they were unrecoverable because nothing had verified a copy first. A
migration that deletes before verifying is the same mistake wearing a
different hat.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import clipstore


class MigrationError(Exception):
    """Understandable error message about a migration that cannot be trusted."""


@dataclass
class Report:
    clips: int = 0
    copied: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_verified(source: Path, target: Path, report: Report) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if _digest(source) != _digest(target):
        raise MigrationError(
            f"Copy does not match its source: {source} -> {target}. "
            f"Nothing was deleted; the original is untouched."
        )
    report.copied.append(str(target))


def migrate_event(old_event_dir: str | Path,
                  new_event_dir: str | Path) -> Report:
    """Copies one event from the repository layout into the workspace."""
    old = Path(old_event_dir).resolve()
    new = Path(new_event_dir).resolve()
    if old == new:
        raise MigrationError(f"Source and target are the same directory: {old}")

    clips_file = old / "clips.json"
    if not clips_file.exists():
        raise MigrationError(f"No clips.json in {old} - nothing to migrate.")

    try:
        clips = json.loads(clips_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise MigrationError(f"clips.json is unreadable: {clips_file}\n{error}") from error

    report = Report()
    new.mkdir(parents=True, exist_ok=True)

    sources = old / "clip_urls.json"
    if sources.exists():
        _copy_verified(sources, new / "sources.json", report)

    # The event's OWN overrides. An event may carry its own brand.json,
    # fonts, assets and layout.py; a migration that silently dropped them
    # would destroy configuration the tool documents as supported, and the
    # loss would only surface as a differently branded short much later.
    for name in ("brand.json", "layout.py"):
        source = old / name
        if source.exists():
            _copy_verified(source, new / name, report)
    for name in ("fonts", "assets"):
        source = old / name
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    _copy_verified(item, new / name / item.relative_to(source),
                                   report)

    by_url: dict[str, Path] = {}
    for entry in clips:
        if not isinstance(entry, dict) or not entry.get("url"):
            report.unmapped.append(f"clips.json entry without url: {entry!r}")
            continue
        directory = clipstore.write_clip(new, entry)
        by_url[entry["url"]] = directory
        report.clips += 1

        old_draft = old / "drafts" / f"{_slug_of(entry)}.mp4"
        if old_draft.exists():
            _copy_verified(old_draft, clipstore.short_path(directory), report)

    for transcript in sorted((old / "transcripts").glob("*.json")):
        try:
            payload = json.loads(transcript.read_text(encoding="utf-8"))
            source_url = payload.get("source")
        except (json.JSONDecodeError, OSError):
            source_url = None
        directory = by_url.get(source_url) if source_url else None
        if directory is None:
            report.unmapped.append(
                f"{transcript.name}: no clip with source {source_url!r}")
            continue
        _copy_verified(transcript, clipstore.transcript_path(directory), report)

    return report


def _slug_of(entry: dict) -> str:
    """The filename the old layout would have given this clip's draft."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", entry.get("hook", "").lower()).strip("-")[:50]
```

**On the draft mapping:** the old layout named drafts from the hook and
appended `-2` on collision, so a clip whose draft carried a collision suffix
will not be found by `_slug_of` and simply gets no `short.mp4`. That is
acceptable — a short is derived data and re-renders — and it is why the
report lists what was copied rather than claiming completeness.

Add to `bin/yt-shorts`:

```python
def cmd_migrate(identifier: str) -> int:
    """Copies an event from the repository layout into the workspace."""
    space = workspace.resolve()
    if space.origin == "repository":
        print("ERROR: no workspace configured - nothing to migrate into.\n"
              f"Create ~/{workspace.DEFAULT_DIR_NAME} or set "
              f"{workspace.ENV_VAR}, then run migrate again.", file=sys.stderr)
        return 2

    channel_name, event_name = identifier.split("/", 1)
    old = workspace.REPO_CHANNELS / channel_name / "events" / event_name
    new = space.channels_dir / channel_name / "events" / event_name

    for name in ("channel.json", "brand.json"):
        source = workspace.REPO_CHANNELS / channel_name / name
        target = space.channels_dir / channel_name / name
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            print("copied:", target)

    for name in ("fonts", "assets"):
        source = workspace.REPO_CHANNELS / channel_name / name
        target = space.channels_dir / channel_name / name
        if source.is_dir() and not target.exists():
            shutil.copytree(source, target)
            print("copied:", target)

    layout = workspace.REPO_CHANNELS / channel_name / "layout.py"
    layout_target = space.channels_dir / channel_name / "layout.py"
    if layout.exists() and not layout_target.exists():
        shutil.copy2(layout, layout_target)
        print("copied:", layout_target)

    try:
        report = migrate_event(old, new)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"migrated {report.clips} clip(s), {len(report.copied)} file(s) copied")
    for item in report.unmapped:
        print("NOTE: not mapped:", item, file=sys.stderr)
    print(f"\nThe original under {old} was NOT deleted. "
          f"Remove it yourself once you are satisfied.")
    return 0
```

with these imports added at the top of `bin/yt-shorts`:

```python
import shutil                                                 # noqa: E402

from yt_shorts.migrate import MigrationError, migrate_event   # noqa: E402
```

and this branch in `main`, before the `profile_load` call — migration must run
without a loadable profile in the workspace, because the profile is part of
what it copies:

```python
    if command == "migrate":
        raise SystemExit(cmd_migrate(identifier))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_migrate.py -q`
Expected: PASS, 9 tests

- [ ] **Step 5: Migrate the real event and verify it**

```bash
mkdir -p ~/YT-Shorts-Data/channels
bin/yt-shorts migrate erf/community-clips-back-catalogue
```

Then confirm the copy is faithful:

```bash
ls ~/YT-Shorts-Data/channels/erf/events/community-clips-back-catalogue/clips/
shasum -a 256 channels/erf/events/community-clips-back-catalogue/drafts/*.mp4
shasum -a 256 ~/YT-Shorts-Data/channels/erf/events/community-clips-back-catalogue/clips/*/short.mp4
```

The two checksum lists must match as sets. The repository copy stays where it
is.

- [ ] **Step 6: Run the full suite**

Run: `PYTHONPATH=src .venv/bin/pytest -q`
Expected: all pass. Note that with `~/YT-Shorts-Data` now existing, the
workspace resolves to it — confirm the suite still passes, since tests that
need the shipped ERF profile must not depend on the resolution outcome.

- [ ] **Step 7: Update the documentation**

In `README.md`, replace the Layout section's tree with the workspace layout
from the design document, and add a short "Where the data lives" section
naming the three resolution steps and the `migrate` command.

In `CLAUDE.md`, add to the Architecture section: two or three sentences on
`workspace`/`clipid`/`clipstore`/`editorial`, and the rule that derived data
is never edited in place while editorial data is never written by a
derivation step.

- [ ] **Step 8: Commit**

```bash
git add src/yt_shorts/migrate.py bin/yt-shorts tests/test_migrate.py README.md CLAUDE.md
git commit -m "Copy an event into the workspace, verified file by file"
```

---

## Self-review notes

Checked against the spec:

- Workspace resolution incl. the error case — Task 1
- Clip identity from the URL, slug frozen at creation — Task 2
- Editorial file, its absence meaning untouched, conflict detection, status — Task 3
- One clip, one directory — Task 4
- Repository fallback still working — Task 5 (test) and Task 1 (resolution step 3)
- Harvest/render/gallery on the new layout, editorial title and words applied — Tasks 6 and 7
- Byte-identical output — Task 7, step 5
- Migration copying, verifying, reporting, leaving the original — Task 8
- Startup line naming the resolved workspace, which is the mitigation for the
  known risk of two runs on differently resolved paths — Task 7, step 3

Not covered here, by design: the glossary and Whisper prompt biasing (stage B),
the application (stage C), source listing through the YouTube API (stage D),
upload (stage E).
