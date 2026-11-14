"""Every-30-minutes Garmin sync: wellness for the last 3 days, then activities.

Activities are synced only when the scheduler passes fit_dir. FIT downloads stop
at a deadline so one run never overlaps the next; leftovers are fetched next run.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from coach.ingest import garmin_sync
from coach.jobs.morning import local_now

DAYS = 3
FIT_BUDGET_S = 600


def run(conn: sqlite3.Connection, client: Any, now: datetime, *, fit_dir: Path | None = None) -> dict[str, Any]:
    if client is None:
        return {"skipped": "garmin not configured"}
    today = local_now(now).date()
    out: dict[str, Any] = {
        "wellness": garmin_sync.sync_wellness(client, conn, today - timedelta(days=DAYS - 1), today)}
    if fit_dir is not None:
        try:
            out["activities"] = garmin_sync.sync_activities(
                client, conn, fit_dir, garmin_sync.activities_window(conn, today), today,
                deadline=time.monotonic() + FIT_BUDGET_S)
        except garmin_sync.SyncLocked:
            out["activities"] = "locked"
    return out
