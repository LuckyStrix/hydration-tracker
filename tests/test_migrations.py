"""Upgrading a database that already has data in it.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
without an explicit migration step an older database keeps its old shape and
the application starts failing on a column that is not there. Upgrading is
supposed to be "pull and restart"; this is what makes that true.
"""

from __future__ import annotations

import sqlite3


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
    for table, column, definition, _backfill in db.MIGRATIONS:
        if "NOT NULL" in definition.upper():
            assert "DEFAULT" in definition.upper(), f"{table}.{column} would fail on a populated table"


def _activity_table_without_ended_at(connection: sqlite3.Connection) -> None:
    """`activity` as it looked before `ended_at` existed, with a ride in it."""
    connection.execute("DROP TABLE IF EXISTS activity")
    connection.execute(
        """
        CREATE TABLE activity (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'manual',
            external_id TEXT,
            started_at TEXT NOT NULL,
            duration_s REAL NOT NULL,
            name TEXT,
            activity_type TEXT,
            distance_m REAL,
            kcal REAL,
            avg_hr REAL,
            sweat_ml_reported REAL,
            sweat_ml_estimated REAL,
            sweat_ml_measured REAL,
            sweat_ml_used REAL NOT NULL DEFAULT 0,
            sweat_source TEXT NOT NULL DEFAULT 'none',
            fluid_consumed_ml REAL NOT NULL DEFAULT 0,
            temp_c REAL,
            humidity_pct REAL,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            voided_at TEXT,
            voided_reason TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO activity (started_at, duration_s, kcal, sweat_ml_used, created_at)
        VALUES ('2026-08-01T10:00:00+00:00', 5400, 900, 1800, '2026-08-01T12:00:00+00:00')
        """
    )


def test_an_activity_that_predates_ended_at_still_reaches_the_ledger():
    """The column arrives defaulted to the empty string, which sorts below
    every real timestamp -- so without a backfill the overlap query in
    `build_events` matches nothing and the ride silently leaves the ledger."""
    connection = db.connect(":memory:")
    db.init(connection)
    _activity_table_without_ended_at(connection)

    db.init(connection)

    row = connection.execute("SELECT started_at, ended_at FROM activity").fetchone()
    assert row["ended_at"] == "2026-08-01T11:30:00+00:00", "start plus duration, in the stored format"

    found = connection.execute(
        "SELECT count(*) FROM activity WHERE ended_at >= ? AND started_at <= ?",
        ("2026-07-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
    ).fetchone()[0]
    assert found == 1, "the ledger's own overlap query finds it"
    connection.close()


def test_a_database_that_took_the_backfill_less_migration_is_repaired():
    """The first version of this migration shipped without a backfill, so a
    database can already be sitting there holding empty strings. The column
    exists, so adding it is skipped -- the repair has to happen anyway."""
    connection = db.connect(":memory:")
    db.init(connection)
    _activity_table_without_ended_at(connection)
    connection.execute("ALTER TABLE activity ADD COLUMN ended_at TEXT NOT NULL DEFAULT ''")
    assert connection.execute("SELECT ended_at FROM activity").fetchone()["ended_at"] == ""

    db.init(connection)

    assert connection.execute("SELECT ended_at FROM activity").fetchone()["ended_at"] != ""
    connection.close()


def test_a_fresh_profile_seeds_the_history_start_date_to_today():
    connection = db.connect(":memory:")
    db.init(connection)
    row = connection.execute("SELECT created_at, history_start_date FROM profile").fetchone()
    assert row["history_start_date"] == row["created_at"][:10]
    connection.close()


def test_a_database_missing_history_start_date_backfills_it_from_created_at():
    """The column arrives defaulted to the empty string, which `service.
    history_start_utc` would otherwise have to special-case forever."""
    connection = db.connect(":memory:")
    db.init(connection)
    connection.execute(
        "UPDATE profile SET created_at = '2024-03-02T00:00:00+00:00', history_start_date = ''"
    )

    db.init(connection)

    row = connection.execute("SELECT history_start_date FROM profile").fetchone()
    assert row["history_start_date"] == "2024-03-02"
    connection.close()


def test_the_backfill_leaves_a_real_ended_at_alone():
    connection = db.connect(":memory:")
    db.init(connection)
    service.record_activity(
        connection, started_at=db.from_iso("2026-08-01T10:00:00+00:00"), duration_s=3600
    )
    before = connection.execute("SELECT ended_at FROM activity").fetchone()["ended_at"]

    db.init(connection)

    assert connection.execute("SELECT ended_at FROM activity").fetchone()["ended_at"] == before
    connection.close()
