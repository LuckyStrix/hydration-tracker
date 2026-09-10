"""Upgrading a database that already has data in it.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
without an explicit migration step an older database keeps its old shape and
the application starts failing on a column that is not there. Upgrading is
supposed to be "pull and restart"; this is what makes that true.
"""

from __future__ import annotations

import sqlite3

import pytest

from hydration import db, service


def test_a_database_missing_the_newer_columns_is_brought_up_to_date():
    connection = db.connect(":memory:")
    db.init(connection)

    # Rebuild `profile` as it looked before the feedback work, with a row in it.
    connection.execute("ALTER TABLE profile RENAME TO profile_old")
    connection.execute(
        """
        CREATE TABLE profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT NOT NULL DEFAULT 'me',
            body_mass_kg REAL NOT NULL DEFAULT 75.0,
            height_cm REAL NOT NULL DEFAULT 178.0,
            sex TEXT NOT NULL DEFAULT 'male',
            birth_year INTEGER NOT NULL DEFAULT 1995,
            timezone TEXT NOT NULL DEFAULT 'America/New_York',
            wake_hour REAL NOT NULL DEFAULT 7.0,
            bed_hour REAL NOT NULL DEFAULT 23.0,
            sweat_sodium_mmol_l REAL NOT NULL DEFAULT 40.0,
            sweat_calibration REAL NOT NULL DEFAULT 1.0,
            sweat_calibration_n INTEGER NOT NULL DEFAULT 0,
            absorption_cap_ml_h REAL NOT NULL DEFAULT 800.0,
            food_water_ml_day REAL NOT NULL DEFAULT 700.0,
            caffeine_diuresis_ml_mg REAL NOT NULL DEFAULT 0.0,
            trust_urine REAL NOT NULL DEFAULT 0.6,
            default_temp_c REAL NOT NULL DEFAULT 21.0,
            default_humidity_pct REAL NOT NULL DEFAULT 45.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO profile (id, body_mass_kg, created_at, updated_at) VALUES (1, 82.0, ?, ?)",
        (db.utcnow(), db.utcnow()),
    )
    connection.execute("DROP TABLE profile_old")

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(profile)")}
    assert "baseline_loss_scale" not in columns

    db.init(connection)

    profile = service.load_profile(connection)
    assert profile.baseline_loss_scale == 1.0, "the new column arrived with its default"
    assert profile.body_mass_kg == 82.0, "and the existing data survived"
    connection.close()


def test_running_init_repeatedly_changes_nothing():
    connection = db.connect(":memory:")
    for _ in range(3):
        db.init(connection)
    assert connection.execute("SELECT count(*) FROM profile").fetchone()[0] == 1
    beverages = connection.execute("SELECT count(*) FROM beverage").fetchone()[0]
    db.init(connection)
    assert connection.execute("SELECT count(*) FROM beverage").fetchone()[0] == beverages
    connection.close()


def test_every_migration_carries_a_default():
    """They are applied to tables that already have rows, so a NOT NULL column
    without a default cannot be added at all."""
    for table, column, definition in db.MIGRATIONS:
        if "NOT NULL" in definition.upper():
            assert "DEFAULT" in definition.upper(), f"{table}.{column} would fail on a populated table"
