"""Garmin Connect Personal API client (official, read-only).

Auth: the personal API key (GARMIN_PERSONAL_API_KEY) is exchanged for a 24h
bearer JWT at the token endpoint. There is no refresh token: the client
re-exchanges shortly before the JWT's `exp`, or once after a 401.
429 and 5xx are retried with backoff; anything else raises.

Range reads are split into windows of at most WINDOW_DAYS days.
Training readiness and acute/chronic load are not exposed by this API.
"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import httpx

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
RETRY_STATUS = {429, 500, 502, 503, 504}
WINDOW_DAYS = 28
TOKEN_MARGIN_S = 300


class GarminError(RuntimeError):
    pass


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, TypeError):
        return None


def windows(oldest: date, newest: date, days: int = WINDOW_DAYS) -> Iterator[tuple[date, date]]:
    start = oldest
    while start <= newest:
        end = min(start + timedelta(days=days - 1), newest)
        yield start, end
        start = end + timedelta(days=1)


class GarminClient:
    PAGE = 100  # activity search page size

    def __init__(self, api_key: str, *, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None, retries: int = 3, backoff_s: float = 1.0,
                 clock: Any = time.time):
        self._api_key = api_key
        self.retries = retries
        self.backoff_s = backoff_s
        self._clock = clock
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.Client(base_url=base_url, timeout=30, transport=transport,
                                  headers={"User-Agent": "personal coach; Garmin Personal API client"})

    @classmethod
    def from_settings(cls, settings: Any) -> GarminClient:
        if not settings.garmin_personal_api_key:
            raise GarminError("GARMIN_PERSONAL_API_KEY must be set")
        return cls(settings.garmin_personal_api_key.get_secret_value())

    def close(self) -> None:
        self._http.close()

    # --- auth ------------------------------------------------------------------

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(self.retries + 1):
            resp = self._http.request(method, path, **kw)
            if resp.status_code not in RETRY_STATUS or attempt == self.retries:
                return resp
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_s * 2**attempt)
        raise AssertionError("unreachable")

    def _bearer(self, *, force: bool = False) -> str:
        if force or self._token is None or self._clock() >= self._token_exp - TOKEN_MARGIN_S:
            resp = self._send("POST", TOKEN_PATH, headers={"Accept": "*/*", "Authorization": f"ApiKey {self._api_key}"})
            if resp.is_error:
                # never echo the body: it may reflect the key
                raise GarminError(f"token exchange -> {resp.status_code}")
            token = resp.text.strip().strip('"')
            if not token:
                raise GarminError("token exchange returned an empty token")
            self._token = token
            self._token_exp = _jwt_exp(token) or self._clock() + 23 * 3600
        return self._token  # type: ignore[return-value]

    def _response(self, path: str, params: dict[str, Any] | None = None,
                  accept: str = "application/json") -> httpx.Response:
        for fresh in (False, True):
            headers = {"Authorization": f"Bearer {self._bearer(force=fresh)}", "Accept": accept}
            resp = self._send("GET", path, params=params, headers=headers)
            if resp.status_code != 401:
                break
        if resp.is_error:
            body = resp.text[:200] if "json" in resp.headers.get("content-type", "") else ""
            raise GarminError(f"GET {path} -> {resp.status_code}: {body}")
        return resp

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._response(path, params)
        if not resp.content:
            return None
        return resp.json()

    # --- reads -----------------------------------------------------------------

    def sleep_summaries(self, oldest: date, newest: date) -> list[dict]:
        out: list[dict] = []
        for a, b in windows(oldest, newest):
            data = self._get("/sleep-service/sleep/dailySleepSummariesByDate",
                             {"startDate": a.isoformat(), "endDate": b.isoformat()})
            out.extend(d for d in data or [] if isinstance(d, dict))
        return out

    def daily_summaries(self, oldest: date, newest: date) -> list[dict]:
        out: list[dict] = []
        for a, b in windows(oldest, newest):
            data = self._get("/usersummary-service/v2/dailysummaries",
                             {"startDate": a.isoformat(), "endDate": b.isoformat()})
            out.extend(d for d in (data or {}).get("data") or [] if isinstance(d, dict))
        return out

    def hrv_summaries(self, oldest: date, newest: date) -> list[dict]:
        out: list[dict] = []
        for a, b in windows(oldest, newest):
            data = self._get(f"/hrv-service/hrv/daily/{a.isoformat()}/{b.isoformat()}")
            out.extend(d for d in (data or {}).get("hrvSummaries") or [] if isinstance(d, dict))
        return out

    # --- activities ------------------------------------------------------------

    def activities(self, oldest: date, newest: date) -> list[dict]:
        """Activity summaries started between oldest and newest (inclusive), all pages."""
        out: list[dict] = []
        for a, b in windows(oldest, newest):
            start = 0
            while True:
                data = self._get("/activitylist-service/activities/search/activities",
                                 {"startDate": a.isoformat(), "endDate": b.isoformat(),
                                  "start": start, "limit": self.PAGE})
                if isinstance(data, dict):
                    data = data.get("activities") or []
                page = [d for d in data or [] if isinstance(d, dict)]
                out.extend(page)
                if len(page) < self.PAGE:
                    break
                start += self.PAGE
        return out

    def activity(self, activity_id: int | str) -> dict:
        return self._get(f"/activity-service/activity/{_activity_id(activity_id)}") or {}

    def fit(self, activity_id: int | str) -> bytes:
        """The original FIT file of an activity (Garmin serves it zipped)."""
        resp = self._response(f"/download-service/files/activity/{_activity_id(activity_id)}",
                              accept="application/zip, application/octet-stream")
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                name = next((n for n in zf.namelist() if n.lower().endswith(".fit")), None)
                if name is None:
                    raise GarminError(f"activity {activity_id}: no FIT file in download")
                return zf.read(name)
        except zipfile.BadZipFile as exc:
            raise GarminError(f"activity {activity_id}: download is not a zip") from exc

    def hr_zones(self) -> list[dict]:
        data = self._get("/biometric-service/heartRateZones/v2")
        return [d for d in data or [] if isinstance(d, dict)]


def _activity_id(activity_id: int | str) -> str:
    s = str(activity_id)
    if not (s.isascii() and s.isdecimal() and int(s) > 0):
        raise GarminError("activity id must be a positive integer")
    return s

