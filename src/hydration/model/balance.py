"""The water-balance ledger.

Two compartments, stepped five minutes at a time:

    gut_ml      fluid swallowed but not yet absorbed
    deficit_ml  body water owed, relative to euhydrated. Positive means dry.

Every term that moves either one is in this file or imported into it, and the
whole thing is a pure function: dataclasses in, a timeline out, no database.
That is what makes the model testable without a browser, which matters because
the model is where all the actual risk lives.

The ledger on its own would drift -- every constant in it is a population
average being applied to one specific person. What keeps it honest is that
observations pull it back: a urine colour, a morning weight. Those corrections
happen here too, at the timestamp they were observed, and each one is recorded
so the history page can show where the model was wrong and by how much.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import constants as k
from . import sweat as sweat_model
from . import urine as urine_model


# -- inputs ----------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """The person the model is about. Mirrors the `profile` table."""

    body_mass_kg: float = 75.0
    height_cm: float = 178.0
    sex: str = "male"
    birth_year: int = 1995
    timezone: str = "America/New_York"

    wake_hour: float = 7.0
    bed_hour: float = 23.0

    sweat_sodium_mmol_l: float = k.SWEAT_SODIUM_MMOL_PER_L
    sweat_calibration: float = 1.0
    absorption_cap_ml_h: float = k.ABSORPTION_CAP_ML_PER_H
    food_water_ml_day: float = k.FOOD_WATER_ML_PER_DAY
    caffeine_diuresis_ml_per_mg: float = k.CAFFEINE_DIURESIS_ML_PER_MG
    trust_urine: float = k.TRUST_URINE

    default_temp_c: float = 21.0
    default_humidity_pct: float = 45.0

    @property
    def age(self) -> int:
        return max(1, datetime.now(timezone.utc).year - self.birth_year)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def rmr_kcal_per_day(self) -> float:
        """Mifflin-St Jeor. Used only for the metabolic-water term, where a
        ten percent error is a few millilitres a day."""
        base = 10.0 * self.body_mass_kg + 6.25 * self.height_cm - 5.0 * self.age
        if self.sex == "male":
            return base + 5.0
        if self.sex == "female":
            return base - 161.0
        return base - 78.0

    def deficit_pct(self, deficit_ml: float) -> float:
        """Deficit as percent of body mass -- the unit the thresholds use."""
        if self.body_mass_kg <= 0:
            return 0.0
        return deficit_ml / 1000.0 / self.body_mass_kg * 100.0

    def pct_to_ml(self, pct: float) -> float:
        return pct / 100.0 * self.body_mass_kg * 1000.0


@dataclass(frozen=True)
class IntakeEvent:
    at: datetime
    volume_ml: float
    hydration_index: float = 1.0
    sodium_mg: float = 0.0
    caffeine_mg: float = 0.0
    alcohol_g: float = 0.0
    is_multivitamin: bool = False


@dataclass(frozen=True)
class VoidEvent:
    at: datetime
    colour: int
    volume_ml: float | None = None
    is_first_morning: bool = False


@dataclass(frozen=True)
class WeightEvent:
    at: datetime
    mass_kg: float
    context: str = "morning"  # morning | pre_activity | post_activity | other


@dataclass(frozen=True)
class ActivityEvent:
    at: datetime
    duration_s: float
    sweat_ml: float
    kcal: float = 0.0
    fluid_consumed_ml: float = 0.0


@dataclass(frozen=True)
class MealEvent:
    at: datetime
    water_ml: float = 0.0
    sodium_mg: float = 0.0


Event = IntakeEvent | VoidEvent | WeightEvent | ActivityEvent | MealEvent


class Environment:
    """Ambient conditions over time, from Home Assistant.

    Samples arrive every fifteen minutes or so and the ledger asks for
    arbitrary instants, so this holds the most recent reading at or before the
    instant asked for, and falls back to the profile's defaults when asked
    about a time before any sensor data exists.
    """

    def __init__(self, samples: list[tuple[datetime, float, float]], default_temp_c: float, default_humidity_pct: float):
        self._samples = sorted(samples, key=lambda s: s[0])
        self._default = (default_temp_c, default_humidity_pct)
        self._cursor = 0

    def at(self, when: datetime) -> tuple[float, float]:
        # The ledger walks forward in time, so the cursor almost never rewinds;
        # this keeps a month of history linear rather than quadratic.
        if self._cursor > 0 and self._samples[self._cursor - 1][0] > when:
            self._cursor = 0
        while self._cursor < len(self._samples) and self._samples[self._cursor][0] <= when:
            self._cursor += 1
        if self._cursor == 0:
            return self._default
        _, temp_c, humidity = self._samples[self._cursor - 1]
        return temp_c, humidity

    @classmethod
    def constant(cls, temp_c: float, humidity_pct: float) -> "Environment":
        return cls([], temp_c, humidity_pct)


# -- outputs ---------------------------------------------------------------

@dataclass
class Sample:
    """The ledger's state at one instant.

    The cumulative counters are cumulative on purpose: any windowed question
    ('how much did I sweat in the last four hours') is then a subtraction
    between two samples rather than a second pass over the events.
    """

    at: datetime
    deficit_ml: float
    gut_ml: float
    temp_c: float
    humidity_pct: float

    intake_ml: float = 0.0
    absorbed_ml: float = 0.0
    sweat_ml: float = 0.0
    urine_ml: float = 0.0
    insensible_ml: float = 0.0
    sweat_sodium_mg: float = 0.0
    supplemental_sodium_mg: float = 0.0
    activity_kcal: float = 0.0


@dataclass
class Correction:
    """A moment where an observation disagreed with the ledger.

    Kept in full -- what the model thought, what was observed, where it landed
    and why that much -- because the sequence of these is the evidence for
    whether the model's constants suit this particular person.
    """

    at: datetime
    kind: str  # 'urine' | 'weight'
    ledger_ml: float
    observed_ml: float
    blended_ml: float
    confidence: float
    reasons: list[str] = field(default_factory=list)

    @property
    def shift_ml(self) -> float:
        return self.blended_ml - self.ledger_ml


@dataclass
class Timeline:
    profile: Profile
    samples: list[Sample]
    corrections: list[Correction]
    weight_trend_kg: float | None = None

    @property
    def final(self) -> Sample:
        return self.samples[-1]

    def at(self, when: datetime) -> Sample:
        """Nearest sample at or before `when`, or the first if `when` predates
        the simulation."""
        chosen = self.samples[0]
        for sample in self.samples:
            if sample.at > when:
                break
            chosen = sample
        return chosen

    def delta(self, attr: str, hours: float, *, end: datetime | None = None) -> float:
        """How much a cumulative counter advanced over the last `hours`."""
        end = end or self.final.at
        start = self.at(end - timedelta(hours=hours))
        return getattr(self.at(end), attr) - getattr(start, attr)


# -- the simulation --------------------------------------------------------

def simulate(
    profile: Profile,
    events: list[Event],
    *,
    start: datetime,
    end: datetime,
    environment: Environment | None = None,
    initial_deficit_ml: float = 0.0,
    initial_weight_trend_kg: float | None = None,
    step_minutes: int = k.STEP_MINUTES,
    apply_observers: bool = True,
) -> Timeline:
    """Run the ledger from `start` to `end`.

    `initial_deficit_ml` is normally zero: anchoring a few days back and
    letting the observers pull the estimate into line is more robust than
    trying to know the true starting value, and by the time the window reaches
    the present the anchor has washed out.
    """
    env = environment or Environment.constant(profile.default_temp_c, profile.default_humidity_pct)
    step = timedelta(minutes=step_minutes)
    step_h = step_minutes / 60.0

    ordered = sorted(events, key=_event_time)
    sweat_rate, kcal_rate = _activity_rates(ordered, start, end, step, step_minutes)

    state = _State(
        deficit_ml=initial_deficit_ml,
        gut_ml=0.0,
        weight_trend_kg=initial_weight_trend_kg,
    )
    cumulative = Sample(at=start, deficit_ml=initial_deficit_ml, gut_ml=0.0, temp_c=0.0, humidity_pct=0.0)
    samples: list[Sample] = []
    corrections: list[Correction] = []

    # The sample recorded for an instant is the state *as of* that instant --
    # everything up to and including it, nothing after. So the initial state is
    # recorded before any step runs, and each step's result is labelled with
    # the time it ends. Labelling a step's result with the time it *started*
    # reads one step into the future, which is a quiet off-by-one that makes
    # the absorption cap look violated when it is not.
    samples.append(
        replace(cumulative, at=start, deficit_ml=state.deficit_ml, gut_ml=state.gut_ml,
                temp_c=env.at(start)[0], humidity_pct=env.at(start)[1])
    )

    event_idx = 0
    when = start
    index = 0
    while when < end:
        temp_c, humidity = env.at(when)
        window_end = when + step

        # 1. Events landing inside this step. Intakes reach the stomach and
        #    meals count immediately; observers wait until after the physics so
        #    they correct the state as it stands at their timestamp.
        pending_observers: list[Event] = []
        while event_idx < len(ordered) and _event_time(ordered[event_idx]) < window_end:
            event = ordered[event_idx]
            event_idx += 1
            if _event_time(event) < start:
                continue
            if isinstance(event, IntakeEvent):
                _apply_intake(state, cumulative, event)
            elif isinstance(event, MealEvent):
                state.gut_ml += event.water_ml
                cumulative.intake_ml += event.water_ml
                cumulative.supplemental_sodium_mg += event.sodium_mg
            elif isinstance(event, ActivityEvent):
                # Fluid drunk during an activity that Garmin recorded but that
                # was never logged as a drink. Counted once, here.
                if event.fluid_consumed_ml:
                    state.gut_ml += event.fluid_consumed_ml
                    cumulative.intake_ml += event.fluid_consumed_ml
            else:
                pending_observers.append(event)

        # 2. Physics for this step.
        _step_physics(profile, state, cumulative, temp_c, humidity, step_h, index, sweat_rate, kcal_rate, when)

        # 3. Observers.
        if apply_observers:
            for event in pending_observers:
                correction = _apply_observer(profile, state, ordered, event)
                if correction is not None:
                    corrections.append(correction)

        samples.append(
            replace(
                cumulative,
                at=window_end,
                deficit_ml=state.deficit_ml,
                gut_ml=state.gut_ml,
                temp_c=temp_c,
                humidity_pct=humidity,
            )
        )
        when = window_end
        index += 1

    return Timeline(profile=profile, samples=samples, corrections=corrections, weight_trend_kg=state.weight_trend_kg)


@dataclass
class _State:
    deficit_ml: float
    gut_ml: float
    weight_trend_kg: float | None
    alcohol_pending_ml: float = 0.0
    caffeine_pending_ml: float = 0.0


def _event_time(event: Event) -> datetime:
    return event.at


def _apply_intake(state: _State, cumulative: Sample, event: IntakeEvent) -> None:
    """A drink lands in the stomach, not in the bloodstream.

    The hydration index is applied here rather than at absorption. Strictly it
    describes how much of a drink is *retained* an hour or two later -- milk
    and oral rehydration solution beat water because their sodium and their
    slower emptying reduce the urine that follows -- so folding it in at the
    gut is a simplification. It puts the effect in the right direction and
    roughly the right size, which is what the ledger needs; modelling retention
    properly would mean modelling osmolality, and nothing here would use it.
    """
    state.gut_ml += event.volume_ml * event.hydration_index
    cumulative.intake_ml += event.volume_ml
    cumulative.supplemental_sodium_mg += event.sodium_mg
    state.alcohol_pending_ml += event.alcohol_g * k.ALCOHOL_DIURESIS_ML_PER_G


def _step_physics(
    profile: Profile,
    state: _State,
    cumulative: Sample,
    temp_c: float,
    humidity: float,
    step_h: float,
    index: int,
    sweat_rate: dict[int, float],
    kcal_rate: dict[int, float],
    when: datetime,
) -> None:
    mass = profile.body_mass_kg

    # -- absorption: gut -> body ------------------------------------------
    # First-order emptying, but capped. The cap is what makes chugging fail:
    # without it a litre swallowed would be a litre absorbed within the hour,
    # and the advice would collapse to a single number instead of a schedule.
    emptied = state.gut_ml * (1.0 - math.exp(-step_h * 60.0 / k.GASTRIC_TAU_MIN))
    emptied = min(emptied, profile.absorption_cap_ml_h * step_h)
    state.gut_ml -= emptied
    state.deficit_ml -= emptied
    cumulative.absorbed_ml += emptied

    # -- insensible loss ---------------------------------------------------
    heat_index = sweat_model.heat_index_c(temp_c, humidity)
    heat_multiple = 1.0 + k.INSENSIBLE_HEAT_COEFF * max(0.0, heat_index - k.INSENSIBLE_HEAT_BASE_C)
    heat_multiple = min(heat_multiple, k.INSENSIBLE_HEAT_MAX_MULTIPLE)
    insensible = k.INSENSIBLE_ML_PER_KG_H * mass * step_h * heat_multiple
    insensible += k.FECAL_ML_PER_DAY * step_h / 24.0
    state.deficit_ml += insensible
    cumulative.insensible_ml += insensible

    # -- sweat -------------------------------------------------------------
    sweat_ml = sweat_rate.get(index, 0.0)
    if sweat_ml:
        state.deficit_ml += sweat_ml
        cumulative.sweat_ml += sweat_ml
        cumulative.sweat_sodium_mg += (
            sweat_ml / 1000.0 * profile.sweat_sodium_mmol_l * k.MG_PER_MMOL_SODIUM
        )

    # -- urine -------------------------------------------------------------
    # The floor leaves regardless; surplus on top of it is the body shedding
    # water it does not need, which is exactly why urine runs pale after a big
    # drink. That behaviour is emergent here, not special-cased anywhere.
    urine = k.OBLIGATORY_URINE_ML_PER_KG_H * mass * step_h
    if state.deficit_ml < 0:
        surplus = -state.deficit_ml
        shed = surplus * (1.0 - 0.5 ** (step_h * 60.0 / k.DIURESIS_HALFLIFE_MIN))
        urine += min(shed, k.MAX_DIURESIS_ML_PER_MIN * step_h * 60.0)

    if state.alcohol_pending_ml > 0:
        released = state.alcohol_pending_ml * (1.0 - math.exp(-step_h * 60.0 / k.ALCOHOL_DIURESIS_TAU_MIN))
        state.alcohol_pending_ml -= released
        urine += released

    state.deficit_ml += urine
    cumulative.urine_ml += urine

    # -- gains that are not drinks ----------------------------------------
    activity_kcal = kcal_rate.get(index, 0.0)
    cumulative.activity_kcal += activity_kcal
    total_kcal = profile.rmr_kcal_per_day * step_h / 24.0 + activity_kcal
    metabolic = total_kcal * k.METABOLIC_WATER_ML_PER_KCAL
    state.deficit_ml -= metabolic

    # Food water trickles across waking hours only -- you are not eating at
    # three in the morning, and spreading it over the full day makes the model
    # read wet at breakfast and dry at bedtime.
    local_hour = when.astimezone(profile.tz).hour + when.astimezone(profile.tz).minute / 60.0
    awake_hours = _awake_hours(profile)
    if _is_awake(profile, local_hour) and awake_hours > 0:
        state.deficit_ml -= profile.food_water_ml_day * step_h / awake_hours


def _awake_hours(profile: Profile) -> float:
    span = profile.bed_hour - profile.wake_hour
    return span if span > 0 else span + 24.0


def _is_awake(profile: Profile, local_hour: float) -> bool:
    if profile.bed_hour > profile.wake_hour:
        return profile.wake_hour <= local_hour < profile.bed_hour
    # Bedtime after midnight.
    return local_hour >= profile.wake_hour or local_hour < profile.bed_hour


def _activity_rates(
    events: list[Event], start: datetime, end: datetime, step: timedelta, step_minutes: int
) -> tuple[dict[int, float], dict[int, float]]:
    """Spread each activity's sweat and burn evenly across the steps it covers.

    Even distribution is a simplification -- sweat rate ramps up over the first
    ten minutes and keeps running after you stop -- but the ledger is only ever
    read at five-minute resolution and the total is what matters.
    """
    sweat_rate: dict[int, float] = {}
    kcal_rate: dict[int, float] = {}
    for event in events:
        if not isinstance(event, ActivityEvent) or event.duration_s <= 0:
            continue
        finish = event.at + timedelta(seconds=event.duration_s)
        if finish < start or event.at > end:
            continue
        total_steps = max(1, int(math.ceil(event.duration_s / (step_minutes * 60))))
        for offset in range(total_steps):
            moment = event.at + offset * step
            if moment < start or moment > end:
                continue
            index = int((moment - start) / step)
            sweat_rate[index] = sweat_rate.get(index, 0.0) + event.sweat_ml / total_steps
            kcal_rate[index] = kcal_rate.get(index, 0.0) + event.kcal / total_steps
    return sweat_rate, kcal_rate


def _apply_observer(profile: Profile, state: _State, events: list[Event], event: Event) -> Correction | None:
    if isinstance(event, VoidEvent):
        ctx = _void_context(events, event)
        reading = urine_model.read(ctx)
        # The profile can dial overall trust in colour readings up or down
        # without touching each timing rule.
        weight = reading.confidence * profile.trust_urine
        observed = reading.deficit_ml(profile.body_mass_kg)
        before = state.deficit_ml
        state.deficit_ml = (1.0 - weight) * before + weight * observed
        return Correction(
            at=event.at,
            kind="urine",
            ledger_ml=before,
            observed_ml=observed,
            blended_ml=state.deficit_ml,
            confidence=reading.confidence,
            reasons=list(reading.reasons),
        )

    if isinstance(event, WeightEvent):
        if event.context != "morning":
            # Pre/post activity weights measure a *change*; they are paired up
            # into a measured sweat loss elsewhere and must not also be read as
            # an absolute hydration state, or that sweat gets counted twice.
            return None
        if state.weight_trend_kg is None:
            state.weight_trend_kg = event.mass_kg
            return None
        observed = urine_model.deficit_from_weight_ml(event.mass_kg, state.weight_trend_kg)
        alpha = 1.0 - 0.5 ** (1.0 / k.WEIGHT_TREND_HALFLIFE_DAYS)
        state.weight_trend_kg = alpha * event.mass_kg + (1.0 - alpha) * state.weight_trend_kg
        if observed is None:
            return None
        before = state.deficit_ml
        state.deficit_ml = (1.0 - k.TRUST_WEIGHT) * before + k.TRUST_WEIGHT * observed
        return Correction(
            at=event.at,
            kind="weight",
            ledger_ml=before,
            observed_ml=observed,
            blended_ml=state.deficit_ml,
            confidence=k.TRUST_WEIGHT,
            reasons=[f"morning weight {event.mass_kg:.1f} kg against a {state.weight_trend_kg:.1f} kg trend"],
        )

    return None


def _void_context(events: list[Event], void: VoidEvent) -> urine_model.VoidContext:
    """Gather the timing signals that decide how much this void is worth."""

    def minutes_since(predicate) -> float | None:
        best: datetime | None = None
        for event in events:
            if event is void or _event_time(event) > void.at:
                continue
            if predicate(event):
                if best is None or _event_time(event) > best:
                    best = _event_time(event)
        if best is None:
            return None
        return (void.at - best).total_seconds() / 60.0

    def is_activity_end(event: Event) -> bool:
        if not isinstance(event, ActivityEvent):
            return False
        return event.at + timedelta(seconds=event.duration_s) <= void.at

    return urine_model.VoidContext(
        colour=void.colour,
        at=void.at,
        minutes_since_previous_void=minutes_since(lambda e: isinstance(e, VoidEvent)),
        minutes_since_significant_intake=minutes_since(
            lambda e: isinstance(e, IntakeEvent) and e.volume_ml >= urine_model.SIGNIFICANT_INTAKE_ML
        ),
        minutes_since_exercise=minutes_since(is_activity_end),
        minutes_since_multivitamin=minutes_since(lambda e: isinstance(e, IntakeEvent) and e.is_multivitamin),
        is_first_morning=void.is_first_morning,
    )


def daily_target_ml(profile: Profile, *, sweat_ml: float = 0.0, temp_c: float | None = None, humidity_pct: float | None = None) -> float:
    """Beverage volume that keeps a normal day in balance.

    Deliberately *not* computed from the obligatory urine floor. A person who
    is genuinely well hydrated passes far more urine than the minimum the
    kidney can get away with, and a target built from the floor would tell a
    healthy adult to drink a litre a day. This uses the target urine output
    instead, which is the difference between 'not in trouble' and 'hydrated'.
    """
    temp_c = profile.default_temp_c if temp_c is None else temp_c
    humidity_pct = profile.default_humidity_pct if humidity_pct is None else humidity_pct

    heat_index = sweat_model.heat_index_c(temp_c, humidity_pct)
    heat_multiple = min(
        1.0 + k.INSENSIBLE_HEAT_COEFF * max(0.0, heat_index - k.INSENSIBLE_HEAT_BASE_C),
        k.INSENSIBLE_HEAT_MAX_MULTIPLE,
    )
    losses = (
        k.INSENSIBLE_ML_PER_KG_H * profile.body_mass_kg * 24.0 * heat_multiple
        + k.TARGET_URINE_ML_PER_KG_DAY * profile.body_mass_kg
        + k.FECAL_ML_PER_DAY
        + sweat_ml
    )
    gains = profile.food_water_ml_day + profile.rmr_kcal_per_day * k.METABOLIC_WATER_ML_PER_KCAL
    return max(0.0, losses - gains)
