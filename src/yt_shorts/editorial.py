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
import math
from dataclasses import dataclass
from pathlib import Path

from . import atomicwrite

EDIT_FILENAME = "edit.json"

CANDIDATE = "candidate"
KEPT = "kept"
DISCARDED = "discarded"
STATUSES = (CANDIDATE, KEPT, DISCARDED)

# A floor on what must REMAIN after a trim, never a target length: below three
# seconds there is no short left, only a mistake.
MIN_REMAINING_SECONDS = 3.0


class EditError(Exception):
    """Understandable error message about an unusable edit.json."""


@dataclass
class Edit:
    title: str | None
    status: str
    transcript: dict | None
    window: tuple[float, float] | None = None
    upload: dict | None = None
    trim: tuple[float, float] | None = None


def checksum(words: list[dict]) -> str:
    """Identifies a word list, so a correction can record what it was made
    against. Serialised with sorted keys and no whitespace so the same words
    always hash the same way regardless of how they were written out."""
    # Normalize numeric values so 1 and 1.0 hash identically
    normalized = _normalize_numbers(words)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_numbers(obj):
    """Recursively normalize numeric values: convert int to float when equal.
    This ensures 1 and 1.0 hash the same without modifying stored data."""
    if isinstance(obj, dict):
        return {k: _normalize_numbers(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_normalize_numbers(item) for item in obj]
    elif isinstance(obj, int) and not isinstance(obj, bool):
        # Convert int to float to match pipeline output
        return float(obj)
    return obj


def normalise_word_boundaries(words: list[dict]) -> list[dict]:
    """Restores the word boundary a text field cannot express.

    faster-whisper marks the START of a word with a leading space, and
    captions._to_caption joins the tokens with "" precisely so it can rely on
    that: a continuation token carries no leading space, so " C" + ".L" +
    ".R." must render as C.L.R. rather than C .L .R. (see that function's own
    docstring - the real example is this project's "Speedy" clip).

    A human types "Rei", not " Rei". Without this, a hand-corrected word glues
    itself to its predecessor and the caption reads IT'SREIRACING - measured on
    the operator's own clip.

    The rule: a text beginning with a letter or digit gets exactly one leading
    space. Everything else is returned unchanged - empty (the operator clearing
    a word), already-spaced (which is what makes this idempotent), and
    punctuation-led, which is the deliberate way to write a continuation.

    "Letter or digit" is str.isalnum(), Unicode-aware on purpose: the
    corrections here are full of German proper nouns, and 'Überholmanöver'
    needs its boundary exactly like 'Rei' does.

    Measured basis for that discriminator: across the 11518 decoder tokens in
    this workspace's transcripts, 91 carry no leading space, and every single
    one starts with '.' (71) or '-' (20). Not one starts with a letter or a
    digit, at any index. The counts date a growing corpus and are worth
    re-measuring rather than trusting; the PROPERTY is what this rule rests on.

    Called from the two routes where words arrive from a client (the words
    PATCH and the preview POST), never from save(): save is handed a complete
    Edit, and rewriting its content would make every round-trip test lie about
    what it stores.
    """
    normalised = []
    for word in words:
        copy = dict(word)
        text = copy.get("text") or ""
        # text[:1] rather than text[0]: an empty string slices to "", whose
        # isalnum() is False, so the empty case needs no branch of its own.
        if text[:1].isalnum():
            copy["text"] = " " + text
        normalised.append(copy)
    return normalised


def load(clip_dir: str | Path) -> Edit:
    """Reads a clip's editorial layer. A missing file means 'untouched' and
    is not an error; an unreadable or schema-invalid one is."""
    path = Path(clip_dir) / EDIT_FILENAME
    if not path.exists():
        return Edit(title=None, status=CANDIDATE, transcript=None, window=None)

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

        # Validate each word entry
        words = transcript.get("words")
        for index, word in enumerate(words):
            if not isinstance(word, dict) or not {"start", "end", "text"} <= word.keys():
                raise EditError(
                    f"Word entry {index} is missing start, end or text in {path}\n"
                    f"Entry: {word!r}"
                )
            # Validate that timestamps are numeric
            for key in ("start", "end"):
                if not isinstance(word[key], (int, float)) or isinstance(word[key], bool):
                    raise EditError(
                        f"Word entry {index} has non-numeric {key}={word[key]!r} in {path}"
                    )

    window = payload.get("window")
    if window is not None:
        if (not isinstance(window, list) or len(window) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in window)):
            raise EditError(
                f"'window' must be a [start, end] pair of numbers or absent: {path}")
        window = (float(window[0]), float(window[1]))

    trim = payload.get("trim")
    if trim is not None:
        # load has no clip duration to check a trim against - only
        # validate_trim's SHAPE half applies here (see its own docstring).
        # float("inf") is passed because it cannot constrain anything: no
        # head+tail+MIN_REMAINING_SECONDS is ever greater than infinity, so
        # this call can only reject on shape, never on the relationship a
        # caller holding clip.json would need to check.
        validate_trim(trim, float("inf"), path)
        trim = (float(trim[0]), float(trim[1]))

    upload = payload.get("upload")
    validate_upload_override(upload, path)

    return Edit(title=title, status=status, transcript=transcript, window=window,
                upload=upload, trim=trim)


