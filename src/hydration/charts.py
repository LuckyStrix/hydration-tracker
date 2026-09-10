"""Charts, rendered as inline SVG on the server.

No chart library and no JavaScript, for a reason that is worth stating: the
Content-Security-Policy this app ships has `script-src 'none'`, so there is no
JS to hang a charting library on. That turns out to be a feature rather than a
constraint -- the pages render fully on first paint, work with JS disabled, and
print correctly.

Two conventions make that work:

  * **Colour lives in CSS, not here.** Every mark carries a `class`, and
    `app.css` defines the fills and strokes for both light and dark. One render
    is correct in either theme, and the palette can be changed without touching
    Python. Presentation attributes and classes only -- never a `style=`
    attribute, which the CSP would drop.
  * **Tooltips are `<title>` children.** Browsers show them natively on hover.
    It is the whole interaction layer available without script, and it is
    enough: every mark can say what it is.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PLOT = dict(width=880, height=260, left=54, right=16, top=16, bottom=30)


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


@dataclass
class Box:
    """Plot geometry and the two scale functions built from it."""

    width: int
    height: int
    left: int
    right: int
    top: int
    bottom: int

    @property
    def inner_w(self) -> float:
        return self.width - self.left - self.right

    @property
    def inner_h(self) -> float:
        return self.height - self.top - self.bottom


def _box(**overrides) -> Box:
    return Box(**{**PLOT, **overrides})


def _open(box: Box, label: str) -> list[str]:
    """Open an SVG with an accessible name.

    role="img" plus a <title> is what makes a chart announce itself to a screen
    reader as one object rather than a pile of unlabelled shapes.
    """
    return [
        f'<svg class="chart" viewBox="0 0 {box.width} {box.height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="{_esc(label)}">',
        f"<title>{_esc(label)}</title>",
    ]


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to something a person would have chosen."""
    if value <= 0:
        return 1.0
    for step in (0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        if value <= step:
            return float(step)
    return float(int(value / 100 + 1) * 100)


def _time_ticks(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[datetime, str]]:
    """Pick readable time labels for whatever span was asked for."""
    span_h = (end - start).total_seconds() / 3600.0
    ticks: list[tuple[datetime, str]] = []
    if span_h <= 30:
        step = 3 if span_h <= 14 else 6
        cursor = start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        while cursor <= end.astimezone(tz):
            if cursor >= start.astimezone(tz) and cursor.hour % step == 0:
                ticks.append((cursor, cursor.strftime("%-I%p").lower()))
            cursor += timedelta(hours=1)
    else:
        days = max(1, int(span_h / 24 / 7) or 1)
        cursor = start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= end.astimezone(tz):
            if cursor >= start.astimezone(tz):
                ticks.append((cursor, cursor.strftime("%-d %b")))
            cursor += timedelta(days=days)
    return ticks[:12]


def _axes(box: Box, ticks: list[tuple[float, str]], x_ticks: list[tuple[float, str]]) -> list[str]:
    """Recessive grid and axis labels. Hairlines, muted ink, never competing
    with the data."""
    parts = []
    for y, label in ticks:
        parts.append(
            f'<line class="grid" x1="{box.left}" y1="{y:.1f}" x2="{box.width - box.right}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{box.left - 8}" y="{y + 4:.1f}" text-anchor="end">{_esc(label)}</text>'
        )
    for x, label in x_ticks:
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{box.height - box.bottom + 18}" '
            f'text-anchor="middle">{_esc(label)}</text>'
        )
    parts.append(
        f'<line class="axis" x1="{box.left}" y1="{box.height - box.bottom}" '
        f'x2="{box.width - box.right}" y2="{box.height - box.bottom}"/>'
    )
    return parts


def _empty(box: Box, message: str) -> str:
    return "".join(
        _open(box, message)
        + [
            f'<text class="empty" x="{box.width / 2}" y="{box.height / 2}" '
            f'text-anchor="middle">{_esc(message)}</text>',
            "</svg>",
        ]
    )


# -- the deficit chart -----------------------------------------------------

