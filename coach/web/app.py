"""The coach web app: the dashboard at GET /.

    uv run --with fastapi --with jinja2 --with uvicorn uvicorn coach.web.app:app --port 8000

The database is read-only here; the importers own every write. COACH_DB points
at the SQLite file (default data/coach.sqlite). A missing file renders the
day-one dashboard from an empty in-memory schema instead of failing.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from coach.ingest.garmin_export import ensure_schema
from coach.web.context import build_context
from coach.web.env import STATIC_DIR, build_env

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "coach.sqlite"
# Lapo trains in Italy; "today" is the Rome calendar day, not the server's UTC one.
TZ = ZoneInfo("Europe/Rome")


def open_db(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def create_app(db_path: Path | str | None = None) -> FastAPI:
    db_path = Path(db_path or os.environ.get("COACH_DB", DEFAULT_DB))
    template = build_env().get_template("dashboard.html")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        # One short-lived connection per request: sqlite3 connections can't cross threads.
        conn = open_db(db_path)
        try:
            ctx = build_context(conn, datetime.now(TZ).date())
        finally:
            conn.close()
        return template.render(static_url="/static", **ctx)

    return app


app = create_app()
