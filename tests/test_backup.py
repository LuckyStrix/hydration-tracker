"""Getting a copy out, and putting one back.

Backups that are never restored are a habit, not a safety net. The restore
path is the half that actually matters, so it is the half that is tested:
that it puts the data back, and that it refuses anything it should not install.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from hydration import db, service

UTC = timezone.utc


def at(hour: int = 12) -> datetime:
    """A fixed instant in the past -- `_validated_time` refuses the future."""
    return datetime(2026, 6, 15, hour, 0, tzinfo=UTC)


def test_a_backup_can_be_restored_over_a_changed_database(conn, tmp_path):
    service.log_intake(conn, beverage="Water", volume_ml=500.0, at=at(hour=9))
    service.log_intake(conn, beverage="Coffee", volume_ml=250.0, at=at(hour=10))
    copy = db.backup(conn, tmp_path / "hydration-20260615T120000Z.db")

    service.log_intake(conn, beverage="Water", volume_ml=750.0, at=at(hour=14))
    assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 3

    counts = db.restore(conn, copy)

    assert counts["intake"] == 2
    assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 2
    assert {row["volume_ml"] for row in conn.execute("SELECT volume_ml FROM intake")} == {500.0, 250.0}


def test_a_restore_is_visible_to_the_connection_that_was_already_open(conn, tmp_path):
    """The copy goes through SQLite's backup API rather than over the file,
    because the live database may well be open in another process."""
    service.log_intake(conn, beverage="Water", volume_ml=500.0, at=at(hour=9))
    copy = db.backup(conn, tmp_path / "hydration-20260615T120000Z.db")
    conn.execute("DELETE FROM intake")

    db.restore(conn, copy)

    assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 1


def test_a_restore_reaches_a_second_connection_on_the_same_file(tmp_path):
    """The realistic case: the CLI restores while the web server holds the same
    database open. Replacing the file underneath an open connection produces
    something between a stale reader and a corrupt database, which is why this
    goes through SQLite's backup API instead."""
    live = tmp_path / "hydration.db"
    writer = db.connect(live)
    db.init(writer)
    service.log_intake(writer, beverage="Water", volume_ml=500.0, at=at(hour=9))
    copy = db.backup(writer, tmp_path / "hydration-20260615T120000Z.db")

    server = db.connect(live)
    assert server.execute("SELECT count(*) FROM intake").fetchone()[0] == 1
    writer.execute("DELETE FROM intake")
    assert server.execute("SELECT count(*) FROM intake").fetchone()[0] == 0

    db.restore(writer, copy)

    assert server.execute("SELECT count(*) FROM intake").fetchone()[0] == 1
    writer.close()
    server.close()


def test_restoring_something_that_is_not_a_hydration_database_is_refused(conn, tmp_path):
    """Installing the wrong file silently leaves you with no history and no
    error, which is the one outcome a restore must never produce."""
    stranger = tmp_path / "not-ours.db"
    other = sqlite3.connect(stranger)
    other.execute("CREATE TABLE receipts (id INTEGER PRIMARY KEY)")
    other.commit()
    other.close()

    with pytest.raises(db.NotABackup, match="not a hydration backup"):
        db.restore(conn, stranger)


def test_restoring_a_corrupt_file_is_refused(conn, tmp_path):
    rubbish = tmp_path / "hydration-broken.db"
    rubbish.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4096)

    with pytest.raises(db.NotABackup):
        db.restore(conn, rubbish)


