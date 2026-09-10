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

PASSWORD = os.environ.get("HYDRATION_PASSWORD")
"""Optional bootstrap password. If set, it is hashed into the database on
first start; after that the settings page owns it. Left unset, the first visit
asks you to choose one."""

MAX_BODY_BYTES = int(_number("HYDRATION_MAX_BODY_BYTES", 256 * 1024))


# -- garmin ----------------------------------------------------------------

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GARMIN_TOKEN_DIR = Path(os.environ.get("HYDRATION_GARMIN_TOKENS", str(DATA_DIR / "garmin_tokens")))

SYNC_MINUTES = int(_number("HYDRATION_SYNC_MINUTES", 15))
SYNC_ENABLED = _flag("HYDRATION_SYNC_ENABLED", True)
GARMIN_BACKFILL_DAYS = int(_number("HYDRATION_GARMIN_BACKFILL_DAYS", 30))

GARMIN_WRITE_BACK = _flag("HYDRATION_GARMIN_WRITE_BACK", False)
"""Push logged drinks into Garmin's own hydration log so the watch widget
agrees. Off by default: it writes to someone else's system, and a sync loop
that both reads and writes is a sync loop that can argue with itself."""


# -- display ---------------------------------------------------------------

TIMEZONE = os.environ.get("TZ", "America/New_York")
"""Fallback only. The profile's own timezone wins once one is set."""
