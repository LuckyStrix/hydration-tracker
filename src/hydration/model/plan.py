"""Turning the ledger's state into something to actually do.

The deficit is a number; "you are 1.2 L down" is not advice. What makes it
advice is a schedule, and what makes the schedule non-trivial is that you
cannot absorb fluid faster than roughly 800 mL/h no matter how much you drink.
Told to fix a 1.2 L deficit, the naive answer is "drink 1.2 L" and the result
is a full stomach, an unchanged deficit, and a trip to the bathroom.

So the planner works within three constraints:

  * never ask for fluid faster than it can be absorbed,
  * never ask for more than a comfortable mouthful at once,
  * never schedule a drink close enough to bed to wake you up at 3am.

The last one is a real constraint, not a nicety. An app that costs you sleep to
fix a rounding error in a water estimate has made your day worse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import constants as k
from . import electrolytes
from .balance import Confidence, Profile, Timeline, daily_target_ml
from .electrolytes import OverdrinkWarning, SodiumAdvice

# Intervals the planner is willing to ask for, longest first. Longest that
# works wins -- being interrupted every fifteen minutes is a cost.
CANDIDATE_INTERVALS_MIN = (90, 60, 45, 30, 20, 15)

MIN_WORTH_MENTIONING_ML = 120.0
"""Below this the honest answer is 'you are fine', not a schedule."""


@dataclass(frozen=True)
class Dose:
    at: datetime
    volume_ml: float
    sodium_mg: float = 0.0
    why: str = ""


@dataclass(frozen=True)
class UpcomingActivity:
    """A workout that has not happened yet, from the Garmin calendar or typed
    in by hand. Changes the plan from 'catch up' to 'get ahead'."""

    at: datetime
    duration_s: float
    expected_sweat_ml: float
    name: str = "your session"


@dataclass(frozen=True)
class Plan:
    at: datetime
    deficit_ml: float
    deficit_pct: float
    status: str  # ok | drink | drink_urgent | add_sodium | slow_down
    headline: str
    detail: list[str]
    doses: list[Dose]
    sodium: SodiumAdvice
    overdrink: OverdrinkWarning | None
    medical_flags: list[str]
    daily_target_ml: float
    daily_intake_ml: float
    confidence: Confidence

    @property
    def next_dose(self) -> Dose | None:
        return self.doses[0] if self.doses else None

    @property
    def total_planned_ml(self) -> float:
        return sum(dose.volume_ml for dose in self.doses)


def make_plan(
    timeline: Timeline,
    profile: Profile,
    *,
    now: datetime | None = None,
    upcoming: UpcomingActivity | None = None,
    hours_since_last_void: float | None = None,
    recent_dark_voids: int = 0,
    symptom_flags: tuple[str, ...] = (),
) -> Plan:
    now = now or timeline.final.at
    state = timeline.final
    confidence = timeline.confidence(now)
    deficit = state.deficit_ml
    deficit_pct = profile.deficit_pct(deficit)

    detail: list[str] = []

    # -- how much, in total ------------------------------------------------
    # Fluid already in the stomach is on its way; asking for it again is how
    # you end up over-drinking on the app's own advice.
    correction = max(0.0, deficit - state.gut_ml)
    if state.gut_ml > MIN_WORTH_MENTIONING_ML:
        detail.append(f"{state.gut_ml / 1000:.2f} L already drunk and still absorbing, so it is not counted again.")

    preload = 0.0
    if upcoming is not None:
        hours_out = (upcoming.at - now).total_seconds() / 3600.0
        if 0 < hours_out <= 4.0:
            preload = k.PRELOAD_ML_PER_KG_4H * profile.body_mass_kg
            if hours_out <= 2.0 and deficit > 0:
                preload = k.PRELOAD_ML_PER_KG_2H * profile.body_mass_kg
            detail.append(
                f"Pre-loading {preload / 1000:.2f} L for {upcoming.name} in {hours_out:.1f} h."
            )

    maintenance_rate_ml_h = _maintenance_rate(profile, timeline)

    # -- over what window --------------------------------------------------
    severe = deficit_pct >= k.DEFICIT_PCT_SEVERE
    window_h, window_note = _drinking_window(profile, now, severe=severe, upcoming=upcoming)
    if window_note:
        detail.append(window_note)

    if window_h <= 0:
        doses: list[Dose] = []
        total = 0.0
    else:
        total = correction + preload + maintenance_rate_ml_h * window_h
        absorbable = profile.absorption_cap_ml_h * window_h
        if total > absorbable:
            detail.append(
                f"Capped at {absorbable / 1000:.2f} L -- more than that cannot be absorbed in "
                f"{window_h:.1f} h, and the rest would just sit in your stomach."
            )
            total = absorbable
        doses = _schedule(total, now, window_h) if total >= MIN_WORTH_MENTIONING_ML else []

    # -- sodium ------------------------------------------------------------
    sodium = electrolytes.assess(
        timeline, profile, planned_intake_ml=total if window_h > 0 else 0.0, symptom_flags=symptom_flags
    )
    if sodium.recommend and doses:
        doses = _distribute_sodium(doses, sodium.recommended_mg)

    overdrink = electrolytes.overdrink_check(timeline, profile)
    if overdrink is not None:
        # The guard overrides the schedule outright. Handing someone a drinking
        # plan and a "stop drinking" warning in the same breath is worse than
        # useless.
        doses = []

    # -- assemble ----------------------------------------------------------
    status = _status(deficit_pct, sodium, overdrink, confidence)
    daily_intake = timeline.delta("intake_ml", 24.0)
    target = daily_target_ml(
        profile,
        sweat_ml=timeline.delta("sweat_ml", 24.0),
        temp_c=state.temp_c,
        humidity_pct=state.humidity_pct,
    )

    headline = _headline(
        status=status,
        doses=doses,
        profile=profile,
        deficit_pct=deficit_pct,
        sodium=sodium,
        overdrink=overdrink,
        maintenance_rate_ml_h=maintenance_rate_ml_h,
        window_h=window_h,
        confidence=confidence,
    )

    if confidence.is_stale:
        detail.insert(0, f"Working from a guess -- {confidence.reason}.")

    if sodium.recommend:
        for reason in sodium.reasons:
            detail.append(reason[0].upper() + reason[1:] + ".")
        if sodium.residual_mg > 200:
            detail.append(
                f"About {sodium.residual_mg:.0f} mg of sodium beyond that is best covered by a salty meal."
            )

    return Plan(
        at=now,
        deficit_ml=deficit,
        deficit_pct=deficit_pct,
        status=status,
        headline=headline,
        detail=detail,
        doses=doses,
        sodium=sodium,
        overdrink=overdrink,
        medical_flags=_medical_flags(
            deficit_pct, hours_since_last_void, recent_dark_voids, confidence
        ),
        daily_target_ml=target,
        daily_intake_ml=daily_intake,
        confidence=confidence,
    )


# -- pieces ----------------------------------------------------------------

def _maintenance_rate(profile: Profile, timeline: Timeline) -> float:
    """Ongoing baseline losses per waking hour, so a zero deficit still gets a
    plan.

    Without this the advice at deficit zero is 'drink nothing', which is true
    for exactly as long as it takes to become false.

    Sweat is deliberately excluded. Sweat that has already happened is already
    in the deficit, and adding it here would ask you to replace it twice --
    an earlier version passed the last 24 hours of sweat into this and told a
    rider who had lost 2.1 L and was 2.2 L down to drink 3.6 L. Sweat still to
    come is handled as a pre-load against a known upcoming session, which is
    the only sweat we can actually anticipate.
    """
    state = timeline.final
    target = daily_target_ml(
        profile,
        sweat_ml=0.0,
        temp_c=state.temp_c,
        humidity_pct=state.humidity_pct,
    )
    awake = profile.bed_hour - profile.wake_hour
    awake = awake if awake > 0 else awake + 24.0
    return target / max(awake, 1.0)


def post_exercise_target_ml(sweat_ml: float) -> float:
    """How much to drink after a session that lost `sweat_ml`.

    Replaces more than was lost, because some of what you drink leaves as
    urine before it is retained. Used by the activity retrospective rather than
    by the live plan: the live plan does not need the heuristic, since the
    ledger models that urine loss directly and applying both would count it
    twice.
    """
    return sweat_ml * k.POST_EXERCISE_REPLACE_FRACTION


def _drinking_window(
    profile: Profile, now: datetime, *, severe: bool, upcoming: UpcomingActivity | None
) -> tuple[float, str | None]:
    """How many hours ahead the plan may schedule fluid."""
    local = now.astimezone(profile.tz)
    hour = local.hour + local.minute / 60.0

    hours_to_bed = profile.bed_hour - hour
    if hours_to_bed < 0:
        hours_to_bed += 24.0
    usable = hours_to_bed - k.NOCTURIA_CUTOFF_H

    horizon = k.PLAN_HORIZON_H
    if upcoming is not None:
        hours_out = (upcoming.at - now).total_seconds() / 3600.0
        if 0 < hours_out < horizon:
            # Everything must be in before the session starts, not during it.
            horizon = hours_out

    if severe:
        return min(horizon, max(hours_to_bed, 1.0)), (
            "Deficit is large enough to be worth a broken night, so the bedtime cutoff is ignored."
        )

    if usable <= 0:
        return 0.0, (
            f"Within {k.NOCTURIA_CUTOFF_H:.0f} h of bed -- catching up now would just wake you. "
            f"A small drink if thirsty, and start early tomorrow."
        )

    return min(horizon, usable), None


def _schedule(total_ml: float, now: datetime, window_h: float) -> list[Dose]:
    """Split the volume into evenly spaced, swallowable doses.

    Prefers the longest interval that keeps each dose under the bolus cap: the
    fewest interruptions that still gets the fluid in.
    """
    window_min = window_h * 60.0
    for interval in CANDIDATE_INTERVALS_MIN:
        count = max(1, int(window_min // interval))
        if total_ml / count <= k.MAX_BOLUS_ML:
            break
    else:
        interval = CANDIDATE_INTERVALS_MIN[-1]
        count = max(1, int(window_min // interval))

    per_dose = total_ml / count
    return [
        Dose(
            at=now + timedelta(minutes=interval * i),
            volume_ml=per_dose,
            why="scheduled" if i else "now",
        )
        for i in range(count)
    ]


def _distribute_sodium(doses: list[Dose], total_mg: float) -> list[Dose]:
    """Spread sodium across the doses rather than front-loading it.

    A gram of sodium in one glass tastes like seawater and is likely to come
    straight back up.
    """
    if not doses or total_mg <= 0:
        return doses
    share = total_mg / len(doses)
    return [
        Dose(at=d.at, volume_ml=d.volume_ml, sodium_mg=share, why=d.why)
        for d in doses
    ]


def _status(
    deficit_pct: float,
    sodium: SodiumAdvice,
    overdrink: OverdrinkWarning | None,
    confidence: Confidence,
) -> str:
    if overdrink is not None:
        return "slow_down"
    if confidence.is_stale:
        # Deliberately its own state rather than being folded into 'ok'. The
        # honest answer is that nobody knows, and a dashboard should show that
        # differently from a checked and healthy one.
        return "unknown"
    if deficit_pct >= k.DEFICIT_PCT_SIGNIFICANT:
        return "drink_urgent"
    if deficit_pct >= 0.5:
        return "drink"
    if sodium.recommend:
        return "add_sodium"
    return "ok"


def _headline(
    *,
    status: str,
    doses: list[Dose],
    profile: Profile,
    deficit_pct: float,
    sodium: SodiumAdvice,
    overdrink: OverdrinkWarning | None,
    maintenance_rate_ml_h: float,
    window_h: float,
    confidence: Confidence,
) -> str:
    """One line. This is what Home Assistant shows, so it has to stand alone."""
    if overdrink is not None:
        return overdrink.message

    if confidence.is_stale:
        # Ask for the cheapest thing that would fix it. A bathroom visit takes
        # one tap and re-anchors the whole estimate.
        return (
            "Not enough recent data to say. Log a bathroom visit or this morning's "
            "weight and I can tell you where you stand."
        )

    def litres(ml: float) -> str:
        return f"{ml / 1000:.2f} L"

    def clock(when: datetime) -> str:
        return when.astimezone(profile.tz).strftime("%-I:%M %p")

    if not doses:
        if window_h <= 0:
            base = f"{litres(profile.pct_to_ml(deficit_pct))} down, but it is nearly bedtime -- sip if thirsty and catch up in the morning."
        elif deficit_pct <= 0:
            base = "Well hydrated. Nothing needed right now."
        else:
            base = "On track. Keep sipping."
        return _with_sodium(base, sodium)

    first = doses[0]
    if len(doses) == 1:
        base = f"Drink {litres(first.volume_ml)} now."
    else:
        interval_min = round((doses[1].at - doses[0].at).total_seconds() / 60.0)
        last = doses[-1]
        base = (
            f"Drink {litres(first.volume_ml)} now, then {litres(doses[1].volume_ml)} "
            f"every {interval_min} min until {clock(last.at)}."
        )
    return _with_sodium(base, sodium)


def _with_sodium(base: str, sodium: SodiumAdvice) -> str:
    if sodium.recommend and sodium.recommended_mg > 0:
        return f"{base} Add {sodium.recommended_mg:.0f} mg sodium."
    return base


def _medical_flags(
    deficit_pct: float,
    hours_since_last_void: float | None,
    recent_dark_voids: int,
    confidence: Confidence,
) -> list[str]:
    """The few things that mean stop using an app and talk to a doctor.

    Deliberately short. A list that flags everything gets dismissed, and then
    it flags nothing.

    Suppressed entirely when the estimate is stale. A medical warning derived
    from a guess is a false alarm, and false alarms are how a real one gets
    ignored -- which is the specific way this used to fail: a fortnight of not
    logging produced an alarming deficit and a warning about it.
    """
    if confidence.is_stale:
        return []

    flags: list[str] = []
    if deficit_pct >= k.DEFICIT_PCT_SEVERE:
        flags.append(
            f"Estimated {deficit_pct:.1f}% of body mass down. Past about 3% this stops being a "
            f"training question -- if you also feel dizzy or confused, seek medical help."
        )
    if hours_since_last_void is not None and hours_since_last_void >= k.NO_VOID_FLAG_H:
        flags.append(
            f"No urine logged for {hours_since_last_void:.0f} hours. If that is accurate and you "
            f"feel unwell, it needs attention rather than another glass of water."
        )
    if recent_dark_voids >= 3:
        flags.append(
            "Several very dark voids in a row. If drinking normally has not shifted it, or the "
            "colour is red, brown or tea-like, get it looked at -- that is not dehydration."
        )
    return flags
