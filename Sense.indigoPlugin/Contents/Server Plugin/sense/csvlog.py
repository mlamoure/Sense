"""Optional per-poll CSV log of whole-house power, one file per day, oldest files pruned."""

from __future__ import annotations

import os
from datetime import datetime

FILE_PREFIX = "activeLog-"
HEADER = "Timestamp,power\n"


class CsvLog:
    def __init__(self, folder: str, keep_days: int = 30, now=datetime.now):
        self.folder = folder
        self.keep_days = keep_days
        self._now = now
        self._current = ""

    def append(self, watts: int) -> str:
        """Append one row to today's file (creating it, and the folder, as needed)."""
        stamp = self._now()
        path = os.path.join(self.folder, f"{FILE_PREFIX}{stamp:%Y-%m-%d}.csv")
        if path != self._current:
            os.makedirs(self.folder, exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w") as fh:
                    fh.write(HEADER)
            self._current = path
            self.prune()
        with open(path, "a") as fh:
            fh.write(f"{stamp:%Y-%m-%d %H:%M:%S.%f},{int(watts)}\n")
        return path

    def prune(self) -> list[str]:
        """Delete the oldest daily files beyond `keep_days`; the legacy activeLog.csv is untouched."""
        if not os.path.isdir(self.folder):
            return []
        daily = sorted(
            f for f in os.listdir(self.folder) if f.startswith(FILE_PREFIX) and f.endswith(".csv")
        )
        removed = []
        for name in daily[: max(0, len(daily) - self.keep_days)]:
            os.remove(os.path.join(self.folder, name))
            removed.append(name)
        return removed
