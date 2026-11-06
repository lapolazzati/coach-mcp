"""Monthly Claude spend ledger and gate (KTD5, R30).

Spend is the sum of `llm_call.cost_usd` for the current calendar month in Europe/Rome.
At 80% Sonnet is routed to Haiku and Lapo is alerted once per month; at 100% every call
is refused before it reaches the network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
TIERS = {"sonnet": SONNET, "haiku": HAIKU}

# USD per million tokens: (input, output). Haiku has a long-context tier above
# 100K total input tokens (input + cache write + cache read) that prices every token.
PRICES: dict[str, list[tuple[int, float, float]]] = {
    SONNET: [(0, 2.00, 10.00)],
    HAIKU: [(0, 0.10, 0.50), (100_001, 0.50, 2.50)],
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10
ALERT_FRACTION = 0.80


class BudgetExhausted(RuntimeError):
    """Monthly cap reached: no Claude call may be made until the month rolls over."""


def _u(usage, name: str) -> int:
    return int(getattr(usage, name, 0) or 0)


def cost_usd(model: str, usage) -> float:
    tiers = PRICES[model]  # KeyError for an unknown model is deliberate
    i, o = _u(usage, "input_tokens"), _u(usage, "output_tokens")
    cw, cr = _u(usage, "cache_creation_input_tokens"), _u(usage, "cache_read_input_tokens")
    total_in = i + cw + cr
    p_in, p_out = next((pi, po) for floor, pi, po in reversed(tiers) if total_in >= floor)
    return (i * p_in + o * p_out + cw * p_in * CACHE_WRITE_MULT + cr * p_in * CACHE_READ_MULT) / 1e6


def _month_bounds_utc(now: datetime) -> tuple[str, str, str]:
    local = now.astimezone(ROME)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    to_utc = lambda d: d.astimezone(timezone.utc).isoformat()  # noqa: E731
    return to_utc(start), to_utc(nxt), f"{start.year:04d}-{start.month:02d}"


def month_to_date_spend(conn: sqlite3.Connection, now: datetime) -> float:
    lo, hi, _ = _month_bounds_utc(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_call WHERE ts_utc >= ? AND ts_utc < ?", (lo, hi)
    ).fetchone()
    return float(row[0])


def check(conn: sqlite3.Connection, tier: str, now: datetime, budget: float,
          alert: Callable[[str], None] | None) -> str:
    """Return the model to use for `tier`, or raise BudgetExhausted."""
    spent = month_to_date_spend(conn, now)
    if spent >= budget:
        raise BudgetExhausted(f"Claude budget used: ${spent:.2f} of ${budget:.2f} this month")
    model = TIERS[tier]
    if spent >= ALERT_FRACTION * budget:
        model = HAIKU
        _, _, month = _month_bounds_utc(now)
        key = f"budget_alert:{month}"
        if conn.execute("SELECT 1 FROM sync_state WHERE key = ?", (key,)).fetchone() is None:
            conn.execute("INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?)",
                         (key, f"{spent:.2f}", now.astimezone(timezone.utc).isoformat()))
            conn.commit()
            if alert:
                alert(f"Claude spend is ${spent:.2f} of ${budget:.0f} this month. "
                      "Switching to the cheaper model until the 1st.")
    return model
