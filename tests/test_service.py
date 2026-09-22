"""Tests for the layer between stored rows and the model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hydration import db, service
from hydration.errors import ConflictError, NotFound, ValidationError
from hydration.model import balance as B
from hydration.model import constants as k
from hydration.model import plan as P

UTC = timezone.utc
EASTERN = ZoneInfo("America/New_York")


def at(year=2026, month=6, day=15, hour=12, minute=0) -> datetime:
    """A fixed instant safely in the past.

    It has to be in the past: `_validated_time` refuses timestamps from the
    future, so a fixture anchored to today's date fails for every hour later
    than the moment the suite happens to run.
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# -- logging ---------------------------------------------------------------

def test_a_drink_carries_its_beverage_composition(tz_conn):
    service.log_intake(tz_conn, beverage="Beer", volume_ml=350.0, at=at())
    events = service.build_events(tz_conn, at() - timedelta(hours=1), at() + timedelta(hours=1))
    drink = next(e for e in events if isinstance(e, B.IntakeEvent))
    assert drink.alcohol_g == pytest.approx(0.35 * 0.045 * service.ETHANOL_G_PER_L, rel=0.01)
    assert drink.hydration_index == 1.0


def test_a_per_drink_override_beats_the_catalogue(tz_conn):
    """An electrolyte tab dropped into plain water."""
    service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, sodium_mg=500.0, at=at())
    events = service.build_events(tz_conn, at() - timedelta(hours=1), at() + timedelta(hours=1))
    assert next(e for e in events if isinstance(e, B.IntakeEvent)).sodium_mg == 500.0


def test_beverages_can_be_named_however_hass_spells_them(tz_conn):
    assert service.find_beverage(tz_conn, "water")["name"] == "Water"
    assert service.find_beverage(tz_conn, "  WATER ")["name"] == "Water"
    with pytest.raises(NotFound):
        service.find_beverage(tz_conn, "unobtainium")


def test_implausible_input_is_refused_rather_than_stored(tz_conn):
    with pytest.raises(ValidationError):
        service.log_intake(tz_conn, beverage="Water", volume_ml=-100)
    with pytest.raises(ValidationError):
        service.log_intake(tz_conn, beverage="Water", volume_ml=9000)
    with pytest.raises(ValidationError):
        service.log_void(tz_conn, colour=11)
    with pytest.raises(ValidationError):
        service.log_weight(tz_conn, mass_kg=750.0)
    with pytest.raises(ValidationError):
        # 72 is a nice room in Fahrenheit and lethal in Celsius. Catching this
        # matters: the sensor pushing it is a config away from being wrong.
        service.log_environment(tz_conn, temp_c=72.0, humidity_pct=40.0)


def test_a_future_timestamp_is_refused(tz_conn):
    """A clock-skewed Home Assistant would otherwise file entries past `now`,
    where the ledger cannot see them."""
    with pytest.raises(ValidationError):
        service.log_intake(
            tz_conn, beverage="Water", volume_ml=250, at=datetime.now(UTC) + timedelta(hours=2)
        )


def test_entries_are_retracted_not_deleted(tz_conn):
    row_id = service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, at=at())
    service.void_entry(tz_conn, "intake", row_id, reason="mistyped")

    stored = tz_conn.execute("SELECT * FROM intake WHERE id = ?", (row_id,)).fetchone()
    assert stored is not None, "the row must survive"
    assert stored["voided_reason"] == "mistyped"

    events = service.build_events(tz_conn, at() - timedelta(hours=1), at() + timedelta(hours=1))
    assert not [e for e in events if isinstance(e, B.IntakeEvent)], "but must not reach the model"

    with pytest.raises(ConflictError):
        service.void_entry(tz_conn, "intake", row_id)


# -- first-morning detection ----------------------------------------------

def test_the_void_after_a_night_is_flagged_as_first_morning(tz_conn):
    bedtime = datetime(2026, 6, 14, 22, 30, tzinfo=EASTERN).astimezone(UTC)
    morning = datetime(2026, 6, 15, 7, 15, tzinfo=EASTERN).astimezone(UTC)
    service.log_void(tz_conn, colour=4, at=bedtime)
    void_id = service.log_void(tz_conn, colour=6, at=morning)
    assert tz_conn.execute("SELECT is_first_morning FROM void WHERE id = ?", (void_id,)).fetchone()[0] == 1


