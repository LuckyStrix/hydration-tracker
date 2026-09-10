"""The aggregation behind the history charts.

Nothing here writes, and nothing here is subtle -- which is exactly why a
mistake in it is hard to catch by looking. A bar chart with one wrong bar looks
like a day you drank less, not like a bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hydration import reports, service

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")


def _steady_drinking(conn, end: datetime, days: int) -> None:
    """500 mL every hour, so every complete local day totals exactly 12 L."""
    moment = end - timedelta(days=days)
    while moment < end:
        service.log_intake(conn, beverage="Water", volume_ml=500.0, at=moment)
        moment += timedelta(hours=1)


def test_every_complete_day_is_a_complete_day(tz_conn):
    """`start` is an instant -- "now minus N days" -- which lands in the middle
    of a local day. The earliest bar used to hold only the hours after it and
    was drawn full height beside complete days: a day that looked like half the
    drinking of its neighbours, for no reason visible on the chart."""
    end = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)  # 11am Eastern
    _steady_drinking(tz_conn, end, days=6)

    summaries = reports.daily_summaries(tz_conn, end - timedelta(days=2), end, EASTERN)

    assert [day["total_ml"] for day in summaries[:-1]] == [12000.0, 12000.0]


def test_asking_for_n_days_gives_n_buckets(tz_conn):
    end = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)
    _steady_drinking(tz_conn, end, days=6)
    for days in (1, 3, 5):
        summaries = reports.daily_summaries(tz_conn, end - timedelta(days=days - 1), end, EASTERN)
        assert len(summaries) == days


def test_today_is_allowed_to_be_partial(tz_conn):
    """It genuinely is. The last bar is the day you are living in."""
    end = datetime(2026, 6, 15, 15, 0, tzinfo=UTC)  # 11am Eastern, so 11 hours in
    _steady_drinking(tz_conn, end, days=6)

    summaries = reports.daily_summaries(tz_conn, end - timedelta(days=2), end, EASTERN)

    assert summaries[-1]["total_ml"] == 5500.0
    assert summaries[-1]["date"] == end.astimezone(EASTERN).date()


def test_a_late_evening_drink_belongs_to_the_day_it_was_drunk(tz_conn):
    """11pm Eastern is stored as 03:00 the next day in UTC. Grouping on the
    stored string would file it under tomorrow."""
    end = datetime(2026, 6, 16, 15, 0, tzinfo=UTC)
    late = datetime(2026, 6, 14, 23, 30, tzinfo=EASTERN)
    service.log_intake(tz_conn, beverage="Water", volume_ml=400.0, at=late)

    summaries = reports.daily_summaries(tz_conn, end - timedelta(days=3), end, EASTERN)
    by_date = {day["date"]: day["total_ml"] for day in summaries}

    assert by_date[late.date()] == 400.0
    assert by_date[late.date() + timedelta(days=1)] == 0.0
