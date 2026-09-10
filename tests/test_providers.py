"""Reading Garmin's payloads.

`providers/garmin.py` searches for known keys rather than indexing fixed paths,
because Garmin moves them. These tests pin what that search is supposed to
find, and -- more usefully -- what it is supposed to leave alone.
"""

from __future__ import annotations

from hydration.providers import garmin


def _summary(**extra) -> dict:
    return {
        "activityId": 1234,
        "startTimeGMT": "2026-06-15 10:00:00",
        "duration": 3600.0,
        "activityName": "Morning ride",
        **extra,
    }


def test_a_ride_temperature_below_freezing_is_read():
    """A winter ride is exactly when the sweat estimate should differ most, and
    a `> 0` test on the value threw every sub-zero reading away."""
    activity = garmin._activity_from(_summary(avgTemperature=-4.0), None)
    assert activity.temp_c == -4.0


def test_the_average_temperature_is_preferred_over_the_minimum():
    """A ride that started cold was otherwise estimated at its coldest moment
    for its whole length."""
    activity = garmin._activity_from(_summary(minTemperature=2.0, avgTemperature=14.0), None)
    assert activity.temp_c == 14.0


def test_humidity_is_read_when_it_is_there():
    activity = garmin._activity_from(_summary(avgTemperature=22.0, humidity=70.0), None)
    assert activity.humidity_pct == 70.0


def test_humidity_is_simply_absent_when_it_is_not():
    """Which is the usual case. `record_activity` fills that one field in from
    the environment table and keeps the temperature Garmin did report."""
    activity = garmin._activity_from(_summary(avgTemperature=22.0), None)
    assert activity.temp_c == 22.0
    assert activity.humidity_pct is None


def test_a_padded_zero_still_counts_as_missing_for_the_other_numbers():
    """Garmin fills absent figures with 0, and a zero-calorie ride is a missing
    reading rather than a measurement."""
    activity = garmin._activity_from(_summary(calories=0, sweatLoss=0), None)
    assert activity.kcal is None
    assert activity.sweat_ml is None


def test_a_number_is_found_however_deeply_it_is_buried():
    activity = garmin._activity_from(
        _summary(), {"summaryDTO": {"nested": {"calories": 812.0}}}
    )
    assert activity.kcal == 812.0


def test_a_boolean_is_not_mistaken_for_a_number():
    """`True` is an int in Python, and would have been read as 1 degree."""
    activity = garmin._activity_from(_summary(temperature=True, avgTemperature=19.0), None)
    assert activity.temp_c == 19.0


def test_the_gmt_timestamp_is_preferred_over_the_local_one():
    """`startTimeGMT` is UTC despite saying nothing about it. Reading the local
    one as UTC files every activity hours out of place."""
    activity = garmin._activity_from(
        _summary(startTimeLocal="2026-06-15 06:00:00"), None
    )
    assert activity.started_at.hour == 10