def test_a_mid_afternoon_void_is_not(tz_conn):
    """Wrongly flagging one discounts a perfectly good reading, so the rule
    errs toward not flagging."""
    lunch = datetime(2026, 6, 15, 12, 30, tzinfo=EASTERN).astimezone(UTC)
    afternoon = datetime(2026, 6, 15, 18, 0, tzinfo=EASTERN).astimezone(UTC)
    service.log_void(tz_conn, colour=4, at=lunch)
    void_id = service.log_void(tz_conn, colour=6, at=afternoon)
    assert tz_conn.execute("SELECT is_first_morning FROM void WHERE id = ?", (void_id,)).fetchone()[0] == 0


def test_the_stored_flag_survives_a_later_change_to_wake_hour(tz_conn):
    """History must not be rewritten by a settings change."""
    morning = datetime(2026, 6, 15, 7, 15, tzinfo=EASTERN).astimezone(UTC)
    void_id = service.log_void(tz_conn, colour=6, at=morning)
    service.save_profile(tz_conn, wake_hour=15.0)
    assert tz_conn.execute("SELECT is_first_morning FROM void WHERE id = ?", (void_id,)).fetchone()[0] == 1


# -- activities ------------------------------------------------------------

def test_resyncing_an_activity_does_not_double_count_it(tz_conn):
    """The bug this guards against would add the same ride every fifteen
    minutes, forever, and show a sweat loss that never happened."""
    started = at(hour=10)
    for _ in range(3):
        service.record_activity(
            tz_conn,
            provider="garmin",
            external_id="9876",
            started_at=started,
            duration_s=3600,
            kcal=800,
            sweat_ml_reported=1400,
        )
    assert tz_conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 1

    events = service.build_events(tz_conn, started - timedelta(hours=1), started + timedelta(hours=3))
    assert sum(e.sweat_ml for e in events if isinstance(e, B.ActivityEvent)) == 1400


def test_manual_activities_are_not_collapsed_together(tz_conn):
    """Two hand-entered sessions have no external id and must stay distinct."""
    service.record_activity(tz_conn, started_at=at(hour=8), duration_s=1800, kcal=300)
    service.record_activity(tz_conn, started_at=at(hour=17), duration_s=1800, kcal=300)
    assert tz_conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_garmins_number_is_preferred_over_ours_but_both_are_kept(tz_conn):
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=at(hour=10),
        duration_s=3600, kcal=800, sweat_ml_reported=1400,
    )
    row = tz_conn.execute("SELECT * FROM activity").fetchone()
    assert row["sweat_source"] == "garmin"
    assert row["sweat_ml_used"] == 1400
    assert row["sweat_ml_estimated"] > 0, "ours is still computed, for comparison"


def test_a_weight_pair_measures_sweat_and_outranks_both_models(tz_conn):
    started = at(hour=10)
    service.log_weight(tz_conn, mass_kg=75.0, context="pre_activity", at=started)
    service.log_weight(tz_conn, mass_kg=73.4, context="post_activity", at=started + timedelta(hours=1))
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=started,
        duration_s=3600, kcal=800, sweat_ml_reported=1400, fluid_consumed_ml=500,
    )
    row = tz_conn.execute("SELECT * FROM activity").fetchone()
    assert row["sweat_source"] == "measured"
    # Lost 1.6 kg on the scale while drinking 0.5 L, so 2.1 L actually left.
    assert row["sweat_ml_measured"] == pytest.approx(2100.0)


def test_a_nonsense_weight_pair_is_ignored_rather_than_trusted(tz_conn):
    """A measurement this far out is worse than none, because it outranks both
    models."""
    started = at(hour=10)
    service.log_weight(tz_conn, mass_kg=75.0, context="pre_activity", at=started)
    service.log_weight(tz_conn, mass_kg=60.0, context="post_activity", at=started + timedelta(hours=1))
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=started,
        duration_s=3600, kcal=800, sweat_ml_reported=1400,
    )
    row = tz_conn.execute("SELECT * FROM activity").fetchone()
    assert row["sweat_ml_measured"] is None
    assert row["sweat_source"] == "garmin"


