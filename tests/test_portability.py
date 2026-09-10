"""Export and import.

The property that matters most is idempotence. The realistic use is "pull my
history onto another machine", possibly more than once, and an import that
silently doubled every reading would corrupt the ledger in a way that is very
hard to notice afterwards.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from hydration import portability, security, service
from hydration.errors import ValidationError

UTC = timezone.utc


def at(hour=12, day=15):
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


@pytest.fixture
def populated(tz_conn):
    service.log_intake(tz_conn, beverage="Coffee", volume_ml=300.0, at=at(7))
    service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, at=at(9))
    service.log_void(tz_conn, colour=5, at=at(10))
    service.log_weight(tz_conn, mass_kg=75.5, at=at(6))
    service.log_meal(tz_conn, label="lunch", water_ml=300.0, sodium_mg=900.0, at=at(13))
    service.log_symptom(tz_conn, kind="thirst", at=at(15))
    service.log_feedback(tz_conn, verdict="a_bit_dry", at=at(21))
    service.record_activity(
        tz_conn, provider="garmin", external_id="55", started_at=at(17),
        duration_s=3600, kcal=700, sweat_ml_reported=1200,
    )
    service.log_environment(tz_conn, temp_c=22.0, humidity_pct=45.0, at=at(12))
    return tz_conn


def _counts(conn):
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("intake", "void", "body_weight", "activity", "meal", "symptom",
                      "feedback", "environment")
    }


# -- export ----------------------------------------------------------------

def test_the_export_contains_the_whole_log(populated):
    payload = json.loads(portability.export_json(populated))
    assert payload["format"] == "hydration-tracker-export"
    for table in ("intake", "void", "body_weight", "activity", "meal", "feedback"):
        assert payload["tables"][table], f"{table} missing from the export"


def test_the_export_never_carries_credentials(populated):
    """An export is a file that gets emailed and left in cloud storage. It must
    not be a way in."""
    security.set_password(populated, "a-real-password")
    security.issue_token(populated, "hass")

    raw = portability.export_json(populated)
    payload = json.loads(raw)

    for table in portability.EXCLUDED_TABLES:
        assert table not in payload["tables"], f"{table} was exported"
    assert "scrypt$" not in raw, "the password hash reached the export"
    assert "token_hash" not in raw


def test_the_csv_is_readable_by_a_spreadsheet(populated):
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(portability.export_csv(populated))))
    assert rows
    assert set(rows[0]) == set(portability.CSV_COLUMNS)
    kinds = {row["kind"] for row in rows}
    assert {"drink", "void", "weight", "activity", "meal", "feedback"} <= kinds
    # Metric throughout, with the unit named -- a column holding a mix of
    # litres, pounds and degrees would be unusable.
    assert {row["unit"] for row in rows if row["kind"] == "drink"} == {"mL"}


# -- import ----------------------------------------------------------------

def test_a_round_trip_into_an_empty_database_restores_everything(populated, conn_factory):
    payload = json.loads(portability.export_json(populated))
    before = _counts(populated)

    fresh = conn_factory()
    portability.import_payload(fresh, payload)
    assert _counts(fresh) == before


def test_importing_the_same_file_twice_adds_nothing(populated, conn_factory):
    payload = json.loads(portability.export_json(populated))
    fresh = conn_factory()

    first = portability.import_payload(fresh, payload)
    after_first = _counts(fresh)
    second = portability.import_payload(fresh, payload)

    assert sum(first.values()) > 0
    assert sum(second.values()) == 0, "the second import must be a no-op"
    assert _counts(fresh) == after_first


def test_drinks_keep_pointing_at_the_right_drink(populated, conn_factory):
    """Beverage ids are the exporting database's ids and mean nothing here, so
    they are remapped by name. Getting this wrong would silently turn every
    coffee into whatever happened to be row 3."""
    payload = json.loads(portability.export_json(populated))
    fresh = conn_factory()
    portability.import_payload(fresh, payload)

    names = [
        row["name"]
        for row in fresh.execute(
            "SELECT b.name FROM intake i JOIN beverage b ON b.id = i.beverage_id ORDER BY i.at"
        )
    ]
    assert names == ["Coffee", "Water"]


def test_an_import_merges_rather_than_replaces(populated, conn_factory):
    fresh = conn_factory()
    service.log_intake(fresh, beverage="Water", volume_ml=999.0, at=at(hour=8, day=1))

    portability.import_payload(fresh, json.loads(portability.export_json(populated)))
    volumes = {row[0] for row in fresh.execute("SELECT volume_ml FROM intake")}
    assert 999.0 in volumes, "the row that was already here must survive"
    assert 500.0 in volumes


def test_the_profile_is_only_restored_when_asked(populated, conn_factory):
    service.save_profile(populated, body_mass_kg=88.0)
    payload = json.loads(portability.export_json(populated))

    fresh = conn_factory()
    portability.import_payload(fresh, payload)
    assert service.load_profile(fresh).body_mass_kg != 88.0

    fresh2 = conn_factory()
    portability.import_payload(fresh2, payload, restore_profile=True)
    assert service.load_profile(fresh2).body_mass_kg == 88.0


def test_a_bad_file_changes_nothing(populated):
    before = _counts(populated)
    for bad in ({"format": "something else"}, {"format": "hydration-tracker-export"},
                {"format": "hydration-tracker-export", "version": 99}):
        with pytest.raises(ValidationError):
            portability.import_payload(populated, bad)
    assert _counts(populated) == before


def test_malformed_json_is_refused_with_a_reason():
    with pytest.raises(ValidationError):
        portability.parse_export(b"{not json")


def test_an_import_is_all_or_nothing(populated, conn_factory):
    """One transaction. A file that fails halfway must leave the database
    exactly as it was."""
    payload = json.loads(portability.export_json(populated))
    payload["tables"]["void"].append({"id": 999, "at": "2026-06-15T10:00:00+00:00", "colour": 47})

    fresh = conn_factory()
    before = _counts(fresh)
    with pytest.raises(sqlite3.IntegrityError):
        portability.import_payload(fresh, payload)
    assert _counts(fresh) == before, "a failed import left rows behind"
