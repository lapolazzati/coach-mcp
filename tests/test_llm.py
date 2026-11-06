"""U4: Claude client + budget ledger. No network: the SDK is faked."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from coach.db.migrate import connect, migrate
from coach.llm import budget
from coach.llm.client import HAIKU, SONNET, LLMClient


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.sqlite")
    migrate(c)
    yield c
    c.close()


NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)


def usage(i=1000, o=200, cw=0, cr=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o,
                           cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


def spend(conn, usd, ts="2026-11-10T08:00:00+00:00", model=HAIKU):
    conn.execute("INSERT INTO llm_call (ts_utc, task, tier_requested, model, cost_usd, status)"
                 " VALUES (?, 't', 'haiku', ?, ?, 'ok')", (ts, model, usd))
    conn.commit()


# --- cost maths ---------------------------------------------------------------

def test_sonnet_cost_includes_cache_tokens():
    # 1000 in @2 + 200 out @10 + 2000 cache write @2.5 + 4000 cache read @0.2 per MTok
    c = budget.cost_usd(SONNET, usage(1000, 200, 2000, 4000))
    assert c == pytest.approx((1000 * 2 + 200 * 10 + 2000 * 2.5 + 4000 * 0.2) / 1e6)


def test_haiku_low_tier():
    assert budget.cost_usd(HAIKU, usage(10_000, 1000)) == pytest.approx((10_000 * 0.10 + 1000 * 0.50) / 1e6)


def test_haiku_high_tier_above_100k_total_input():
    # 60K input + 50K cache read = 110K total input → high tier for every token
    c = budget.cost_usd(HAIKU, usage(60_000, 1000, 0, 50_000))
    assert c == pytest.approx((60_000 * 0.50 + 1000 * 2.50 + 50_000 * 0.05) / 1e6)


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        budget.cost_usd("gpt-4", usage())


# --- month-to-date + gating ----------------------------------------------------

def test_month_to_date_uses_rome_month_boundary(conn):
    spend(conn, 5.0, ts="2026-10-31T22:30:00+00:00")  # 23:30 Rome on 31 Oct → October
    spend(conn, 7.0, ts="2026-10-31T23:30:00+00:00")  # 00:30 Rome on 1 Nov → November
    assert budget.month_to_date_spend(conn, NOW) == pytest.approx(7.0)


def test_under_80_percent_keeps_tier(conn):
    spend(conn, 10.0)
    alerts = []
    assert budget.check(conn, "sonnet", NOW, 100.0, alerts.append) == SONNET
    assert alerts == []


def test_80_percent_downgrades_sonnet_and_alerts_once_per_month(conn):
    spend(conn, 85.0)
    alerts = []
    assert budget.check(conn, "sonnet", NOW, 100.0, alerts.append) == HAIKU
    assert budget.check(conn, "haiku", NOW, 100.0, alerts.append) == HAIKU
    assert len(alerts) == 1
    # a new month starts clean
    later = datetime(2026, 12, 2, 10, 0, tzinfo=timezone.utc)
    assert budget.check(conn, "sonnet", later, 100.0, alerts.append) == SONNET
    assert len(alerts) == 1


def test_100_percent_raises(conn):
    spend(conn, 100.0)
    with pytest.raises(budget.BudgetExhausted):
        budget.check(conn, "haiku", NOW, 100.0, lambda _m: None)


# --- client -------------------------------------------------------------------

class Verdict(BaseModel):
    action: str


class FakeSDK:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def parse(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(obj, **u):
    return SimpleNamespace(parsed_output=obj, stop_reason="end_turn", usage=usage(**u))


def client(conn, sdk, alerts=None):
    s = SimpleNamespace(anthropic_monthly_budget_usd=100.0)
    return LLMClient(conn, s, sdk=sdk, alert=(alerts.append if alerts is not None else None),
                     now=lambda: NOW)


def rows(conn):
    return conn.execute("SELECT model, status, cost_usd FROM llm_call ORDER BY id").fetchall()


def test_call_shape_no_temperature_cached_system_and_thinking_only_on_sonnet(conn):
    sdk = FakeSDK([ok(Verdict(action="keep")), ok(Verdict(action="keep"))])
    c = client(conn, sdk)
    assert c.call("readiness", Verdict, "sonnet", "hi").action == "keep"
    c.call("readiness", Verdict, "haiku", "hi")
    s, h = sdk.calls
    for kw in (s, h):
        assert "temperature" not in kw and "top_p" not in kw and "tool_choice" not in kw
        assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert kw["output_format"] is Verdict
    assert s["model"] == SONNET and s["thinking"] == {"type": "adaptive"}
    assert h["model"] == HAIKU and "thinking" not in h
    assert [r[1] for r in rows(conn)] == ["ok", "ok"]


def test_system_prefix_is_long_enough_to_cache():
    from coach.llm.client import system_prompt
    assert len(system_prompt()) / 4 >= 1024  # comfortably over the cacheable minimum


def test_parse_failure_retries_once_then_none(conn):
    bad = SimpleNamespace(parsed_output=None, stop_reason="end_turn", usage=usage())
    sdk = FakeSDK([bad, bad])
    assert client(conn, sdk).call("t", Verdict, "haiku", "x") is None
    assert len(sdk.calls) == 2
    assert [r[1] for r in rows(conn)] == ["parse_error", "parse_error"]


def test_parse_failure_then_success(conn):
    bad = SimpleNamespace(parsed_output=None, stop_reason="end_turn", usage=usage())
    sdk = FakeSDK([bad, ok(Verdict(action="move"))])
    assert client(conn, sdk).call("t", Verdict, "haiku", "x").action == "move"


def test_refusal_returns_none_without_retry(conn):
    sdk = FakeSDK([SimpleNamespace(parsed_output=None, stop_reason="refusal", usage=usage())])
    assert client(conn, sdk).call("t", Verdict, "haiku", "x") is None
    assert len(sdk.calls) == 1
    assert rows(conn)[0][1] == "refusal"


def test_api_error_logged_and_none(conn):
    sdk = FakeSDK([RuntimeError("boom")])
    assert client(conn, sdk).call("t", Verdict, "haiku", "x") is None
    assert rows(conn)[0][1] == "api_error"


def test_budget_exhausted_makes_no_network_call(conn):
    spend(conn, 150.0)
    sdk = FakeSDK([])
    with pytest.raises(budget.BudgetExhausted):
        client(conn, sdk).call("t", Verdict, "haiku", "x")
    assert sdk.calls == []


def test_oversized_prompt_rejected_before_network(conn):
    sdk = FakeSDK([])
    with pytest.raises(ValueError):
        client(conn, sdk).call("t", Verdict, "haiku", "x" * 400_000)
    assert sdk.calls == []


def test_ledger_records_cost(conn):
    sdk = FakeSDK([ok(Verdict(action="keep"), i=1000, o=100)])
    client(conn, sdk).call("t", Verdict, "haiku", "x")
    assert rows(conn)[0][2] == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)


def test_no_key_and_no_sdk_means_unavailable(conn):
    s = SimpleNamespace(anthropic_monthly_budget_usd=100.0, anthropic_api_key=None)
    c = LLMClient(conn, s, now=lambda: NOW)
    assert c.available is False
    assert c.call("t", Verdict, "haiku", "x") is None
