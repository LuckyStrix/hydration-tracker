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

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, so a database created by an earlier
# version keeps its old shape and the application starts failing on a column
# that is missing. SQLite has no "ADD COLUMN IF NOT EXISTS", so each one is
# checked and added.
#
# Append here; never edit or reorder. Every entry must carry a DEFAULT, because
# it is being added to a table that already has rows in it.
#
# The fourth element is a backfill: a DEFAULT gets the column onto the table,
# but a default is not always a *correct* value for the rows already there. A
# column whose default is a placeholder needs one of these, or the upgrade
# leaves real rows holding a value that reads as valid and behaves as garbage.
# `activity.ended_at` is the case that taught this: it arrived defaulted to the
# empty string, which every range query silently sorts below every real
# timestamp, so every activity logged before the upgrade dropped out of the
# ledger without a word.
MIGRATIONS: tuple[tuple[str, str, str, str | None], ...] = (
    ("profile", "baseline_loss_scale", "REAL NOT NULL DEFAULT 1.0", None),
    ("profile", "feedback_n", "INTEGER NOT NULL DEFAULT 0", None),
    ("activity", "sweat_ml_estimated_raw", "REAL", None),
    (
        "activity",
        "ended_at",
        "TEXT NOT NULL DEFAULT ''",
        # ISO-8601 with the T and the zone, assembled by hand. SQLite's own
        # datetime() emits '2026-06-15 11:00:00' -- a space, no zone -- which
        # does not compare against the strings every other timestamp here uses.
        # Building the format explicitly is the whole point.
        """
        UPDATE activity
           SET ended_at = strftime(
                   '%Y-%m-%dT%H:%M:%S',
                   started_at,
                   '+' || CAST(duration_s AS INTEGER) || ' seconds'
               ) || '+00:00'
         WHERE ended_at IS NULL OR ended_at = ''
        """,
    ),
    ("profile", "volume_entry_unit", "TEXT NOT NULL DEFAULT 'l'", None),
    (
        "profile",
        "history_start_date",
        "TEXT NOT NULL DEFAULT ''",
        "UPDATE profile SET history_start_date = substr(created_at, 1, 10) WHERE history_start_date = ''",
    ),
)


def init(connection: sqlite3.Connection) -> None:
    """Create the schema, bring an older one up to date, and seed it.

    Safe to run on every start, which is what makes upgrading the container a
    matter of pulling and restarting.
    """
    connection.executescript(SCHEMA_PATH.read_text())
    _migrate(connection)
    _seed_profile(connection)
    _seed_beverages(connection)


def _migrate(connection: sqlite3.Connection) -> None:
    for table, column, definition, backfill in MIGRATIONS:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # the table itself is new; the schema script just made it
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        if backfill is not None:
            # Run on every start, not only on the start that adds the column.
            # Each backfill is written to match only rows still holding the
            # placeholder, so repeating it costs one scan and changes nothing
            # -- and a database that already took an earlier, backfill-less
            # version of this migration is repaired rather than left broken.
            connection.execute(backfill)


def _seed_profile(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT 1 FROM profile WHERE id = 1").fetchone()
    if existing:
        return
    now = utcnow()
    connection.execute(
        "INSERT INTO profile (id, created_at, updated_at, history_start_date) VALUES (1, ?, ?, ?)",
        (now, now, now[:10]),
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


BACKUP_GLOB = "hydration-*.db"
"""What `hydration backup` names its files, and therefore the only files
pruning will consider. Anything else in the directory was put there by a
person and is not ours to delete."""


def prune_backups(directory: str | Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups. Returns what was removed.

    Backups are written and never touched again, so without this the directory
    grows forever -- and a full disk is a database that cannot be written to,
    which is a strange way for a hydration log to end.

    Sorted by name rather than by mtime: the names carry a sortable UTC stamp,
    and a file's mtime is whatever the last thing to touch it decided.
    """
    if keep < 1:
        raise ValueError("keep at least one backup")
    existing = sorted(Path(directory).glob(BACKUP_GLOB))
    doomed = existing[: max(0, len(existing) - keep)]
    for path in doomed:
        path.unlink()
    return doomed


REQUIRED_TABLES = frozenset({"profile", "intake", "void", "body_weight", "activity"})
"""Enough of the schema to tell a backup of this application from some other
SQLite file that happens to be lying in the backup directory."""


class NotABackup(ValueError):
    """The file is not something this application should install over itself."""


def inspect_backup(source: str | Path) -> dict[str, int]:
    """Check a file is a restorable hydration backup, and say what is in it.

    Separate from `restore` so a caller can find out that a file is unusable
    *before* doing anything destructive with the database it was going to
    replace. Raises rather than returning a verdict, because there is nothing
    sensible to do with a bad one.
    """
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"no backup at {path}")

    try:
        incoming = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise NotABackup(f"{path} will not open as a database: {exc}") from exc

    try:
        try:
            integrity = incoming.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise NotABackup(f"{path} is not readable as a database: {exc}") from exc
        if integrity != "ok":
            raise NotABackup(f"{path} does not pass an integrity check: {integrity}")

        tables = {row[0] for row in incoming.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise NotABackup(
                f"{path} is not a hydration backup; it has no {', '.join(sorted(missing))} table"
            )

        return {
            table: incoming.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in sorted(REQUIRED_TABLES)
        }
    finally:
        incoming.close()


def restore(connection: sqlite3.Connection, source: str | Path) -> dict[str, int]:
    """Replace the contents of the live database with a backup's.

    The copy goes through SQLite's own backup API rather than over the file,
    for the same reason backups come out through VACUUM INTO: the live database
    may well be open in another process, and replacing a file underneath an
    open connection produces something between a stale reader and a corrupt
    database. This takes the write lock and swaps the pages properly, so the
    running application picks the new content up.

    The incoming file is checked first -- see `inspect_backup`. A restore that
    silently installs the wrong file leaves you with no history and no error.
    """
    counts = inspect_backup(source)
    incoming = sqlite3.connect(f"file:{Path(source)}?mode=ro", uri=True)
    try:
        incoming.backup(connection)
    finally:
        incoming.close()
    return counts


def unused_backup_path(directory: str | Path, stem: str) -> Path:
    """A path in `directory` that no backup already occupies.

    `backup` refuses to overwrite, which is right -- but two backups inside the
    same second are a real thing when a command takes one automatically, and
    that should not be a traceback.
    """
    directory = Path(directory)
    candidate = directory / f"{stem}.db"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.db"
        suffix += 1
    return candidate