def test_calibration_waits_for_enough_weighed_sessions(tz_conn):
    started = at(hour=6)
    for day in range(5):
        when = started + timedelta(days=day)
        service.log_weight(tz_conn, mass_kg=75.0, context="pre_activity", at=when)
        service.log_weight(tz_conn, mass_kg=73.5, context="post_activity", at=when + timedelta(hours=1))
        service.record_activity(
            tz_conn, provider="garmin", external_id=str(day), started_at=when,
            duration_s=3600, kcal=800,
        )
        factor, count = service.refit_sweat_calibration(tz_conn)
        if day < 3:
            assert factor == 1.0, "must not chase noise from one or two sessions"
    assert count == 5
    assert factor != 1.0, "and must adapt once there is real evidence"


# -- time zones ------------------------------------------------------------

def test_a_late_night_drink_lands_on_the_right_local_day(tz_conn):
    """23:00 in New York is 03:00 UTC the next day. Storing UTC is correct;
    reporting it on the wrong local day is not."""
    local = datetime(2026, 6, 15, 23, 0, tzinfo=EASTERN)
    service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, at=local.astimezone(UTC))

    stored = tz_conn.execute("SELECT at FROM intake").fetchone()["at"]
    assert stored.startswith("2026-06-16T03:00"), "stored as UTC"
    assert db.from_iso(stored).astimezone(EASTERN).date().isoformat() == "2026-06-15", "read back as the 15th"


def test_the_ledger_sees_a_drink_logged_across_the_utc_boundary(tz_conn):
    local = datetime(2026, 6, 15, 23, 0, tzinfo=EASTERN)
    service.log_intake(tz_conn, beverage="Water", volume_ml=800.0, at=local.astimezone(UTC))
    timeline = service.timeline_for(
        tz_conn, start=local.astimezone(UTC) - timedelta(hours=2), end=local.astimezone(UTC) + timedelta(hours=4)
    )
    assert timeline.final.intake_ml == pytest.approx(800.0)


# -- assembling state ------------------------------------------------------

def test_environment_readings_reach_the_ledger(tz_conn):
    start = at(hour=8)
    for hour in range(8):
        service.log_environment(
            tz_conn, temp_c=32.0, humidity_pct=75.0, at=start + timedelta(hours=hour)
        )
    hot = service.timeline_for(tz_conn, start=start, end=start + timedelta(hours=8))

    cool = service.timeline_for(
        tz_conn, start=start - timedelta(days=30), end=start - timedelta(days=30) + timedelta(hours=8)
    )
    assert hot.final.insensible_ml > cool.final.insensible_ml * 1.1


# -- history start -----------------------------------------------------------

def test_the_ledger_never_reads_before_the_history_start_date(tz_conn):
    """A lookback window is normally computed from 'now', with no idea whether
    the tracker existed that far back. Without a floor it walks straight
    through the empty weeks before signup and the model free-runs insensible
    loss across the whole stretch -- see `service.history_start_utc`."""
    service.save_profile(tz_conn, history_start_date="2026-06-16")
    floor = service.history_start_utc(tz_conn)

    timeline = service.timeline_for(tz_conn, start=at(day=1), end=at(day=17))

    assert timeline.samples[0].at == floor
    assert all(sample.at >= floor for sample in timeline.samples)


def test_a_floor_moved_past_the_window_end_does_not_invert_it(tz_conn):
    """Moving the floor forward after older events are already logged must not
    turn a valid backward-looking window into one where start is after end."""
    service.save_profile(tz_conn, history_start_date="2026-07-01")

    timeline = service.timeline_for(tz_conn, start=at(day=1), end=at(day=15))

    assert timeline.samples[0].at == at(day=15)


