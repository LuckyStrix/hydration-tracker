"""SQLite access: connections, transactions, schema, seed data, backup.

Hand-written SQL against stdlib `sqlite3`. No ORM -- the dependency footprint
stays small and every query that touches a health record stays readable.

Two things in here are load-bearing and should not be "tidied":

  * `transaction()` opens with BEGIN IMMEDIATE, so two writers serialise at the
    start of the write rather than discovering the conflict at COMMIT. The
    Garmin sync thread and a browser request genuinely do write at the same
    time.
  * Timestamps go in and out as ISO-8601 UTC strings. They sort
    lexicographically, which is what makes every range query in this app a
    plain BETWEEN on a text column.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_local = threading.local()


# -- time ------------------------------------------------------------------

def utcnow() -> str:
    """The one way this application asks what time it is."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(moment: datetime) -> str:
    """Normalise any datetime to the stored representation.

    A naive datetime is treated as UTC rather than rejected: they arrive from
    JSON payloads and `datetime.fromisoformat` on input that omitted the zone,
    and silently storing one as local time would put an entry hours out of
    place.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# -- connections -----------------------------------------------------------

def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection configured the way this application needs it."""
    connection = sqlite3.connect(
        str(path),
        # The sync thread and the request handlers share a process; each gets
        # its own connection from `get()`, so the check is redundant and only
        # gets in the way of the CLI.
        check_same_thread=False,
        # Wait rather than fail when another writer holds the lock. Writes here
        # are milliseconds long; a busy timeout of zero turns a brief overlap
        # into a 500.
        timeout=10.0,
        isolation_level=None,  # explicit transactions only -- see transaction()
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    # WAL plus NORMAL loses at most the last transaction on a host crash, and
    # this is a hydration log on a desktop PC, not a ledger.
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def get(path: str | Path) -> sqlite3.Connection:
    """One connection per thread, reused.

    sqlite3 connections are cheap but not free, and the background sync thread
    would otherwise open one every fifteen minutes forever.
    """
    existing = getattr(_local, "connection", None)
    if existing is not None and getattr(_local, "path", None) == str(path):
        return existing
    if existing is not None:
        existing.close()
    connection = connect(path)
    _local.connection = connection
    _local.path = str(path)
    return connection


def close() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None
        _local.path = None


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a write in BEGIN IMMEDIATE.

    IMMEDIATE rather than the default deferred transaction: it takes the write
    lock up front, so a concurrent writer blocks here instead of getting most
    of the way through and failing at COMMIT. Do not "optimise" this to
    deferred.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


# -- schema ----------------------------------------------------------------

def init(connection: sqlite3.Connection) -> None:
    """Create the schema and seed it. Safe to run on every start."""
    connection.executescript(SCHEMA_PATH.read_text())
    _seed_profile(connection)
    _seed_beverages(connection)


def _seed_profile(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT 1 FROM profile WHERE id = 1").fetchone()
    if existing:
        return
    now = utcnow()
    connection.execute(
        "INSERT INTO profile (id, created_at, updated_at) VALUES (1, ?, ?)", (now, now)
    )


# Per litre. The hydration index numbers follow the beverage hydration index
# work (Maughan et al.): milk and oral rehydration solution retain better than
# water because their sodium and slower gastric emptying reduce the urine that
# follows, and ordinary coffee and beer sit close to water rather than below
# it. Sodium figures are typical label values -- the settings page can edit any
# of them, and an intake row can override per drink.
SEED_BEVERAGES: tuple[dict, ...] = (
    dict(name="Water", hydration_index=1.0, sort_order=10),
    dict(name="Sparkling water", hydration_index=1.0, sort_order=20),
    dict(name="Coffee", hydration_index=0.99, caffeine_mg_per_l=400.0, sort_order=30),
    dict(name="Tea", hydration_index=1.0, caffeine_mg_per_l=200.0, sort_order=40),
    dict(name="Milk", hydration_index=1.5, sodium_mg_per_l=440.0, potassium_mg_per_l=1500.0,
         kcal_per_l=620.0, sort_order=50),
    dict(name="Electrolyte tab in water", hydration_index=1.3, sodium_mg_per_l=1000.0,
         potassium_mg_per_l=200.0, sort_order=60),
    dict(name="LMNT / high-sodium mix", hydration_index=1.4, sodium_mg_per_l=2000.0,
         potassium_mg_per_l=800.0, sort_order=65),
    dict(name="Sports drink", hydration_index=1.1, sodium_mg_per_l=460.0,
         potassium_mg_per_l=125.0, kcal_per_l=260.0, sort_order=70),
    dict(name="Oral rehydration solution", hydration_index=1.5, sodium_mg_per_l=2300.0,
         potassium_mg_per_l=780.0, kcal_per_l=100.0, sort_order=75),
    dict(name="Juice", hydration_index=1.1, potassium_mg_per_l=1800.0, kcal_per_l=450.0,
         sort_order=80),
    dict(name="Soda", hydration_index=1.0, sodium_mg_per_l=100.0, caffeine_mg_per_l=100.0,
         kcal_per_l=420.0, sort_order=90),
    dict(name="Diet soda", hydration_index=1.0, sodium_mg_per_l=100.0, caffeine_mg_per_l=130.0,
         sort_order=95),
    dict(name="Beer", hydration_index=1.0, alcohol_pct=4.5, kcal_per_l=430.0, sort_order=100),
    dict(name="Wine", hydration_index=0.9, alcohol_pct=13.0, kcal_per_l=830.0, sort_order=110),
    dict(name="Spirits", hydration_index=0.7, alcohol_pct=40.0, kcal_per_l=2500.0, sort_order=120),
    dict(name="Broth / soup", hydration_index=1.4, sodium_mg_per_l=3000.0, sort_order=130),
    # Not a drink. It exists so a void logged in the hours afterwards can be
    # discounted -- riboflavin turns urine bright yellow regardless of how
    # hydrated you are, and a colour reading taken then is close to worthless.
    dict(name="Multivitamin", hydration_index=1.0, is_multivitamin=1, sort_order=200),
)


def _seed_beverages(connection: sqlite3.Connection) -> None:
    for entry in SEED_BEVERAGES:
        connection.execute(
            """
            INSERT INTO beverage (name, hydration_index, sodium_mg_per_l, potassium_mg_per_l,
                                  caffeine_mg_per_l, alcohol_pct, kcal_per_l, is_multivitamin,
                                  sort_order)
            VALUES (:name, :hydration_index, :sodium_mg_per_l, :potassium_mg_per_l,
                    :caffeine_mg_per_l, :alcohol_pct, :kcal_per_l, :is_multivitamin, :sort_order)
            ON CONFLICT(name) DO NOTHING
            """,
            {
                "sodium_mg_per_l": 0.0,
                "potassium_mg_per_l": 0.0,
                "caffeine_mg_per_l": 0.0,
                "alcohol_pct": 0.0,
                "kcal_per_l": 0.0,
                "is_multivitamin": 0,
                **entry,
            },
        )


# -- settings --------------------------------------------------------------

def get_setting(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = connection.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(connection: sqlite3.Connection, key: str, value: str | None) -> None:
    connection.execute(
        """
        INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utcnow()),
    )


# -- backup ----------------------------------------------------------------

def backup(connection: sqlite3.Connection, destination: str | Path) -> Path:
    """Write a consistent copy with VACUUM INTO.

    This is the only safe way to get a copy of this database out while the
    application is running. Copying the file itself -- with `cp`, a backup
    agent, or any folder-syncing tool -- catches it mid-write and produces a
    replica that may not open. VACUUM INTO takes a read lock and writes a
    complete, defragmented database, so the result is consistent by
    construction.

    This is why the live database belongs in a Docker volume rather than in a
    directory that something else is watching, and why backups are pushed
    *out* to such a directory rather than the database being pointed *at* one.
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"{target} already exists; refusing to overwrite a backup")
    connection.execute("VACUUM INTO ?", (str(target),))
    return target
