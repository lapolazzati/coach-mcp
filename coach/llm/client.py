"""Thin Claude client (KTD4/KTD5).

Every call is structured (`messages.parse` + a Pydantic schema), budget-gated, and
ledgered one row per attempt. Any failure returns None so callers fall back to the
deterministic rules-only path, which is the oracle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from coach.llm import budget
from coach.llm.budget import HAIKU, SONNET  # re-exported

__all__ = ["LLMClient", "HAIKU", "SONNET", "system_prompt"]

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

MAX_PROMPT_TOKENS = 60_000  # chars/4 estimate; keeps every call far below Haiku's long-context tier


@lru_cache(maxsize=1)
def system_prompt() -> str:
    return resources.files("coach.llm").joinpath("prompts/system.md").read_text(encoding="utf-8")


class LLMClient:
    def __init__(self, conn: sqlite3.Connection, settings, sdk=None,
                 alert: Callable[[str], None] | None = None,
                 now: Callable[[], datetime] | None = None):
        self.conn = conn
        self.budget_usd = float(settings.anthropic_monthly_budget_usd)
        self.alert = alert
        self.now = now or (lambda: datetime.now(timezone.utc))
        if sdk is None and getattr(settings, "anthropic_api_key", None):
            import anthropic
            sdk = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2, timeout=60)
        self.sdk = sdk

    @property
    def available(self) -> bool:
        return self.sdk is not None

    def call(self, task: str, schema: type[T], tier: str, user_content: str,
             max_tokens: int = 2000) -> T | None:
        if not self.available:
            return None
        system = system_prompt()
        if (len(system) + len(user_content)) / 4 > MAX_PROMPT_TOKENS:
            raise ValueError(f"{task}: prompt too large for one call")
        model = budget.check(self.conn, tier, self.now(), self.budget_usd, self.alert)

        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_content}],
            output_format=schema,
        )
        if model == SONNET:
            kwargs["thinking"] = {"type": "adaptive"}

        for _attempt in range(2):
            try:
                resp = self.sdk.messages.parse(**kwargs)
            except (ValidationError, json.JSONDecodeError) as e:
                self._ledger(task, tier, model, None, "parse_error", repr(e)[:500])
                continue
            except Exception as e:  # network, auth, rate limit, server error
                log.warning("claude %s failed: %r", task, e)
                self._ledger(task, tier, model, None, "api_error", repr(e)[:500])
                return None
            if resp.stop_reason == "refusal":
                self._ledger(task, tier, model, resp.usage, "refusal", None)
                return None
            parsed = getattr(resp, "parsed_output", None)
            if parsed is None:
                self._ledger(task, tier, model, resp.usage, "parse_error", f"stop_reason={resp.stop_reason}")
                continue
            self._ledger(task, tier, model, resp.usage, "ok", None)
            return parsed
        return None

    def _ledger(self, task, tier, model, usage, status, error):
        g = lambda n: int(getattr(usage, n, 0) or 0) if usage is not None else 0  # noqa: E731
        cost = budget.cost_usd(model, usage) if usage is not None else 0.0
        self.conn.execute(
            "INSERT INTO llm_call (ts_utc, task, tier_requested, model, input_tokens, output_tokens,"
            " cache_write_tokens, cache_read_tokens, cost_usd, status, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.now().astimezone(timezone.utc).isoformat(), task, tier, model,
             g("input_tokens"), g("output_tokens"), g("cache_creation_input_tokens"),
             g("cache_read_input_tokens"), cost, status, error),
        )
        self.conn.commit()