def test_the_weight_trend_is_seeded_from_history_before_the_window(tz_conn):
    """Otherwise the first morning weight inside a window sets the trend to
    itself, and the observer is silently dead for a day."""
    for day in range(10, 0, -1):
        service.log_weight(tz_conn, mass_kg=75.0, at=at(hour=7) - timedelta(days=day))
    trend = service._weight_trend_before(tz_conn, at(hour=7), service.load_profile(tz_conn))
    assert trend == pytest.approx(75.0, abs=0.1)


def test_current_state_returns_a_usable_plan(tz_conn):
    now = datetime.now(UTC)
    service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, at=now - timedelta(hours=2))
    service.log_void(tz_conn, colour=5, at=now - timedelta(hours=1))
    timeline, plan = service.current_state(tz_conn, now=now)
    assert timeline.samples
    assert plan.headline
    assert plan.status in {"ok", "drink", "drink_urgent", "add_sodium", "slow_down"}


def test_recommendations_are_recorded_only_when_they_change(tz_conn):
    now = datetime.now(UTC)
    _, plan = service.current_state(tz_conn, now=now)
    assert service.record_recommendation(tz_conn, plan) is True
    assert service.record_recommendation(tz_conn, plan) is False
    assert tz_conn.execute("SELECT count(*) FROM recommendation").fetchone()[0] == 1


def test_a_symptom_after_sweating_reaches_the_sodium_advice(tz_conn):
    now = datetime.now(UTC)
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=now - timedelta(hours=2),
        duration_s=5400, kcal=1200, sweat_ml_reported=1800,
    )
    service.log_symptom(tz_conn, kind="cramp", severity=3, at=now - timedelta(minutes=30))
    _, plan = service.current_state(tz_conn, now=now)
    assert plan.sodium.recommend
    assert any("cramp" in reason for reason in plan.sodium.reasons)


def test_calibration_does_not_feed_back_on_itself(tz_conn):
    """Regression: the fit must read the uncalibrated estimate.

    Fitting against an already-calibrated estimate makes each refit measure how
    well the *previous* refit worked. It converges on 1.0 and quietly erases
    the personal factor -- the model silently reverts to the population
    average while still claiming to be calibrated.
    """
    started = at(hour=6)
    for day in range(6):
        when = started + timedelta(days=day)
        service.log_weight(tz_conn, mass_kg=79.0, context="pre_activity", at=when)
        service.log_weight(tz_conn, mass_kg=76.6, context="post_activity", at=when + timedelta(hours=1, minutes=30))
        service.record_activity(
            tz_conn, provider="garmin", external_id=f"r{day}", started_at=when,
            duration_s=5400, kcal=1000,
        )
        service.refit_sweat_calibration(tz_conn)

    first = service.load_profile(tz_conn).sweat_calibration
    assert first > 1.2, "this person sweats well above the population model"

    # Re-recording the same activities, now with the factor in force, must not
    # move it. If the raw column were not kept, it would collapse toward 1.0.
    for day in range(6):
        when = started + timedelta(days=day)
        service.record_activity(
            tz_conn, provider="garmin", external_id=f"r{day}", started_at=when,
            duration_s=5400, kcal=1000,
        )
    service.refit_sweat_calibration(tz_conn)
    assert service.load_profile(tz_conn).sweat_calibration == pytest.approx(first, rel=0.01)


# -- the end-of-day question -----------------------------------------------

def test_feedback_is_recorded_and_reaches_the_model(tz_conn):
    now = datetime.now(UTC)
    service.log_feedback(tz_conn, verdict="a_bit_dry", at=now - timedelta(minutes=30))
    events = service.build_events(tz_conn, now - timedelta(hours=2), now)
    assert any(isinstance(e, B.FeedbackEvent) for e in events)


def test_a_nonsense_verdict_is_refused(tz_conn):
    with pytest.raises(ValidationError):
        service.log_feedback(tz_conn, verdict="tremendous")


