"""intervals.icu API client.

Auth is HTTP Basic with the literal user "API_KEY" and the key as password.
429 and 5xx are retried with backoff; anything else raises.
"""

from __future__ import annotations

import time
from datetime import date

import httpx

BASE_URL = "https://intervals.icu/api/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}


class IntervalsError(RuntimeError):
    pass


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str, *, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None, retries: int = 3, backoff_s: float = 1.0):
        self.athlete_id = athlete_id
        self.retries = retries
        self.backoff_s = backoff_s
        self._http = httpx.Client(
            base_url=base_url,
            auth=("API_KEY", api_key),
            headers={"User-Agent": "Mozilla/5.0 (personal coach; intervals.icu API client)"},
            timeout=30,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings) -> IntervalsClient:
        if not (settings.intervals_api_key and settings.intervals_athlete_id):
            raise IntervalsError("INTERVALS_API_KEY and INTERVALS_ATHLETE_ID must be set")
        return cls(settings.intervals_api_key.get_secret_value(), settings.intervals_athlete_id)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in range(self.retries + 1):
            resp = self._http.request(method, path, **kw)
            if resp.status_code not in RETRY_STATUS or attempt == self.retries:
                break
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_s * 2**attempt)
        if resp.is_error:
            raise IntervalsError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    @property
    def _athlete(self) -> str:
        return f"/athlete/{self.athlete_id}"

    # --- reads ---------------------------------------------------------------

    def get_athlete(self) -> dict:
        return self._request("GET", self._athlete).json()

    def ride_ftp(self) -> int | None:
        for s in self.get_athlete().get("sportSettings", []):
            if "Ride" in s.get("types", []) or "VirtualRide" in s.get("types", []):
                return s.get("ftp")
        return None

    def list_events(self, oldest: date, newest: date, category: str = "WORKOUT") -> list[dict]:
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat(), "category": category}
        return self._request("GET", f"{self._athlete}/events", params=params).json()

    def get_wellness(self, oldest: date, newest: date) -> list[dict]:
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        return self._request("GET", f"{self._athlete}/wellness", params=params).json()

    def list_activities(self, oldest: date, newest: date) -> list[dict]:
        params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
        return self._request("GET", f"{self._athlete}/activities", params=params).json()

    def download_zwo(self, event_id: int) -> bytes:
        return self._request("GET", f"{self._athlete}/events/{event_id}/downloadzwo").content

    # --- writes --------------------------------------------------------------

    def upsert_events(self, events: list[dict]) -> list[dict]:
        """Create or update by external_id, so re-sending a plan never duplicates it."""
        return self._request("POST", f"{self._athlete}/events/bulk", params={"upsert": "true"}, json=events).json()

    def delete_events(self, external_ids: list[str]) -> None:
        body = [{"external_id": x} for x in external_ids]
        self._request("PUT", f"{self._athlete}/events/bulk-delete", json=body)
