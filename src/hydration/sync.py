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
FAILURES = "sync.consecutive_failures"

MAX_BACKOFF_MIN = 360.0
"""Six hours. Far enough apart that a broken sync costs nothing, close enough
that a fixed one is picked up the same day."""

AUTH_BACKOFF_MIN = 60.0
"""Where the backoff *starts* when Garmin refused the credentials rather than
failing to answer.

The two are different problems. A network blip fixes itself and is worth
retrying soon. A 401 does not fix itself -- nothing changes until a person
edits `.env` -- and retrying it on the ordinary interval means 96 failed login
attempts a day against Garmin's SSO, which is how a typo in a password turns
into a locked account. That is a considerably worse problem than the typo, and
the app would have caused it."""

AUTH_FAILURE_MARKERS = (
    "401", "unauthorized", "authentication failed", "invalid", "credential",
    "429", "too many", "locked",
)
"""Matched against the message, because the client raises its own exception
types and those move between releases. Over-matching is the safe direction: the
cost is waiting an hour to retry something that would have worked."""

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
    failed = 0
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
            failed += 1
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
    _write_back(connection, client)

    with db.transaction(connection):
        # The cursor only moves when everything landed. Moving it regardless
        # meant anything that failed got one more chance inside the two-day
        # overlap and was then stepped over for good -- a transient error
        # losing a ride permanently, quietly, with a warning in a log nobody
        # reads.
        if failed:
            db.set_setting(connection, LAST_ERROR, f"{failed} activities could not be recorded")
        else:
            db.set_setting(connection, CURSOR, db.to_iso(now))
            db.set_setting(connection, LAST_OK, db.to_iso(now))
            db.set_setting(connection, LAST_ERROR, None)
            db.set_setting(connection, FAILURES, "0")

    message = f"Synced {added} activities and {weights} new weigh-ins."
    if failed:
        message += f" {failed} could not be recorded and will be retried."
    if count:
        message += f" Sweat calibration now {factor:.2f} from {count} weighed sessions."
    return {
        "ok": not failed,
        "message": message,
        "activities": added,
        "weights": weights,
        "failed": failed,
    }


def _write_back(connection: sqlite3.Connection, client) -> None:
    """Push today's logged total into Garmin's own hydration log.

    Off unless HYDRATION_GARMIN_WRITE_BACK is set, and one-directional even
    then: we never read our own number back, so the two systems cannot argue.
    Failures are swallowed on purpose -- this is a courtesy to the watch
    widget, and it must not be able to fail a sync that otherwise worked.
    """
    if not config.GARMIN_WRITE_BACK:
        return
    from .providers import garmin

    try:
        profile = service.load_profile(connection)
        today = datetime.now(profile.tz).date()
        total_ml = service.intake_ml_on(connection, today, profile.tz)
        garmin.push_hydration(client, today, total_ml)
    except Exception as exc:
        log.warning("could not write hydration back to garmin: %s", exc)


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


def _is_auth_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in AUTH_FAILURE_MARKERS)


def _record_failure(connection: sqlite3.Connection, message: str) -> dict:
    log.info("garmin sync unavailable: %s", message)
    failures = _failure_count(connection) + 1
    with db.transaction(connection):
        db.set_setting(connection, LAST_ERROR, message)
        db.set_setting(connection, FAILURES, str(failures))
    return {"ok": False, "message": message, "failures": failures}


def _failure_count(connection: sqlite3.Connection) -> int:
    try:
        return max(0, int(db.get_setting(connection, FAILURES) or 0))
    except ValueError:
        return 0


def next_attempt_minutes(connection: sqlite3.Connection) -> float:
    """How long to wait before trying Garmin again.

    Exponential in the number of consecutive failures, and starting much higher
    when the failure was Garmin refusing the credentials -- see
    AUTH_BACKOFF_MIN for why that distinction is the important one.
    """
    failures = _failure_count(connection)
    if failures == 0:
        return float(config.SYNC_MINUTES)

    base = config.SYNC_MINUTES
    if _is_auth_failure(db.get_setting(connection, LAST_ERROR) or ""):
        base = max(base, AUTH_BACKOFF_MIN)
    return min(base * 2 ** (failures - 1), MAX_BACKOFF_MIN)


def sync_status(connection: sqlite3.Connection) -> dict:
    return {
        "enabled": config.SYNC_ENABLED,
        "interval_min": config.SYNC_MINUTES,
        "last_run": db.get_setting(connection, LAST_RUN),
        "last_ok": db.get_setting(connection, LAST_OK),
        "last_error": db.get_setting(connection, LAST_ERROR),
        "failures": _failure_count(connection),
        "auth_failure": _is_auth_failure(db.get_setting(connection, LAST_ERROR) or ""),
        "retry_in_min": next_attempt_minutes(connection) if _failure_count(connection) else None,
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

        wait_min = next_attempt_minutes(connection)
        if wait_min > config.SYNC_MINUTES:
            log.info("backing off; next garmin attempt in %.0f min", wait_min)
        # "Sync now" on the settings page and `hydration sync` both call
        # run_sync_once directly, so a person who has just fixed their
        # credentials never has to wait this out.
        _stop.wait(wait_min * 60)


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
