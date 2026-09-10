"""Every mutation, and the reads that assemble the model's inputs.

The rule, borrowed from the stockroom app and worth just as much here: nothing
outside this module writes to the tables. The web layer parses and renders, the
model computes, and this is the only place that decides what a row means.

The other half of the file is `build_events` and `current_state`, which turn
stored rows into the dataclasses `model.balance` wants. That translation is the
seam between "what was logged" and "what it implies", and keeping it in one
function is what stops the two drifting apart.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db
from .errors import ConflictError, NotFound, ValidationError
from .model import balance as B
from .model import constants as k
from .model import plan as P
from .model import sweat as sweat_model

ETHANOL_G_PER_L = 789.0
"""Density of ethanol. Turns 'a 4.5% pint' into grams, which is what the
diuresis term is expressed in."""

FIRST_MORNING_GAP_H = 5.0
FIRST_MORNING_WAKE_WINDOW_H = 3.0
"""What counts as 'the void that followed a night's sleep': a long gap, at
roughly the hour you get up. Deliberately conservative -- failing to flag one
costs a little accuracy, while wrongly flagging a mid-afternoon void discounts
a perfectly good reading."""


# -- profile ---------------------------------------------------------------

PROFILE_FIELDS = {
    "display_name", "body_mass_kg", "height_cm", "sex", "birth_year", "timezone",
    "wake_hour", "bed_hour", "sweat_sodium_mmol_l", "sweat_calibration",
    "sweat_calibration_n", "baseline_loss_scale", "feedback_n",
    "absorption_cap_ml_h", "food_water_ml_day",
    "caffeine_diuresis_ml_mg", "trust_urine", "default_temp_c", "default_humidity_pct",
    "volume_entry_unit",
}


def profile_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row is None:
        raise NotFound("no profile row; the database was not initialised")
    return row


def load_profile(connection: sqlite3.Connection) -> B.Profile:
    """The stored profile as the dataclass the model takes."""
    row = profile_row(connection)
    return B.Profile(
        body_mass_kg=row["body_mass_kg"],
        height_cm=row["height_cm"],
        sex=row["sex"],
        birth_year=row["birth_year"],
        timezone=row["timezone"],
        wake_hour=row["wake_hour"],
        bed_hour=row["bed_hour"],
        sweat_sodium_mmol_l=row["sweat_sodium_mmol_l"],
        sweat_calibration=row["sweat_calibration"],
        baseline_loss_scale=row["baseline_loss_scale"],
        absorption_cap_ml_h=row["absorption_cap_ml_h"],
        food_water_ml_day=row["food_water_ml_day"],
        caffeine_diuresis_ml_per_mg=row["caffeine_diuresis_ml_mg"],
        trust_urine=row["trust_urine"],
        default_temp_c=row["default_temp_c"],
        default_humidity_pct=row["default_humidity_pct"],
    )


def save_profile(connection: sqlite3.Connection, **fields) -> None:
    unknown = set(fields) - PROFILE_FIELDS
    if unknown:
        raise ValidationError(f"unknown profile field(s): {', '.join(sorted(unknown))}")
    if not fields:
        return
    if "timezone" in fields:
        try:
            ZoneInfo(fields["timezone"])
        except Exception:
            raise ValidationError(f"{fields['timezone']!r} is not a known time zone") from None
    if "body_mass_kg" in fields and not 20.0 < float(fields["body_mass_kg"]) < 400.0:
        raise ValidationError("body mass is outside any plausible range")

    assignments = ", ".join(f"{name} = :{name}" for name in fields)
    with db.transaction(connection):
        connection.execute(
            f"UPDATE profile SET {assignments}, updated_at = :updated_at WHERE id = 1",
            {**fields, "updated_at": db.utcnow()},
        )


# -- beverages -------------------------------------------------------------

def list_beverages(connection: sqlite3.Connection, *, include_archived: bool = False) -> list[sqlite3.Row]:
    clause = "" if include_archived else "WHERE archived_at IS NULL"
    return connection.execute(
        f"SELECT * FROM beverage {clause} ORDER BY sort_order, name"
    ).fetchall()


def find_beverage(connection: sqlite3.Connection, identifier: str | int) -> sqlite3.Row:
    """Look a beverage up by id or by name.

    Name lookup is case-insensitive because Home Assistant sends whatever is in
    the dropdown, and 'water' failing to match 'Water' would be a baffling
    500 at the far end of a REST command.
    """
    if isinstance(identifier, int) or str(identifier).isdigit():
        row = connection.execute("SELECT * FROM beverage WHERE id = ?", (int(identifier),)).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM beverage WHERE lower(name) = lower(?)", (str(identifier).strip(),)
        ).fetchone()
    if row is None:
        raise NotFound(f"no beverage matching {identifier!r}")
    return row


# -- logging ---------------------------------------------------------------

def log_intake(
    connection: sqlite3.Connection,
    *,
    beverage: str | int,
    volume_ml: float,
    at: datetime | None = None,
    sodium_mg: float | None = None,
    caffeine_mg: float | None = None,
    note: str | None = None,
    source: str = "web",
) -> int:
    if volume_ml <= 0:
        raise ValidationError("a drink has to have a volume")
    if volume_ml > 5000:
        raise ValidationError("that is more than five litres in one go; check the units")
    row = find_beverage(connection, beverage)
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO intake (at, beverage_id, volume_ml, sodium_mg, caffeine_mg, note,
                                source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (moment, row["id"], volume_ml, sodium_mg, caffeine_mg, note, source, db.utcnow()),
        )
        return cursor.lastrowid


def log_void(
    connection: sqlite3.Connection,
    *,
    colour: int,
    at: datetime | None = None,
    volume_ml: float | None = None,
    urgency: int | None = None,
    is_first_morning: bool | None = None,
    note: str | None = None,
    source: str = "web",
) -> int:
    colour = int(colour)
    if not 1 <= colour <= 8:
        raise ValidationError("urine colour is a 1-8 value from the chart")
    moment = _validated_time(at)
    if is_first_morning is None:
        is_first_morning = _looks_like_first_morning(connection, db.from_iso(moment))
    with db.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO void (at, colour, volume_ml, is_first_morning, urgency, note,
                              source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (moment, colour, volume_ml, int(bool(is_first_morning)), urgency, note, source, db.utcnow()),
        )
        return cursor.lastrowid


def log_weight(
    connection: sqlite3.Connection,
    *,
    mass_kg: float,
    at: datetime | None = None,
    context: str = "morning",
    activity_id: int | None = None,
    source: str = "web",
) -> int:
    if not 20.0 < mass_kg < 400.0:
        raise ValidationError("that weight is outside any plausible range; check the units")
    if context not in {"morning", "pre_activity", "post_activity", "other"}:
        raise ValidationError(f"unknown weight context {context!r}")
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO body_weight (at, mass_kg, context, activity_id, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (moment, mass_kg, context, activity_id, source, db.utcnow()),
        )
        row_id = cursor.lastrowid

    # A pre/post pair is a measurement of one session's sweat loss, and it
    # almost always lands after the session is already in the table -- Garmin
    # syncs within minutes, the scale reading arrives when you get to it.
    # `record_activity` works the measurement out at insert time, so without
    # this the pair would sit there contributing nothing.
    if context in {"pre_activity", "post_activity"}:
        if refresh_activity_sweat(connection, db.from_iso(moment)) is not None:
            refit_sweat_calibration(connection)
    return row_id


def log_environment(
    connection: sqlite3.Connection,
    *,
    temp_c: float,
    humidity_pct: float,
    at: datetime | None = None,
    location: str = "indoor",
    source: str = "hass",
) -> int:
    if not -60.0 <= temp_c <= 70.0:
        raise ValidationError("that temperature is not a room or a road; check Celsius vs Fahrenheit")
    if not 0.0 <= humidity_pct <= 100.0:
        raise ValidationError("relative humidity is a percentage")
    if location not in {"indoor", "outdoor"}:
        raise ValidationError(f"unknown location {location!r}")
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO environment (at, temp_c, humidity_pct, location, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (moment, temp_c, humidity_pct, location, source, db.utcnow()),
        )
        return cursor.lastrowid


def log_symptom(
    connection: sqlite3.Connection,
    *,
    kind: str,
    severity: int = 2,
    at: datetime | None = None,
    note: str | None = None,
    source: str = "web",
) -> int:
    if not kind.strip():
        raise ValidationError("a symptom needs a name")
    if not 1 <= int(severity) <= 5:
        raise ValidationError("severity runs 1 to 5")
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            "INSERT INTO symptom (at, kind, severity, note, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (moment, kind.strip().lower(), int(severity), note, source, db.utcnow()),
        )
        return cursor.lastrowid


def log_meal(
    connection: sqlite3.Connection,
    *,
    label: str | None = None,
    water_ml: float = 0.0,
    sodium_mg: float = 0.0,
    at: datetime | None = None,
    source: str = "web",
) -> int:
    if water_ml < 0 or sodium_mg < 0:
        raise ValidationError("a meal cannot contain negative water or sodium")
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            "INSERT INTO meal (at, label, water_ml, sodium_mg, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (moment, label, water_ml, sodium_mg, source, db.utcnow()),
        )
        return cursor.lastrowid


def log_feedback(
    connection: sqlite3.Connection,
    *,
    verdict: str,
    at: datetime | None = None,
    note: str | None = None,
    source: str = "web",
) -> int:
    """Record how the day felt, then re-fit the baseline from the history.

    The re-fit is what makes this more than a diary entry: a run of answers
    saying the model reads wet moves the loss constants, so the app stops
    making the same mistake rather than being contradicted daily.
    """
    if verdict not in k.FEEDBACK_DEFICIT_PCT:
        raise ValidationError(f"unknown verdict {verdict!r}")
    moment = _validated_time(at)
    with db.transaction(connection):
        cursor = connection.execute(
            "INSERT INTO feedback (at, verdict, note, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (moment, verdict, note, source, db.utcnow()),
        )
        row_id = cursor.lastrowid
    refit_baseline_scale(connection)
    return row_id


def refit_baseline_scale(connection: sqlite3.Connection) -> tuple[float, int]:
    """Fit the baseline-loss multiplier from how the days actually felt.

    Each verdict is compared against what the ledger believed at that moment
    *without* the verdict's own correction applied -- otherwise the fit would
    be reading back its own influence, the same feedback loop the sweat
    calibration had to be rescued from.

    A residual saying "drier than the model thought" means the baseline losses
    are too low, so the multiplier goes up. It is deliberately a scale on the
    loss terms rather than an offset on the deficit: an offset would be undone
    by the next observation, while this changes what the model expects of a day
    and therefore survives.
    """
    rows = connection.execute(
        """
        SELECT at, verdict FROM feedback
        WHERE voided_at IS NULL ORDER BY at DESC LIMIT 30
        """
    ).fetchall()
    profile = load_profile(connection)
    if len(rows) < k.MIN_FEEDBACK_FOR_FIT:
        save_profile(connection, feedback_n=len(rows))
        return profile.baseline_loss_scale, len(rows)

    # One simulation covering the whole span, then a lookup per verdict.
    # Simulating a day per row ran thirty simulations on every button press and
    # took the better part of a second.
    moments = [db.from_iso(row["at"]) for row in rows]
    # The window must match the one the application itself reads from. A verdict
    # is a reply to a number you were shown, so the residual has to be measured
    # against that same number -- and the ledger is not indifferent to its
    # window when observations are sparse. Fitted against a one-day window while
    # the app displayed a three-day one, the two disagreed by 2.2 L on real
    # data, and the fit moved the baseline confidently in the wrong direction.
    timeline = timeline_for(
        connection,
        start=min(moments) - timedelta(days=k.DEFAULT_LOOKBACK_DAYS),
        end=max(moments),
        profile=profile,
        apply_feedback=False,
    )

    residuals = [
        profile.pct_to_ml(k.FEEDBACK_DEFICIT_PCT[row["verdict"]]) - timeline.at(moment).deficit_ml
        for row, moment in zip(rows, moments, strict=True)  # built from the same rows
    ]
    mean_residual = sum(residuals) / len(residuals)

    # Convert "the model is this many millilitres out by the end of a day" into
    # a proportional change in the day's baseline losses.
    daily_baseline = (
        k.INSENSIBLE_ML_PER_KG_H * profile.body_mass_kg * 24.0
        + k.OBLIGATORY_URINE_ML_PER_KG_H * profile.body_mass_kg * 24.0
    )
    adjustment = mean_residual / daily_baseline if daily_baseline else 0.0
    # Damped. See constants.FEEDBACK_LEARNING_RATE -- at full gain this is a
    # unity-gain feedback controller and it oscillates into its own bounds.
    scale = min(
        max(
            profile.baseline_loss_scale + adjustment * k.FEEDBACK_LEARNING_RATE,
            k.BASELINE_SCALE_MIN,
        ),
        k.BASELINE_SCALE_MAX,
    )
    save_profile(connection, baseline_loss_scale=scale, feedback_n=len(rows))
    return scale, len(rows)


VOIDABLE_TABLES = {"intake", "void", "body_weight", "activity", "symptom", "meal", "feedback"}


def void_entry(connection: sqlite3.Connection, table: str, row_id: int, reason: str = "corrected") -> None:
    """Retract a row without deleting it.

    A health log you can silently rewrite is one you cannot trust when you look
    back at it in six months, so a mistyped entry is retracted and superseded
    rather than edited away.
    """
    if table not in VOIDABLE_TABLES:
        raise ValidationError(f"{table!r} is not a table entries can be retracted from")
    row = connection.execute(f"SELECT voided_at FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise NotFound(f"no {table} row {row_id}")
    if row["voided_at"] is not None:
        raise ConflictError("that entry has already been retracted")
    with db.transaction(connection):
        connection.execute(
            f"UPDATE {table} SET voided_at = ?, voided_reason = ? WHERE id = ?",
            (db.utcnow(), reason, row_id),
        )


# -- activities ------------------------------------------------------------

def record_activity(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    duration_s: float,
    provider: str = "manual",
    external_id: str | None = None,
    name: str | None = None,
    activity_type: str | None = None,
    distance_m: float | None = None,
    kcal: float | None = None,
    avg_hr: float | None = None,
    sweat_ml_reported: float | None = None,
    fluid_consumed_ml: float = 0.0,
    temp_c: float | None = None,
    humidity_pct: float | None = None,
    raw: dict | None = None,
) -> int:
    """Insert or update one activity, and work out its sweat loss.

    Idempotent on (provider, external_id): re-syncing the same ride updates the
    row rather than inserting a second one. Without that the ledger would show
    a sweat loss that never happened, every fifteen minutes, forever.
    """
    profile = load_profile(connection)
    moment = _validated_time(started_at)

    if temp_c is None or humidity_pct is None:
        # Filled in field by field, not both at once. Garmin reports a ride
        # temperature and never a humidity, so replacing the pair because one
        # was missing threw away the only reading actually taken where the
        # sweating happened and substituted the sensor in the hallway at home.
        # Temperature is the input the heat-fraction anchors move most on.
        fallback_temp_c, fallback_humidity_pct = _conditions_at(
            connection, started_at, profile, prefer="outdoor"
        )
        if temp_c is None:
            temp_c = fallback_temp_c
        if humidity_pct is None:
            humidity_pct = fallback_humidity_pct

    # Computed twice, deliberately. The raw figure is the population model with
    # no personal factor; the calibrated one is what this person's own weighed
    # sessions say that should become. Only the raw one may be fed back into
    # the fit -- see the note on sweat_ml_estimated_raw in schema.sql.
    estimated_raw = sweat_model.estimate_sweat_ml(
        kcal=kcal or 0.0,
        duration_s=duration_s,
        temp_c=temp_c,
        humidity_pct=humidity_pct,
        calibration=1.0,
    )
    estimated = sweat_model.estimate_sweat_ml(
        kcal=kcal or 0.0,
        duration_s=duration_s,
        temp_c=temp_c,
        humidity_pct=humidity_pct,
        calibration=profile.sweat_calibration,
    )
    measured = _measured_sweat_ml(connection, started_at, duration_s, fluid_consumed_ml)
    resolution = sweat_model.resolve_sweat(
        measured_ml=measured,
        reported_ml=sweat_ml_reported,
        estimated_ml=estimated,
        duration_s=duration_s,
    )

    payload = {
        "provider": provider,
        "external_id": external_id,
        "started_at": moment,
        "ended_at": db.to_iso(started_at + timedelta(seconds=duration_s)),
        "duration_s": duration_s,
        "name": name,
        "activity_type": activity_type,
        "distance_m": distance_m,
        "kcal": kcal,
        "avg_hr": avg_hr,
        "sweat_ml_reported": sweat_ml_reported,
        "sweat_ml_estimated": estimated,
        "sweat_ml_estimated_raw": estimated_raw,
        "sweat_ml_measured": measured,
        "sweat_ml_used": resolution.ml,
        "sweat_source": resolution.source,
        "fluid_consumed_ml": fluid_consumed_ml,
        "temp_c": temp_c,
        "humidity_pct": humidity_pct,
        "raw_json": json.dumps(raw) if raw else None,
        "created_at": db.utcnow(),
    }

    with db.transaction(connection):
        existing = None
        if external_id is not None:
            existing = connection.execute(
                "SELECT id FROM activity WHERE provider = ? AND external_id = ?",
                (provider, external_id),
            ).fetchone()
        else:
            # Nothing to be idempotent on. A hand-logged session has no
            # external id, so the unique index cannot catch a double submit --
            # and two copies of a ride is two copies of its sweat, straight
            # into the ledger. Same provider, same start is the natural key.
            clash = connection.execute(
                """
                SELECT id FROM activity
                WHERE provider = ? AND external_id IS NULL AND started_at = ? AND voided_at IS NULL
                """,
                (provider, moment),
            ).fetchone()
            if clash is not None:
                raise ConflictError(
                    "an activity starting at that time is already recorded; "
                    "retract it first if you meant to replace it"
                )
        if existing is not None:
            assignments = ", ".join(f"{name_} = :{name_}" for name_ in payload if name_ != "created_at")
            connection.execute(
                f"UPDATE activity SET {assignments} WHERE id = :id", {**payload, "id": existing["id"]}
            )
            return existing["id"]
        columns = ", ".join(payload)
        placeholders = ", ".join(f":{name_}" for name_ in payload)
        cursor = connection.execute(
            f"INSERT INTO activity ({columns}) VALUES ({placeholders})", payload
        )
        return cursor.lastrowid


def log_manual_activity(connection: sqlite3.Connection, **fields) -> int:
    """Record a session typed in by hand, and re-fit the calibration from it.

    The Garmin sync re-fits once at the end of its run, which is the right
    place for it: thirty activities, one fit. A hand-logged session has no such
    moment, and without one the personal sweat factor stayed at 1.0 forever for
    anyone not syncing from Garmin -- however many sessions they weighed.
    """
    activity_id = record_activity(connection, provider="manual", **fields)
    refit_sweat_calibration(connection)
    return activity_id


def refresh_activity_sweat(connection: sqlite3.Connection, moment: datetime) -> int | None:
    """Recompute the sweat figures of whichever activity a weighing belongs to.

    Returns the activity's id if one was found and updated, otherwise None.
    The window matches `_measured_sweat_ml`: a weighing within three quarters
    of an hour of either end of a session is part of that session.
    """
    row = connection.execute(
        """
        SELECT * FROM activity
        WHERE voided_at IS NULL AND ended_at >= ? AND started_at <= ?
        ORDER BY started_at DESC LIMIT 1
        """,
        (db.to_iso(moment - timedelta(minutes=45)), db.to_iso(moment + timedelta(minutes=45))),
    ).fetchone()
    if row is None:
        return None

    measured = _measured_sweat_ml(
        connection, db.from_iso(row["started_at"]), row["duration_s"], row["fluid_consumed_ml"]
    )
    if measured is None and row["sweat_ml_measured"] is None:
        return None  # still only half a pair

    resolution = sweat_model.resolve_sweat(
        measured_ml=measured,
        reported_ml=row["sweat_ml_reported"],
        estimated_ml=row["sweat_ml_estimated"],
        duration_s=row["duration_s"],
    )
    with db.transaction(connection):
        connection.execute(
            """
            UPDATE activity SET sweat_ml_measured = ?, sweat_ml_used = ?, sweat_source = ?
            WHERE id = ?
            """,
            (measured, resolution.ml, resolution.source, row["id"]),
        )
    return row["id"]


def _measured_sweat_ml(
    connection: sqlite3.Connection, started_at: datetime, duration_s: float, fluid_consumed_ml: float
) -> float | None:
    """Sweat loss from a pre/post weight pair, if one brackets this activity.

    The arithmetic is the honest bit: you finished lighter by the fluid you
    lost *minus* whatever you drank along the way, so the drink has to be added
    back or every bottle taken on the ride reads as sweat that never happened.
    """
    finished = started_at + timedelta(seconds=duration_s)
    window_start = db.to_iso(started_at - timedelta(minutes=45))
    window_end = db.to_iso(finished + timedelta(minutes=45))

    before = connection.execute(
        """
        SELECT mass_kg FROM body_weight
        WHERE voided_at IS NULL AND context = 'pre_activity' AND at BETWEEN ? AND ?
        ORDER BY at DESC LIMIT 1
        """,
        (window_start, db.to_iso(started_at + timedelta(minutes=15))),
    ).fetchone()
    after = connection.execute(
        """
        SELECT mass_kg FROM body_weight
        WHERE voided_at IS NULL AND context = 'post_activity' AND at BETWEEN ? AND ?
        ORDER BY at ASC LIMIT 1
        """,
        (db.to_iso(finished - timedelta(minutes=15)), window_end),
    ).fetchone()
    if before is None or after is None:
        return None

    lost_kg = before["mass_kg"] - after["mass_kg"]
    sweat_ml = lost_kg * 1000.0 + fluid_consumed_ml
    if sweat_ml < 0 or sweat_ml > k.MAX_SWEAT_RATE_ML_PER_H * (duration_s / 3600.0 + 1.0):
        # Different scale, kit on in one weighing, or a typo. A measurement
        # this far out is worse than no measurement, because it outranks both
        # models.
        return None
    return sweat_ml


def refit_sweat_calibration(connection: sqlite3.Connection) -> tuple[float, int]:
    """Refit the personal sweat multiplier from every measured activity.

    Called after a sync. The model stops being a population average and starts
    being about this person somewhere around the fourth weighed session.
    """
    rows = connection.execute(
        """
        SELECT sweat_ml_estimated_raw, sweat_ml_measured FROM activity
        WHERE voided_at IS NULL AND sweat_ml_measured IS NOT NULL AND sweat_ml_estimated_raw > 0
        ORDER BY started_at DESC LIMIT 40
        """
    ).fetchall()
    profile = load_profile(connection)
    factor, count = sweat_model.fit_calibration(
        [(row["sweat_ml_estimated_raw"], row["sweat_ml_measured"]) for row in rows],
        current=profile.sweat_calibration,
    )
    save_profile(connection, sweat_calibration=factor, sweat_calibration_n=count)
    return factor, count


# -- assembling the model's inputs ----------------------------------------

def build_events(
    connection: sqlite3.Connection,
    start: datetime,
    end: datetime,
    *,
    include_feedback: bool = True,
) -> list[B.Event]:
    """Turn stored rows into the events the ledger consumes.

    This is the seam between what was logged and what it implies, and it is
    deliberately the only place that crossing happens.
    """
    lo, hi = db.to_iso(start), db.to_iso(end)
    events: list[B.Event] = []

    for row in connection.execute(
        """
        SELECT i.*, b.hydration_index, b.sodium_mg_per_l, b.caffeine_mg_per_l,
               b.alcohol_pct, b.is_multivitamin
        FROM intake i JOIN beverage b ON b.id = i.beverage_id
        WHERE i.voided_at IS NULL AND i.at BETWEEN ? AND ?
        """,
        (lo, hi),
    ):
        litres = row["volume_ml"] / 1000.0
        sodium = row["sodium_mg"] if row["sodium_mg"] is not None else litres * row["sodium_mg_per_l"]
        caffeine = (
            row["caffeine_mg"] if row["caffeine_mg"] is not None else litres * row["caffeine_mg_per_l"]
        )
        events.append(
            B.IntakeEvent(
                at=db.from_iso(row["at"]),
                volume_ml=row["volume_ml"],
                hydration_index=row["hydration_index"],
                sodium_mg=sodium,
                caffeine_mg=caffeine,
                alcohol_g=litres * (row["alcohol_pct"] / 100.0) * ETHANOL_G_PER_L,
                is_multivitamin=bool(row["is_multivitamin"]),
            )
        )

    for row in connection.execute(
        "SELECT * FROM void WHERE voided_at IS NULL AND at BETWEEN ? AND ?", (lo, hi)
    ):
        events.append(
            B.VoidEvent(
                at=db.from_iso(row["at"]),
                colour=row["colour"],
                volume_ml=row["volume_ml"],
                is_first_morning=bool(row["is_first_morning"]),
            )
        )

    for row in connection.execute(
        "SELECT * FROM body_weight WHERE voided_at IS NULL AND at BETWEEN ? AND ?", (lo, hi)
    ):
        events.append(
            B.WeightEvent(at=db.from_iso(row["at"]), mass_kg=row["mass_kg"], context=row["context"])
        )

    # Activities are caught by overlap rather than by start time: a session that
    # began before the window but ran into it still cost fluid inside it.
    for row in connection.execute(
        """
        SELECT * FROM activity
        WHERE voided_at IS NULL AND ended_at >= ? AND started_at <= ?
        """,
        (lo, hi),
    ):
        events.append(
            B.ActivityEvent(
                at=db.from_iso(row["started_at"]),
                duration_s=row["duration_s"],
                sweat_ml=row["sweat_ml_used"],
                kcal=row["kcal"] or 0.0,
                fluid_consumed_ml=row["fluid_consumed_ml"],
            )
        )

    if include_feedback:
        for row in connection.execute(
            "SELECT * FROM feedback WHERE voided_at IS NULL AND at BETWEEN ? AND ?", (lo, hi)
        ):
            events.append(B.FeedbackEvent(at=db.from_iso(row["at"]), verdict=row["verdict"]))

    for row in connection.execute(
        "SELECT * FROM meal WHERE voided_at IS NULL AND at BETWEEN ? AND ?", (lo, hi)
    ):
        events.append(
            B.MealEvent(
                at=db.from_iso(row["at"]), water_ml=row["water_ml"], sodium_mg=row["sodium_mg"]
            )
        )

    return sorted(events, key=lambda event: event.at)


def build_environment(
    connection: sqlite3.Connection, start: datetime, end: datetime, profile: B.Profile
) -> B.Environment:
    """Ambient conditions for the ledger's baseline losses.

    Indoor readings, because that is where most of a day's insensible loss
    happens. Activities carry their own conditions and use those for the sweat
    estimate, so the outdoor sensor is not ignored -- it is used where it
    actually applies.
    """
    rows = connection.execute(
        """
        SELECT at, temp_c, humidity_pct FROM environment
        WHERE at BETWEEN ? AND ? AND location = 'indoor'
        ORDER BY at
        """,
        (db.to_iso(start - timedelta(hours=6)), db.to_iso(end)),
    ).fetchall()
    if not rows:
        rows = connection.execute(
            "SELECT at, temp_c, humidity_pct FROM environment WHERE at BETWEEN ? AND ? ORDER BY at",
            (db.to_iso(start - timedelta(hours=6)), db.to_iso(end)),
        ).fetchall()
    return B.Environment(
        [(db.from_iso(row["at"]), row["temp_c"], row["humidity_pct"]) for row in rows],
        profile.default_temp_c,
        profile.default_humidity_pct,
    )


def _conditions_at(
    connection: sqlite3.Connection, moment: datetime, profile: B.Profile, prefer: str = "indoor"
) -> tuple[float, float]:
    row = connection.execute(
        """
        SELECT temp_c, humidity_pct FROM environment
        WHERE at <= ? AND location = ? ORDER BY at DESC LIMIT 1
        """,
        (db.to_iso(moment), prefer),
    ).fetchone()
    if row is None:
        row = connection.execute(
            "SELECT temp_c, humidity_pct FROM environment WHERE at <= ? ORDER BY at DESC LIMIT 1",
            (db.to_iso(moment),),
        ).fetchone()
    if row is None:
        return profile.default_temp_c, profile.default_humidity_pct
    return row["temp_c"], row["humidity_pct"]


def timeline_for(
    connection: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    profile: B.Profile | None = None,
    apply_feedback: bool = True,
) -> B.Timeline:
    """`apply_feedback=False` is for the baseline fit, which must not read back
    its own influence."""
    profile = profile or load_profile(connection)
    return B.simulate(
        profile,
        build_events(connection, start - timedelta(hours=12), end, include_feedback=apply_feedback),
        start=start,
        end=end,
        environment=build_environment(connection, start, end, profile),
        initial_weight_trend_kg=_weight_trend_before(connection, start, profile),
    )


def _weight_trend_before(
    connection: sqlite3.Connection, moment: datetime, profile: B.Profile
) -> float | None:
    """Seed the mass EWMA from history before the window opens.

    Without this, the first morning weight inside a window sets the trend to
    itself and produces no correction -- the observer would be silently dead
    for the first day of every simulation.
    """
    rows = connection.execute(
        """
        SELECT mass_kg FROM body_weight
        WHERE voided_at IS NULL AND context = 'morning' AND at < ?
        ORDER BY at DESC LIMIT 14
        """,
        (db.to_iso(moment),),
    ).fetchall()
    if not rows:
        return None
    alpha = 1.0 - 0.5 ** (1.0 / k.WEIGHT_TREND_HALFLIFE_DAYS)
    trend = rows[-1]["mass_kg"]
    for row in reversed(rows[:-1]):
        trend = alpha * row["mass_kg"] + (1.0 - alpha) * trend
    return trend


def current_state(
    connection: sqlite3.Connection, *, now: datetime | None = None, lookback_days: int | None = None
) -> tuple[B.Timeline, P.Plan]:
    """The ledger up to now, and what to do about it. The app's main read."""
    now = now or datetime.now(timezone.utc)
    lookback = lookback_days or k.DEFAULT_LOOKBACK_DAYS
    profile = load_profile(connection)
    timeline = timeline_for(connection, start=now - timedelta(days=lookback), end=now, profile=profile)

    return timeline, P.make_plan(
        timeline,
        profile,
        now=now,
        upcoming=None,
        hours_since_last_void=_hours_since_last_void(connection, now),
        recent_dark_voids=_recent_dark_voids(connection, now),
        symptom_flags=_recent_symptoms(connection, now),
    )


