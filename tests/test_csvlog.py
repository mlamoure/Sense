from __future__ import annotations

import os
from datetime import datetime

from sense.csvlog import CsvLog


class Now:
    def __init__(self):
        self.t = datetime(2026, 9, 20, 17, 0, 0)

    def __call__(self):
        return self.t


def test_writes_daily_files_with_header(tmp_path):
    now = Now()
    log = CsvLog(str(tmp_path / "csv"), keep_days=30, now=now)
    p = log.append(2175)
    log.append(2180)
    assert os.path.basename(p) == "activeLog-2026-09-20.csv"
    assert (
        open(p).read()
        == "Timestamp,power\n2026-09-20 17:00:00.000000,2175\n2026-09-20 17:00:00.000000,2180\n"
    )
    now.t = datetime(2026, 9, 21, 0, 0, 1)
    p2 = log.append(5)
    assert os.path.basename(p2) == "activeLog-2026-09-21.csv"
    assert open(p2).read().count("\n") == 2


def test_prunes_oldest_but_keeps_legacy_file(tmp_path):
    folder = tmp_path / "csv"
    folder.mkdir()
    (folder / "activeLog.csv").write_text("legacy\n")
    for day in range(1, 6):
        (folder / f"activeLog-2026-09-0{day}.csv").write_text("x\n")
    now = Now()
    log = CsvLog(str(folder), keep_days=3, now=now)
    log.append(1)  # creates 2026-09-20, then prunes to the 3 newest daily files
    names = sorted(os.listdir(folder))
    assert names == [
        "activeLog-2026-09-04.csv",
        "activeLog-2026-09-05.csv",
        "activeLog-2026-09-20.csv",
        "activeLog.csv",
    ]
