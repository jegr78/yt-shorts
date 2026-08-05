"""The one 'safe single path segment' rule, shared by event_admin and
channel_admin. A name here becomes a directory name, so the same validation
guards both the event name and the channel slug (and every {event}/{channel}
URL segment): '..', a slash or a leading dot must never reach the filesystem.
No FastAPI, no heavy imports."""

from __future__ import annotations

import re

# \Z (not $) so a trailing newline is rejected: Python's $ matches just before
# a final '\n', which would let "round-1\n" through.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_NAME_LENGTH = 100


def validate_segment(value: str, *, what: str) -> None:
    """Raise ValueError (naming `what`) if `value` is not one safe path segment:
    not a string, empty, > MAX_NAME_LENGTH, a leading dot, '..', a slash, or any
    char outside [A-Za-z0-9._-].

    The isinstance check is what makes "raises ValueError" true rather than
    nearly true, and callers rely on the difference. Without it a non-empty
    non-string reached `len()` or `NAME_PATTERN.match()` and came back out as a
    TypeError - so `detect._has`, whose whole job is that "an id that is not a
    safe segment answers False rather than raising", let exactly that case
    through and sank the list of 99 it exists to protect. A value that is not a
    string is not a valid segment; that is the same answer, in the same
    currency, as one with a slash in it.
    """
    if (not isinstance(value, str) or not value
            or len(value) > MAX_NAME_LENGTH or not NAME_PATTERN.match(value)):
        raise ValueError(
            f"not a valid {what}: {value!r} (use letters, digits, '.', '-', "
            f"'_'; no slashes, no leading dot, max {MAX_NAME_LENGTH} chars)")
