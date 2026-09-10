"""The background Garmin sync, with Garmin stubbed out.

The rule this file mostly exists to hold is the cursor's: it may only move past
work that actually landed. Moving it regardless meant a transient error lost a
ride permanently and quietly, which is the failure mode this whole application
is written against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hydration import config, db, sync
from hydration.providers import base, garmin

UTC = timezone.utc


@pytest.fixture
def garmin_returns(monkeypatch):
    """Stub the provider. Returns a setter for what the next sync will see."""
    state: dict = {"activities": [], "weigh_ins": [], "pushed": []}

    monkeypatch.setattr(garmin, "connect", lambda interactive=False: object())
    monkeypatch.setattr(garmin, "fetch_activities", lambda client, since, until=None: state["activities"])
    monkeypatch.setattr(garmin, "fetch_weigh_ins", lambda client, since, until=None: state["weigh_ins"])
    monkeypatch.setattr(
        garmin, "push_hydration",
        lambda client, when, total_ml: state["pushed"].append((when, total_ml)) or True,
    )
    return state


def _activity(external_id: str, *, hours_ago: float = 4.0, duration_s: float = 3600.0) -> base.Activity:
    return base.Activity(
        external_id=external_id,
        started_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        duration_s=duration_s,
        name="Ride",
        kcal=800.0,
    )


def _unrecordable(external_id: str) -> base.Activity:
    """One the service layer will refuse: a start time in the future."""
    return _activity(external_id, hours_ago=-6.0)


def test_a_clean_run_moves_the_cursor(tz_conn, garmin_returns):
    garmin_returns["activities"] = [_activity("1"), _activity("2", hours_ago=8)]

    result = sync.run_sync_once(tz_conn)

    assert result["ok"] and result["activities"] == 2
    assert db.get_setting(tz_conn, sync.CURSOR) is not None
    assert db.get_setting(tz_conn, sync.LAST_ERROR) is None


def test_a_failed_activity_holds_the_cursor_back(tz_conn, garmin_returns):
    """It gets one more chance inside the two-day overlap and is then stepped
    over for good. So the cursor waits."""
    garmin_returns["activities"] = [_activity("good"), _unrecordable("bad")]

    result = sync.run_sync_once(tz_conn)

    assert result["failed"] == 1
    assert not result["ok"]
    assert db.get_setting(tz_conn, sync.CURSOR) is None, "still where it was"
    assert "could not be recorded" in db.get_setting(tz_conn, sync.LAST_ERROR)


def test_the_next_run_picks_the_failure_up_again(tz_conn, garmin_returns):
    garmin_returns["activities"] = [_unrecordable("bad")]
    sync.run_sync_once(tz_conn)

    # Whatever was wrong with it has passed.
    garmin_returns["activities"] = [_activity("bad")]
    result = sync.run_sync_once(tz_conn)

    assert result["ok"] and result["activities"] == 1
    assert tz_conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 1
    assert db.get_setting(tz_conn, sync.CURSOR) is not None


def test_a_sync_that_cannot_reach_garmin_records_it_and_does_not_raise(tz_conn, monkeypatch):
    def refuse(interactive=False):
        raise RuntimeError("Garmin is having a bad morning")

    monkeypatch.setattr(garmin, "connect", refuse)

    result = sync.run_sync_once(tz_conn)

    assert not result["ok"]
    assert "bad morning" in db.get_setting(tz_conn, sync.LAST_ERROR)
    assert db.get_setting(tz_conn, sync.CURSOR) is None


# -- write-back ------------------------------------------------------------

def test_nothing_is_written_back_unless_it_is_asked_for(tz_conn, garmin_returns, monkeypatch):
    """It writes into someone else's system, so it is off by default."""
    monkeypatch.setattr(config, "GARMIN_WRITE_BACK", False)
    sync.run_sync_once(tz_conn)
    assert garmin_returns["pushed"] == []


def test_the_days_total_is_written_back_when_it_is(tz_conn, garmin_returns, monkeypatch):
    """The flag was documented and wired to nothing -- `push_hydration` had no
    caller at all."""
    from hydration import service

    monkeypatch.setattr(config, "GARMIN_WRITE_BACK", True)
    profile = service.load_profile(tz_conn)
    today = datetime.now(profile.tz)
    service.log_intake(tz_conn, beverage="Water", volume_ml=600.0, at=today - timedelta(minutes=30))

    sync.run_sync_once(tz_conn)

    assert len(garmin_returns["pushed"]) == 1
    when, total_ml = garmin_returns["pushed"][0]
    assert when == today.date()
    assert total_ml == pytest.approx(600.0)


def test_a_write_back_that_fails_does_not_fail_the_sync(tz_conn, garmin_returns, monkeypatch):
    """It is a courtesy to the watch widget, not part of the job."""
    monkeypatch.setattr(config, "GARMIN_WRITE_BACK", True)

    def explode(client, when, total_ml):
        raise RuntimeError("nope")

    monkeypatch.setattr(garmin, "push_hydration", explode)

    assert sync.run_sync_once(tz_conn)["ok"]
