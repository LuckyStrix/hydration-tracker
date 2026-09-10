"""Configuration, read from the environment.

Everything is overridable through an env var so the container can be
configured without a rebuild, and everything has a default that works, so a
first run needs no configuration at all.
"""

from __future__ import annotations

import os
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# -- storage ---------------------------------------------------------------

DATA_DIR = Path(os.environ.get("HYDRATION_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("HYDRATION_DB_PATH", str(DATA_DIR / "hydration.db")))

BACKUP_DIR = Path(os.environ.get("HYDRATION_BACKUP_DIR", str(DATA_DIR / "backups")))
"""Where `hydration backup` writes. In the container this is bind-mounted to
the host so backups are reachable, while the live database above stays in a
Docker volume -- see db.backup for why that separation is not optional."""


# -- serving ---------------------------------------------------------------

HOST = os.environ.get("HYDRATION_HOST", "0.0.0.0")
PORT = int(_number("HYDRATION_PORT", 8080))

BEHIND_PROXY = _flag("HYDRATION_BEHIND_PROXY")
"""Set when something terminates TLS in front of the app -- `tailscale serve`,
say. Turns on secure cookies and makes the app read X-Forwarded-Proto. Leave it
off for plain HTTP over the tailnet, or every cookie will be dropped."""

SESSION_DAYS = int(_number("HYDRATION_SESSION_DAYS", 30))
SESSION_COOKIE = "hydration_session"

LOGIN_FREE_ATTEMPTS = int(_number("HYDRATION_LOGIN_FREE_ATTEMPTS", 5))
"""Wrong passwords allowed before the login starts locking. Mistyping it a few
times is normal and should cost nothing; a sustained run should not be a
guessing budget. See security.record_failed_login for the shape of the lock."""

PASSWORD = os.environ.get("HYDRATION_PASSWORD")
"""Optional bootstrap password. If set, it is hashed into the database on
first start; after that the settings page owns it. Left unset, the first visit
asks you to choose one."""

MAX_BODY_BYTES = int(_number("HYDRATION_MAX_BODY_BYTES", 256 * 1024))
"""Ceiling on an ordinary request body. Generous for a form with a dozen
fields on it, and small enough that nothing can be pushed through one."""

IMPORT_MAX_BODY_BYTES = int(_number("HYDRATION_IMPORT_MAX_BODY_BYTES", 64 * 1024 * 1024))
"""And the ceiling for `/import`, which is the one route whose body is a whole
health log. It has to be a separate number: a year of drinking exports to about
a megabyte, so holding the import to the ordinary limit meant the export could
not be read back -- the restore path failing on the size of the thing being
restored, with a 413 that named nothing useful."""


# -- garmin ----------------------------------------------------------------

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GARMIN_TOKEN_DIR = Path(os.environ.get("HYDRATION_GARMIN_TOKENS", str(DATA_DIR / "garmin_tokens")))

SYNC_MINUTES = int(_number("HYDRATION_SYNC_MINUTES", 15))
SYNC_ENABLED = _flag("HYDRATION_SYNC_ENABLED", True)
GARMIN_BACKFILL_DAYS = int(_number("HYDRATION_GARMIN_BACKFILL_DAYS", 30))

BACKUP_HOURS = int(_number("HYDRATION_BACKUP_HOURS", 24))
BACKUP_KEEP = int(_number("HYDRATION_BACKUP_KEEP", 14))
BACKUP_AUTOMATIC = _flag("HYDRATION_BACKUP_AUTOMATIC", True)
"""Take a backup on a timer, inside the application.

`hydration backup` exists and works, but a backup you have to remember is a
backup you will not have. Doing it here rather than from the host's scheduler
means it does not depend on a Windows task surviving a reboot, and the app is
the only thing that can take a *consistent* copy anyway -- see db.backup.

Set BACKUP_KEEP to bound the directory: backups are written and never touched
again, and a full disk is a database that cannot be written to."""


GARMIN_WRITE_BACK = _flag("HYDRATION_GARMIN_WRITE_BACK", False)
"""Push logged drinks into Garmin's own hydration log so the watch widget
agrees. Off by default: it writes to someone else's system, and a sync loop
that both reads and writes is a sync loop that can argue with itself."""


# -- display ---------------------------------------------------------------

TIMEZONE = os.environ.get("TZ", "America/New_York")
"""Fallback only. The profile's own timezone wins once one is set."""