def _hours_since_last_void(connection: sqlite3.Connection, now: datetime) -> float | None:
    row = connection.execute(
        "SELECT at FROM void WHERE voided_at IS NULL AND at <= ? ORDER BY at DESC LIMIT 1",
        (db.to_iso(now),),
    ).fetchone()
    if row is None:
        return None
    return (now - db.from_iso(row["at"])).total_seconds() / 3600.0


def _recent_dark_voids(connection: sqlite3.Connection, now: datetime) -> int:
    rows = connection.execute(
        """
        SELECT colour FROM void
        WHERE voided_at IS NULL AND at <= ? AND is_first_morning = 0
        ORDER BY at DESC LIMIT 3
        """,
        (db.to_iso(now),),
    ).fetchall()
    if len(rows) < 3:
        return 0
    return sum(1 for row in rows if row["colour"] >= 7)


def _recent_symptoms(connection: sqlite3.Connection, now: datetime) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT kind FROM symptom WHERE voided_at IS NULL AND at BETWEEN ? AND ?",
        (db.to_iso(now - timedelta(hours=6)), db.to_iso(now)),
    ).fetchall()
    return tuple(row["kind"] for row in rows)


def intake_ml_on(connection: sqlite3.Connection, day: date, tz: ZoneInfo) -> float:
    """Total fluid logged on one local day.

    Local, not UTC: "how much have I drunk today" is a question about the day
    you are living in, and for a US evening the UTC day has already rolled over.
    """
    start = datetime.combine(day, time.min, tzinfo=tz)
    row = connection.execute(
        """
        SELECT COALESCE(SUM(volume_ml), 0) AS total FROM intake
        WHERE voided_at IS NULL AND at >= ? AND at < ?
        """,
        (db.to_iso(start), db.to_iso(start + timedelta(days=1))),
    ).fetchone()
    return float(row["total"])


