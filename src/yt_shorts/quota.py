"""A local per-day estimate of upload quota, per channel account.

videos.insert costs ~1600 units against a default 10,000/day (~6 uploads). The
count is a LOCAL ESTIMATE - the API is the authority - so it only WARNS, never
blocks an upload the API would accept. The day boundary is Pacific, where
YouTube's quota resets. `now` is injected so the reset is testable.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

INSERT_COST = 1600
DAILY_DEFAULT = 10000
PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_day(now: datetime) -> str:
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


class QuotaTracker:
    def __init__(self, auth_dir, channel_id, *, daily: int = DAILY_DEFAULT):
        self.path = Path(auth_dir) / "quota.json"
        self.channel_id = channel_id
        self.daily = daily

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def spent_today(self, now: datetime) -> int:
        data = self._read().get(self.channel_id, {})
        return int(data.get(_pacific_day(now), 0))

    def book_insert(self, now: datetime) -> None:
        data = self._read()
        day = _pacific_day(now)
        channel = data.setdefault(self.channel_id, {})
        channel[day] = int(channel.get(day, 0)) + INSERT_COST
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(json.dumps(data, indent=2))

    def _atomic_write(self, text: str) -> None:
        # Write to a temp file in the same dir then os.replace: a process dying
        # mid-write can never leave a torn/partial quota.json behind (the read
        # side already tolerates a missing file, and replace is atomic).
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=".quota-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass  # temp file never got created; nothing to clean up
            raise

    def remaining_uploads(self, now: datetime) -> int:
        return max(0, (self.daily - self.spent_today(now)) // INSERT_COST)
