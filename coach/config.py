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

    intervals_api_key: SecretStr | None = None
    intervals_athlete_id: str | None = None

    garmin_personal_api_key: SecretStr | None = None
    garmin_personal_api_key_expires: date | None = None

    anthropic_api_key: SecretStr | None = None
    anthropic_monthly_budget_usd: float = 100.0

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None

    google_sa_json: SecretStr | None = None      # service-account JSON (inline) for DWD
    google_subject: str = "l@ethon.io"
    google_calendar_ids: str = "primary"          # comma-separated
    google_maps_api_key: SecretStr | None = None
    coach_home_address: str | None = None

    @field_validator("intervals_api_key", "garmin_personal_api_key", "intervals_athlete_id",
                     "garmin_personal_api_key_expires", "anthropic_api_key", "telegram_bot_token",
                     "telegram_chat_id", "google_sa_json", "google_maps_api_key", "coach_home_address",
                     mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        # Empty lines in .env ("KEY=") mean "not set yet", not an empty secret.
        return None if isinstance(v, str) and not v.strip() else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
