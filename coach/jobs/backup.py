"""Nightly backup slot (03:15). A stub until the backup unit lands: it only logs."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


def run(now: datetime) -> dict[str, Any]:
    log.info("backup: stub, nothing to do at %s", now.isoformat())
    return {"stub": True}