def test_the_baseline_waits_for_enough_answers_then_moves(tz_conn):
    """One grumpy evening should change nothing; a consistent run should."""
    start = datetime.now(UTC) - timedelta(days=10)
    for day in range(4):
        service.log_feedback(tz_conn, verdict="very_dry", at=start + timedelta(days=day))
    assert service.load_profile(tz_conn).baseline_loss_scale == 1.0, "four is not a pattern"

    for day in range(4, 10):
        service.log_feedback(tz_conn, verdict="very_dry", at=start + timedelta(days=day))
    moved = service.load_profile(tz_conn).baseline_loss_scale
    assert moved > 1.0, "consistently feeling drier than the model says should raise the baseline"


def test_the_baseline_moves_the_other_way_too(tz_conn):
    start = datetime.now(UTC) - timedelta(days=10)
    for day in range(8):
        service.log_feedback(tz_conn, verdict="waterlogged", at=start + timedelta(days=day))
    assert service.load_profile(tz_conn).baseline_loss_scale < 1.0


def test_the_baseline_fit_stays_within_bounds(tz_conn):
    """A run of extreme answers must not make the model incoherent."""
    start = datetime.now(UTC) - timedelta(days=60)
    for day in range(50):
        service.log_feedback(tz_conn, verdict="very_dry", at=start + timedelta(days=day))
    scale = service.load_profile(tz_conn).baseline_loss_scale
    assert k.BASELINE_SCALE_MIN <= scale <= k.BASELINE_SCALE_MAX


def test_the_baseline_fit_does_not_read_back_its_own_influence(tz_conn):
    """The same feedback-loop trap the sweat calibration had to be rescued from:
    the fit compares each verdict against a ledger simulated *without* the
    verdicts applied, so it measures the model's error and not its own echo."""
    start = datetime.now(UTC) - timedelta(days=12)
    for day in range(8):
        service.log_feedback(tz_conn, verdict="a_bit_dry", at=start + timedelta(days=day))

    timeline = service.timeline_for(
        tz_conn, start=start - timedelta(days=1), end=datetime.now(UTC), apply_feedback=False
    )
    assert not [c for c in timeline.corrections if c.kind == "feedback"]


def test_the_baseline_fit_converges_instead_of_oscillating(tz_conn):
    """It is a feedback controller: the ledger it measures against already
    contains the multiplier being fitted. At full gain that oscillates -- six
    identical 'a bit dry' answers, which should raise the baseline, drove it
    into its own *floor* instead. Damped, each step must be smaller than the
    last and the direction must be right."""
    start = datetime.now(UTC) - timedelta(days=12)
    for day in range(8):
        service.log_feedback(tz_conn, verdict="a_bit_dry", at=start + timedelta(days=day))

    scale = service.load_profile(tz_conn).baseline_loss_scale
    assert scale > 1.0, "feeling drier than the model says must raise the baseline"

    steps = []
    for _ in range(5):
        service.refit_baseline_scale(tz_conn)
        latest = service.load_profile(tz_conn).baseline_loss_scale
        steps.append(abs(latest - scale))
        scale = latest

    assert steps[0] >= steps[-1], "the steps must shrink, not grow"
    assert k.BASELINE_SCALE_MIN <= scale <= k.BASELINE_SCALE_MAX


def test_repeated_answers_in_one_sitting_do_not_break_the_fit(tz_conn):
    """The demo case that exposed the oscillation: several answers logged
    seconds apart rather than spread over days."""
    for _ in range(6):
        service.log_feedback(tz_conn, verdict="a_bit_dry")
    scale = service.load_profile(tz_conn).baseline_loss_scale
    assert scale > 1.0, f"'a bit dry' must not lower the baseline (got {scale})"
    assert scale <= k.BASELINE_SCALE_MAX


