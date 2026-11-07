"""Every-30-minutes Garmin wellness sync for the last 3 days."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from coach.ingest import garmin_sync
from coach.jobs.morning import local_now

DAYS = 3


def run(conn: sqlite3.Connection, client: Any, now: datetime) -> dict[str, Any]:
    if client is None:
        return {"skipped": "garmin not configured"}
    today = local_now(now).date()
    return {"wellness": garmin_sync.sync_wellness(client, conn, today - timedelta(days=DAYS - 1), today)}