# -- recommendations -------------------------------------------------------

RECOMMENDATION_MIN_CHANGE_ML = 150.0
RECOMMENDATION_MIN_SODIUM_CHANGE_MG = 100.0
"""How far the advice has to move before it is a different piece of advice.

Below these it is the same recommendation, rephrased -- and the history is for
answering "what was I told, and did following it help", which a row every
minute makes impossible to read.
"""


def _materially_different(latest: sqlite3.Row, plan: P.Plan) -> bool:
    """Is this a different piece of advice, or the same one said again?

    Compared on substance rather than on the rendered headline. The headline
    carries a clock time -- "...every 20 min until 5:40 PM" -- which advances
    with the wall clock, so *every* headline differs from the one a minute
    before it. Home Assistant polls `/api/v1/status` once a minute, and
    comparing the strings therefore wrote 1440 rows a day: precisely the
    duplicate-burial this check exists to prevent, arriving through the door it
    was watching.
    """
    if latest["status"] != plan.status:
        return True
    if abs(latest["deficit_ml"] - plan.deficit_ml) >= RECOMMENDATION_MIN_CHANGE_ML:
        return True
    if abs(latest["sodium_mg"] - plan.sodium.recommended_mg) >= RECOMMENDATION_MIN_SODIUM_CHANGE_MG:
        return True
    return False