def test_the_baseline_fit_measures_against_what_the_app_actually_showed(tz_conn):
    """A verdict is a reply to a number you were shown.

    The ledger is not indifferent to its window when observations are sparse,
    so the fit has to use the same lookback the app reads from. Fitted against
    a one-day window while the app displayed a three-day one, the two
    disagreed by 2.2 L on real data and the baseline moved confidently the
    wrong way: 'a bit dry' lowered it.
    """
    now = datetime.now(UTC)
    # A day that leaves the two windows disagreeing: a big sweat loss inside
    # the last 24 hours, with the drinking that covered it further back.
    service.record_activity(
        tz_conn, provider="garmin", external_id="r1", started_at=now - timedelta(hours=20),
        duration_s=7200, kcal=1600, sweat_ml_reported=2400,
    )
    for hour in range(30, 8, -2):
        service.log_intake(tz_conn, beverage="Water", volume_ml=500.0, at=now - timedelta(hours=hour))
    for hour in (26, 20, 14, 8):
        service.log_void(tz_conn, colour=4, at=now - timedelta(hours=hour))

    for day in range(6):
        service.log_feedback(tz_conn, verdict="a_bit_dry", at=now - timedelta(hours=6 - day))

    scale = service.load_profile(tz_conn).baseline_loss_scale
    assert scale > 1.0, f"feeling drier than the model said must raise the baseline, got {scale}"


# -- the ride's own conditions ---------------------------------------------

def test_garmins_ride_temperature_is_not_replaced_by_the_sensor_at_home(tz_conn):
    """Garmin reports a temperature and never a humidity. Filling both from the
    environment table because one was missing threw away the only reading taken
    where the sweating actually happened -- and temperature is the input the
    sweat estimate moves most on."""
    started = at(hour=10)
    service.log_environment(tz_conn, temp_c=21.0, humidity_pct=45.0, at=started, location="outdoor")
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=started,
        duration_s=3600, kcal=800, temp_c=34.0,
    )
    row = tz_conn.execute("SELECT * FROM activity").fetchone()
    assert row["temp_c"] == 34.0, "the ride's own temperature survives"
    assert row["humidity_pct"] == 45.0, "and the missing half is filled in"


def test_a_hot_ride_is_estimated_wetter_than_a_temperate_one(tz_conn):
    """Which is the whole reason the temperature has to survive."""
    def estimate(temp_c: float) -> float:
        service.record_activity(
            tz_conn, provider="garmin", external_id=f"t{temp_c}", started_at=at(hour=10),
            duration_s=3600, kcal=800, temp_c=temp_c, humidity_pct=45.0,
        )
        return tz_conn.execute(
            "SELECT sweat_ml_estimated FROM activity WHERE external_id = ?", (f"t{temp_c}",)
        ).fetchone()[0]

    assert estimate(34.0) > estimate(18.0)


# -- hand-logged sessions --------------------------------------------------

def test_the_same_hand_logged_session_cannot_be_submitted_twice(tz_conn):
    """A manual activity has no external id, so the unique index cannot catch a
    double submit -- and two copies of a ride is two copies of its sweat."""
    service.log_manual_activity(tz_conn, started_at=at(hour=8), duration_s=1800, kcal=300)
    with pytest.raises(ConflictError):
        service.log_manual_activity(tz_conn, started_at=at(hour=8), duration_s=1800, kcal=300)
    assert tz_conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 1


def test_a_retracted_session_leaves_the_slot_free_again(tz_conn):
    """Retracting is how you correct one, so the correction has to be able to
    take the same start time."""
    activity_id = service.log_manual_activity(tz_conn, started_at=at(hour=8), duration_s=1800)
    service.void_entry(tz_conn, "activity", activity_id, reason="wrong duration")
    service.log_manual_activity(tz_conn, started_at=at(hour=8), duration_s=3600)
    assert tz_conn.execute(
        "SELECT count(*) FROM activity WHERE voided_at IS NULL"
    ).fetchone()[0] == 1


def test_an_activity_in_the_future_is_refused(tz_conn):
    """Every other writer refuses one. This one used to take it."""
    with pytest.raises(ValidationError):
        service.record_activity(
            tz_conn, started_at=datetime.now(UTC) + timedelta(hours=3), duration_s=1800
        )


# -- calibration without a Garmin sync -------------------------------------

