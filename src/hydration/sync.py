"""The background Garmin sync.

One daemon thread, waking on an interval. The rules it lives by:

  * **A sync failure never breaks the app.** Everything is caught, recorded in
    `setting`, and shown on the settings page. A request must not be able to
    fail because Garmin is having a bad morning.
  * **It is safe to run twice.** Activities are keyed on (provider,
    external_id) and weigh-ins are matched on their timestamp, so re-syncing
    the same window updates rather than duplicates. Without that the ledger
    would accumulate sweat that never happened, every fifteen minutes.
  * **It re-fits the calibration afterwards**, since a new weighed session is
    exactly the evidence that would change it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import config, db, service

log = logging.getLogger("hydration.sync")

LAST_RUN = "sync.last_run"
LAST_OK = "sync.last_ok"
LAST_ERROR = "sync.last_error"
CURSOR = "sync.garmin_cursor"

_thread: threading.Thread | None = None
_stop = threading.Event()


# -- the work --------------------------------------------------------------

def run_sync_once(connection: sqlite3.Connection, *, interactive: bool = False) -> dict:
    """Pull activities and weigh-ins, then re-fit. Never raises."""
    from .providers import garmin

    now = datetime.now(timezone.utc)
    with db.transaction(connection):
        db.set_setting(connection, LAST_RUN, db.to_iso(now))

    try:
        client = garmin.connect(interactive=interactive)
    except Exception as exc:
        return _record_failure(connection, str(exc))

    cursor_raw = db.get_setting(connection, CURSOR)
    since = (
        db.from_iso(cursor_raw) - timedelta(days=2)
        if cursor_raw
        else now - timedelta(days=config.GARMIN_BACKFILL_DAYS)
    )
    # The two-day overlap is deliberate. Garmin backfills an activity's own
    # numbers after upload -- sweat loss in particular appears late -- so a
    # cursor that moved on cleanly would freeze the first, emptier version.

    try:
        activities = garmin.fetch_activities(client, since, now)
        weigh_ins = garmin.fetch_weigh_ins(client, since, now)
    except Exception as exc:
        return _record_failure(connection, f"fetch failed: {exc}")

    added = 0
    for activity in activities:
        try:
            service.record_activity(
                connection,
                provider="garmin",
                external_id=activity.external_id,
                started_at=activity.started_at,
                duration_s=activity.duration_s,
                name=activity.name,
                activity_type=activity.activity_type,
                distance_m=activity.distance_m,
                kcal=activity.kcal,
                avg_hr=activity.avg_hr,
                sweat_ml_reported=activity.sweat_ml,
                fluid_consumed_ml=activity.fluid_consumed_ml,
                temp_c=activity.temp_c,
                humidity_pct=activity.humidity_pct,
                raw=activity.raw,
            )
            added += 1
        except Exception as exc:
            log.warning("could not record activity %s: %s", activity.external_id, exc)

    weights = 0
    for weigh_in in weigh_ins:
        if _weight_already_recorded(connection, weigh_in.at):
            continue
        try:
            service.log_weight(
                connection,
                mass_kg=weigh_in.mass_kg,
                at=weigh_in.at,
                context="morning",
                source="garmin",
            )
            weights += 1
        except Exception as exc:
            log.warning("could not record weigh-in: %s", exc)

    factor, count = service.refit_sweat_calibration(connection)

    with db.transaction(connection):
        db.set_setting(connection, CURSOR, db.to_iso(now))
        db.set_setting(connection, LAST_OK, db.to_iso(now))
        db.set_setting(connection, LAST_ERROR, None)

    message = f"Synced {added} activities and {weights} new weigh-ins."
    if count:
        message += f" Sweat calibration now {factor:.2f} from {count} weighed sessions."
    return {"ok": True, "message": message, "activities": added, "weights": weights}


def _weight_already_recorded(connection: sqlite3.Connection, moment: datetime) -> bool:
    """Match on a window rather than an exact timestamp.

    Garmin rounds and occasionally re-reports the same weigh-in a second or two
    apart; an exact match would let the duplicate through.
    """
    row = connection.execute(
        """
        SELECT 1 FROM body_weight
        WHERE voided_at IS NULL AND source = 'garmin' AND at BETWEEN ? AND ?
        """,
        (
            db.to_iso(moment - timedelta(minutes=5)),
            db.to_iso(moment + timedelta(minutes=5)),
        ),
    ).fetchone()
    return row is not None


def _record_failure(connection: sqlite3.Connection, message: str) -> dict:
    log.info("garmin sync unavailable: %s", message)
    with db.transaction(connection):
        db.set_setting(connection, LAST_ERROR, message)
    return {"ok": False, "message": message}


def sync_status(connection: sqlite3.Connection) -> dict:
    return {
        "enabled": config.SYNC_ENABLED,
        "interval_min": config.SYNC_MINUTES,
        "last_run": db.get_setting(connection, LAST_RUN),
        "last_ok": db.get_setting(connection, LAST_OK),
        "last_error": db.get_setting(connection, LAST_ERROR),
        "running": _thread is not None and _thread.is_alive(),
    }


# -- the thread ------------------------------------------------------------

def _loop() -> None:
    # Its own connection: sqlite3 objects belong to the thread that made them,
    # and `db.get` keeps one per thread for exactly this reason.
    connection = db.get(config.DB_PATH)
    # A short initial delay so a container restart does not hit Garmin at the
    # same instant every time.
    if _stop.wait(30):
        return
    while not _stop.is_set():
        try:
            run_sync_once(connection)
        except Exception:
            # run_sync_once already handles its own failures; this is the last
            # net, and it exists so the thread cannot die and leave sync
            # silently switched off for as long as the container runs.
            log.exception("sync loop caught an unexpected error")
        _stop.wait(config.SYNC_MINUTES * 60)


def start_sync() -> None:
    global _thread
    if not config.SYNC_ENABLED:
        log.info("garmin sync disabled by configuration")
        return
    if not (config.GARMIN_EMAIL and config.GARMIN_PASSWORD):
        log.info("garmin sync idle: no credentials configured")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="garmin-sync", daemon=True)
    _thread.start()
    log.info("garmin sync started, every %s minutes", config.SYNC_MINUTES)


def stop_sync() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