def record_recommendation(connection: sqlite3.Connection, plan: P.Plan) -> bool:
    """Store the plan if the advice has actually changed.

    Writing one every time the page is refreshed -- or every time Home
    Assistant polls -- would bury the history in duplicates and make 'what was
    I told today' unreadable.
    """
    latest = connection.execute(
        "SELECT status, deficit_ml, sodium_mg FROM recommendation ORDER BY at DESC LIMIT 1"
    ).fetchone()
    if latest is not None and not _materially_different(latest, plan):
        return False
    with db.transaction(connection):
        connection.execute(
            """
            INSERT INTO recommendation (at, status, headline, deficit_ml, deficit_pct,
                                        target_ml, sodium_mg, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                db.to_iso(plan.at),
                plan.status,
                plan.headline,
                plan.deficit_ml,
                plan.deficit_pct,
                plan.total_planned_ml,
                plan.sodium.recommended_mg,
                json.dumps({"detail": plan.detail, "flags": plan.medical_flags}),
                db.utcnow(),
            ),
        )
    return True


# -- helpers ---------------------------------------------------------------

def _validated_time(at: datetime | None) -> str:
    """Reject timestamps from the future.

    A clock-skewed Home Assistant posting an hour ahead would otherwise put
    entries beyond `now`, where the ledger cannot see them and the day's totals
    quietly disagree with the timeline.
    """
    if at is None:
        return db.utcnow()
    moment = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if moment > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValidationError("that timestamp is in the future; check the clock on the device sending it")
    return db.to_iso(moment)


def _looks_like_first_morning(connection: sqlite3.Connection, moment: datetime) -> bool:
    """Does this void look like the one that followed a night's sleep?

    Stored on the row rather than recomputed on read, so that later changes to
    the profile's wake hour cannot silently rewrite how old entries were read.
    """
    profile = load_profile(connection)
    local_hour = moment.astimezone(profile.tz).hour + moment.astimezone(profile.tz).minute / 60.0
    if abs(local_hour - profile.wake_hour) > FIRST_MORNING_WAKE_WINDOW_H:
        return False
    previous = connection.execute(
        "SELECT at FROM void WHERE voided_at IS NULL AND at < ? ORDER BY at DESC LIMIT 1",
        (db.to_iso(moment),),
    ).fetchone()
    if previous is None:
        return True
    gap_h = (moment - db.from_iso(previous["at"])).total_seconds() / 3600.0
    return gap_h >= FIRST_MORNING_GAP_H
