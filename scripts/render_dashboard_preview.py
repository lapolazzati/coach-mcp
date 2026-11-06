"""Render the dashboard template with SAMPLE data to static HTML for review.

    python scripts/render_dashboard_preview.py

Writes .impeccable/review/dashboard.html (full sample) and dashboard-day1.html
(empty first-day state). Needs only jinja2.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coach.web.env import STATIC_DIR, build_env
from coach.web.sample_context import sample_context

OUT = ROOT / ".impeccable" / "review"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy(STATIC_DIR / "dashboard.css", OUT / "dashboard.css")
    shutil.copytree(STATIC_DIR / "fonts", OUT / "fonts", dirs_exist_ok=True)
    tpl = build_env().get_template("dashboard.html")
    for state, name in (("full", "dashboard.html"), ("day1", "dashboard-day1.html")):
        html = tpl.render(static_url=".", **sample_context(state))
        (OUT / name).write_text(html, encoding="utf-8")
        print(OUT / name)


if __name__ == "__main__":
    main()
