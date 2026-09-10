"""Urine colour as a measurement, weighted by when it was produced.

A colour chart is the cheapest hydration measurement there is, and it is also
the one most often misread -- because *when* the sample was made changes what
it means far more than most trackers admit.

Concretely: the first void of the morning is dark in a perfectly hydrated
person, because vasopressin runs high overnight and the kidney concentrates on
purpose. A pale sample twenty minutes after a big glass of water says nothing
about body water at all -- it is the glass of water, passing through. And a
multivitamin turns urine fluorescent yellow for hours regardless of hydration.

So this module returns a reading *and* a confidence, and the ledger blends the
two. A confident reading moves the estimate a long way; a compromised one
barely nudges it. Every confidence carries the reason it was chosen, which is
what makes the history page able to explain itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import constants as k


@dataclass(frozen=True)
class VoidContext:
    """Everything about a void's timing that changes what its colour means.

    All durations are in minutes and may be None when there is no prior event
    to measure from -- the first void ever logged has no predecessor.
    """

    colour: int
    at: datetime
    minutes_since_previous_void: float | None = None
    minutes_since_significant_intake: float | None = None
    minutes_since_exercise: float | None = None
    minutes_since_multivitamin: float | None = None
    is_first_morning: bool = False


@dataclass(frozen=True)
class UrineReading:
    """What a void says about body water, and how much to believe it."""

    deficit_pct: float
    confidence: float
    reasons: list[str] = field(default_factory=list)

    def deficit_ml(self, body_mass_kg: float) -> float:
        """Percent of body mass is the standard unit; the ledger wants mL.

        One percent of body mass is one percent of a kilogram per kilogram, and
        a kilogram of body water is a litre of it.
        """
        return self.deficit_pct / 100.0 * body_mass_kg * 1000.0



SIGNIFICANT_INTAKE_ML = 300.0
"""Below this a drink is too small to have visibly diluted anything."""

DILUTION_WINDOW_MIN = 60.0
POST_EXERCISE_WINDOW_MIN = 60.0
MULTIVITAMIN_WINDOW_MIN = 360.0
FRESH_SAMPLE_MIN = 90.0
STALE_SAMPLE_MIN = 300.0


def colour_to_deficit_pct(colour: int, *, is_first_morning: bool = False) -> float:
    """Map an Armstrong 8-point colour to a deficit as percent of body mass.

    The first-morning shift is the interesting part: overnight urine is
    concentrated by design, so the same shade means less dehydration than it
    would at 3pm. Shifting one shade lighter before reading the table is the
    standard correction, and it is applied *in addition to* the reduced
    confidence below -- the reading is both biased and noisy, and those are two
    different problems.
    """
    clamped = min(max(int(colour), 1), 8)
    if is_first_morning:
        clamped = max(1, clamped - 1)
    return k.URINE_COLOUR_DEFICIT_PCT[clamped]


def confidence_for(ctx: VoidContext) -> tuple[float, list[str]]:
    """How much this particular sample can be trusted, and why.

    The rules are checked worst-first and the lowest applicable confidence
    wins, because these compromise a reading rather than average out: a pale
    sample that is *both* twenty minutes after a litre of water *and* the first
    of the morning is not more informative for being doubly confounded.
    """
    reasons: list[str] = []
    candidates: list[tuple[float, str]] = []

    since_vitamin = ctx.minutes_since_multivitamin
    if since_vitamin is not None and since_vitamin <= MULTIVITAMIN_WINDOW_MIN:
        candidates.append((0.10, "riboflavin from a supplement colours urine regardless of hydration"))

    since_drink = ctx.minutes_since_significant_intake
    if since_drink is not None and since_drink <= DILUTION_WINDOW_MIN:
        candidates.append((0.15, f"only {since_drink:.0f} min after a large drink -- diluted, not informative"))

    if ctx.is_first_morning:
        candidates.append((0.35, "first void of the day is concentrated overnight by design"))

    since_exercise = ctx.minutes_since_exercise
    if since_exercise is not None and since_exercise <= POST_EXERCISE_WINDOW_MIN:
        candidates.append((0.50, "concentrated from sweating, which is not the same as being short of water"))

    since_void = ctx.minutes_since_previous_void
    if since_void is not None and since_void >= STALE_SAMPLE_MIN:
        candidates.append((0.50, f"{since_void / 60:.1f} h since the last void -- integrates too long a window"))

    if not candidates:
        if since_void is not None and since_void <= FRESH_SAMPLE_MIN:
            reasons.append("recent previous void, so this reflects current kidney output")
            return 0.90, reasons
        reasons.append("unremarkable timing")
        return 0.75, reasons

    confidence, reason = min(candidates, key=lambda pair: pair[0])
    reasons.append(reason)
    return confidence, reasons


def read(ctx: VoidContext) -> UrineReading:
    """Turn a logged void into a weighted observation of body water."""
    confidence, reasons = confidence_for(ctx)
    deficit_pct = colour_to_deficit_pct(ctx.colour, is_first_morning=ctx.is_first_morning)
    if ctx.is_first_morning:
        reasons.append("read one shade lighter to allow for overnight concentration")
    return UrineReading(deficit_pct=deficit_pct, confidence=confidence, reasons=reasons)


def weight_for(reading: UrineReading, trust_urine: float = k.TRUST_URINE) -> float:
    """How far this reading is allowed to pull the ledger, all in.

    `trust_urine` is a parameter rather than the constant because the profile
    can dial overall trust in colour readings up or down. Reading the constant
    here meant this function -- and `blend` below -- quietly ignored that
    setting, while the ledger applied it; the two agreed only at the default,
    and the tests were pinning the path the application did not take.
    """
    return reading.confidence * trust_urine


def blend(
    ledger_ml: float,
    reading: UrineReading,
    body_mass_kg: float,
    trust_urine: float = k.TRUST_URINE,
) -> float:
    """Pull the ledger towards what the urine says, in proportion to trust.

    This is the whole reason the app is more than a drink counter. The ledger
    accumulates error -- every constant in it is a population average applied
    to one person. The urine is a noisy look at the truth. Neither alone is
    good enough; a weighted blend of both is considerably better than either.
    """
    w = weight_for(reading, trust_urine)
    return (1.0 - w) * ledger_ml + w * reading.deficit_ml(body_mass_kg)


# -- the other observer ----------------------------------------------------

def deficit_from_weight_ml(mass_kg: float, trend_mass_kg: float) -> float | None:
    """Body water deficit implied by weight below its own trend.

    Stronger than urine colour because it is an actual measurement of mass, and
    over a day or two essentially all mass change is fluid. The trend is what
    makes it work: comparing today against a smoothed baseline separates 'I am
    two pounds down this morning' (water) from 'I have lost six pounds since
    March' (not water).

    Returns None when the deviation is too large to be body water at all --
    that is a different scale, or clothes, or a typo, and treating it as a
    three-litre deficit would be actively harmful.
    """
    deviation_kg = trend_mass_kg - mass_kg
    if abs(deviation_kg) > k.MAX_PLAUSIBLE_FLUID_SWING_KG:
        return None
    return deviation_kg * 1000.0
