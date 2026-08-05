# Upload Metadata & Publishing Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator set an upload's description, tags, category and made-for-kids (channel default + per-clip override) and choose its visibility (private/unlisted/public) and an optional scheduled publish time — all from the studio, with a default-private guardrail.

**Architecture:** `editorial.Edit` gains a per-clip `upload` override; `youtube_upload.build_metadata` reads the effective metadata and takes `visibility`/`publish_at` (defaulting to private) with YouTube-limit guardrails; `profile._validate_upload` validates the channel-default metadata fields; the studio's `post_upload` route accepts a visibility/schedule/confirm body, threads it through the upload job, and records the *actual* resulting privacy. The frontend adds an editable metadata area and a visibility/schedule picker with a confirm gate to the UploadPanel, plus an upload-defaults card in the channel Brand editor.

**Tech Stack:** Python 3 / FastAPI (backend), React + Mantine + Vite + TypeScript (frontend), pytest + FastAPI TestClient + Playwright-in-pytest (backend/E2E), Vitest (frontend units).

## Global Constraints

- `PYTHONPATH=src` mandatory for every Python invocation; tests: `PYTHONPATH=src .venv/bin/pytest -q`.
- **Default privacy is `private`.** A non-private (`unlisted`/`public`) or scheduled (`publishAt`) upload happens ONLY when the operator explicitly selects AND confirms it, per upload — enforced on BOTH the server (`post_upload` refuses without `confirm`) and the client (UI checkbox gates the button). No auto-publish on any derived signal.
- Scheduling maps to `privacyStatus=private` + `status.publishAt` (YouTube publishes to **public** at that time); `publish_at` combined with a non-private `visibility` is a caller error (`build_metadata` rejects it).
- YouTube limits enforced in `build_metadata` (clear `UploadError`, not a raw API 400): title ≤ 100 chars, description ≤ 5000, combined tags ≤ 500 chars, each tag a non-empty string, `publish_at` valid RFC3339, `category_id` numeric, `visibility` ∈ {private,unlisted,public}.
- `upload_policy` unchanged: a `manual` channel never API-uploads; all of this applies to `api` channels only.
- Effective metadata = per-clip `edit.upload` override → merged channel/event `config["upload"]` default → built-in default. Per-clip tags REPLACE the default list; per-clip description is free text (no templating); the channel default description stays a `{source_title}`/`{title}` template.
- `upload.mode` is never touched by the metadata editing (only the existing api/manual toggle sets it). `upload` metadata defaults are edited at the CHANNEL level (`brand.json`), NOT via the event brand editor (upload stays excluded there).
- Secrets unchanged: tokens in `<workspace>/auth/`, never logged; the job logs only the video id/url and the resulting privacy.
- Frontend pure logic in non-component `.ts` modules (Vitest-tested); `npm test` before committing frontend; `npm run build` regenerates and commits `src/yt_shorts/studio/static/`.
- The mechanical linter stays green: `python3 tools/lint.py`. No bare `except: pass` without a comment.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work on `master`.

---

### Task 1: `editorial.Edit` — per-clip upload override

**Files:**
- Modify: `src/yt_shorts/editorial.py`
- Test: `tests/test_editorial.py`

**Interfaces:**
- Produces:
  - `Edit` gains `upload: dict | None = None`.
  - `load`/`save` read/write it (save omits when None).
  - `effective_upload(edit: Edit, config: dict) -> dict` → the merged `{description?, tags?, category_id?, made_for_kids?}`: per-clip override keys win over `config["upload"]`.

**Design notes:** Read `editorial.py`'s `Edit`/`load`/`save` first. Validate an `upload` payload in `load`: if present it must be a dict; `description` (if present) a str; `tags` (if present) a list of str; `category_id` (if present) a str or int (stored as-is); `made_for_kids` (if present) a bool. `effective_upload` does a shallow per-key overlay of the per-clip override on top of `config.get("upload", {})` (excluding `mode`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_editorial.py
from yt_shorts import editorial


def test_upload_override_roundtrips(tmp_path):
    editorial.save(tmp_path, editorial.Edit(
        title=None, status=editorial.KEPT, transcript=None,
        upload={"description": "My short", "tags": ["gt7", "erf"], "category_id": "20"}))
    loaded = editorial.load(tmp_path)
    assert loaded.upload == {"description": "My short", "tags": ["gt7", "erf"], "category_id": "20"}


