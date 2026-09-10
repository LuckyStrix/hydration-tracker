"""Sodium: what sweat took out, what drinks put back, and which way to warn.

The framing matters, so it is worth stating plainly. This module does **not**
try to balance total dietary sodium -- a normal diet supplies several grams a
day and the kidney matches excretion to intake without any help from an app.
What it tracks is the *acute* gap: sodium lost in sweat, against sodium taken
in deliberately as tabs, sports drinks or a salty meal that was actually
logged. Ordinary food is assumed to cover ordinary losses, which is true, and
not to cover a two-litre sweat loss, which is also true.

The consequence is that not logging meals biases this toward recommending
sodium. That is the safe direction and a dismissible one.

The other half of this module is the guard that runs the opposite way. Almost
every hydration app nags in one direction only. Drinking large volumes of
dilute fluid while not actually in deficit is how exercise-associated
hyponatraemia happens, and in endurance settings that has killed more people
than dehydration has. That warning is on by default and is not a footnote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import constants as k
from .balance import Profile, Timeline


@dataclass(frozen=True)
class SodiumAdvice:
    """Acute sodium state and what to do about it."""

    sweat_sodium_mg_24h: float
    supplemental_sodium_mg_24h: float
    gap_mg: float
    recommend: bool
    recommended_mg: float
    concentration_mg_per_l: float
    reasons: list[str] = field(default_factory=list)

    @property
    def residual_mg(self) -> float:
        """Gap left over after the recommended dose -- normal food covers it."""
        return max(0.0, self.gap_mg - self.recommended_mg)


@dataclass(frozen=True)
class OverdrinkWarning:
    """Fired when intake is running fast, dilute, and unnecessary."""

    intake_ml_1h: float
    concentration_mg_per_l: float
    deficit_ml: float
    message: str


def concentration_for(profile: Profile) -> float:
    """Sodium to pair with each litre of replacement fluid.

    Scaled across the 300-700 mg/L band by how salty this person's sweat is,
    which the profile asks about directly -- salt crust on skin after a ride,
    white stains on dark kit, cramping late in long sessions.
    """
    span = 80.0 - 20.0
    position = (profile.sweat_sodium_mmol_l - 20.0) / span
    position = min(max(position, 0.0), 1.0)
    return k.SODIUM_REPLACE_MG_PER_L_LOW + position * (
        k.SODIUM_REPLACE_MG_PER_L_HIGH - k.SODIUM_REPLACE_MG_PER_L_LOW
    )


def assess(
    timeline: Timeline,
    profile: Profile,
    *,
    planned_intake_ml: float = 0.0,
    symptom_flags: tuple[str, ...] = (),
) -> SodiumAdvice:
    """Decide whether the advice should mention sodium, and how much."""
    sweat_sodium_24h = timeline.delta("sweat_sodium_mg", 24.0)
    supplemental_24h = timeline.delta("supplemental_sodium_mg", 24.0)
    gap = max(0.0, sweat_sodium_24h - supplemental_24h)

    sweat_4h = timeline.delta("sweat_ml", 4.0)
    reasons: list[str] = []

    if sweat_4h > k.SODIUM_TRIGGER_SWEAT_ML_4H:
        reasons.append(f"{sweat_4h / 1000:.1f} L of sweat in the last 4 hours")
    if planned_intake_ml > k.SODIUM_TRIGGER_PLANNED_ML_4H:
        reasons.append(
            f"replacing {planned_intake_ml / 1000:.1f} L -- that much plain water dilutes what is left"
        )
    if gap > k.SODIUM_TRIGGER_DAILY_GAP_MG:
        reasons.append(f"{gap:.0f} mg behind on sodium for the day")
    if symptom_flags and sweat_4h > 500:
        named = ", ".join(symptom_flags)
        reasons.append(f"{named} logged after sweating, which is often sodium rather than water")

    concentration = concentration_for(profile)
    recommend = bool(reasons)
    volume_l = max(planned_intake_ml, 0.0) / 1000.0
    recommended = min(gap, volume_l * concentration) if recommend else 0.0

    # If there is a real gap but nothing scheduled to drink yet, still name a
    # dose -- otherwise a big sweat loss with no plan reads as 'no sodium
    # needed', which is the wrong answer.
    if recommend and recommended <= 0 and gap > 0:
        recommended = min(gap, concentration)

    return SodiumAdvice(
        sweat_sodium_mg_24h=sweat_sodium_24h,
        supplemental_sodium_mg_24h=supplemental_24h,
        gap_mg=gap,
        recommend=recommend,
        recommended_mg=round(recommended / 50.0) * 50.0,
        concentration_mg_per_l=concentration,
        reasons=reasons,
    )


def overdrink_check(timeline: Timeline, profile: Profile) -> OverdrinkWarning | None:
    """The guard that points the other way.

    Three conditions together, because any one alone is fine: a lot of fluid,
    very little sodium in it, and no actual deficit to justify it. Someone
    genuinely two litres down who drinks fast is doing the right thing and must
    not be told off for it.
    """
    intake_1h = timeline.delta("intake_ml", 1.0)
    if intake_1h < k.OVERDRINK_ML_PER_H:
        return None

    deficit = timeline.final.deficit_ml
    if deficit > 0:
        return None

    sodium_1h = timeline.delta("supplemental_sodium_mg", 1.0)
    concentration = sodium_1h / (intake_1h / 1000.0) if intake_1h else 0.0
    if concentration >= k.OVERDRINK_SODIUM_MG_PER_L:
        return None

    return OverdrinkWarning(
        intake_ml_1h=intake_1h,
        concentration_mg_per_l=concentration,
        deficit_ml=deficit,
        message=(
            f"You have drunk {intake_1h / 1000:.1f} L in the last hour with little sodium, "
            f"and you are not short of water. Ease off and take salt with the next drink -- "
            f"this is the direction that causes hyponatraemia."
        ),
    )
