"""The housekeeping thread: scheduled backups, and clearing out dead sessions.

Separate from the Garmin sync on purpose. That thread is about somebody else's
API and is switched off whenever there are no credentials; this one is about
not losing the database, which is true regardless of whether Garmin is in the
picture at all.

Like the sync thread, nothing in here may raise into the application, and the
thread must not be able to die -- a housekeeping thread that quietly stopped
weeks ago is worse than none, because you believe you have backups.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone

from . import config, db, security

log = logging.getLogger("hydration.maintenance")

LAST_BACKUP = "maintenance.last_backup"
LAST_ERROR = "maintenance.last_error"

_thread: threading.Thread | None = None
_stop = threading.Event()


def run_backup_once(connection: sqlite3.Connection) -> dict:
    """Write a backup and prune the old ones. Never raises."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        written = db.backup(connection, config.BACKUP_DIR / f"hydration-{stamp}.db")
        pruned = db.prune_backups(config.BACKUP_DIR, config.BACKUP_KEEP) if config.BACKUP_KEEP else []
    except Exception as exc:
        log.warning("scheduled backup failed: %s", exc)
        with db.transaction(connection):
            db.set_setting(connection, LAST_ERROR, str(exc))
        return {"ok": False, "message": str(exc)}

    with db.transaction(connection):
        db.set_setting(connection, LAST_BACKUP, db.utcnow())
        db.set_setting(connection, LAST_ERROR, None)
    log.info("backed up to %s, pruned %d", written, len(pruned))
    return {"ok": True, "path": str(written), "pruned": len(pruned)}


def backup_status(connection: sqlite3.Connection) -> dict:
    return {
        "automatic": config.BACKUP_AUTOMATIC,
        "every_hours": config.BACKUP_HOURS,
        "keep": config.BACKUP_KEEP,
        "directory": str(config.BACKUP_DIR),
        "last_backup": db.get_setting(connection, LAST_BACKUP),
        "last_error": db.get_setting(connection, LAST_ERROR),
        "running": _thread is not None and _thread.is_alive(),
    }


def _due(connection: sqlite3.Connection) -> bool:
    """Has it been long enough since the last one?

    Read from the database rather than from a timer, so restarting the
    container does not either skip a backup or take one on every restart.
    """
    last = db.get_setting(connection, LAST_BACKUP)
    if not last:
        return True
    try:
        since_h = (datetime.now(timezone.utc) - db.from_iso(last)).total_seconds() / 3600.0
    except ValueError:
        return True
    return since_h >= config.BACKUP_HOURS


def _loop() -> None:
    # Its own connection: sqlite3 objects belong to the thread that made them.
    connection = db.get(config.DB_PATH)
    # Not at the instant of startup -- a container that restart-loops would
    # otherwise spend its life taking backups.
    if _stop.wait(120):
        return
    while not _stop.is_set():
        try:
            if _due(connection):
                run_backup_once(connection)
            security.purge_expired_sessions(connection)
        except Exception:
            # run_backup_once handles its own failures; this is the last net,
            # and it exists so the thread cannot die and leave the backups
            # silently switched off for as long as the container runs.
            log.exception("maintenance loop caught an unexpected error")
        _stop.wait(1800)


def start_maintenance() -> None:
    global _thread
    if not config.BACKUP_AUTOMATIC:
        log.info("scheduled backups disabled by configuration")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="hydration-maintenance", daemon=True)
    _thread.start()
    log.info("scheduled backups started, every %s hours, keeping %s", config.BACKUP_HOURS, config.BACKUP_KEEP)


def stop_maintenance() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=5)