def test_upload_absent_is_omitted(tmp_path):
    editorial.save(tmp_path, editorial.Edit(title=None, status=editorial.CANDIDATE, transcript=None))
    import json
    payload = json.loads((tmp_path / editorial.EDIT_FILENAME).read_text())
    assert "upload" not in payload


def test_upload_bad_type_rejected(tmp_path):
    (tmp_path / editorial.EDIT_FILENAME).write_text('{"status": "kept", "upload": {"tags": "nope"}}')
    with pytest.raises(editorial.EditError):
        editorial.load(tmp_path)


def test_effective_upload_merges_clip_over_config():
    edit = editorial.Edit(title=None, status=editorial.KEPT, transcript=None,
                          upload={"description": "clip desc", "tags": ["a"]})
    config = {"upload": {"description": "tmpl", "tags": ["x"], "category_id": "20",
                         "made_for_kids": False, "mode": "api"}}
    eff = editorial.effective_upload(edit, config)
    assert eff["description"] == "clip desc"        # clip wins
    assert eff["tags"] == ["a"]                      # clip replaces
    assert eff["category_id"] == "20"                # from config
    assert "mode" not in eff                          # mode is not upload metadata
```

(`import pytest` is already at the top of the file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q`
Expected: FAIL (Edit has no `upload` / effective_upload missing)

- [ ] **Step 3: Write minimal implementation**

Add `upload: dict | None = None` to the `Edit` dataclass. In `load`, after the `window` block, parse+validate `upload`:

```python
    upload = payload.get("upload")
    if upload is not None:
        if not isinstance(upload, dict):
            raise EditError(f"'upload' must be an object or absent: {path}")
        if "description" in upload and not isinstance(upload["description"], str):
            raise EditError(f"'upload.description' must be a string: {path}")
        if "tags" in upload and not (isinstance(upload["tags"], list)
                                     and all(isinstance(t, str) for t in upload["tags"])):
            raise EditError(f"'upload.tags' must be a list of strings: {path}")
        if "category_id" in upload and not isinstance(upload["category_id"], (str, int)):
            raise EditError(f"'upload.category_id' must be a string or number: {path}")
        if "made_for_kids" in upload and not isinstance(upload["made_for_kids"], bool):
            raise EditError(f"'upload.made_for_kids' must be a boolean: {path}")
    # ...
    return Edit(title=title, status=status, transcript=transcript, window=window, upload=upload)
```

In `save`, before writing:

```python
    if edit.upload:
        payload["upload"] = edit.upload
```

Add the helper:

```python
UPLOAD_META_KEYS = ("description", "tags", "category_id", "made_for_kids")


def effective_upload(edit: Edit, config: dict) -> dict:
    """The upload metadata an upload should use: the channel/event `upload`
    defaults (minus `mode`), with this clip's own `edit.upload` override applied
    per key. `mode` is the api/manual class, not metadata, so it never appears."""
    base = config.get("upload", {}) if isinstance(config, dict) else {}
    result = {k: base[k] for k in UPLOAD_META_KEYS if k in base}
    for k in UPLOAD_META_KEYS:
        if edit.upload and k in edit.upload:
            result[k] = edit.upload[k]
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_editorial.py -q` → PASS. Then `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/editorial.py tests/test_editorial.py
git commit -m "feat(upload): per-clip upload metadata override in edit.json"
```

---

### Task 2: `youtube_upload` — visibility, scheduling, effective metadata, actual privacy

**Files:**
- Modify: `src/yt_shorts/youtube_upload.py`
- Test: `tests/test_youtube_upload.py`

**Interfaces:**
- Consumes: `editorial.effective_upload` (Task 1).
- Produces:
  - `VISIBILITIES = ("private", "unlisted", "public")`
  - `build_metadata(clip, edit, config, *, visibility="private", publish_at=None) -> dict` — effective metadata + `privacyStatus=visibility` + optional `status.publishAt`; guardrails raise `UploadError`.
  - `UploadResult` gains `privacy_status: str`; `upload_short` reads it from the insert response (`response["status"]["privacyStatus"]`, falling back to the requested value).

**Design notes:** Read the current `build_metadata`/`upload_short`/`UploadResult`. Keep the existing invalid-template `UploadError`. Update the module docstring + the `privacyStatus` comment to the new contract (default private; non-private/scheduled is an explicit operator choice; no auto-publish on a signal).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_youtube_upload.py — add to the existing build_metadata tests
import pytest

