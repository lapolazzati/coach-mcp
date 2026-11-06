"""Server-side SVG geometry for the dashboard.

Charts are drawn in Jinja from these pre-scaled coordinates, so the page
ships no charting library and renders fully without JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scale:
    """Maps a value domain onto an SVG pixel range."""

    d0: float
    d1: float
    r0: float
    r1: float

    def __call__(self, v: float) -> float:
        if self.d1 == self.d0:
            return (self.r0 + self.r1) / 2
        return round(self.r0 + (v - self.d0) / (self.d1 - self.d0) * (self.r1 - self.r0), 1)


def line_path(values: list[float | None], width: float, height: float,
              vmin: float, vmax: float, pad: float = 2) -> str:
    """Polyline as an SVG path. Gaps (None) break the line instead of interpolating."""
    if not values:
        return ""
    x = Scale(0, max(len(values) - 1, 1), 0, width)
    y = Scale(vmin, vmax, height - pad, pad)
    out, pen_down = [], False
    for i, v in enumerate(values):
        if v is None:
            pen_down = False
            continue
        out.append(f"{'L' if pen_down else 'M'}{x(i)} {y(v)}")
        pen_down = True
    return " ".join(out)


def to_min(hm: str) -> int:
    """'3:30' -> 210."""
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def fmt_hm(minutes: float) -> str:
    """218 -> '3:38'."""
    m = int(round(minutes))
    return f"{m // 60}:{m % 60:02d}"


def area_path(values: list[float], width: float, height: float,
              vmin: float, vmax: float) -> str:
    """Closed area under a series, for the course profile."""
    line = line_path(values, width, height, vmin, vmax, pad=0)
    return f"{line} L{width} {height} L0 {height} Z" if line else ""
