"""Runtime settings, read from the environment (/opt/coach/.env on the server).

Secrets stay optional so the app boots and serves the dashboard before every
integration is wired; each integration checks for its own key when called.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    coach_db_path: Path = ROOT / "data" / "coach.sqlite"
    coach_tz: str = "Europe/Rome"
    coach_bind_host: str = "127.0.0.1"
    coach_bind_port: int = 8000
    # U7 jobs (morning readiness, sync, weekly, backup); off until the deploy turns it on.
    coach_scheduler_enabled: bool = False

    intervals_api_key: SecretStr | None = None
    intervals_athlete_id: str | None = None

    garmin_personal_api_key: SecretStr | None = None
    garmin_personal_api_key_expires: date | None = None

    # Remote MCP server for the Claude app (coach-mcp service). All optional so the
    # dashboard boots without them; coach-mcp itself refuses to start if unset.
    coach_mcp_public_url: str | None = None
    coach_mcp_allowed_github_id: int | None = None
    coach_mcp_github_client_id: str | None = None
    coach_mcp_github_client_secret: SecretStr | None = None
    coach_mcp_jwt_key: SecretStr | None = None
    coach_mcp_storage_key: SecretStr | None = None
    coach_mcp_bind_port: int = 9091

    google_sa_json: SecretStr | None = None      # service-account JSON (inline) for DWD
    google_subject: str = "l@ethon.io"
    google_calendar_ids: str = "primary"          # comma-separated
    google_maps_api_key: SecretStr | None = None
    coach_home_address: str | None = None

    @field_validator("intervals_api_key", "garmin_personal_api_key", "intervals_athlete_id",
                     "garmin_personal_api_key_expires", "coach_mcp_public_url",
                     "coach_mcp_allowed_github_id", "coach_mcp_github_client_id",
                     "coach_mcp_github_client_secret", "coach_mcp_jwt_key",
                     "coach_mcp_storage_key", "google_sa_json", "google_maps_api_key", "coach_home_address",
                     mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        # Empty lines in .env ("KEY=") mean "not set yet", not an empty secret.
        return None if isinstance(v, str) and not v.strip() else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
