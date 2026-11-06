import pathlib
import sqlite3
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coach.config import get_settings
from coach.db.migrate import connect, current_version, migrate, migrations
from coach.integrations.intervals import IntervalsClient, IntervalsError
from coach.workouts.builder import event_payload, workout_text
from coach.workouts.model import Repeat, Session, Step

# --- migrations ------------------------------------------------------------


def test_migrate_is_idempotent_and_records_versions(tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    first = migrate(conn)
    assert first == [p.name for _, p in migrations()]
    assert migrate(conn) == []
    assert current_version(conn) == max(v for v, _ in migrations())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"activity", "weight", "wellness_day", "schema_migrations"} <= tables


def test_migrate_adopts_db_built_without_runner(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for v, p in migrations():
        if v <= 3:  # pre-runner DBs only ever had 001-003; 004+ are runner-only
            conn.executescript(p.read_text())
    conn.execute("INSERT INTO weight (date, kg, source, updated_at) VALUES ('2026-11-01', 82, 'a', 'x')")
    conn.commit()
    assert len(migrate(conn)) == len(migrations())  # recorded, no errors
    assert conn.execute("SELECT count(*) FROM weight").fetchone()[0] == 1


# --- healthz ---------------------------------------------------------------


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_DB_PATH", str(tmp_path / "data" / "coach.sqlite"))
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY_EXPIRES", "2027-05-01")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_healthz_migrates_on_startup(app_env):
    from coach.main import build_app

    with TestClient(build_app()) as client:
        body = client.get("/healthz").json()
        assert client.get("/").status_code == 200
    assert body["ok"] is True
    assert body["schema_version"] == max(v for v, _ in migrations())
    assert body["intervals_configured"] is True
    assert body["garmin_key_expires"] == "2027-05-01"


def test_blank_env_values_are_unset(app_env, monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "")
    get_settings.cache_clear()
    assert get_settings().intervals_api_key is None


MCP_VARS = ("COACH_MCP_PUBLIC_URL", "COACH_MCP_ALLOWED_GITHUB_ID", "COACH_MCP_GITHUB_CLIENT_ID",
            "COACH_MCP_GITHUB_CLIENT_SECRET", "COACH_MCP_JWT_KEY", "COACH_MCP_STORAGE_KEY")


def test_healthz_ok_without_mcp_settings(app_env, monkeypatch):
    for k in MCP_VARS:
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    from coach.main import build_app
    with TestClient(build_app()) as client:
        assert client.get("/healthz").status_code == 200
    s = get_settings()
    assert s.coach_mcp_public_url is None and s.coach_mcp_allowed_github_id is None
    assert s.coach_mcp_bind_port == 9091


def test_mcp_settings_parse(app_env, monkeypatch):
    values = dict(zip(MCP_VARS, ("https://coach-mcp.example.com", "12345", "cid", "csecret",
                                 "jwt-key", "storage-key"), strict=True))
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    s = get_settings()
    assert s.coach_mcp_allowed_github_id == 12345
    assert s.coach_mcp_public_url == "https://coach-mcp.example.com"
    assert s.coach_mcp_github_client_secret is not None
    assert s.coach_mcp_github_client_secret.get_secret_value() == "csecret"


def test_mcp_github_id_must_be_numeric(app_env, monkeypatch):
    monkeypatch.setenv("COACH_MCP_ALLOWED_GITHUB_ID", "lapo")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_package_data_has_no_llm_prompts():
    import tomllib
    data = tomllib.loads(pathlib.Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    globs = data["tool"]["setuptools"]["package-data"]["coach"]
    assert "db/migrations/*.sql" in globs
    assert not any("llm" in g for g in globs)
    deps = " ".join(data["project"]["dependencies"])
    assert "fastmcp" in deps and "anthropic" not in deps and "telegram" not in deps


# --- workout builder -------------------------------------------------------

START = datetime(2026, 11, 5, 7, 0)


def _sweet_spot() -> Session:
    return Session(
        external_id="plan-2026-11-05-ride", sport="ride", start=START, name="Sweet spot 3x8",
        steps=[Step(duration_s=600, power_lo=55, power_hi=65, ramp=True, label="Warmup"),
               Repeat(times=3, label="Main set", steps=[Step(duration_s=480, power_lo=88, power_hi=92),
                                                        Step(duration_s=240, power_lo=55)]),
               Step(duration_s=300, power_lo=50, label="Cooldown")],
    )


def test_ride_text_uses_ftp_percent():
    assert workout_text(_sweet_spot()) == (
        "- Warmup 10m ramp 55-65%\n\nMain set 3x\n- 8m 88-92%\n- 4m 55%\n\n- Cooldown 5m 50%"
    )
    assert _sweet_spot().total_s() == 600 + 3 * 720 + 300


def test_run_text_uses_hr_or_pace():
    s = Session(external_id="r", sport="run", start=START, name="Easy run",
                steps=[Step(duration_s=1800, hr_zone=2), Step(duration_s=90, pace_s_per_km=285)])
    assert workout_text(s) == "- 30m Z2 HR\n\n- 1m30s 4:45/km Pace"


def test_sport_target_mismatch_rejected():
    with pytest.raises(ValueError):
        Session(external_id="x", sport="ride", start=START, name="x", steps=[Step(duration_s=60, hr_zone=2)])
    with pytest.raises(ValueError):
        Session(external_id="x", sport="run", start=START, name="x", steps=[Step(duration_s=60, power_lo=90)])


def test_event_payload():
    p = event_payload(_sweet_spot())
    assert p["category"] == "WORKOUT" and p["type"] == "VirtualRide"
    assert p["external_id"] == "plan-2026-11-05-ride"
    assert p["start_date_local"] == "2026-11-05T07:00:00"
    assert "moving_time" not in p
    gym = event_payload(Session(external_id="g", sport="gym", start=START, name="Gym", duration_s=2700, indoor=True))
    assert gym["type"] == "WeightTraining" and gym["moving_time"] == 2700 and gym["description"] == ""


# --- intervals.icu client --------------------------------------------------


def _client(handler) -> IntervalsClient:
    return IntervalsClient("secret", "i42", transport=httpx.MockTransport(handler), backoff_s=0)


def test_client_auth_and_paths():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/events/bulk"):
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(200, json={"id": "i42", "sportSettings": [{"types": ["Ride", "VirtualRide"], "ftp": 230}]})

    c = _client(handler)
    assert c.ride_ftp() == 230
    c.upsert_events([event_payload(_sweet_spot())])
    assert seen[0].url.path == "/api/v1/athlete/i42"
    assert seen[0].headers["authorization"] == "Basic " + __import__("base64").b64encode(b"API_KEY:secret").decode()
    assert seen[1].method == "POST" and seen[1].url.params["upsert"] == "true"


def test_client_retries_429_then_succeeds():
    calls = iter([httpx.Response(429), httpx.Response(503), httpx.Response(200, json=[])])
    c = _client(lambda req: next(calls))
    assert c.list_events(START.date(), START.date()) == []


def test_client_raises_on_4xx_without_retry():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(401, text="nope")

    with pytest.raises(IntervalsError, match="401"):
        _client(handler).get_athlete()
    assert len(n) == 1