def deficit_chart(timeline, corrections, tz: ZoneInfo, *, body_mass_kg: float) -> str:
    """Body water over time, with the moments an observation corrected it.

    The single most important chart in the app, and the one that earns the
    whole design: the line is what the ledger believed, and each marker is a
    urine colour or a morning weight disagreeing with it. Seeing the two
    together is how you judge whether the model suits you.

    Deficit is a polarity measure -- dry above the line, surplus below -- so the
    zero rule is emphasised and the background carries the impairment
    thresholds as status bands rather than as more series colours.
    """
    box = _box()
    samples = timeline.samples
    if not samples:
        return _empty(box, "No data yet")

    start, end = samples[0].at, samples[-1].at
    span = max((end - start).total_seconds(), 1.0)

    values_pct = [s.deficit_ml / 1000.0 / body_mass_kg * 100.0 for s in samples]
    high = max(2.2, max(values_pct) * 1.15)
    low = min(-0.8, min(values_pct) * 1.15)

    def x_of(moment: datetime) -> float:
        return box.left + (moment - start).total_seconds() / span * box.inner_w

    def y_of(pct: float) -> float:
        return box.top + (high - pct) / (high - low) * box.inner_h

    parts = _open(box, "Body water deficit over time, with observed corrections")

    # Impairment bands. Status colours, because these genuinely mean
    # 'fine / noticeable / impaired' rather than identifying a series.
    for lo, hi, css in ((1.0, 2.0, "band-warning"), (2.0, high, "band-serious")):
        if hi > lo:
            y_top, y_bottom = y_of(hi), y_of(lo)
            parts.append(
                f'<rect class="{css}" x="{box.left}" y="{y_top:.1f}" '
                f'width="{box.inner_w:.1f}" height="{max(0, y_bottom - y_top):.1f}"/>'
            )

    y_ticks = []
    tick = low
    while tick <= high + 1e-9:
        if abs(tick - round(tick * 2) / 2) < 1e-9:
            y_ticks.append((y_of(tick), f"{tick:+.1f}%"))
        tick += 0.5
    x_ticks = [(x_of(moment.astimezone(timezone.utc)), label) for moment, label in _time_ticks(start, end, tz)]
    parts += _axes(box, y_ticks, x_ticks)

    # The zero rule -- euhydrated. Emphasised over the ordinary gridlines
    # because every reading is read relative to it.
    parts.append(
        f'<line class="zero" x1="{box.left}" y1="{y_of(0):.1f}" '
        f'x2="{box.width - box.right}" y2="{y_of(0):.1f}"/>'
    )

    points = " ".join(f"{x_of(s.at):.1f},{y_of(pct):.1f}" for s, pct in zip(samples, values_pct, strict=True))
    parts.append(f'<polyline class="series-1-line" points="{points}"/>')

    for correction in corrections:
        if not (start <= correction.at <= end):
            continue
        x = x_of(correction.at)
        observed_pct = correction.observed_ml / 1000.0 / body_mass_kg * 100.0
        blended_pct = correction.blended_ml / 1000.0 / body_mass_kg * 100.0
        css = "mark-urine" if correction.kind == "urine" else "mark-weight"
        tip = (
            f"{correction.kind}: ledger said {correction.ledger_ml / 1000:+.2f} L, "
            f"observation said {correction.observed_ml / 1000:+.2f} L, "
            f"settled at {correction.blended_ml / 1000:+.2f} L "
            f"(confidence {correction.confidence:.0%}). "
            + "; ".join(correction.reasons)
        )
        parts.append(
            f'<line class="correction-link" x1="{x:.1f}" y1="{y_of(observed_pct):.1f}" '
            f'x2="{x:.1f}" y2="{y_of(blended_pct):.1f}"><title>{_esc(tip)}</title></line>'
        )
        parts.append(
            f'<circle class="{css}" cx="{x:.1f}" cy="{y_of(observed_pct):.1f}" r="5">'
            f"<title>{_esc(tip)}</title></circle>"
        )

    parts.append("</svg>")
    return "".join(parts)


# -- daily bars ------------------------------------------------------------