from yt_shorts import editorial
from yt_shorts.youtube_upload import UploadError, build_metadata


def _clip():
    return {"hook": "CRASH", "source_title": "ERF R3"}


def _edit(upload=None):
    return editorial.Edit(title="CRASH", status=editorial.KEPT, transcript=None, upload=upload)


def test_default_visibility_is_private():
    meta = build_metadata(_clip(), _edit(), {})
    assert meta["status"]["privacyStatus"] == "private"


def test_explicit_public():
    meta = build_metadata(_clip(), _edit(), {}, visibility="public")
    assert meta["status"]["privacyStatus"] == "public"


def test_scheduled_is_private_plus_publishat():
    meta = build_metadata(_clip(), _edit(), {}, visibility="private",
                          publish_at="2099-01-01T10:00:00Z")
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["status"]["publishAt"] == "2099-01-01T10:00:00Z"


def test_publishat_with_nonprivate_rejected():
    with pytest.raises(UploadError):
        build_metadata(_clip(), _edit(), {}, visibility="public",
                       publish_at="2099-01-01T10:00:00Z")


def test_bad_visibility_rejected():
    with pytest.raises(UploadError):
        build_metadata(_clip(), _edit(), {}, visibility="friends")


def test_per_clip_metadata_wins():
    meta = build_metadata(_clip(), _edit({"description": "hand written", "tags": ["gt7"],
                                          "category_id": "17"}),
                          {"upload": {"description": "tmpl {title}", "tags": ["x"],
                                      "category_id": "20"}})
    assert meta["snippet"]["description"] == "hand written"   # free text, no templating
    assert meta["snippet"]["tags"] == ["gt7"]
    assert meta["snippet"]["categoryId"] == "17"


def test_channel_template_still_formats_when_no_clip_override():
    meta = build_metadata(_clip(), _edit(),
                          {"upload": {"description": "Clip from {source_title}"}})
    assert meta["snippet"]["description"] == "Clip from ERF R3"


def test_title_too_long_rejected():
    with pytest.raises(UploadError):
        build_metadata(_clip(), _edit(), {}, )  # replace title with 101 chars:
    # (implementer: construct an edit whose effective title is 101 chars and assert UploadError)


def test_tags_too_long_rejected():
    with pytest.raises(UploadError):
        build_metadata(_clip(), _edit({"tags": ["x" * 300, "y" * 300]}), {})
```

(For `test_title_too_long_rejected`, build an edit with a 101-char title and assert the raise — fix the placeholder in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube_upload.py -q`
Expected: FAIL (build_metadata has no `visibility` kwarg)

- [ ] **Step 3: Write minimal implementation**

Rewrite `build_metadata` (keep the template path; add effective metadata + visibility/publish_at + guardrails):

```python
VISIBILITIES = ("private", "unlisted", "public")

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500


def build_metadata(clip, edit, config, *, visibility="private", publish_at=None) -> dict:
    if visibility not in VISIBILITIES:
        raise UploadError(
            f"visibility must be one of {', '.join(VISIBILITIES)}, got {visibility!r}")
    if publish_at is not None and visibility != "private":
        raise UploadError(
            "a scheduled publish time is only valid with private visibility "
            "(YouTube publishes it publicly at that time)")

    meta_defaults = editorial.effective_upload(edit, config)
    title = editorial.effective_title(edit, clip.get("hook", ""))
    if len(title) > TITLE_MAX:
        raise UploadError(f"title is longer than {TITLE_MAX} characters")

    description = _effective_description(edit, meta_defaults, clip, title)
    if len(description) > DESCRIPTION_MAX:
        raise UploadError(f"description is longer than {DESCRIPTION_MAX} characters")

    tags = list(meta_defaults.get("tags", []))
    if any(not t for t in tags):
        raise UploadError("a tag must not be empty")
    if sum(len(t) for t in tags) > TAGS_TOTAL_MAX:
        raise UploadError(f"tags are longer than {TAGS_TOTAL_MAX} characters combined")

    if publish_at is not None:
        _require_rfc3339(publish_at)

    status = {
        "privacyStatus": visibility,
        "selfDeclaredMadeForKids": bool(meta_defaults.get("made_for_kids", False)),
    }
    if publish_at is not None:
        status["publishAt"] = publish_at
    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(meta_defaults.get("category_id", DEFAULT_CATEGORY)),
        },
        "status": status,
    }


def _effective_description(edit, meta_defaults: dict, clip: dict, title: str) -> str:
    """A per-clip description is FINAL free text (used verbatim, never
    reformatted - so a literal '{' in it is safe); the channel default is a
    {source_title}/{title} template (unchanged behavior). The source decides
    which: if this clip overrode description, use it as-is."""
    if edit.upload and "description" in edit.upload:
        return str(edit.upload["description"])
    template = meta_defaults.get("description", DEFAULT_DESCRIPTION)
    try:
        return template.format(source_title=clip.get("source_title", ""), title=title)
    except (KeyError, IndexError, ValueError) as error:
        raise UploadError(
            f"upload.description template is invalid ({type(error).__name__}: "
            f"{error}); only {{source_title}} and {{title}} are available: "
            f"{template!r}") from error


def _require_rfc3339(value: str) -> None:
    from datetime import datetime
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as error:
        raise UploadError(f"publish_at is not a valid timestamp: {value!r}") from error
```

