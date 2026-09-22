"""Read-only aggregation for the history and insights pages.

Kept out of `service.py` so that file stays about mutations and about handing
the model its inputs. Nothing here writes.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, service, units
from .model import balance as B
from .model import constants as k
from .model import urine as urine_model


def _local_day(moment: str, tz: ZoneInfo) -> date:
    return db.from_iso(moment).astimezone(tz).date()


def daily_summaries(
    connection: sqlite3.Connection, start: datetime, end: datetime, tz: ZoneInfo
) -> list[dict]:
    """Per-local-day totals for the bar charts.

    Bucketed by *local* day, not by UTC day. A drink at 11pm Eastern is stored
    as 03:00 the next day in UTC, and grouping on the stored string would file
    it under tomorrow -- which is the kind of quiet error that makes a chart
    look merely a bit odd rather than obviously broken.

    The query window is widened to the local midnight that opens the first
    bucket. `start` is an instant, usually "now minus N days", which lands in
    the middle of a local day -- so without this the earliest bar held only the
    hours after it and was drawn full height beside complete days. A day that
    looked like half the drinking of its neighbours, every time, for no reason
    anyone could see from the chart.
    """
    buckets: dict[date, dict] = {}

    window_start = datetime.combine(start.astimezone(tz).date(), time.min, tzinfo=tz)
    lo, hi = db.to_iso(window_start), db.to_iso(end)

    cursor = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while cursor <= last:
        buckets[cursor] = {
            "date": cursor,
            "total_ml": 0.0,
            "sweat_ml": 0.0,
            "by_beverage": {},
            "sodium_in_mg": 0.0,
            "sodium_sweat_mg": 0.0,
            "voids": 0,
        }
        cursor += timedelta(days=1)

    profile = service.load_profile(connection)

    for row in connection.execute(
        """
        SELECT i.at, i.volume_ml, i.sodium_mg, b.name, b.sodium_mg_per_l
        FROM intake i JOIN beverage b ON b.id = i.beverage_id
        WHERE i.voided_at IS NULL AND i.at BETWEEN ? AND ?
        """,
        (lo, hi),
    ):
        day = buckets.get(_local_day(row["at"], tz))
        if day is None:
            continue
        day["total_ml"] += row["volume_ml"]
        day["by_beverage"][row["name"]] = day["by_beverage"].get(row["name"], 0.0) + row["volume_ml"]
        sodium = (
            row["sodium_mg"]
            if row["sodium_mg"] is not None
            else row["volume_ml"] / 1000.0 * row["sodium_mg_per_l"]
        )
        day["sodium_in_mg"] += sodium

    for row in connection.execute(
        "SELECT started_at, sweat_ml_used FROM activity WHERE voided_at IS NULL AND started_at BETWEEN ? AND ?",
        (lo, hi),
    ):
        day = buckets.get(_local_day(row["started_at"], tz))
        if day is None:
            continue
        day["sweat_ml"] += row["sweat_ml_used"] or 0.0
        day["sodium_sweat_mg"] += (
            (row["sweat_ml_used"] or 0.0) / 1000.0 * profile.sweat_sodium_mmol_l * k.MG_PER_MMOL_SODIUM
        )

    for row in connection.execute(
        "SELECT at FROM void WHERE voided_at IS NULL AND at BETWEEN ? AND ?", (lo, hi)
    ):
        day = buckets.get(_local_day(row["at"], tz))
        if day is not None:
            day["voids"] += 1

    return [buckets[key] for key in sorted(buckets)]


def intake_today_ml(connection: sqlite3.Connection, tz: ZoneInfo, now: datetime) -> float:
    """Millilitres logged since local midnight, for a tile actually labelled 'today'.

    Deliberately not `timeline.delta("intake_ml", 24.0)`: the model only knows
    elapsed hours, not local calendar days, so its "last 24h" and "since local
    midnight" diverge every time some of today's drinking happened yesterday
    (or none of it has happened yet) -- e.g. at 6am the rolling window is still
    full of last night's intake, which is not what 'drunk today' means to a
    person reading the homepage.
    """
    start_of_day = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    lo, hi = db.to_iso(start_of_day.astimezone(timezone.utc)), db.to_iso(now)
    row = connection.execute(
        "SELECT COALESCE(SUM(volume_ml), 0.0) AS total FROM intake WHERE voided_at IS NULL AND at BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    return row["total"]


def void_points(connection: sqlite3.Connection, start: datetime, end: datetime, tz: ZoneInfo) -> list[dict]:
    """Voids shaped for the colour chart, carrying why each was trusted."""
    events = service.build_events(connection, start - timedelta(hours=12), end)

    # One pass for every void's timing context, then a lookup per row. Deriving
    # each context separately is O(voids x events) and was the single largest
    # cost on the history page.
    readings = {
        events[index].at: urine_model.read(context)
        for index, context in B.build_void_contexts(events).items()
    }

    points: list[dict] = []
    for row in connection.execute(
        "SELECT * FROM void WHERE voided_at IS NULL AND at BETWEEN ? AND ? ORDER BY at",
        (db.to_iso(start), db.to_iso(end)),
    ):
        moment = db.from_iso(row["at"])
        reading = readings.get(moment)
        if reading is None:
            # A void the event window did not reach. Read it with no timing
            # context rather than dropping it off the chart.
            reading = urine_model.read(
                urine_model.VoidContext(
                    colour=row["colour"], at=moment, is_first_morning=bool(row["is_first_morning"])
                )
            )
        points.append(
            {
                "at": moment,
                "colour": row["colour"],
                "label": moment.astimezone(tz).strftime("%a %-d %b, %-I:%M %p"),
                "confidence": reading.confidence,
                "low_confidence": reading.confidence < 0.5,
                "why": reading.reasons[0] if reading.reasons else "",
                "volume_ml": row["volume_ml"],
                "id": row["id"],
            }
        )
    return points


def weight_points(connection: sqlite3.Connection, start: datetime, end: datetime, tz: ZoneInfo) -> list[dict]:
    """Morning weights with the running trend the observer compares against."""
    rows = connection.execute(
        """
        SELECT at, mass_kg FROM body_weight
        WHERE voided_at IS NULL AND context = 'morning' AND at BETWEEN ? AND ?
        ORDER BY at
        """,
        (db.to_iso(start), db.to_iso(end)),
    ).fetchall()
    if not rows:
        return []

    profile = service.load_profile(connection)
    trend = service._weight_trend_before(connection, db.from_iso(rows[0]["at"]), profile)
    alpha = 1.0 - 0.5 ** (1.0 / k.WEIGHT_TREND_HALFLIFE_DAYS)

    points = []
    for row in rows:
        mass = row["mass_kg"]
        trend = mass if trend is None else alpha * mass + (1.0 - alpha) * trend
        moment = db.from_iso(row["at"])
        points.append(
            {
                "at": moment,
                "kg": mass,
                "lb": units.kg_to_lb(mass),
                "trend_lb": units.kg_to_lb(trend),
                "label": moment.astimezone(tz).strftime("%a %-d %b"),
            }
        )
    return points


def recent_activities(connection: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM activity WHERE voided_at IS NULL ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def timeline_entries(connection: sqlite3.Connection, start: datetime, end: datetime) -> list[dict]:
    """Everything logged in a window, newest first, for the day view.

    One UNION rather than five queries and a merge sort in Python: the tables
    have nothing in common but a timestamp, and interleaving them correctly is
    exactly what the database is for.
    """
    lo, hi = db.to_iso(start), db.to_iso(end)
    rows = connection.execute(
        """
        SELECT i.at AS at, 'intake' AS kind, b.name AS label,
               i.volume_ml AS amount, i.id AS row_id, i.note AS note, i.source AS source
          FROM intake i JOIN beverage b ON b.id = i.beverage_id
         WHERE i.voided_at IS NULL AND i.at BETWEEN :lo AND :hi
        UNION ALL
        SELECT at, 'void', 'Colour ' || colour, colour, id, note, source
          FROM void WHERE voided_at IS NULL AND at BETWEEN :lo AND :hi
        UNION ALL
        SELECT at, 'weight', context, mass_kg, id, NULL, source
          FROM body_weight WHERE voided_at IS NULL AND at BETWEEN :lo AND :hi
        UNION ALL
        SELECT started_at, 'activity', COALESCE(name, activity_type, 'Activity'),
               sweat_ml_used, id, sweat_source, provider
          FROM activity WHERE voided_at IS NULL AND started_at BETWEEN :lo AND :hi
        UNION ALL
        SELECT at, 'symptom', kind, severity, id, note, source
          FROM symptom WHERE voided_at IS NULL AND at BETWEEN :lo AND :hi
        UNION ALL
        SELECT at, 'meal', COALESCE(label, 'Meal'), water_ml, id, NULL, source
          FROM meal WHERE voided_at IS NULL AND at BETWEEN :lo AND :hi
        ORDER BY at DESC
        """,
        {"lo": lo, "hi": hi},
    ).fetchall()
    return [
        {
            "at": db.from_iso(row["at"]),
            "kind": row["kind"],
            "label": row["label"],
            "amount": row["amount"],
            "row_id": row["row_id"],
            "note": row["note"],
            "source": row["source"],
        }
        for row in rows
    ]


# -- insights --------------------------------------------------------------

def model_bias(connection: sqlite3.Connection, days: int = 30) -> dict:
    """How far the ledger sits from what the observations said.

    The single most useful diagnostic in the app. A persistent positive bias
    means the ledger runs drier than reality and the insensible or obligatory
    constants are too high for this person; a negative one means the reverse.
    Without this the model is unfalsifiable, which for something giving health
    advice is not an acceptable place to leave it.
    """
    end = datetime.now(timezone.utc)
    timeline = service.timeline_for(connection, start=end - timedelta(days=days), end=end)
    corrections = timeline.corrections
    if not corrections:
        return {"count": 0}

    shifts = [c.ledger_ml - c.observed_ml for c in corrections]
    mean = sum(shifts) / len(shifts)
    return {
        "count": len(shifts),
        "mean_ml": mean,
        "abs_mean_ml": sum(abs(s) for s in shifts) / len(shifts),
        "direction": "drier" if mean > 0 else "wetter",
        "reading": (
            "The ledger tracks the observations closely."
            if abs(mean) < 250
            else (
                f"The ledger reads about {abs(mean) / 1000:.2f} L {'drier' if mean > 0 else 'wetter'} "
                f"than colour and weight suggest. Persistent bias in one direction means a model "
                f"constant does not suit you -- see docs/tuning.md."
            )
        ),
    }


def deficit_by_hour(connection: sqlite3.Connection, tz: ZoneInfo, days: int = 30) -> list[dict]:
    """Average deficit by hour of the local day.

    Answers 'when do I fall behind', which is the question that actually
    changes behaviour -- a person who is reliably 1 L down at 3pm needs a
    different habit, not a bigger bottle at 6pm.
    """
    end = datetime.now(timezone.utc)
    timeline = service.timeline_for(connection, start=end - timedelta(days=days), end=end)
    profile = timeline.profile

    buckets: dict[int, list[float]] = {hour: [] for hour in range(24)}
    for sample in timeline.samples:
        buckets[sample.at.astimezone(tz).hour].append(profile.deficit_pct(sample.deficit_ml))

    return [
        {
            "hour": hour,
            "label": datetime(2000, 1, 1, hour).strftime("%-I%p").lower(),
            "mean_pct": sum(values) / len(values) if values else 0.0,
        }
        for hour, values in sorted(buckets.items())
    ]
