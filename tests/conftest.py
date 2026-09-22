from __future__ import annotations

import sqlite3
import pytest

from hydration import db, service


HISTORY_FLOOR = "2020-01-01"
"""Predates every fixed test date in this suite. `history_start_date`
defaults to the day the profile row was created -- today, on the real clock
the suite runs on -- which would otherwise clamp every test built on a fixed
past 'now' (see the module docstring in test_migrations.py and CLAUDE.md on
`_validated_time`) to nothing."""


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.init(connection)
    service.save_profile(connection, history_start_date=HISTORY_FLOOR)
    yield connection
    connection.close()


@pytest.fixture
def tz_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A profile with a known time zone, so day-boundary tests are stable."""
    service.save_profile(conn, timezone="America/New_York", body_mass_kg=75.0)
    return conn


@pytest.fixture
def conn_factory():
    """Makes additional empty databases, for import round-trip tests."""
    made = []

    def factory() -> sqlite3.Connection:
        connection = db.connect(":memory:")
        db.init(connection)
        service.save_profile(connection, timezone="America/New_York", history_start_date=HISTORY_FLOOR)
        made.append(connection)
        return connection

    yield factory
    for connection in made:
        connection.close()