Add a test proving a per-clip description containing a literal `{` is used verbatim (not reformatted): `build_metadata(_clip(), _edit({"description": "score {200}"}), {})` → `meta["snippet"]["description"] == "score {200}"`. The `if edit.upload and "description" in edit.upload` branch guarantees this, since a clip override skips `str.format` entirely.

Extend `UploadResult` + `upload_short`:

```python
@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy_status: str = "private"
```

In `upload_short`, after getting `response`:

```python
    privacy = (response.get("status") or {}).get("privacyStatus", metadata["status"]["privacyStatus"])
    return UploadResult(video_id=video_id,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        privacy_status=privacy)
```

Update the module docstring + the old `# always - never overridable` comment to the new contract.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_youtube_upload.py -q` → PASS (fix the two placeholder tests to construct real long-title/description). `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/youtube_upload.py tests/test_youtube_upload.py
git commit -m "feat(upload): visibility + scheduling + effective metadata in build_metadata"
```

---

### Task 3: `profile._validate_upload` — validate the default metadata fields

**Files:**
- Modify: `src/yt_shorts/profile.py`
- Test: `tests/test_profile.py`

**Interfaces:** Extend `_validate_upload` so the channel `upload` defaults (`description` str, `tags` list of str, `category_id` str/int, `made_for_kids` bool) are validated (in addition to `mode`), collecting problems the same way. This makes `brand_admin.update_brand` (whose whitelist already includes `upload`) reject a bad default before it can break an upload.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_profile.py (or wherever _validate_upload is tested)
from yt_shorts import profile
from pathlib import Path


def test_validate_upload_accepts_metadata_fields():
    cfg = {"upload": {"mode": "api", "description": "d {title}", "tags": ["a", "b"],
                      "category_id": "20", "made_for_kids": False}}
    assert profile._validate_upload(cfg, Path("brand.json")) == []


def test_validate_upload_rejects_bad_tags():
    cfg = {"upload": {"tags": "not-a-list"}}
    assert profile._validate_upload(cfg, Path("brand.json")) != []


def test_validate_upload_rejects_bad_made_for_kids():
    cfg = {"upload": {"made_for_kids": "yes"}}
    assert profile._validate_upload(cfg, Path("brand.json")) != []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q -k validate_upload`
Expected: FAIL (bad tags/made_for_kids currently pass)

- [ ] **Step 3: Write minimal implementation** — read the current `_validate_upload` and add, after the `mode` check, collected checks:

```python
    if "description" in upload and not isinstance(upload["description"], str):
        problems.append(f"{path.name}: 'upload.description' must be a string")
    if "tags" in upload and not (isinstance(upload["tags"], list)
                                 and all(isinstance(t, str) for t in upload["tags"])):
        problems.append(f"{path.name}: 'upload.tags' must be a list of strings")
    if "category_id" in upload and not isinstance(upload["category_id"], (str, int)):
        problems.append(f"{path.name}: 'upload.category_id' must be a string or number")
    if "made_for_kids" in upload and not isinstance(upload["made_for_kids"], bool):
        problems.append(f"{path.name}: 'upload.made_for_kids' must be a boolean")
```

