"""Build a YouTube upload and perform it (resumable) behind an injected service.

build_metadata is pure. upload_short takes an already-built API service, injected
so tests never touch google or the network.

Privacy DEFAULTS to private and stays there unless the caller passes an
explicit ``visibility`` - non-private (or a scheduled ``publish_at``, which
YouTube only accepts alongside ``private``) is always an explicit operator
choice made through the CLI or studio UI at upload time, never something a
signal in the pipeline decides on its own. Nothing here auto-publishes.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import editorial

DEFAULT_CATEGORY = "20"  # Gaming - sim racing sits here more naturally than Sports
DEFAULT_DESCRIPTION = "Clip from {source_title}."

VISIBILITIES = ("private", "unlisted", "public")

# YouTube's own published limits: guard them here, at build time, rather than
# let a good render fail opaquely inside the API call.
TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_TOTAL_MAX = 500


def build_metadata(clip: dict, edit, config: dict, *,
                   visibility: str = "private", publish_at: str | None = None) -> dict:
    if visibility not in VISIBILITIES:
        raise UploadError(
            f"visibility must be one of {', '.join(VISIBILITIES)}, got {visibility!r}")
    if publish_at is not None and visibility != "private":
        raise UploadError(
            "a scheduled publish time is only valid with private visibility "
            "(YouTube publishes it publicly at that time)")
    if publish_at is not None:
        _require_future_rfc3339(publish_at)

    meta = editorial.effective_upload(edit, config)

    title = editorial.effective_title(edit, clip.get("hook", ""))
    if len(title) > TITLE_MAX:
        raise UploadError(
            f"title is {len(title)} characters, longer than YouTube's {TITLE_MAX}-character limit")

    description = _effective_description(edit, meta, clip, title)
    if len(description) > DESCRIPTION_MAX:
        raise UploadError(
            f"description is {len(description)} characters, longer than "
            f"YouTube's {DESCRIPTION_MAX}-character limit")

    tags = list(meta.get("tags", []))
    if any(not isinstance(t, str) or not t for t in tags):
        raise UploadError("each tag must be a non-empty string")
    tags_length = sum(len(t) for t in tags)
    if tags_length > TAGS_TOTAL_MAX:
        raise UploadError(
            f"tags are {tags_length} characters combined, longer than "
            f"YouTube's {TAGS_TOTAL_MAX}-character limit")

    status = {
        "privacyStatus": visibility,
        "selfDeclaredMadeForKids": bool(meta.get("made_for_kids", False)),
    }
    if publish_at is not None:
        status["publishAt"] = publish_at

    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": str(meta.get("category_id", DEFAULT_CATEGORY)),
        },
        "status": status,
    }


def _effective_description(edit, meta: dict, clip: dict, title: str) -> str:
    """A per-clip description (``edit.upload["description"]``) is final free
    text and is used VERBATIM - never passed through ``str.format`` - so an
    operator's literal ``{`` is safe. The channel/event default stays a
    ``{source_title}``/``{title}`` template, formatted only in that case.
    """
    if edit.upload and "description" in edit.upload:
        return str(edit.upload["description"])
    template = meta.get("description", DEFAULT_DESCRIPTION)
    # An operator-supplied template with an unknown placeholder ({event}), a
    # stray brace, or a positional {} raises deep inside str.format; turn that
    # into a clear UploadError naming the bad template rather than a raw
    # KeyError/ValueError the caller has to decode.
    try:
        return template.format(source_title=clip.get("source_title", ""), title=title)
    except (KeyError, IndexError, ValueError) as error:
        raise UploadError(
            f"upload.description template is invalid ({type(error).__name__}: "
            f"{error}); only {{source_title}} and {{title}} are available: "
            f"{template!r}") from error


def _require_future_rfc3339(value: str) -> None:
    """A scheduled publish is only ever valid alongside ``visibility="private"``
    (see ``build_metadata``'s own guard), but YouTube will still publish the
    video PUBLICLY the instant ``publishAt`` arrives - a past timestamp means
    "the instant this request lands", i.e. immediate public exposure, which is
    exactly the accident these guardrails exist to prevent. RFC3339 mandates a
    timezone offset, so a naive value is rejected outright rather than assumed
    to be UTC or local time.
    """
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except (ValueError, AttributeError) as error:
        raise UploadError(f"publish_at is not a valid timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise UploadError("publish_at must include a timezone offset")
    if parsed <= datetime.now(timezone.utc):
        raise UploadError("publish_at must be in the future")


@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy_status: str = "private"


class UploadError(Exception):
    """Understandable message about a failed upload."""


def _default_media(short_path):
    # Real resumable media; imported lazily so tests inject their own factory.
    from googleapiclient.http import MediaFileUpload
    return MediaFileUpload(str(short_path), resumable=True)


def upload_short(short_path, metadata, *, service, media_factory=None) -> UploadResult:
    """Resumable videos.insert via the injected service. Never touches google in tests.

    ``.execute()`` performs the whole resumable upload in one call, which is
    simplest and correct for a single short. A caller that wants progress can
    switch to a ``request.next_chunk()`` loop without changing this signature.
    """
    media = (media_factory or _default_media)(short_path)
    try:
        request = service.videos().insert(
            part=",".join(metadata.keys()), body=metadata, media_body=media)
        response = request.execute()
    except UploadError:
        raise
    except Exception as error:  # noqa: BLE001 - reported, never swallowed
        message = str(error)
        if "quota" in message.lower():
            raise UploadError(
                "today's upload quota is used up; it resets at midnight Pacific"
            ) from error
        raise UploadError(f"upload failed: {message}") from error
    video_id = response.get("id")
    if not video_id:
        raise UploadError(f"upload returned no video id: {response!r}")
    # YouTube is the authority on what actually happened, not the request we
    # sent - fall back to the requested value only if the response omits it.
    privacy_status = (response.get("status") or {}).get(
        "privacyStatus", metadata["status"]["privacyStatus"])
    return UploadResult(video_id=video_id,
                        url=f"https://www.youtube.com/watch?v={video_id}",
                        privacy_status=privacy_status)
