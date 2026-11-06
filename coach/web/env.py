"""The one Jinja environment for the dashboard, shared by the app and the preview script."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from coach.web.charts import Scale, area_path, fmt_hm, line_path, to_min

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(line_path=line_path, area_path=area_path, scale=Scale,
                       fmt_hm=fmt_hm, to_min=to_min)
    return env
