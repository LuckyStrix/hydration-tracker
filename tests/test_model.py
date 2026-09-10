"""Tests that pin the hydration model's behaviour.

These matter more than the rest of the suite. Everything else in the app is
data entry and presentation, and a bug there is visible. A bug in here produces
a plausible-looking number that is quietly wrong, which is worse.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hydration.model import balance as B
from hydration.model import constants as k
from hydration.model import electrolytes, plan as P, sweat, urine


UTC = timezone.utc


@pytest.fixture
def profile() -> B.Profile:
    return B.Profile(body_mass_kg=75.0, height_cm=178.0, timezone="America/New_York")


@pytest.fixture
def noon(profile: B.Profile) -> datetime:
    """A fixed local noon, so tests never depend on when they are run."""
    return datetime(2026, 9, 10, 12, 0, tzinfo=profile.tz).astimezone(UTC)


@pytest.fixture
def frictionless(monkeypatch, profile: B.Profile) -> B.Profile:
    """A body that loses and gains nothing on its own.

    Lets the intake terms be checked against exact arithmetic instead of
    against a moving baseline.
    """
    monkeypatch.setattr(k, "INSENSIBLE_ML_PER_KG_H", 0.0)
    monkeypatch.setattr(k, "OBLIGATORY_URINE_ML_PER_KG_H", 0.0)
    monkeypatch.setattr(k, "FECAL_ML_PER_DAY", 0.0)
    monkeypatch.setattr(k, "METABOLIC_WATER_ML_PER_KCAL", 0.0)
    monkeypatch.setattr(k, "MAX_DIURESIS_ML_PER_MIN", 0.0)
    # Also assume the log is being kept, so the regression toward the
    # no-evidence prior never fires. These tests check a single term against
    # exact arithmetic; that term is checked on its own further down.
    monkeypatch.setattr(k, "UNLOGGED_GRACE_H", 1e6)
    return B.Profile(**{**profile.__dict__, "food_water_ml_day": 0.0})


# -- conservation ----------------------------------------------------------

def test_water_drunk_is_water_absorbed(frictionless, noon):
    """With no losses, a litre in is a litre absorbed -- eventually."""
    timeline = B.simulate(
        frictionless,
        [B.IntakeEvent(at=noon, volume_ml=1000.0)],
        start=noon - timedelta(minutes=5),
        end=noon + timedelta(hours=6),
    )
    assert timeline.final.absorbed_ml == pytest.approx(1000.0, abs=1.0)
    assert timeline.final.deficit_ml == pytest.approx(-1000.0, abs=1.0)
    assert timeline.final.gut_ml == pytest.approx(0.0, abs=1.0)


def test_water_is_not_absorbed_instantly(frictionless, noon):
    """...and not before. The deficit must not move the moment you swallow."""
    timeline = B.simulate(
        frictionless,
        [B.IntakeEvent(at=noon, volume_ml=1000.0)],
        start=noon - timedelta(minutes=5),
        end=noon + timedelta(hours=6),
    )
    five_min = timeline.at(noon + timedelta(minutes=5))
    assert five_min.gut_ml > 800.0
    assert five_min.absorbed_ml < 200.0


def test_hydration_index_scales_retention(frictionless, noon):
    """A drink with a hydration index above 1.0 does more per millilitre."""
    def absorbed(index: float) -> float:
        timeline = B.simulate(
            frictionless,
            [B.IntakeEvent(at=noon, volume_ml=500.0, hydration_index=index)],
            start=noon - timedelta(minutes=5),
            end=noon + timedelta(hours=6),
        )
        return timeline.final.absorbed_ml

    assert absorbed(1.5) == pytest.approx(absorbed(1.0) * 1.5, rel=0.01)


# -- the absorption cap ----------------------------------------------------

def test_chugging_two_litres_does_not_absorb_two_litres(frictionless, noon):
    """The constraint the whole plan rests on.

    Without the cap, advice collapses from a schedule to a single number, and
    the single number is wrong.
    """
    timeline = B.simulate(
        frictionless,
        [B.IntakeEvent(at=noon, volume_ml=2000.0)],
        start=noon - timedelta(minutes=5),
        end=noon + timedelta(hours=6),
    )
    ten_min = timeline.at(noon + timedelta(minutes=10))
    assert ten_min.absorbed_ml <= k.ABSORPTION_CAP_ML_PER_H * (10.0 / 60.0) + 1.0
    assert ten_min.gut_ml > 1700.0


def test_absorption_never_exceeds_the_hourly_cap(frictionless, noon):
    """Checked across every step, not just the convenient one."""
    timeline = B.simulate(
        frictionless,
        [B.IntakeEvent(at=noon + timedelta(minutes=30 * i), volume_ml=1500.0) for i in range(6)],
        start=noon - timedelta(minutes=5),
        end=noon + timedelta(hours=6),
    )
    per_step_cap = k.ABSORPTION_CAP_ML_PER_H * (k.STEP_MINUTES / 60.0)
    previous = 0.0
    for sample in timeline.samples:
        assert sample.absorbed_ml - previous <= per_step_cap + 1e-6
        previous = sample.absorbed_ml


# -- losses ----------------------------------------------------------------

def test_drinking_nothing_builds_a_deficit(profile, noon):
    """A day of nothing costs about a litre -- obligatory urine plus insensible
    loss, less what food and metabolism give back.

    The voids are here so the log counts as being kept: an empty event stream
    means 'stopped logging', and the ledger correctly stops believing it drank
    nothing. Observers are off so only the loss terms are under test.
    """
    keeping_the_log = [
        B.VoidEvent(at=noon + timedelta(hours=h), colour=4) for h in range(0, 24, 4)
    ]
    timeline = B.simulate(
        profile, keeping_the_log, start=noon, end=noon + timedelta(hours=24), apply_observers=False
    )
    assert 800.0 < timeline.final.deficit_ml < 1400.0


def test_silence_is_not_read_as_dehydration(profile, noon):
    """The failure this whole mechanism exists to prevent.

    An unbounded ledger reached nearly 20% of body mass over a fortnight of
    not logging -- a figure nobody survives -- and then raised medical warnings
    about it. Going quiet must converge on an ordinary day, and say so.
    """
    for days in (7, 14, 60):
        timeline = B.simulate(profile, [], start=noon - timedelta(days=days), end=noon)
        pct = profile.deficit_pct(timeline.final.deficit_ml)
        assert 0.0 < pct < 1.0, f"{days} days of silence read as {pct:.1f}% of body mass"
        assert timeline.confidence().is_stale


def test_a_synced_ride_is_still_believed_during_a_quiet_stretch(profile, noon):
    """Sweat is externally evidenced. Assuming you drank normally does not mean
    assuming you did not ride."""
    ride = [B.ActivityEvent(at=noon - timedelta(hours=3), duration_s=5400, sweat_ml=2000, kcal=1300)]
    timeline = B.simulate(profile, ride, start=noon - timedelta(days=7), end=noon)
    assert timeline.final.deficit_ml > 1800.0
    assert not timeline.confidence().is_stale, "a ride three hours ago is recent evidence"


def test_the_deficit_cannot_reach_impossible_values(profile, noon):
    """A backstop, not a model. Past ~6% of body mass a person is in hospital."""
    brutal = [
        B.ActivityEvent(at=noon + timedelta(hours=h), duration_s=7200, sweat_ml=4000, kcal=2500)
        for h in range(0, 20, 3)
    ]
    timeline = B.simulate(profile, brutal, start=noon, end=noon + timedelta(hours=24))
    assert profile.deficit_pct(timeline.final.deficit_ml) <= k.MAX_DEFICIT_PCT + 0.01


def test_surplus_is_shed_rather_than_banked(profile, noon):
    """Drinking far too much does not leave you indefinitely in surplus -- the
    kidneys dump it, which is why urine runs pale after a big drink."""
    timeline = B.simulate(
        profile,
        [B.IntakeEvent(at=noon, volume_ml=2500.0)],
        start=noon,
        end=noon + timedelta(hours=12),
    )
    trough = min(s.deficit_ml for s in timeline.samples)
    assert trough < -800.0, "should go into surplus at all"
    assert timeline.final.deficit_ml > trough + 500.0, "and then shed it"


def test_alcohol_diuresis_moves_the_stated_volume(frictionless, noon):
    """The mechanism in isolation: ten millilitres of urine per gram of ethanol.

    Checked against a body with no other losses, because in a realistic ledger
    this term overlaps with ordinary surplus diuresis -- both drain the same
    pool, and you cannot lose the same water twice.
    """
    def deficit(alcohol_g: float) -> float:
        events = [B.IntakeEvent(at=noon, volume_ml=40.0, alcohol_g=alcohol_g)]
        return B.simulate(
            frictionless, events, start=noon, end=noon + timedelta(hours=8)
        ).final.deficit_ml

    assert deficit(56.0) - deficit(0.0) == pytest.approx(56.0 * k.ALCOHOL_DIURESIS_ML_PER_G, rel=0.02)


def test_beer_roughly_breaks_even_where_spirits_dehydrate(profile, noon):
    """The result that catches people out, and a good check that the volume and
    the diuresis are both wired up.

    Four beers carry 1.4 L of water with them, and that outweighs the ethanol's
    effect -- which is why low-strength beer scores close to water on the
    beverage hydration index. Four shots carry almost no water and the same
    ethanol, so they leave you clearly worse off.
    """
    def deficit(volume_ml: float, alcohol_g: float, count: int = 4) -> float:
        events = [
            B.IntakeEvent(at=noon + timedelta(minutes=30 * i), volume_ml=volume_ml, alcohol_g=alcohol_g)
            for i in range(count)
        ]
        return B.simulate(profile, events, start=noon, end=noon + timedelta(hours=8)).final.deficit_ml

    drank_nothing = deficit(0.0, 0.0, count=0)
    beer = deficit(350.0, 14.0)
    spirits = deficit(40.0, 14.0)

    assert beer < drank_nothing, "the fluid in beer more than covers its own diuresis"
    assert spirits > drank_nothing + 250.0, "spirits bring the diuresis without the fluid"
    assert spirits > beer


def test_heat_increases_baseline_loss(profile, noon):
    """A hot room costs water even sitting still."""
    def deficit(temp_c: float, humidity: float) -> float:
        env = B.Environment.constant(temp_c, humidity)
        return B.simulate(
            profile, [], start=noon, end=noon + timedelta(hours=12), environment=env
        ).final.deficit_ml

    assert deficit(33.0, 70.0) > deficit(20.0, 45.0) * 1.1


# -- sweat -----------------------------------------------------------------

def test_sweat_estimate_matches_a_known_marathon(profile):
    """The calibration case the heat-fraction anchors were fitted to: a 3.5
    hour marathon at ~700 kcal/h in 18 C produces about 3 litres."""
    ml = sweat.estimate_sweat_ml(kcal=2450, duration_s=3.5 * 3600, temp_c=18.0, humidity_pct=60.0)
    assert 2600.0 <= ml <= 3300.0


def test_sweat_estimate_for_a_hard_hour():
    ml = sweat.estimate_sweat_ml(kcal=1000, duration_s=3600, temp_c=25.0, humidity_pct=50.0)
    assert 1200.0 <= ml <= 1700.0


def test_humidity_raises_sweat_loss_rather_than_lowering_it():
    """The term people get backwards. Sweat that cannot evaporate does not cool
    you, so the body makes more of it -- and all of it is still lost fluid."""
    dry = sweat.estimate_sweat_ml(kcal=800, duration_s=3600, temp_c=30.0, humidity_pct=25.0)
    humid = sweat.estimate_sweat_ml(kcal=800, duration_s=3600, temp_c=30.0, humidity_pct=85.0)
    assert humid > dry


def test_sweat_rate_is_capped_at_something_physiological():
    ml = sweat.estimate_sweat_ml(kcal=5000, duration_s=3600, temp_c=40.0, humidity_pct=95.0)
    assert ml <= k.MAX_SWEAT_RATE_ML_PER_H + 1.0


def test_cold_weather_burns_calories_without_the_same_fluid_cost():
    cold = sweat.estimate_sweat_ml(kcal=800, duration_s=3600, temp_c=2.0, humidity_pct=60.0)
    warm = sweat.estimate_sweat_ml(kcal=800, duration_s=3600, temp_c=28.0, humidity_pct=60.0)
    assert cold < warm * 0.7


def test_measured_sweat_beats_garmin_which_beats_our_estimate():
    both = sweat.resolve_sweat(measured_ml=1800, reported_ml=1500, estimated_ml=1200)
    assert both.source == "measured" and both.ml == 1800

    no_scale = sweat.resolve_sweat(measured_ml=None, reported_ml=1500, estimated_ml=1200)
    assert no_scale.source == "garmin" and no_scale.ml == 1500

    ours = sweat.resolve_sweat(measured_ml=None, reported_ml=None, estimated_ml=1200)
    assert ours.source == "estimated" and ours.ml == 1200

    nothing = sweat.resolve_sweat(measured_ml=None, reported_ml=None, estimated_ml=None)
    assert nothing.source == "none" and nothing.ml == 0.0


def test_calibration_needs_enough_evidence_and_stays_bounded():
    assert sweat.fit_calibration([(1000, 1200)], current=1.0)[0] == 1.0, "one pair is noise"

    fitted, count = sweat.fit_calibration([(1000, 1200)] * 4, current=1.0)
    assert count == 4 and fitted == pytest.approx(1.2)

    absurd, _ = sweat.fit_calibration([(100, 9000)] * 5, current=1.0)
    assert absurd == k.SWEAT_CALIBRATION_MAX, "one mistyped weight must not break the model"


# -- urine timing ----------------------------------------------------------

def test_timing_decides_how_much_a_colour_is_worth(noon):
    """The heart of the urine observer. Same colour, four contexts."""
    def moved(**kwargs) -> float:
        ctx = urine.VoidContext(colour=6, at=noon, **kwargs)
        return abs(urine.blend(0.0, urine.read(ctx), 75.0))

    fresh = moved(minutes_since_previous_void=60)
    ordinary = moved(minutes_since_previous_void=180)
    first_morning = moved(is_first_morning=True, minutes_since_previous_void=480)
    diluted = moved(minutes_since_significant_intake=20, minutes_since_previous_void=180)

    assert fresh > ordinary > first_morning > diluted
    assert diluted < fresh * 0.25, "a sample taken right after a big drink says almost nothing"


def test_first_morning_void_is_read_lighter_as_well_as_trusted_less(noon):
    """Two separate corrections, because they are two separate problems: the
    reading is biased dark *and* it is noisy."""
    assert urine.colour_to_deficit_pct(6, is_first_morning=True) < urine.colour_to_deficit_pct(6)


def test_a_supplement_makes_the_colour_meaningless(noon):
    ctx = urine.VoidContext(colour=7, at=noon, minutes_since_multivitamin=90)
    reading = urine.read(ctx)
    assert reading.confidence <= 0.1
    assert "riboflavin" in reading.reasons[0]


def test_pale_urine_reads_as_surplus_not_merely_fine():
    assert urine.colour_to_deficit_pct(1) < 0
    assert urine.colour_to_deficit_pct(8) > urine.colour_to_deficit_pct(4)


def test_an_implausible_weight_swing_is_discarded():
    """Five kilos overnight is a different scale or a typo, not body water.
    Treating it as a five-litre deficit would be actively dangerous."""
    assert urine.deficit_from_weight_ml(70.0, 75.0) is None
    assert urine.deficit_from_weight_ml(74.0, 75.0) == pytest.approx(1000.0)


def test_a_void_corrects_the_ledger_and_records_why(profile, noon):
    timeline = B.simulate(
        profile,
        [B.VoidEvent(at=noon + timedelta(hours=2), colour=7)],
        start=noon,
        end=noon + timedelta(hours=4),
    )
    assert len(timeline.corrections) == 1
    correction = timeline.corrections[0]
    assert correction.kind == "urine"
    assert correction.shift_ml > 0, "dark urine should push the estimate drier"
    assert correction.reasons


def test_pre_and_post_activity_weights_are_not_read_as_hydration_state(profile, noon):
    """They measure a change and are paired into a sweat loss elsewhere. Read
    as absolute state too, that sweat would be counted twice."""
    timeline = B.simulate(
        profile,
        [
            B.WeightEvent(at=noon, mass_kg=75.0, context="morning"),
            B.WeightEvent(at=noon + timedelta(hours=1), mass_kg=73.0, context="post_activity"),
        ],
        start=noon,
        end=noon + timedelta(hours=3),
    )
    assert all(c.kind != "weight" or "morning" in c.reasons[0] for c in timeline.corrections)
    assert not any(c.observed_ml > 1500 for c in timeline.corrections)


# -- sodium ----------------------------------------------------------------

def _timeline_after_a_big_sweat(profile: B.Profile, noon: datetime) -> B.Timeline:
    return B.simulate(
        profile,
        [B.ActivityEvent(at=noon, duration_s=5400, sweat_ml=2100, kcal=1400)],
        start=noon - timedelta(hours=1),
        end=noon + timedelta(hours=3),
    )


def test_a_big_sweat_triggers_sodium_advice(profile, noon):
    advice = electrolytes.assess(_timeline_after_a_big_sweat(profile, noon), profile, planned_intake_ml=2000)
    assert advice.recommend
    assert advice.recommended_mg > 0
    assert advice.gap_mg > 1000


def test_a_quiet_day_does_not_trigger_sodium_advice(profile, noon):
    timeline = B.simulate(profile, [], start=noon, end=noon + timedelta(hours=4))
    assert not electrolytes.assess(timeline, profile).recommend


def test_saltier_sweat_means_a_stronger_drink(profile):
    salty = B.Profile(**{**profile.__dict__, "sweat_sodium_mmol_l": 75.0})
    mild = B.Profile(**{**profile.__dict__, "sweat_sodium_mmol_l": 25.0})
    assert electrolytes.concentration_for(salty) > electrolytes.concentration_for(mild)
    assert k.SODIUM_REPLACE_MG_PER_L_LOW <= electrolytes.concentration_for(mild)
    assert electrolytes.concentration_for(salty) <= k.SODIUM_REPLACE_MG_PER_L_HIGH


# -- the guard that points the other way -----------------------------------

def test_overdrinking_dilute_fluid_is_warned_about(profile, noon):
    """Most hydration apps nag in one direction only. This is the other one,
    and it is the one that kills endurance athletes."""
    events = [
        B.IntakeEvent(at=noon + timedelta(minutes=10 * i), volume_ml=400.0) for i in range(5)
    ]
    timeline = B.simulate(profile, events, start=noon - timedelta(hours=1), end=noon + timedelta(minutes=50))
    warning = electrolytes.overdrink_check(timeline, profile)
    assert warning is not None
    assert "hyponatraemia" in warning.message


def test_someone_genuinely_dehydrated_is_not_told_to_slow_down(profile, noon):
    """Drinking fast while two litres down is the right thing to do."""
    events = [B.ActivityEvent(at=noon - timedelta(hours=2), duration_s=5400, sweat_ml=2600, kcal=1600)]
    events += [B.IntakeEvent(at=noon + timedelta(minutes=10 * i), volume_ml=400.0) for i in range(5)]
    timeline = B.simulate(profile, events, start=noon - timedelta(hours=3), end=noon + timedelta(minutes=50))
    assert electrolytes.overdrink_check(timeline, profile) is None


def test_sodium_in_the_fluid_defuses_the_warning(profile, noon):
    events = [
        B.IntakeEvent(at=noon + timedelta(minutes=10 * i), volume_ml=400.0, sodium_mg=200.0)
        for i in range(5)
    ]
    timeline = B.simulate(profile, events, start=noon - timedelta(hours=1), end=noon + timedelta(minutes=50))
    assert electrolytes.overdrink_check(timeline, profile) is None


# -- the plan --------------------------------------------------------------

def _plan_for(profile: B.Profile, events, *, now: datetime, hours: float = 8.0, **kwargs) -> P.Plan:
    timeline = B.simulate(profile, events, start=now - timedelta(hours=hours), end=now)
    return P.make_plan(timeline, profile, now=now, **kwargs)


def test_a_plan_never_asks_for_more_than_can_be_absorbed(profile):
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=3), duration_s=10800, sweat_ml=3600, kcal=2400)],
        now=now,
        hours=10.0,
    )
    assert result.doses
    span_h = (result.doses[-1].at - result.doses[0].at).total_seconds() / 3600.0
    interval_h = span_h / max(1, len(result.doses) - 1) if len(result.doses) > 1 else 1.0
    rate = result.doses[0].volume_ml / interval_h
    assert rate <= profile.absorption_cap_ml_h * 1.05


def test_no_single_dose_is_larger_than_a_mouthful(profile):
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=2), duration_s=7200, sweat_ml=2800, kcal=1800)],
        now=now,
        hours=9.0,
    )
    assert all(dose.volume_ml <= k.MAX_BOLUS_ML + 1.0 for dose in result.doses)


def test_nothing_is_scheduled_close_to_bedtime(profile):
    """An app that costs you sleep to fix a rounding error has made your day
    worse."""
    late = datetime(2026, 9, 10, 22, 15, tzinfo=profile.tz).astimezone(UTC)
    # A recent drink, so the estimate is current rather than a guess -- this
    # test is about the bedtime rule, not about staleness.
    recent = [B.IntakeEvent(at=late - timedelta(minutes=30), volume_ml=200.0)]
    result = _plan_for(profile, recent, now=late)
    assert result.doses == []
    assert "bedtime" in result.headline.lower()


def test_a_severe_deficit_overrides_the_bedtime_cutoff(profile):
    """The cutoff is a comfort rule, not a safety rule, and it loses to one."""
    late = datetime(2026, 9, 10, 22, 15, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.ActivityEvent(at=late - timedelta(hours=4), duration_s=10800, sweat_ml=4000, kcal=2600)],
        now=late,
        hours=10.0,
    )
    assert result.doses, "3%+ down is worth a broken night"
    assert result.status == "drink_urgent"


def test_fluid_already_in_the_stomach_is_not_asked_for_twice(profile):
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    dry = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=3), duration_s=5400, sweat_ml=2100, kcal=1400)],
        now=now,
    )
    just_drunk = _plan_for(
        profile,
        [
            B.ActivityEvent(at=now - timedelta(hours=3), duration_s=5400, sweat_ml=2100, kcal=1400),
            B.IntakeEvent(at=now - timedelta(minutes=5), volume_ml=800.0),
        ],
        now=now,
    )
    assert just_drunk.total_planned_ml < dry.total_planned_ml


def test_a_ride_that_already_happened_is_not_replaced_twice(profile):
    """Regression: folding past sweat into the forward maintenance rate told a
    rider 2.2 L down to drink 3.6 L."""
    now = datetime(2026, 9, 10, 14, 0, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=4), duration_s=5400, sweat_ml=2100, kcal=1400)],
        now=now,
    )
    assert result.total_planned_ml < result.deficit_ml * 1.5


def test_an_upcoming_session_is_pre_loaded_for(profile):
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    ride = P.UpcomingActivity(at=now + timedelta(hours=3), duration_s=5400, expected_sweat_ml=1800)
    without = _plan_for(profile, [], now=now)
    with_ride = _plan_for(profile, [], now=now, upcoming=ride)
    assert with_ride.total_planned_ml > without.total_planned_ml
    assert any("Pre-loading" in line for line in with_ride.detail)


def test_the_overdrink_warning_replaces_the_schedule(profile):
    """Handing someone a drinking plan and a stop-drinking warning at the same
    time is worse than useless."""
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.IntakeEvent(at=now - timedelta(minutes=10 * i), volume_ml=450.0) for i in range(5)],
        now=now,
    )
    assert result.status == "slow_down"
    assert result.doses == []


def test_the_headline_stands_alone(profile):
    """Home Assistant shows this line and nothing else, so it has to make sense
    with no surrounding context."""
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=3), duration_s=5400, sweat_ml=2100, kcal=1400)],
        now=now,
    )
    assert result.headline
    assert "L" in result.headline
    assert result.headline[0].isupper() and result.headline.rstrip().endswith(".")


def test_medical_flags_are_rare_and_specific(profile):
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    quiet = _plan_for(profile, [B.IntakeEvent(at=now - timedelta(hours=1), volume_ml=400.0)], now=now)
    assert quiet.medical_flags == []

    bad = _plan_for(
        profile,
        [B.ActivityEvent(at=now - timedelta(hours=4), duration_s=14400, sweat_ml=4500, kcal=3000)],
        now=now,
        hours=10.0,
        hours_since_last_void=14.0,
    )
    assert len(bad.medical_flags) == 2


def test_a_normal_night_of_sleep_is_not_a_medical_flag(profile):
    """Eight hours without passing urine is called 'asleep'. Flagging it every
    morning is how a warning stops being read."""
    now = datetime(2026, 9, 10, 7, 30, tzinfo=profile.tz).astimezone(UTC)
    result = _plan_for(profile, [], now=now, hours_since_last_void=9.0)
    assert result.medical_flags == []


def test_a_guess_never_raises_a_medical_flag(profile):
    """A warning derived from no data is a false alarm, and false alarms are
    how a real one gets ignored."""
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    stale = _plan_for(profile, [], now=now, hours_since_last_void=30.0)
    assert stale.confidence.is_stale
    assert stale.medical_flags == []
    assert stale.status == "unknown"
    assert "not enough recent data" in stale.headline.lower()


def test_status_is_one_of_the_documented_tokens(profile):
    """Home Assistant colours a card from this, so a new value must be a
    deliberate change, not a typo."""
    now = datetime(2026, 9, 10, 13, 0, tzinfo=profile.tz).astimezone(UTC)
    allowed = {"ok", "drink", "drink_urgent", "add_sodium", "slow_down", "unknown"}
    for events in ([], [B.ActivityEvent(at=now - timedelta(hours=2), duration_s=5400, sweat_ml=2100, kcal=1400)]):
        assert _plan_for(profile, events, now=now).status in allowed


# -- guarding the one unbounded input --------------------------------------

def test_garmins_reported_sweat_is_bounded_before_it_is_believed():
    """It arrives from an undocumented API whose units are not ours to rely on.
    A single litres-for-millilitres change would otherwise put a 1500 litre
    sweat loss straight into the ledger."""
    absurd = sweat.resolve_sweat(
        measured_ml=None, reported_ml=1_500_000, estimated_ml=1200, duration_s=3600
    )
    assert absurd.source == "estimated", "an impossible figure must not win"
    assert absurd.ml == 1200
    assert absurd.reported_ml == 1_500_000, "but it is still recorded, for the comparison"

    sane = sweat.resolve_sweat(
        measured_ml=None, reported_ml=1500, estimated_ml=1200, duration_s=3600
    )
    assert sane.source == "garmin"


# -- subjective feedback as a third observer -------------------------------

def test_how_the_day_felt_moves_the_ledger(profile, noon):
    def deficit_after(verdict: str) -> float:
        events = [B.FeedbackEvent(at=noon + timedelta(hours=1), verdict=verdict)]
        return B.simulate(profile, events, start=noon, end=noon + timedelta(hours=2)).final.deficit_ml

    assert deficit_after("very_dry") > deficit_after("about_right") > deficit_after("waterlogged")


def test_a_single_day_of_feeling_is_not_authoritative(profile, noon):
    """It is one impression, not a measurement. It should nudge, not overrule."""
    events = [B.ActivityEvent(at=noon, duration_s=5400, sweat_ml=2500, kcal=1500),
              B.FeedbackEvent(at=noon + timedelta(hours=3), verdict="about_right")]
    timeline = B.simulate(profile, events, start=noon, end=noon + timedelta(hours=4))
    correction = next(c for c in timeline.corrections if c.kind == "feedback")
    assert correction.confidence == k.TRUST_FEEDBACK
    assert abs(correction.shift_ml) < abs(correction.ledger_ml - correction.observed_ml)


def test_an_unknown_verdict_is_ignored_rather_than_crashing(profile, noon):
    events = [B.FeedbackEvent(at=noon + timedelta(hours=1), verdict="ecstatic")]
    timeline = B.simulate(profile, events, start=noon, end=noon + timedelta(hours=2))
    assert not [c for c in timeline.corrections if c.kind == "feedback"]


def test_a_higher_baseline_scale_costs_more_water(profile, noon):
    """What the fitted scale actually does: change what the model expects of an
    ordinary day."""
    def deficit(scale: float) -> float:
        p = B.Profile(**{**profile.__dict__, "baseline_loss_scale": scale})
        keeping_the_log = [B.VoidEvent(at=noon + timedelta(hours=h), colour=4) for h in range(0, 24, 4)]
        return B.simulate(
            p, keeping_the_log, start=noon, end=noon + timedelta(hours=24), apply_observers=False
        ).final.deficit_ml

    assert deficit(1.25) > deficit(1.0) > deficit(0.8)


# -- the step counter ------------------------------------------------------
#
# `simulate` walks steps and looks each step's sweat and burn up by step
# number. The observer loop used to bind its own loop variable to the same
# name, so the first void or weighing in a window rewound the counter to a
# position in the *event list* -- and every step after that read the rate
# tables at the wrong index. The symptom was the worst kind: no error, a
# plausible number, and it got worse the more carefully the log was kept.

def test_a_ride_is_charged_at_the_hour_it_happened(profile, noon):
    """Regardless of what else was logged earlier in the window."""
    ride = B.ActivityEvent(
        at=noon + timedelta(hours=6), duration_s=3600, sweat_ml=1500.0, kcal=900.0
    )

    def first_sweating_sample(events) -> datetime:
        timeline = B.simulate(
            profile, events, start=noon, end=noon + timedelta(hours=18),
            environment=B.Environment.constant(21.0, 45.0),
        )
        return next(sample.at for sample in timeline.samples if sample.sweat_ml > 0)

    alone = first_sweating_sample([ride])
    voids = [B.VoidEvent(at=noon + timedelta(hours=h), colour=4) for h in (1, 2, 3, 4)]
    assert first_sweating_sample([ride, *voids]) == alone


def test_a_well_kept_log_does_not_lose_a_rides_sweat(profile):
    """The three-day window `current_state` actually runs, with the ride near
    the end of it. The rewound counter never reached the ride's own steps, so
    two litres of sweat left the ledger entirely."""
    end = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    start = end - timedelta(days=3)
    ride = B.ActivityEvent(
        at=end - timedelta(hours=4), duration_s=5400, sweat_ml=2000.0, kcal=1300.0
    )

    diligent = [ride]
    moment = start
    while moment < end:
        diligent.append(B.IntakeEvent(at=moment, volume_ml=250.0))
        diligent.append(B.VoidEvent(at=moment + timedelta(minutes=30), colour=4))
        moment += timedelta(hours=2)

    timeline = B.simulate(
        profile, diligent, start=start, end=end,
        environment=B.Environment.constant(21.0, 45.0),
    )
    assert timeline.final.sweat_ml == pytest.approx(2000.0, rel=0.01)


def test_the_burn_lands_on_the_same_steps_as_the_sweat(profile, noon):
    """Both rate tables are keyed by step number, so both moved together."""
    ride = B.ActivityEvent(
        at=noon + timedelta(hours=6), duration_s=3600, sweat_ml=1500.0, kcal=900.0
    )
    timeline = B.simulate(
        profile, [ride, B.VoidEvent(at=noon + timedelta(hours=1), colour=4)],
        start=noon, end=noon + timedelta(hours=18),
        environment=B.Environment.constant(21.0, 45.0),
    )
    sweating = [s.at for s in timeline.samples if s.sweat_ml > 0]
    burning = [s.at for s in timeline.samples if s.activity_kcal > 0]
    assert sweating[0] == burning[0]
    assert timeline.final.activity_kcal == pytest.approx(900.0, rel=0.01)


# -- caffeine --------------------------------------------------------------
#
# The whole chain existed and reached the ledger: a caffeine figure on every
# beverage, a per-drink override, a profile column, a threshold constant, a
# field on the event and a slot on the state. Nothing read any of it, so a
# person who set the sensitivity got the same answer as a person who did not.

def test_caffeine_does_nothing_by_default(profile, noon):
    """Which is the honest default -- habitual drinkers show no net diuresis.
    It has to stay true after wiring the term up."""
    coffee = [B.IntakeEvent(at=noon, volume_ml=500.0, caffeine_mg=400.0)]
    water = [B.IntakeEvent(at=noon, volume_ml=500.0)]

    def urine(events):
        return B.simulate(
            profile, events, start=noon, end=noon + timedelta(hours=8)
        ).final.urine_ml

    assert urine(coffee) == pytest.approx(urine(water))


def _sensitive(profile: B.Profile, ml_per_mg: float = 1.0) -> B.Profile:
    return B.Profile(**{**profile.__dict__, "caffeine_diuresis_ml_per_mg": ml_per_mg})


def test_someone_caffeine_affects_passes_more_after_a_heavy_morning(profile, noon):
    sensitive = _sensitive(profile)
    heavy = [B.IntakeEvent(at=noon + timedelta(minutes=30 * n), volume_ml=250.0, caffeine_mg=200.0)
             for n in range(4)]
    plain = [B.IntakeEvent(at=noon + timedelta(minutes=30 * n), volume_ml=250.0) for n in range(4)]

    def urine(p, events):
        return B.simulate(p, events, start=noon, end=noon + timedelta(hours=10)).final.urine_ml

    assert urine(sensitive, heavy) > urine(sensitive, plain)


def test_a_single_coffee_stays_under_the_threshold(profile, noon):
    """It is a threshold effect, not a linear one. The third coffee is the one
    that does something, not the first."""
    sensitive = _sensitive(profile)

    def urine(events):
        return B.simulate(
            sensitive, events, start=noon, end=noon + timedelta(hours=10)
        ).final.urine_ml

    one = [B.IntakeEvent(at=noon, volume_ml=250.0, caffeine_mg=100.0)]
    plain = [B.IntakeEvent(at=noon, volume_ml=250.0)]
    assert urine(one) == pytest.approx(urine(plain))


def test_the_caffeine_load_falls_away_between_doses(profile, noon):
    """Two coffees a day apart must not add up the way two an hour apart do --
    the load is a running quantity with caffeine's own half-life, not a total."""
    sensitive = _sensitive(profile)

    def urine(gap_h):
        events = [
            B.IntakeEvent(at=noon, volume_ml=250.0, caffeine_mg=250.0),
            B.IntakeEvent(at=noon + timedelta(hours=gap_h), volume_ml=250.0, caffeine_mg=250.0),
        ]
        return B.simulate(
            sensitive, events, start=noon, end=noon + timedelta(hours=48)
        ).final.urine_ml

    assert urine(1) > urine(24)


