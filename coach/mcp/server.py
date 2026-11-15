"""coach-mcp: the remote MCP server Claude's connector talks to.

Streamable HTTP at ``/mcp`` behind GitHub OAuth (see ``coach.mcp.auth``), plus an
unauthenticated ``/healthz``. Refuses to start without every COACH_MCP_* setting or
with a database older than the schema the tools need.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from coach.config import Settings, get_settings
from coach.db.migrate import current_version
from coach.integrations.garmin import GarminClient
from coach.integrations.intervals import IntervalsClient
from coach.mcp import tools_read, tools_ride, tools_write
from coach.mcp.auth import DEFAULT_STORAGE_DIR, CoachGitHubProvider

REQUIRED_SCHEMA_VERSION = 5
MCP_PATH = "/mcp"

# Settings field -> env var, in the order an operator would fill them in.
_REQUIRED_SETTINGS = {
    "coach_mcp_public_url": "COACH_MCP_PUBLIC_URL",
    "coach_mcp_allowed_github_id": "COACH_MCP_ALLOWED_GITHUB_ID",
    "coach_mcp_github_client_id": "COACH_MCP_GITHUB_CLIENT_ID",
    "coach_mcp_github_client_secret": "COACH_MCP_GITHUB_CLIENT_SECRET",
    "coach_mcp_jwt_key": "COACH_MCP_JWT_KEY",
    "coach_mcp_storage_key": "COACH_MCP_STORAGE_KEY",
}


class StartupError(RuntimeError):
    """coach-mcp cannot serve with the current configuration or database."""


def check_settings(settings: Settings) -> None:
    missing = [
        env
        for field, env in _REQUIRED_SETTINGS.items()
        if getattr(settings, field) is None
    ]
    if missing:
        raise StartupError(
            "coach-mcp refuses to start: missing required setting(s) "
            + ", ".join(missing)
        )
    url = settings.coach_mcp_public_url or ""
    if not url.startswith("https://"):
        raise StartupError(
            "coach-mcp refuses to start: COACH_MCP_PUBLIC_URL must be https://"
        )


def schema_version(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return current_version(conn)
    finally:
        conn.close()


def check_database(db_path: Path) -> None:
    version = schema_version(db_path)
    if version < REQUIRED_SCHEMA_VERSION:
        raise StartupError(
            f"coach-mcp refuses to start: database {db_path} is at schema version {version}, "
            f"needs >= {REQUIRED_SCHEMA_VERSION}; run `python -m coach.db.migrate` first"
        )


def build_server(
    settings: Settings | None = None,
    *,
    storage_dir: Path = DEFAULT_STORAGE_DIR,
    http_client: httpx.AsyncClient | None = None,
) -> FastMCP:
    settings = settings or get_settings()
    check_settings(settings)
    check_database(settings.coach_db_path)
    # check_settings guarantees these are set; the asserts narrow types for mypy.
    assert settings.coach_mcp_public_url is not None
    assert settings.coach_mcp_allowed_github_id is not None
    assert settings.coach_mcp_github_client_id is not None
    assert settings.coach_mcp_github_client_secret is not None
    assert settings.coach_mcp_jwt_key is not None
    assert settings.coach_mcp_storage_key is not None

    auth = CoachGitHubProvider(
        public_url=settings.coach_mcp_public_url,
        github_client_id=settings.coach_mcp_github_client_id,
        github_client_secret=settings.coach_mcp_github_client_secret.get_secret_value(),
        allowed_github_id=settings.coach_mcp_allowed_github_id,
        jwt_key=settings.coach_mcp_jwt_key.get_secret_value(),
        storage_key=settings.coach_mcp_storage_key.get_secret_value(),
        storage_dir=storage_dir,
        http_client=http_client,
    )
    mcp = FastMCP("coach", auth=auth)
    db_path = settings.coach_db_path

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        try:
            version = schema_version(db_path)
        except sqlite3.Error:
            return JSONResponse({"ok": False, "db": "unreadable"}, status_code=503)
        ok = version >= REQUIRED_SCHEMA_VERSION
        return JSONResponse(
            {"ok": ok, "schema_version": version}, status_code=200 if ok else 503
        )

    @mcp.tool
    def ping() -> str:
        """Check that the coach MCP server is reachable and you are signed in."""
        return "pong"

    tools_read.register(mcp, db_path)
    tools_write.register(mcp, db_path, client_factory=lambda: IntervalsClient.from_settings(settings))
    tools_ride.register(
        mcp,
        db_path,
        garmin_factory=(lambda: GarminClient.from_settings(settings)) if settings.garmin_personal_api_key else None,
        intervals_factory=lambda: IntervalsClient.from_settings(settings),
        fit_dir=settings.fit_dir,
    )
    return mcp


def build_app(
    settings: Settings | None = None,
    *,
    storage_dir: Path = DEFAULT_STORAGE_DIR,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    return build_server(
        settings, storage_dir=storage_dir, http_client=http_client
    ).http_app(path=MCP_PATH)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="coach-mcp", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=settings.coach_mcp_bind_port)
    parser.add_argument("--storage-dir", type=Path, default=DEFAULT_STORAGE_DIR)
    args = parser.parse_args(argv)
    try:
        app = build_app(settings, storage_dir=args.storage_dir)
    except (StartupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()