(Match the real function's `problems` list variable name + return style.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q -k validate_upload` → PASS. Full: `PYTHONPATH=src .venv/bin/pytest tests/test_profile.py -q`. `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/profile.py tests/test_profile.py
git commit -m "feat(upload): validate channel upload metadata defaults in profile"
```

---

### Task 4: Studio routes — visibility/schedule body, thread-through, per-clip override, actual privacy

**Files:**
- Modify: `src/yt_shorts/studio/api.py`, `src/yt_shorts/studio/jobs.py`
- Test: `tests/test_studio_api.py`

**Interfaces:**
- `post_upload` gains a body `UploadRequestBody { visibility: str = "private", publish_at: str | None = None, confirm: bool = False, force: bool = False }`; refuses (400) a non-private or scheduled upload unless `confirm`; passes `visibility`/`publish_at` to `start_upload_job`.
- `start_upload_job(..., visibility="private", publish_at=None)` → `_default_uploader(..., visibility, publish_at)` → `build_metadata(clip, edit, config, visibility=..., publish_at=...)`; `upload_record.save(..., result.privacy_status, ...)` records the ACTUAL privacy.
- The `PATCH …/clips/{name}` route (existing) accepts a per-clip `upload` override and saves it via `editorial.save`.
- `upload-preview` needs no change (it calls `build_metadata`, which now reads the effective metadata).

**Design notes:** Read `post_upload` (api.py:1009), `start_upload_job`/`_default_uploader` (jobs.py:249-310), the `PATCH …/clips/{name}` route + its `PatchClipBody` (api.py ~950), and `upload_record.save`. Thread the two new params through. Keep the injected `uploader` seam for tests.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_studio_api.py — extend the upload tests (read TestUploadAndAuthRoutes first)
class TestUploadVisibility:
    def test_public_without_confirm_is_refused(self, client, studio_profile, kept_rendered_clip):
        r = client.post(f"/api/channels/erf/events/studio-test/clips/{kept_rendered_clip}/upload",
                        json={"visibility": "public"})
        assert r.status_code == 400

    def test_public_with_confirm_threads_visibility(self, client, studio_profile,
                                                    kept_rendered_clip, monkeypatch):
        seen = {}
        import yt_shorts.studio.api as api

        def fake_start(profile, job_store, name, *, force=False, visibility="private",
                       publish_at=None, uploader=None, when=None):
            seen["visibility"] = visibility
            seen["publish_at"] = publish_at
            job = job_store.create(); job.finish("done"); return job
        monkeypatch.setattr(api.jobs, "start_upload_job", fake_start)
        r = client.post(f"/api/channels/erf/events/studio-test/clips/{kept_rendered_clip}/upload",
                        json={"visibility": "public", "confirm": True})
        assert r.status_code == 200
        assert seen == {"visibility": "public", "publish_at": None}

    def test_patch_saves_per_clip_upload_override(self, client, studio_profile, a_clip):
        r = client.patch(f"/api/channels/erf/events/studio-test/clips/{a_clip}",
                         json={"upload": {"description": "hand", "tags": ["gt7"]}})
        assert r.status_code == 200
        # reflected in upload-preview
        pv = client.get(f"/api/channels/erf/events/studio-test/clips/{a_clip}/upload-preview").json()
        assert pv["description"] == "hand" and pv["tags"] == ["gt7"]
```

(Reuse/define the `kept_rendered_clip`/`a_clip` fixtures the existing upload/clip tests use — read them first and mirror.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k "UploadVisibility or per_clip_upload"`
Expected: FAIL (post_upload takes no body; PATCH ignores upload)

- [ ] **Step 3: Write minimal implementation**

In `jobs.py`, thread the params:

```python
def _default_uploader(profile, directory, clip, edit, when, *, visibility="private", publish_at=None):
    ...
    metadata = build_metadata(clip, edit, profile.config, visibility=visibility, publish_at=publish_at)
    result = upload_short(clipstore.short_path(directory), metadata, service=service)
    upload_record.save(directory, result.video_id, result.url, result.privacy_status, when=when)
    ...

def start_upload_job(profile, job_store, name, *, force=False, uploader=_default_uploader,
                     when=None, visibility="private", publish_at=None):
    ...
    def run():
        try:
            ...
            record = uploader(profile, directory, clip, edit, stamp,
                              visibility=visibility, publish_at=publish_at)
            ...
```

In `api.py`, add the body model + guard + thread-through, and extend the PATCH route:

```python
class UploadRequestBody(BaseModel):
    visibility: str = "private"
    publish_at: str | None = None
    confirm: bool = False
    force: bool = False


    @app.post(EV + "/clips/{name}/upload")
    def post_upload(channel: str, event: str, name: str, body: UploadRequestBody | None = None) -> dict:
        body = body or UploadRequestBody()
        # Default-private guardrail (server half): anything non-private or
        # scheduled must be explicitly confirmed, per upload.
        if (body.visibility != "private" or body.publish_at is not None) and not body.confirm:
            raise HTTPException(
                status_code=400,
                detail="a non-private or scheduled upload must be confirmed (confirm=true)")
        profile = _load_profile(channel, event)
        ...existing api/kept/short/already-uploaded guards...
        try:
            job = jobs.start_upload_job(profile, app.state.job_store, name, force=body.force,
                                        visibility=body.visibility, publish_at=body.publish_at)
        except LockError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"job_id": job.id}
```

For the PATCH route (`patch_clip`), add `upload` handling to its `PatchClipBody` and save it onto the new `editorial.Edit(upload=...)`. Read the existing route (api.py ~950) — it builds a new `Edit(...)`; add `upload=` from `body.upload` when the field was set (use `body.model_fields_set` so an absent `upload` leaves the existing override untouched, and an explicit `{}`/`null` clears it — mirror how `title`/`window` are handled). Extend `PatchClipBody` with `upload: dict | None = None`.

Note: `force` moves from a query param to the body; update the existing `startUpload`/route callers accordingly (the frontend change is Task 5). Keep backward-compat by also accepting `force` if a caller still sends it as a query param, OR update the one caller — the plan updates the caller in Task 5, so a body-only `force` is fine.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src .venv/bin/pytest tests/test_studio_api.py -q -k "Upload or clip"` → PASS. Whole file green. `python3 tools/lint.py` → green.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/api.py src/yt_shorts/studio/jobs.py tests/test_studio_api.py
git commit -m "feat(studio): upload visibility/schedule body + confirm gate + per-clip metadata"
```

---

### Task 5: Frontend api client + pure `uploadMeta.ts` helpers

**Files:**
- Modify: `src/yt_shorts/studio/web/src/api.ts`
- Create: `src/yt_shorts/studio/web/src/uploadMeta.ts`
- Test: `src/yt_shorts/studio/web/src/uploadMeta.test.ts`

**Interfaces:**
- `api.ts`: `startUpload(name, opts: { visibility?: string; publishAt?: string | null; confirm?: boolean; force?: boolean })` (POST body); extend `UploadPreview` + `PatchClipBody` with the upload metadata fields; keep `getUploadPreview`.
- `uploadMeta.ts` (pure):
  - `VISIBILITIES = ['private','unlisted','public'] as const`; `CATEGORIES` (curated `{id,label}` list: Gaming 20, Sports 17, Autos 2, People&Blogs 22, Entertainment 24).
  - `parseTags(input: string): string[]` (comma/newline split, trim, drop empties, dedup).
  - `tagsToInput(tags: string[]): string` (join with ", ").
  - `toRfc3339(localDateTime: string): string` (a `datetime-local` value → RFC3339 UTC).
  - `needsConfirm(visibility: string, scheduled: boolean): boolean` (true unless plain private).
  - `metadataValid({ title, description, tags }): boolean` (mirror the YT caps: title ≤100, description ≤5000, combined tags ≤500).

- [ ] **Step 1: Write the failing test**

```typescript
// src/yt_shorts/studio/web/src/uploadMeta.test.ts
import { describe, expect, it } from 'vitest'
import { needsConfirm, parseTags, tagsToInput, metadataValid } from './uploadMeta'

describe('parseTags', () => {
  it('splits, trims, drops empties, dedups', () => {
    expect(parseTags('gt7, erf ,, gt7\nsim')).toEqual(['gt7', 'erf', 'sim'])
    expect(parseTags('')).toEqual([])
  })
})
describe('tagsToInput', () => {
  it('joins with comma-space', () => {
    expect(tagsToInput(['a', 'b'])).toBe('a, b')
  })
})
describe('needsConfirm', () => {
  it('is false only for plain private', () => {
    expect(needsConfirm('private', false)).toBe(false)
    expect(needsConfirm('private', true)).toBe(true)   // scheduled
    expect(needsConfirm('public', false)).toBe(true)
    expect(needsConfirm('unlisted', false)).toBe(true)
  })
})
describe('metadataValid', () => {
  it('enforces the YouTube caps', () => {
    expect(metadataValid({ title: 'ok', description: 'x', tags: ['a'] })).toBe(true)
    expect(metadataValid({ title: 'x'.repeat(101), description: '', tags: [] })).toBe(false)
    expect(metadataValid({ title: 'ok', description: '', tags: ['x'.repeat(300), 'y'.repeat(300)] })).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run (in `.../web`): `npm test -- uploadMeta` → FAIL (module missing)

- [ ] **Step 3: Write minimal implementation** — write `uploadMeta.ts` with the helpers above (pure, no React), and add the `api.ts` functions/types mirroring the existing `startUpload`/`getUploadPreview`/`UploadPreview` and `PatchClipBody`.

- [ ] **Step 4: Run test + typecheck**

Run (in `.../web`): `npm test -- uploadMeta` → PASS; `npx tsc -b` → 0; `npm run lint` → clean.

- [ ] **Step 5: Commit**

```bash
git add src/yt_shorts/studio/web/src/api.ts src/yt_shorts/studio/web/src/uploadMeta.ts src/yt_shorts/studio/web/src/uploadMeta.test.ts
git commit -m "feat(studio-web): upload metadata/visibility API client + pure helpers"
```

---

### Task 6: UploadPanel — editable metadata + visibility/schedule + confirm gate

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/UploadPanel.tsx`

**Design notes for the implementer:**
- Read `UploadPanel.tsx` in full — it has a confirmation `Modal` (`openModal`→`confirm`→`onStartUpload`), shows `UploadPreview`, and a reupload-ack checkbox. You EXTEND it, reusing its idioms.
- **Editable metadata (per clip):** in the panel body (or the modal), add editable Description (`Textarea`, seeded from `UploadPreview.description`), Tags (`TextInput` parsed via `parseTags`/`tagsToInput`), Category (`Select` from `uploadMeta.CATEGORIES`), Made-for-kids (`Switch`). A "Save metadata" action PATCHes `{ upload: { description, tags, category_id, made_for_kids } }` via the clip edit route; then refetch the preview. Character counters against the caps (`metadataValid`).
- **Visibility & schedule (per upload, in the confirm modal):** a `SegmentedControl`/`Radio` — Private (default) / Unlisted / Public / Scheduled. "Scheduled" reveals a `datetime-local` input and the copy "becomes **public** at this time." When `needsConfirm(visibility, scheduled)` is true, reveal a required confirmation `Checkbox` ("I understand this upload will be unlisted/public/scheduled") that gates the confirm button (compose with the existing reupload-ack).
- On confirm: call `onStartUpload` (extend its signature, or call `startUpload` here) with `{ visibility, publishAt: scheduled ? toRfc3339(dt) : null, confirm: needsConfirm(...), force: isReupload }`.
- `App.tsx`'s `handleStartUpload` and the `onStartUpload` prop type must be extended to carry the options; update the call site. Keep pure logic in `uploadMeta.ts`.
- The manual-mode panel (`ManualUploadPanel`) is unaffected (no API upload); leave it.

- [ ] **Step 1: Implement** the editable metadata area + visibility/schedule + confirm gate, reusing UploadPanel's Modal/idioms and `uploadMeta.ts`.

- [ ] **Step 2: Typecheck + lint + vitest**

Run (in `.../web`): `npx tsc -b` → 0; `npm run lint` → clean; `npm test` → all pass.

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/UploadPanel.tsx src/yt_shorts/studio/web/src/App.tsx
git commit -m "feat(studio-web): edit upload metadata + choose visibility/schedule with a confirm gate"
```

---

### Task 7: Channel upload-defaults editor

**Files:**
- Modify: `src/yt_shorts/studio/web/src/components/BrandEditor.tsx`, `src/yt_shorts/studio/web/src/api.ts` (types if needed)

**Design notes:** The channel-level `upload` metadata defaults (`description` template, `tags`, `category_id`, `made_for_kids`) become UI-editable. `brand_admin.update_brand`'s whitelist already includes `upload`, and `PUT /api/channels/{channel}/brand` accepts it (Task 3 validates it). Add an **"Upload defaults" Card** to `BrandEditor` (channel scope only — NOT `EventBrandEditor`): Description template (`Textarea`, note the `{source_title}`/`{title}` placeholders), Tags (`TextInput` via `parseTags`), Category (`Select`), Made-for-kids (`Switch`). Fold it into the existing `formFromBrand`/`formToPatch` so it saves through the same `saveBrand` PUT (the `upload` section, preserving `mode` — read the existing brand `upload.mode` and never overwrite it: send only the metadata keys, or merge `mode` back in). Reuse `uploadMeta.ts` helpers.

- [ ] **Step 1: Implement** the Upload-defaults Card in `BrandEditor`, saving via the existing brand PUT, preserving `upload.mode`.

- [ ] **Step 2: Typecheck + lint + vitest**

Run (in `.../web`): `npx tsc -b` → 0; `npm run lint` → clean; `npm test` → all pass.

- [ ] **Step 3: Commit**

```bash
git add src/yt_shorts/studio/web/src/components/BrandEditor.tsx src/yt_shorts/studio/web/src/api.ts
git commit -m "feat(studio-web): channel upload-defaults editor (description/tags/category)"
```

---

### Task 8: CLAUDE.md safety redesign, build, E2E, full verification, commit static

**Files:**
- Modify: `CLAUDE.md`, `src/yt_shorts/studio/static/**` (built), `tests/test_studio_e2e.py`

- [ ] **Step 1: Update `CLAUDE.md`** — replace the "Privacy is always `private`" invariant (in the Upload/stage-E section) with the new contract: default private; a non-private/scheduled upload requires an explicit, confirmed, per-upload operator choice (enforced client + server); no auto-publish on any derived signal; `manual` channels never API-upload. Keep every other upload invariant (secrets in `auth/`, re-upload guard, `made_for_kids`). Make the wording match what the code now does.

- [ ] **Step 2: Add the E2E** in `tests/test_studio_e2e.py` (reuse the upload stub pattern — `_stub_upload_job` — and the kept-rendered-clip fixture): open the UploadPanel, (a) edit description/tags, Save, assert `edit.json` on disk carries the `upload` override and `upload-preview` reflects it; (b) open the confirm modal, pick **Public**, assert the confirm checkbox gates the button, tick it, confirm, and assert the stubbed `start_upload_job` received `visibility="public"`; (c) a **Scheduled** case asserts `publish_at` is sent and visibility stays private. Model selectors on the existing upload E2E.

- [ ] **Step 3: Build** — in `.../web`: `npm run lint` (clean) → `npm run build` (exit 0).

- [ ] **Step 4: Full suites** — `npm test` (web) all pass; `PYTHONPATH=src .venv/bin/pytest -q` all pass; `python3 tools/lint.py` → All checks passed.

- [ ] **Step 5: Commit** the rebuilt static + E2E + CLAUDE.md:

```bash
git add CLAUDE.md src/yt_shorts/studio/static tests/test_studio_e2e.py
git commit -m "docs+build(studio): document the new upload-privacy contract; rebuild static; e2e"
```

- [ ] **Step 6: Manual smoke (optional)** — restart `bin/yt-shorts studio` (backend route changes need a restart), open a kept+rendered ERF clip, edit metadata, pick a visibility, confirm.

---

## Notes for the implementer

- **The privacy relaxation is the highest-risk change.** The default is private; the confirm gate is enforced on BOTH server (`post_upload`) and client (UI checkbox). Task 4's `test_public_without_confirm_is_refused` and Task 8's E2E pin this. Do not weaken it.
- **Actual vs requested privacy:** `upload_short` records the resulting `privacyStatus` from the insert response — an unverified channel may be forced private; the record/UI reflects the real value, not the request.
- **Effective metadata:** per-clip `edit.upload` override → merged channel/event `config["upload"]` → built-in default. Per-clip tags REPLACE; per-clip description is free text (used verbatim; do not reformat a literal `{`), the channel default stays a template.
- **`upload.mode` is untouchable here** — the defaults editor sends only metadata keys and preserves `mode`; the api/manual toggle stays the only writer of `mode`.
- **Backend route changes require a studio restart** to take effect (uvicorn runs without `--reload`); a browser reload only refreshes the frontend bundle.