def validate_upload_override(upload: dict | None, path: str | Path = "upload") -> None:
    """Validates the shape of an `upload` override - the same rules `load`
    applies when reading it back from disk. `upload=None` (no override, or
    clearing one) is always valid. Raises `EditError` naming `path` (an
    edit.json location, or any other label a caller wants in the message) on
    a bad shape, so a caller writing an override - e.g. the studio's PATCH
    route - can reject it BEFORE it ever reaches `save`, rather than writing
    something `load` will only reject on the next read."""
    if upload is None:
        return
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


def save(clip_dir: str | Path, edit: Edit) -> None:
    """Writes the editorial layer, omitting everything that was not set -
    so the file stays a record of decisions rather than a dump of defaults."""
    payload: dict = {}
    if edit.title is not None:
        payload["title"] = edit.title
    payload["status"] = edit.status
    if edit.transcript is not None:
        payload["transcript"] = edit.transcript
    if edit.window is not None:
        payload["window"] = [edit.window[0], edit.window[1]]
    if edit.trim is not None:
        payload["trim"] = [edit.trim[0], edit.trim[1]]
    if edit.upload:
        payload["upload"] = edit.upload

    path = Path(clip_dir) / EDIT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    atomicwrite.write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def effective_title(edit: Edit, harvested_title: str) -> str:
    return edit.title if edit.title is not None else harvested_title


def effective_window(edit: Edit, clip_start: float,
                     clip_end: float) -> tuple[float, float]:
    """The operator's window override if set, else the detected/harvested one.

    Same rule as effective_title: hand work always wins. A moment's detected
    window is derived data; nudging it in the studio is an editorial decision
    that lives in edit.json, and this is where render and preview read it back.
    """
    return edit.window if edit.window is not None else (clip_start, clip_end)


def effective_trim(edit: Edit) -> tuple[float, float]:
    """The operator's head/tail cut, or no cut at all.

    An absent trim and (0.0, 0.0) are the same request; this is the one place
    that flattens the distinction, so no consumer has to test for None.
    """
    return edit.trim if edit.trim is not None else (0.0, 0.0)


def validate_trim(trim, duration: float, path: str | Path = "trim") -> None:
    """Validates a trim's SHAPE and, given a duration, whether it leaves a
    short behind - the single source of truth `load` now calls rather than
    duplicating.

    This is the function that will receive raw HTTP request data (the
    studio's PATCH route), so - like `validate_upload_override` - it cannot
    trust its caller's shape: a two-element list/tuple of non-negative
    numbers, with `bool` rejected explicitly even though it is a subclass of
    `int` (see `load`'s own hard-won comment on the same trap, a few lines
    below). A malformed shape raises `EditError` naming `path`, never a bare
    `IndexError`/`ValueError` from indexing or converting something that was
    never checked.

    `load` calls this too, for the shape half only: it sees just a
    directory, never a clip's length, so it cannot check the duration
    relationship below - that is the half only a caller holding clip.json
    (this function's other caller) can do. `load` passes `float("inf")` as
    the duration specifically because it cannot constrain anything, so that
    call can only ever reject on shape.
    """
    if trim is None:
        return
    # json.loads accepts Infinity/-Infinity/NaN (a Python stdlib extension of
    # the JSON grammar), so a hand-edited edit.json can carry a `trim` the
    # shape check below would otherwise wave through: `float("inf") >= 0` is
    # True, so {"trim": [Infinity, 0.0]} passed as a shape and made the
    # duration comparison below inert (head+tail+floor is always > any
    # finite duration). -Infinity and NaN are already rejected by the `>= 0`
    # clause on their own - a NaN comparison is always False, and
    # -Infinity >= 0 is False - so only the +Infinity case needed this.
    # FastAPI/Pydantic serialise a stored `inf` back out as JSON `null`, so
    # an operator who reached this state could not even see the bad value in
    # the studio, only a clip that could never again be delivered.
    if (not isinstance(trim, (list, tuple)) or len(trim) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and v >= 0 for v in trim)):
        raise EditError(
            f"'trim' must be a [head, tail] pair of non-negative, finite "
            f"numbers or absent: {path}")
    head, tail = float(trim[0]), float(trim[1])
    if head + tail + MIN_REMAINING_SECONDS > duration + 1e-9:
        raise EditError(
            f"a trim of {head}s + {tail}s leaves less than "
            f"{MIN_REMAINING_SECONDS}s of a {duration}s clip: {path}")


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
