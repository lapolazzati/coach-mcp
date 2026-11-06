"""APScheduler wiring for the U7 jobs, started from the app lifespan when enabled.

Each job opens its own connection from conn_factory (so it runs on the
scheduler's thread) and gets a client from client_factory, or None when
intervals.icu is not configured. A failing job logs and waits for its next slot.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.combining import OrTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from coach.jobs import backup, morning, weekly
from coach.jobs import sync as sync_job
from coach.store import sync

log = logging.getLogger(__name__)

JOB_DEFAULTS = {"coalesce": True, "misfire_grace_time": 1800, "max_instances": 1}

ConnFactory = Callable[[], sqlite3.Connection]
ClientFactory = Callable[[], Any]


def _client(client_factory: ClientFactory) -> Any:
    try:
        return client_factory()
    except Exception as exc:  # noqa: BLE001 - unconfigured intervals.icu just means no client
        log.warning("jobs: no intervals client (%s)", exc)
        return None


def _job(name: str, body: Callable[[sqlite3.Connection, Any, datetime], Any], tz: ZoneInfo,
         conn_factory: ConnFactory, client_factory: ClientFactory) -> Callable[[], None]:
    def go() -> None:
        try:
            conn = conn_factory()
        except Exception:
            log.exception("job %s: cannot open the database", name)
            return
        try:
            result = body(conn, _client(client_factory), datetime.now(tz))
            log.info("job %s: %s", name, result)
        except Exception:
            log.exception("job %s failed", name)
        finally:
            conn.close()

    go.__name__ = f"job_{name}"
    return go


def morning_trigger(tz: ZoneInfo) -> OrTrigger:
    """Every 10 minutes, 05:30 to 17:50."""
    return OrTrigger([
        CronTrigger(hour=5, minute="30-50/10", timezone=tz),
        CronTrigger(hour="6-17", minute="*/10", timezone=tz),
    ])


def build_scheduler(settings: Any, conn_factory: ConnFactory, client_factory: ClientFactory,
                    *, now: datetime | None = None) -> BackgroundScheduler:
    tz = ZoneInfo(settings.coach_tz)
    scheduler = BackgroundScheduler(timezone=tz, job_defaults=dict(JOB_DEFAULTS))

    def job(name: str, body: Callable[[sqlite3.Connection, Any, datetime], Any]) -> Callable[[], None]:
        return _job(name, body, tz, conn_factory, client_factory)

    run_morning = job("morning", morning.run)
    scheduler.add_job(run_morning, morning_trigger(tz), id="morning")
    scheduler.add_job(job("sync", sync_job.run), IntervalTrigger(minutes=30, timezone=tz), id="sync")
    scheduler.add_job(job("weekly", lambda conn, _client, at: weekly.run(conn, at)),
                      CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=tz), id="weekly")
    scheduler.add_job(job("backup", lambda _conn, _client, at: backup.run(at)),
                      CronTrigger(hour=3, minute=15, timezone=tz), id="backup")

    start = (now or datetime.now(tz)).astimezone(tz)
    conn = conn_factory()
    try:
        done = sync.get_state(conn, morning.state_key(start.date())) == "done"
    finally:
        conn.close()
    if not done:
        scheduler.add_job(run_morning, DateTrigger(run_date=start, timezone=tz), id="morning_catchup",
                          replace_existing=True)
    return scheduler