def intake_by_beverage_chart(days: list[dict], tz: ZoneInfo) -> str:
    """Intake per day, stacked by what it was.

    Capped at the first seven beverages by volume with the tail folded into
    'Other' -- generating an eighth and ninth hue would produce colours nothing
    can reliably tell apart.
    """
    box = _box(height=240)
    if not days:
        return _empty(box, "No drinks logged yet")

    totals: dict[str, float] = {}
    for day in days:
        for name, ml in day["by_beverage"].items():
            totals[name] = totals.get(name, 0.0) + ml
    ranked = sorted(totals, key=lambda name: totals[name], reverse=True)
    named, tail = ranked[:7], set(ranked[7:])

    high = _nice_ceiling(max((day["total_ml"] for day in days), default=0) / 1000.0)
    slot_w = box.inner_w / max(len(days), 1)
    bar_w = min(38.0, slot_w * 0.62)

    parts = _open(box, "Daily intake by beverage")
    y_ticks = [
        (box.top + box.inner_h * (1 - fraction), f"{high * fraction:.1f} L")
        for fraction in (0, 0.25, 0.5, 0.75, 1.0)
    ]
    x_ticks = [
        (box.left + slot_w * (index + 0.5), day["date"].strftime("%-d %b"))
        for index, day in enumerate(days)
        if len(days) <= 10 or index % max(1, len(days) // 8) == 0
    ]
    parts += _axes(box, y_ticks, x_ticks)

    for index, day in enumerate(days):
        x = box.left + slot_w * (index + 0.5) - bar_w / 2
        cursor = box.height - box.bottom
        stack: list[tuple[str, float]] = [
            (name, ml) for name, ml in day["by_beverage"].items() if name in named
        ]
        other = sum(ml for name, ml in day["by_beverage"].items() if name in tail)
        if other:
            stack.append(("Other", other))
        for name, ml in sorted(stack, key=lambda pair: -pair[1]):
            height = ml / 1000.0 / high * box.inner_h if high else 0
            if height < 0.5:
                continue
            slot = named.index(name) + 1 if name in named else 8
            cursor -= height
            # The 2px trimmed off the height is the surface gap that keeps
            # adjacent stack segments from reading as one solid block.
            tip = f"{day['date']:%a %-d %b} - {name}: {ml / 1000:.2f} L"
            parts.append(
                f'<rect class="series-{slot}-fill bar" x="{x:.1f}" y="{cursor:.1f}" '
                f'width="{bar_w:.1f}" height="{max(0, height - 2):.1f}" rx="3">'
                f'<title>{_esc(tip)}</title></rect>'
            )

    parts.append("</svg>")
    entries = [(name, f"series-{index + 1}") for index, name in enumerate(named)]
    if tail:
        entries.append(("Other", "series-8"))
    return "".join(parts) + _legend(entries)


def sweat_vs_intake_chart(days: list[dict], tz: ZoneInfo) -> str:
    """Two series, one axis -- both are millilitres of water, so they belong on
    the same scale. A second y-axis here would invent a relationship."""
    box = _box(height=240)
    if not days:
        return _empty(box, "No data yet")

    high = _nice_ceiling(
        max((max(day["total_ml"], day["sweat_ml"]) for day in days), default=0) / 1000.0
    )
    slot_w = box.inner_w / max(len(days), 1)
    bar_w = min(16.0, slot_w * 0.3)

    parts = _open(box, "Fluid drunk against sweat lost, per day")
    y_ticks = [
        (box.top + box.inner_h * (1 - fraction), f"{high * fraction:.1f} L")
        for fraction in (0, 0.25, 0.5, 0.75, 1.0)
    ]
    x_ticks = [
        (box.left + slot_w * (index + 0.5), day["date"].strftime("%-d %b"))
        for index, day in enumerate(days)
        if len(days) <= 10 or index % max(1, len(days) // 8) == 0
    ]
    parts += _axes(box, y_ticks, x_ticks)

    for index, day in enumerate(days):
        centre = box.left + slot_w * (index + 0.5)
        for offset, value, slot, label in (
            (-bar_w - 1, day["total_ml"], 1, "drunk"),
            (1, day["sweat_ml"], 2, "sweated"),
        ):
            height = value / 1000.0 / high * box.inner_h if high else 0
            if height < 0.5:
                continue
            tip = f"{day['date']:%a %-d %b} - {label}: {value / 1000:.2f} L"
            parts.append(
                f'<rect class="series-{slot}-fill bar" x="{centre + offset:.1f}" '
                f'y="{box.height - box.bottom - height:.1f}" width="{bar_w:.1f}" '
                f'height="{height:.1f}" rx="3">'
                f'<title>{_esc(tip)}</title></rect>'
            )

    parts.append("</svg>")
    return "".join(parts) + _legend([("Drunk", "series-1"), ("Sweated", "series-2")])


# -- urine colour ----------------------------------------------------------

def urine_chart(voids: list[dict], tz: ZoneInfo, start: datetime, end: datetime) -> str:
    """Urine colour over time.

    The one chart here that uses the colour of the thing being measured. That
    is not a rainbow ramp smuggled in: the y-axis *is* the colour chart, an
    ordered physical scale from pale to amber, and reproducing it is what makes
    a point readable without consulting a legend. Low-confidence readings are
    drawn hollow, so a first-morning void is visibly not the same evidence as a
    mid-afternoon one.
    """
    box = _box(height=200)
    if not voids:
        return _empty(box, "No voids logged yet")

    span = max((end - start).total_seconds(), 1.0)

    def x_of(moment: datetime) -> float:
        return box.left + (moment - start).total_seconds() / span * box.inner_w

    def y_of(colour: float) -> float:
        return box.top + (colour - 0.5) / 8.0 * box.inner_h

    parts = _open(box, "Urine colour over time")
    y_ticks = [(y_of(colour), str(colour)) for colour in range(1, 9)]
    x_ticks = [(x_of(moment.astimezone(timezone.utc)), label) for moment, label in _time_ticks(start, end, tz)]
    parts += _axes(box, y_ticks, x_ticks)

    for colour in range(1, 9):
        parts.append(
            f'<rect class="urine-swatch urine-{colour}" x="{box.left - 44}" '
            f'y="{y_of(colour) - 7:.1f}" width="14" height="14" rx="3"/>'
        )

    for entry in voids:
        moment = entry["at"]
        if not (start <= moment <= end):
            continue
        css = "void-dot-faded" if entry.get("low_confidence") else "void-dot"
        tip = f"{entry['label']} - colour {entry['colour']}"
        if entry.get("why"):
            tip += f" ({entry['why']})"
        colour = entry["colour"]
        parts.append(
            f'<circle class="{css} urine-fill-{colour}" cx="{x_of(moment):.1f}" '
            f'cy="{y_of(colour):.1f}" r="6"><title>{_esc(tip)}</title></circle>'
        )

    parts.append("</svg>")
    return "".join(parts)


# -- weight ----------------------------------------------------------------

def weight_chart(points: list[dict], tz: ZoneInfo) -> str:
    """Morning weight against its own smoothed trend.

    Two series, so a legend. The gap between them is the point: the trend is
    body mass, and the distance from it is water.
    """
    box = _box(height=220)
    if len(points) < 2:
        return _empty(box, "Two or more morning weights needed for a trend")

    start, end = points[0]["at"], points[-1]["at"]
    span = max((end - start).total_seconds(), 1.0)
    masses = [p["lb"] for p in points] + [p["trend_lb"] for p in points]
    low, high = min(masses) - 1.0, max(masses) + 1.0

    def x_of(moment: datetime) -> float:
        return box.left + (moment - start).total_seconds() / span * box.inner_w

    def y_of(lb: float) -> float:
        return box.top + (high - lb) / (high - low) * box.inner_h

    parts = _open(box, "Morning weight against its trend")
    steps = 4
    y_ticks = [
        (y_of(low + (high - low) * i / steps), f"{low + (high - low) * i / steps:.1f} lb")
        for i in range(steps + 1)
    ]
    x_ticks = [(x_of(moment.astimezone(timezone.utc)), label) for moment, label in _time_ticks(start, end, tz)]
    parts += _axes(box, y_ticks, x_ticks)

    trend = " ".join(f"{x_of(p['at']):.1f},{y_of(p['trend_lb']):.1f}" for p in points)
    parts.append(f'<polyline class="series-3-line trend" points="{trend}"/>')

    for point in points:
        cx, cy = x_of(point["at"]), y_of(point["lb"])
        tip = f"{point['label']}: {point['lb']:.1f} lb, trend {point['trend_lb']:.1f} lb"
        parts.append(
            f'<circle class="series-1-dot" cx="{cx:.1f}" cy="{cy:.1f}" r="4">'
            f'<title>{_esc(tip)}</title></circle>'
        )

    parts.append("</svg>")
    return "".join(parts) + _legend([("Morning weight", "series-1"), ("7-day trend", "series-3")])


# -- shared ----------------------------------------------------------------

def _legend(entries: list[tuple[str, str]]) -> str:
    """A legend is always present for two or more series -- identity must never
    rest on colour alone."""
    items = "".join(
        f'<li><span class="swatch {_esc(css)}-fill"></span>{_esc(label)}</li>' for label, css in entries
    )
    return f'<ul class="legend">{items}</ul>'
