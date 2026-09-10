"""Sweat loss: estimating it, and choosing which estimate to believe.

Garmin reports a sweat loss figure for many activities, but not all of them,
and it is itself a model. This module exists for three reasons: to cover the
activities Garmin does not score, to give a second opinion that can be shown
next to Garmin's, and to provide something to calibrate when a pre/post weight
pair measures the real answer.

The estimate is physical rather than a lookup table. Exercise burns energy,
most of that energy becomes heat, and shedding heat means evaporating sweat.
Everything below follows from that.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import constants as k


# -- psychrometrics --------------------------------------------------------

def saturation_vapour_pressure_kpa(temp_c: float) -> float:
    """Magnus-Tetens. Good to a fraction of a percent over habitable range."""
    return 0.61094 * _exp(17.625 * temp_c / (temp_c + 243.04))


def _exp(x: float) -> float:
    import math

    return math.exp(x)


SKIN_VAPOUR_PRESSURE_KPA = saturation_vapour_pressure_kpa(35.0)
"""Wet skin sits near 35 C and is effectively saturated."""

_REFERENCE_GRADIENT_KPA = SKIN_VAPOUR_PRESSURE_KPA - 0.5 * saturation_vapour_pressure_kpa(20.0)
"""The gradient at a pleasant 20 C and 50% humidity, used as the yardstick
against which other conditions are scored."""


def evaporative_efficiency(temp_c: float, humidity_pct: float) -> float:
    """Fraction of produced sweat that actually evaporates.

    Sweat only cools you when it turns to vapour; what drips off is lost fluid
    that did no work. The driving force is the vapour-pressure gradient from
    saturated skin to the surrounding air, so hot humid air -- where that
    gradient collapses -- means the body must produce far more sweat to shed
    the same heat.

    This is the term people get backwards. Humidity *raises* fluid loss.
    """
    humidity = min(max(humidity_pct, 0.0), 100.0) / 100.0
    gradient = SKIN_VAPOUR_PRESSURE_KPA - humidity * saturation_vapour_pressure_kpa(temp_c)
    ratio = gradient / _REFERENCE_GRADIENT_KPA
    return min(max(k.EVAP_EFFICIENCY_BEST * ratio, k.EVAP_EFFICIENCY_WORST), k.EVAP_EFFICIENCY_BEST)


def heat_fraction(temp_c: float) -> float:
    """Share of metabolic energy that has to leave as evaporated sweat.

    Muscle is roughly 20-25% efficient, so about 78% of the burn becomes heat.
    How much of that heat *needs sweat* depends on the air: in the cold, most
    of it leaves by convection and radiation for free, and in real heat the
    body gains radiant heat on top of its own, so sweat has to cover more than
    metabolism alone produced.
    """
    points = [
        (k.HEAT_FRACTION_COLD_C, k.HEAT_FRACTION_COLD),
        (k.HEAT_FRACTION_TEMPERATE_C, k.HEAT_FRACTION_TEMPERATE),
        (k.HEAT_FRACTION_HOT_C, k.HEAT_FRACTION_HOT),
    ]
    if temp_c <= points[0][0]:
        return points[0][1]
    if temp_c >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= temp_c <= x1:
            return y0 + (y1 - y0) * (temp_c - x0) / (x1 - x0)
    return k.HEAT_FRACTION_TEMPERATE


def heat_index_c(temp_c: float, humidity_pct: float) -> float:
    """NWS heat index, in and out in Celsius.

    The published formula is in Fahrenheit and is only meaningful in the heat;
    below about 27 C it returns the air temperature, which is the behaviour the
    insensible-loss term wants anyway.
    """
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    if temp_f < 80.0:
        return temp_c
    h = min(max(humidity_pct, 0.0), 100.0)
    hi_f = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * h
        - 0.22475541 * temp_f * h
        - 6.83783e-3 * temp_f**2
        - 5.481717e-2 * h**2
        + 1.22874e-3 * temp_f**2 * h
        + 8.5282e-4 * temp_f * h**2
        - 1.99e-6 * temp_f**2 * h**2
    )
    # The two corrections the NWS applies at the dry and humid extremes.
    if h < 13.0 and 80.0 <= temp_f <= 112.0:
        hi_f -= ((13.0 - h) / 4.0) * ((17.0 - abs(temp_f - 95.0)) / 17.0) ** 0.5
    elif h > 85.0 and 80.0 <= temp_f <= 87.0:
        hi_f += ((h - 85.0) / 10.0) * ((87.0 - temp_f) / 5.0)
    return (hi_f - 32.0) * 5.0 / 9.0


# -- the estimate ----------------------------------------------------------

def estimate_sweat_ml(
    *,
    kcal: float,
    duration_s: float,
    temp_c: float,
    humidity_pct: float,
    calibration: float = 1.0,
) -> float:
    """Sweat produced during an activity, in millilitres.

        heat that must evaporate = kcal -> kJ, times the sweating fraction
        sweat that must evaporate = that heat / latent heat of vaporisation
        sweat actually produced   = that, divided by evaporative efficiency

    `calibration` is the personal multiplier fitted from measured weight
    changes; it defaults to 1.0 until there is enough data to fit one.
    """
    if kcal <= 0 or duration_s <= 0:
        return 0.0

    heat_kj = kcal * k.KCAL_TO_KJ * heat_fraction(temp_c)
    litres_to_evaporate = heat_kj / k.LATENT_HEAT_SWEAT_KJ_PER_L
    litres_produced = litres_to_evaporate / evaporative_efficiency(temp_c, humidity_pct)
    ml = litres_produced * 1000.0 * calibration

    hours = duration_s / 3600.0
    ceiling = k.MAX_SWEAT_RATE_ML_PER_H * hours
    return min(ml, ceiling)


# -- choosing what to believe ---------------------------------------------

@dataclass(frozen=True)
class SweatResolution:
    """Which sweat figure the ledger used, and what the alternatives said.

    All three are kept so the activity page can show them side by side -- that
    comparison is what makes the calibration trustworthy rather than magic.
    """

    ml: float
    source: str  # 'measured' | 'garmin' | 'estimated' | 'none'
    measured_ml: float | None
    reported_ml: float | None
    estimated_ml: float | None

    @property
    def estimate_error_ml(self) -> float | None:
        """How far our formula was from the measurement, when both exist."""
        if self.measured_ml is None or self.estimated_ml is None:
            return None
        return self.estimated_ml - self.measured_ml


def resolve_sweat(
    *,
    measured_ml: float | None,
    reported_ml: float | None,
    estimated_ml: float | None,
    duration_s: float | None = None,
) -> SweatResolution:
    """Pick the best available sweat figure.

    A pre/post weight pair is an actual measurement of fluid lost and beats
    both models. Garmin's own number comes next: it has the heart rate trace
    and the device's own temperature, which we do not. Ours is the fallback.

    Garmin's figure is bounded before it is believed. It arrives from an
    undocumented API whose field names and units are not ours to rely on, and
    an unbounded one is a single unit change away from putting a 1500 litre
    sweat loss into the ledger. Our own estimate and a weight pair are already
    bounds-checked; this closes the last way in.
    """
    hours = (duration_s or 0) / 3600.0
    ceiling_ml = k.MAX_SWEAT_RATE_ML_PER_H * (hours + 1.0)
    if measured_ml is not None and measured_ml >= 0:
        chosen, source = measured_ml, "measured"
    elif reported_ml is not None and 0 < reported_ml <= ceiling_ml:
        chosen, source = reported_ml, "garmin"
    elif estimated_ml is not None and estimated_ml > 0:
        chosen, source = estimated_ml, "estimated"
    else:
        chosen, source = 0.0, "none"
    return SweatResolution(
        ml=chosen,
        source=source,
        measured_ml=measured_ml,
        reported_ml=reported_ml,
        estimated_ml=estimated_ml,
    )


def fit_calibration(pairs: list[tuple[float, float]], current: float = 1.0) -> tuple[float, int]:
    """Fit the personal sweat multiplier from (estimated_ml, measured_ml) pairs.

    Least-squares through the origin -- the ratio of totals -- because the
    quantity being fitted is a scale factor and an intercept would let a single
    short activity drag the whole line. Returns the new factor and the number
    of pairs it came from; below `MIN_PAIRS_FOR_CALIBRATION` it returns the
    current value unchanged rather than chase noise.
    """
    usable = [(e, m) for e, m in pairs if e and e > 0 and m is not None and m >= 0]
    if len(usable) < k.MIN_PAIRS_FOR_CALIBRATION:
        return current, len(usable)
    total_estimated = sum(e for e, _ in usable)
    total_measured = sum(m for _, m in usable)
    if total_estimated <= 0:
        return current, len(usable)
    factor = total_measured / total_estimated
    return min(max(factor, k.SWEAT_CALIBRATION_MIN), k.SWEAT_CALIBRATION_MAX), len(usable)
