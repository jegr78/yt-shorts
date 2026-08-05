"""Time arithmetic for clip boundaries. Pure logic, no I/O."""

from __future__ import annotations


def to_seconds(value: str | float | int) -> float:
    """Converts HH:MM:SS, MM:SS, SS, or a number into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = value.strip().split(":")
    if len(parts) > 3:
        raise ValueError(f"Not a time value: {value!r}")
    try:
        numbers = [float(p) for p in parts]
    except ValueError as error:
        raise ValueError(f"Not a time value: {value!r}") from error
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def to_text(seconds: float) -> str:
    """Always returns HH:MM:SS, rounded down to whole seconds."""
    if seconds < 0:
        raise ValueError(f"Negative seconds not allowed: {seconds}")
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def with_padding(
    start: float,
    end: float,
    lead_in: float,
    lead_out: float,
    length: float | None,
) -> tuple[float, float]:
    """Extends a range by lead-in and lead-out, without running past the video.

    lead_in and lead_out must be >= 0.
    length=None means: video length unknown, only the front is clamped.
    """
    if end < start:
        raise ValueError(f"End {end} is before start {start}")
    if length is not None and length < start:
        raise ValueError(f"Video length {length} is before start {start}")
    if lead_in < 0:
        raise ValueError(f"Lead-in must not be negative: {lead_in}")
    if lead_out < 0:
        raise ValueError(f"Lead-out must not be negative: {lead_out}")
    new_start = max(0.0, start - lead_in)
    new_end = end + lead_out
    if length is not None:
        new_end = min(length, new_end)
    return (new_start, new_end)