# -- the observers report what they actually used --------------------------

def test_a_weight_correction_quotes_the_trend_it_compared_against(profile, noon):
    """Not the trend after this morning's reading was folded in. Explaining a
    shift with a figure that had no part in producing it is worse than not
    explaining it."""
    events = [
        B.WeightEvent(at=noon - timedelta(days=2), mass_kg=75.0, context="morning"),
        B.WeightEvent(at=noon, mass_kg=73.5, context="morning"),
    ]
    timeline = B.simulate(profile, events, start=noon - timedelta(days=3), end=noon + timedelta(hours=1))
    correction = next(c for c in timeline.corrections if c.kind == "weight")
    assert "75.0 kg trend" in correction.reasons[0]


def test_the_profiles_urine_trust_reaches_the_ledger(profile, noon):
    """`urine.blend` is what the ledger runs, so the setting and the tested
    function cannot disagree."""
    def shift(trust: float) -> float:
        p = B.Profile(**{**profile.__dict__, "trust_urine": trust})
        timeline = B.simulate(
            p, [B.VoidEvent(at=noon + timedelta(hours=1), colour=7)],
            start=noon, end=noon + timedelta(hours=2),
        )
        return abs(next(c for c in timeline.corrections if c.kind == "urine").shift_ml)

    assert shift(0.9) > shift(0.6) > shift(0.1)
