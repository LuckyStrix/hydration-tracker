"""Export and import: getting your data out, and back in.

A personal health log you cannot extract is a personal health log you do not
really own. `hydration backup` produces a SQLite file, which is a fine backup
and a poor export -- it needs this application to read it.

Two formats, for two different jobs:

  * **JSON** is complete and reversible. Everything the model uses, in a form
    `import_payload` can put back.
  * **CSV** is one flat table of everything you logged, for a spreadsheet.
    Lossy on purpose: it is for looking at, not for restoring.

**What is deliberately left out: API tokens, the password hash and the CSRF
key.** An export is a file that gets emailed, dropped in cloud storage and
forgotten about. It must not be a way in.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any

from . import db
from .errors import ValidationError

FORMAT_VERSION = 1

EXPORTED_TABLES = (
    "profile",
    "beverage",
    "intake",
    "void",
    "body_weight",
    "activity",
    "environment",
    "symptom",
    "meal",
    "feedback",
    "recommendation",
)

EXCLUDED_TABLES = ("api_token", "session", "setting")
"""Not an oversight. `api_token` and `setting` hold bearer-token hashes, the
password hash and the CSRF key; `session` holds live sign-ins. None of that is
your hydration history, and all of it would turn a shared export into a
credential leak."""


# -- export ----------------------------------------------------------------

def export_payload(connection: sqlite3.Connection) -> dict[str, Any]:
    """Everything the model uses, as plain JSON-serialisable data."""
    payload: dict[str, Any] = {
        "format": "hydration-tracker-export",
        "version": FORMAT_VERSION,
        "exported_at": db.utcnow(),
        "tables": {},
    }
    for table in EXPORTED_TABLES:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        payload["tables"][table] = [dict(row) for row in rows]
    return payload


def export_json(connection: sqlite3.Connection) -> str:
    return json.dumps(export_payload(connection), indent=2, sort_keys=True)


CSV_COLUMNS = ("at", "kind", "what", "amount", "unit", "source", "note")


def export_csv(connection: sqlite3.Connection) -> str:
    """One flat row per logged thing, newest first.

    Metric, because a spreadsheet should hold one unit per column and the unit
    column says which. Converting to litres and pounds here would make the
    amount column a mix of three different things.
    """
    rows = connection.execute(
        """
        SELECT i.at AS at, 'drink' AS kind, b.name AS what, i.volume_ml AS amount,
               'mL' AS unit, i.source AS source, i.note AS note
          FROM intake i JOIN beverage b ON b.id = i.beverage_id WHERE i.voided_at IS NULL
        UNION ALL
        SELECT at, 'void', 'urine colour', colour, 'chart 1-8', source, note
          FROM void WHERE voided_at IS NULL
        UNION ALL
        SELECT at, 'weight', context, mass_kg, 'kg', source, NULL
          FROM body_weight WHERE voided_at IS NULL
        UNION ALL
        SELECT started_at, 'activity', COALESCE(name, activity_type, 'activity'),
               sweat_ml_used, 'mL sweat', provider, sweat_source
          FROM activity WHERE voided_at IS NULL
        UNION ALL
        SELECT at, 'environment', location, temp_c, 'degC', source,
               'humidity ' || CAST(ROUND(humidity_pct) AS INTEGER) || '%'
          FROM environment
        UNION ALL
        SELECT at, 'symptom', kind, severity, '1-5', source, note
          FROM symptom WHERE voided_at IS NULL
        UNION ALL
        SELECT at, 'meal', COALESCE(label, 'meal'), water_ml, 'mL water', source,
               'sodium ' || CAST(ROUND(sodium_mg) AS INTEGER) || ' mg'
          FROM meal WHERE voided_at IS NULL
        UNION ALL
        SELECT at, 'feedback', verdict, NULL, NULL, source, note
          FROM feedback WHERE voided_at IS NULL
        ORDER BY at DESC
        """
    ).fetchall()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([row[column] if row[column] is not None else "" for column in CSV_COLUMNS])
    return buffer.getvalue()


# -- import ----------------------------------------------------------------

# What makes two rows "the same entry", so re-importing a file twice does not
# double every reading. Timestamp alone is not enough: two drinks logged in the
# same minute are two drinks.
NATURAL_KEYS = {
    "intake": ("at", "beverage_id", "volume_ml"),
    "void": ("at", "colour"),
    "body_weight": ("at", "mass_kg", "context"),
    "activity": ("provider", "external_id", "started_at"),
    "environment": ("at", "location", "temp_c"),
    "symptom": ("at", "kind"),
    "meal": ("at", "label", "water_ml"),
    "feedback": ("at", "verdict"),
    "recommendation": ("at", "headline"),
}


def import_payload(connection: sqlite3.Connection, payload: dict[str, Any], *, restore_profile: bool = False) -> dict[str, int]:
    """Merge an exported payload into this database.

    Merge rather than replace, and idempotent: importing the same file twice
    adds nothing the second time. That matters because the realistic use is
    "pull my history onto a new machine", possibly more than once, and an
    import that silently doubled every reading would corrupt the ledger in a
    way that is very hard to notice.

    The whole thing runs in one transaction. A malformed file leaves the
    database exactly as it was.
    """
    if not isinstance(payload, dict) or payload.get("format") != "hydration-tracker-export":
        raise ValidationError("that is not a hydration tracker export")
    version = payload.get("version")
    if version != FORMAT_VERSION:
        raise ValidationError(f"export format version {version} is not supported")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValidationError("the export has no tables")

    counts: dict[str, int] = {}

    with db.transaction(connection):
        # Beverages first: intake rows point at them, and the ids in the file
        # are the *exporting* database's ids, which mean nothing here.
        beverage_map = _merge_beverages(connection, tables.get("beverage", []), counts)
        activity_map = _merge_activities(connection, tables.get("activity", []), counts)

        for table in ("intake", "void", "body_weight", "environment", "symptom", "meal",
                      "feedback", "recommendation"):
            remap = {}
            if table == "intake":
                remap = {"beverage_id": beverage_map}
            elif table == "body_weight":
                remap = {"activity_id": activity_map}
            counts[table] = _merge_rows(connection, table, tables.get(table, []), remap)

        if restore_profile and tables.get("profile"):
            _restore_profile(connection, tables["profile"][0])
            counts["profile"] = 1

    return counts


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _clean(row: dict, columns: set[str]) -> dict:
    """Drop the primary key and anything this schema does not have.

    Dropping `id` is what lets a merge coexist with rows already here. Dropping
    unknown columns is what lets an export from a slightly older or newer
    version still import instead of failing on one added field.
    """
    return {key: value for key, value in row.items() if key in columns and key != "id"}


def _merge_beverages(connection: sqlite3.Connection, rows: list[dict], counts: dict) -> dict[int, int]:
    """Match on name, insert what is missing, and return old id -> new id."""
    columns = _columns(connection, "beverage")
    mapping: dict[int, int] = {}
    added = 0
    for row in rows:
        name = row.get("name")
        if not name:
            continue
        existing = connection.execute(
            "SELECT id FROM beverage WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if existing:
            mapping[row["id"]] = existing["id"]
            continue
        payload = _clean(row, columns)
        placeholders = ", ".join(f":{key}" for key in payload)
        cursor = connection.execute(
            f"INSERT INTO beverage ({', '.join(payload)}) VALUES ({placeholders})", payload
        )
        mapping[row["id"]] = cursor.lastrowid
        added += 1
    counts["beverage"] = added
    return mapping


def _merge_activities(connection: sqlite3.Connection, rows: list[dict], counts: dict) -> dict[int, int]:
    columns = _columns(connection, "activity")
    mapping: dict[int, int] = {}
    added = 0
    for row in rows:
        existing = connection.execute(
            "SELECT id FROM activity WHERE provider IS ? AND external_id IS ? AND started_at = ?",
            (row.get("provider"), row.get("external_id"), row.get("started_at")),
        ).fetchone()
        if existing:
            mapping[row["id"]] = existing["id"]
            continue
        payload = _clean(row, columns)
        placeholders = ", ".join(f":{key}" for key in payload)
        cursor = connection.execute(
            f"INSERT INTO activity ({', '.join(payload)}) VALUES ({placeholders})", payload
        )
        mapping[row["id"]] = cursor.lastrowid
        added += 1
    counts["activity"] = added
    return mapping


def _merge_rows(
    connection: sqlite3.Connection, table: str, rows: list[dict], remap: dict[str, dict[int, int]]
) -> int:
    columns = _columns(connection, table)
    key_fields = NATURAL_KEYS[table]
    added = 0

    for row in rows:
        payload = _clean(row, columns)
        for field, mapping in remap.items():
            if payload.get(field) is not None:
                mapped = mapping.get(payload[field])
                if mapped is None:
                    # Points at something the export did not carry. Dropping
                    # the reference keeps the row rather than losing it.
                    payload[field] = None
                else:
                    payload[field] = mapped

        if table == "intake" and payload.get("beverage_id") is None:
            continue  # an intake with no drink cannot be reconstructed

        clause = " AND ".join(f"{field} IS :{field}" for field in key_fields)
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE {clause}",
            {field: payload.get(field) for field in key_fields},
        ).fetchone():
            continue

        placeholders = ", ".join(f":{key}" for key in payload)
        connection.execute(
            f"INSERT INTO {table} ({', '.join(payload)}) VALUES ({placeholders})", payload
        )
        added += 1
    return added


PROFILE_IMPORTABLE = {
    "display_name", "body_mass_kg", "height_cm", "sex", "birth_year", "timezone",
    "wake_hour", "bed_hour", "sweat_sodium_mmol_l", "sweat_calibration",
    "sweat_calibration_n", "baseline_loss_scale", "feedback_n", "absorption_cap_ml_h",
    "food_water_ml_day", "caffeine_diuresis_ml_mg", "trust_urine",
    "default_temp_c", "default_humidity_pct", "volume_entry_unit", "history_start_date",
}


def _restore_profile(connection: sqlite3.Connection, row: dict) -> None:
    payload = {key: value for key, value in row.items() if key in PROFILE_IMPORTABLE}
    if not payload:
        return
    assignments = ", ".join(f"{key} = :{key}" for key in payload)
    connection.execute(
        f"UPDATE profile SET {assignments}, updated_at = :updated_at WHERE id = 1",
        {**payload, "updated_at": db.utcnow()},
    )


def parse_export(raw: bytes | str) -> dict:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValidationError("that file is not valid JSON") from None
