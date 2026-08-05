"""Read a stream's derived files - the analysis and the transcript.

Pure and FastAPI-free, like `workspace_listing.py` and the `*_admin` modules:
the studio's routes are a thin mapping of `AnalysisError.kind` onto a status
code, and nothing here knows that HTTP exists.

Both files live under `<workspace>/streams/<video_id>/`, NOT under an event.
That is deliberate and is not a scoping oversight: a transcript and the
analysis derived from it are properties of the STREAM, so two events that
look at the same stream share them rather than each paying an hour of Whisper
decode for a second copy. The event in the studio's URL is how the operator
navigates and which EventLock a write takes - it is not a scope on this data.

A MISSING file and an UNREADABLE one are different answers, which is why
`kind` exists. The screen is designed to work before detection has ever run,
so "no analysis yet" is an ordinary state the caller renders as an empty hit
list; a corrupt file is a real fault the operator has to be told about.
"""

from __future__ import annotations

import json
from pathlib import Path

from .detect import ANALYSIS_FILENAME, analysis_path
from .pathnames import validate_segment

TRANSCRIPT_FILENAME = "transcript.json"


class AnalysisError(RuntimeError):
    """A stream's derived file could not be read. `kind` says which way."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _read(path: Path, what: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AnalysisError("not_found", f"no {what} for this stream yet") from None
    except OSError as error:
        raise AnalysisError("unreadable", f"cannot read the {what}: {error}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise AnalysisError("unreadable", f"the {what} is not valid JSON: {error}") from None


def read_analysis(workspace_dir: str | Path, video_id: str) -> dict:
    """The detection analysis, `streams/<video_id>/moments.json`.

    `analysis_path` validates the video id itself; this call is what makes the
    validation happen BEFORE any filesystem touch here too.
    """
    return _read(analysis_path(workspace_dir, video_id), "analysis")


def read_transcript(workspace_dir: str | Path, video_id: str) -> dict:
    """The whole stream transcript, `streams/<video_id>/transcript.json`.

    Served whole rather than paged: ~2 MB for eight hours over localhost, which
    makes client-side search instant and paging a complication that buys the
    operator nothing.
    """
    validate_segment(video_id, what="video id")
    path = Path(workspace_dir) / "streams" / video_id / TRANSCRIPT_FILENAME
    return _read(path, "transcript")


__all__ = ["ANALYSIS_FILENAME", "TRANSCRIPT_FILENAME", "AnalysisError",
           "read_analysis", "read_transcript"]
