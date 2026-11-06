"""Every-30-minutes sync: wellness and activities for the last 3 days, then reconcile."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from coach.ingest import intervals_sync
from coach.jobs.morning import local_now
from coach.plan import reconcile

DAYS = 3


def run(conn: sqlite3.Connection, client: Any, now: datetime) -> dict[str, Any]:
    if client is None:
        return {"skipped": "intervals not configured"}
    today = local_now(now).date()
    oldest = today - timedelta(days=DAYS - 1)
    return {
        "wellness": intervals_sync.sync_wellness(client, conn, oldest, today),
        "activities": intervals_sync.sync_activities(client, conn, oldest, today),
        "reconcile": reconcile.reconcile(conn, client, now=now),
    }
