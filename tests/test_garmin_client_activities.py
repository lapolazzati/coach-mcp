"""Garmin client: activity reads (search, detail, FIT download, HR zones)."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import httpx
import pytest

from coach.integrations.garmin import TOKEN_PATH, GarminClient, GarminError

FIT = b"\x0e\x10fake-fit-bytes.FIT"


def zipped(name: str = "123_ACTIVITY.fit", data: bytes = FIT) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def make(handler) -> GarminClient:
    def wrap(req: httpx.Request) -> httpx.Response:
        if req.url.path == TOKEN_PATH:
            return httpx.Response(200, text="tok")
        return handler(req)
    return GarminClient("k", transport=httpx.MockTransport(wrap), backoff_s=0, retries=1)


def test_activities_paginates_and_windows():
    seen = []

    def h(req):
        seen.append(dict(req.url.params))
        start = int(req.url.params["start"])
        page = [{"activityId": start + i} for i in range(2)] if start < 4 else []
        return httpx.Response(200, json=page)

    c = make(h)
    c.PAGE = 2
    out = c.activities(date(2026, 11, 1), date(2026, 11, 14))
    assert [a["activityId"] for a in out] == [0, 1, 2, 3]
    assert seen[0]["startDate"] == "2026-11-01" and seen[0]["endDate"] == "2026-11-14"
    assert [p["start"] for p in seen] == ["0", "2", "4"]


def test_activities_tolerates_dict_wrapper():
    c = make(lambda req: httpx.Response(200, json={"activities": [{"activityId": 9}]} if req.url.params["start"] == "0" else []))
    assert [a["activityId"] for a in c.activities(date(2026, 11, 1), date(2026, 11, 1))] == [9]


def test_activity_detail():
    c = make(lambda req: httpx.Response(200, json={"activityId": 5, "summaryDTO": {"averagePower": 160}})
             if req.url.path == "/activity-service/activity/5" else httpx.Response(404))
    assert c.activity(5)["summaryDTO"]["averagePower"] == 160


def test_fit_unzips_original():
    def h(req):
        assert req.url.path == "/download-service/files/activity/123"
        return httpx.Response(200, content=zipped())
    assert make(h).fit(123) == FIT


def test_fit_without_fit_member_raises():
    c = make(lambda req: httpx.Response(200, content=zipped("x.gpx", b"<gpx/>")))
    with pytest.raises(GarminError, match="no FIT"):
        c.fit(123)


@pytest.mark.parametrize("bad", ["0", "-1", "12a", "../x"])
def test_fit_rejects_bad_id(bad):
    c = make(lambda req: pytest.fail("no request expected"))
    with pytest.raises(GarminError):
        c.fit(bad)


def test_fit_http_error_raises():
    with pytest.raises(GarminError, match="404"):
        make(lambda req: httpx.Response(404)).fit(1)


def test_hr_zones():
    zones = [{"sport": "CYCLING", "zone1Floor": 100, "lactateThresholdHeartRateUsed": 166}]
    c = make(lambda req: httpx.Response(200, json=zones) if req.url.path == "/biometric-service/heartRateZones/v2"
             else httpx.Response(404))
    assert c.hr_zones()[0]["sport"] == "CYCLING"
