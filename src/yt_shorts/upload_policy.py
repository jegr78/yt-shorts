"""Which upload class a channel is, and the one guard that enforces it.

A channel is either API-uploadable ("api", the default) or render-only
("manual" - a YouTube manager/editor delegation the Data API cannot upload
to, so the tool renders the short and the operator uploads it by hand in
YouTube Studio). This module is the single source of that decision, imported
by both the CLI and the studio so the rule lives in exactly one place. It
imports nothing heavy (no FastAPI, no google) and reads only a plain config
dict - the same flat config profile.load produces.
"""

from __future__ import annotations

from . import clipstore, editorial, trim, upload_record

RENDER_ONLY_MESSAGE = (
    "channel is render-only (upload.mode=manual): the YouTube Data API cannot "
    "upload to a manager/editor channel. Render the short, download it, and "
    "upload it by hand in YouTube Studio."
)


class RenderOnlyError(Exception):
    """Raised when an API-upload path is reached for a render-only channel."""


class UploadPolicyError(Exception):
    """A clip is not currently eligible for an upload."""


def mode(config: dict) -> str:
    """"manual" only when explicitly set so; "api" (the default) otherwise.

    Any value other than the exact string "manual" - a missing 'upload'
    block, a missing 'mode', a non-dict, or an unexpected value - resolves to
    "api", i.e. the existing behaviour, so this can never accidentally block a
    real owned channel. profile._validate_upload is what rejects an unexpected
    value at load time.
    """
    upload = config.get("upload") if isinstance(config, dict) else None
    value = upload.get("mode") if isinstance(upload, dict) else None
    return "manual" if value == "manual" else "api"


def is_render_only(config: dict) -> bool:
    return mode(config) == "manual"


def require_api_upload(config: dict) -> None:
    """No-op for an api channel; raises RenderOnlyError for a manual one."""
    if is_render_only(config):
        raise RenderOnlyError(RENDER_ONLY_MESSAGE)


def require_eligible(config: dict, directory, edit, *, force: bool = False) -> None:
    """Check the current clip under the caller's EventLock, just before upload."""
    require_api_upload(config)
    if edit.status != editorial.KEPT:
        raise UploadPolicyError("only a kept clip can be uploaded")
    if not clipstore.short_path(directory).is_file():
        raise UploadPolicyError("not rendered: no short.mp4 to upload")
    if trim.is_pending(directory, edit):
        raise UploadPolicyError("trim not applied: apply it before uploading")
    if upload_record.is_uploaded(directory) and not force:
        raise UploadPolicyError("already uploaded: confirm a direct re-upload instead")
