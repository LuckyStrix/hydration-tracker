from __future__ import annotations

import sqlite3
import pytest

from hydration import db, service


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.init(connection)
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
        service.save_profile(connection, timezone="America/New_York")
        made.append(connection)
        return connection

    yield factory
    for connection in made:
        connection.close()