def test_a_weighing_logged_after_the_ride_still_reaches_the_measurement(tz_conn):
    """The usual order of events: the ride syncs within minutes, the scale
    reading arrives when you get to it. `record_activity` works the measurement
    out at insert time, so the pair used to sit there contributing nothing."""
    started = at(hour=10)
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=started,
        duration_s=3600, kcal=800, fluid_consumed_ml=500,
    )
    assert tz_conn.execute("SELECT sweat_ml_measured FROM activity").fetchone()[0] is None

    service.log_weight(tz_conn, mass_kg=75.0, context="pre_activity", at=started)
    service.log_weight(tz_conn, mass_kg=73.4, context="post_activity", at=started + timedelta(hours=1))

    row = tz_conn.execute("SELECT * FROM activity").fetchone()
    assert row["sweat_ml_measured"] == pytest.approx(2100.0)
    assert row["sweat_source"] == "measured"


def test_the_calibration_fits_without_a_garmin_sync_behind_it(tz_conn):
    """It only ever re-fitted at the end of a sync run, so anyone logging by
    hand stayed on the population model however many sessions they weighed."""
    started = at(hour=6)
    for day in range(5):
        when = started + timedelta(days=day)
        service.log_manual_activity(tz_conn, started_at=when, duration_s=3600, kcal=800)
        service.log_weight(tz_conn, mass_kg=75.0, context="pre_activity", at=when)
        service.log_weight(
            tz_conn, mass_kg=73.5, context="post_activity", at=when + timedelta(hours=1)
        )

    row = service.profile_row(tz_conn)
    assert row["sweat_calibration_n"] == 5
    assert row["sweat_calibration"] != 1.0


def test_a_morning_weighing_does_not_disturb_any_activity(tz_conn):
    """Only a pre/post pair measures a session. The morning weight is an
    observer of overall state and must not be read as one leg of a pair."""
    started = at(hour=6)
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=started, duration_s=3600, kcal=800,
    )
    before = tz_conn.execute("SELECT sweat_ml_used, sweat_source FROM activity").fetchone()
    service.log_weight(tz_conn, mass_kg=75.0, context="morning", at=started + timedelta(minutes=10))
    after = tz_conn.execute("SELECT sweat_ml_used, sweat_source FROM activity").fetchone()
    assert tuple(after) == tuple(before)


# -- the recommendation log ------------------------------------------------

def test_polling_the_status_does_not_fill_the_recommendation_log(tz_conn):
    """Home Assistant polls /api/v1/status once a minute. The headline carries
    a clock time -- "...every 20 min until 5:40 PM" -- which advances with the
    wall clock, so comparing rendered headlines made every poll look like new
    advice and wrote 1440 rows a day."""
    now = at(hour=14)
    service.log_void(tz_conn, colour=6, at=now - timedelta(hours=1))

    written = 0
    for minute in range(120):
        _, plan = service.current_state(tz_conn, now=now + timedelta(minutes=minute))
        written += bool(service.record_recommendation(tz_conn, plan))

    assert written == 1, "two hours of polling is one piece of advice"


def test_advice_that_actually_changes_is_still_recorded(tz_conn):
    """The log has to stay useful -- it is what makes the advice falsifiable."""
    now = at(hour=8)
    _, first = service.current_state(tz_conn, now=now)
    assert service.record_recommendation(tz_conn, first)

    # A litre and a half of sweat is a different situation.
    service.record_activity(
        tz_conn, provider="garmin", external_id="1", started_at=now + timedelta(hours=1),
        duration_s=5400, kcal=1000, sweat_ml_reported=1800,
    )
    _, second = service.current_state(tz_conn, now=now + timedelta(hours=3))
    assert service.record_recommendation(tz_conn, second)
    assert tz_conn.execute("SELECT count(*) FROM recommendation").fetchone()[0] == 2


def test_a_change_of_status_is_always_recorded(tz_conn):
    """Even a small one. Crossing from 'drink' to 'drink now' is the moment
    worth being able to look back at."""
    now = at(hour=12)
    _, plan = service.current_state(tz_conn, now=now)
    service.record_recommendation(tz_conn, plan)

    row = tz_conn.execute("SELECT * FROM recommendation ORDER BY at DESC LIMIT 1").fetchone()
    nudged = P.Plan(**{**plan.__dict__, "status": "drink_urgent"})
    assert service._materially_different(row, nudged)
