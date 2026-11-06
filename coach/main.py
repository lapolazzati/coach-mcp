"""Production entry point: migrate, then serve the dashboard plus /healthz.

    uvicorn coach.main:app --host <tailscale-ip> --port 8000
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from coach.config import get_settings
from coach.db import migrate as db_migrate
from coach.web.app import create_app


def build_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        conn = db_migrate.connect(settings.coach_db_path)
        try:
            db_migrate.migrate(conn)
        finally:
            conn.close()
        yield

    app = create_app(settings.coach_db_path)
    app.router.lifespan_context = lifespan

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            conn = sqlite3.connect(f"file:{settings.coach_db_path}?mode=ro", uri=True)
            try:
                version = db_migrate.current_version(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return JSONResponse({"ok": False, "db": str(exc)}, status_code=503)
        return JSONResponse({
            "ok": True,
            "schema_version": version,
            "intervals_configured": bool(settings.intervals_api_key and settings.intervals_athlete_id),
            "garmin_key_expires": settings.garmin_personal_api_key_expires.isoformat()
            if settings.garmin_personal_api_key_expires else None,
        })

    return app


app = build_app()