def test_restoring_a_file_that_is_not_there_is_refused(conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        db.restore(conn, tmp_path / "nothing-here.db")


def test_a_backup_will_not_overwrite_one_that_exists(conn, tmp_path):
    target = tmp_path / "hydration-20260615T120000Z.db"
    db.backup(conn, target)
    with pytest.raises(FileExistsError):
        db.backup(conn, target)


def test_two_backups_in_the_same_second_do_not_collide(conn, tmp_path):
    """Which is a real thing when a command takes one automatically -- the
    restore takes a safety copy before overwriting anything."""
    first = db.backup(conn, db.unused_backup_path(tmp_path, "hydration-20260615T120000Z"))
    second = db.backup(conn, db.unused_backup_path(tmp_path, "hydration-20260615T120000Z"))
    assert first != second
    assert first.exists() and second.exists()


def test_a_bad_file_is_rejected_before_anything_is_touched(tmp_path):
    """`inspect_backup` exists so a caller can find out a file is unusable
    without first doing something destructive on the strength of it."""
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all")
    with pytest.raises(db.NotABackup, match="not readable as a database"):
        db.inspect_backup(junk)


def test_an_empty_file_is_rejected_as_well(tmp_path):
    """It opens, and it passes an integrity check -- SQLite is happy to treat
    an empty file as an empty database. What it does not have is our tables,
    and restoring it would leave no history and no error."""
    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(db.NotABackup, match="not a hydration backup"):
        db.inspect_backup(empty)


def test_inspecting_a_good_backup_says_what_is_in_it(conn, tmp_path):
    service.log_intake(conn, beverage="Water", volume_ml=500.0, at=at(hour=9))
    service.log_void(conn, colour=3, at=at(hour=10))
    copy = db.backup(conn, tmp_path / "hydration-20260615T120000Z.db")

    counts = db.inspect_backup(copy)

    assert counts["intake"] == 1
    assert counts["void"] == 1


# -- retention -------------------------------------------------------------

def test_pruning_keeps_the_newest_and_removes_the_rest(tmp_path):
    """Backups are written and never touched again, so without this the
    directory grows forever -- and a full disk is a database that cannot be
    written to."""
    for stamp in ("20260101T000000Z", "20260201T000000Z", "20260301T000000Z", "20260401T000000Z"):
        (tmp_path / f"hydration-{stamp}.db").write_bytes(b"x")

    removed = db.prune_backups(tmp_path, keep=2)

    assert {path.name for path in removed} == {
        "hydration-20260101T000000Z.db",
        "hydration-20260201T000000Z.db",
    }
    assert sorted(path.name for path in tmp_path.glob("hydration-*.db")) == [
        "hydration-20260301T000000Z.db",
        "hydration-20260401T000000Z.db",
    ]


def test_pruning_leaves_files_it_did_not_write_alone(tmp_path):
    """Anything not matching the pattern was put there by a person."""
    (tmp_path / "hydration-20260101T000000Z.db").write_bytes(b"x")
    (tmp_path / "hydration-20260201T000000Z.db").write_bytes(b"x")
    keeper = tmp_path / "before-the-big-refactor.db"
    keeper.write_bytes(b"x")

    db.prune_backups(tmp_path, keep=1)

    assert keeper.exists()


def test_pruning_everything_is_refused(tmp_path):
    with pytest.raises(ValueError):
        db.prune_backups(tmp_path, keep=0)


def test_pruning_a_directory_with_fewer_backups_than_asked_for_does_nothing(tmp_path):
    (tmp_path / "hydration-20260101T000000Z.db").write_bytes(b"x")
    assert db.prune_backups(tmp_path, keep=5) == []
    assert len(list(tmp_path.glob("hydration-*.db"))) == 1


# -- the scheduled backup --------------------------------------------------

def test_a_scheduled_backup_writes_one_and_prunes_the_old(conn, tmp_path, monkeypatch):
    """A backup you have to remember to take is a backup you will not have."""
    from hydration import config, maintenance

    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_KEEP", 2)
    for stamp in ("20200101T000000Z", "20200102T000000Z"):
        (tmp_path / f"hydration-{stamp}.db").write_bytes(b"x")

    result = maintenance.run_backup_once(conn)

    assert result["ok"]
    assert len(list(tmp_path.glob("hydration-*.db"))) == 2, "the new one, and one old one"
    assert db.get_setting(conn, maintenance.LAST_BACKUP) is not None


def test_a_backup_is_not_due_again_immediately(conn, tmp_path, monkeypatch):
    """Read from the database, not from a timer, so restarting the container
    neither skips one nor takes one on every restart."""
    from hydration import config, maintenance

    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_HOURS", 24)

    assert maintenance._due(conn), "nothing recorded yet"
    maintenance.run_backup_once(conn)
    assert not maintenance._due(conn)


def test_a_failing_backup_is_recorded_rather_than_raised(conn, tmp_path, monkeypatch):
    """Nothing in the housekeeping thread may raise into the application."""
    from hydration import config, maintenance

    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "not-writable")
    monkeypatch.setattr(db, "backup", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))

    result = maintenance.run_backup_once(conn)

    assert not result["ok"]
    assert "read-only" in db.get_setting(conn, maintenance.LAST_ERROR)
