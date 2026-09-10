"""Every tunable number in the model, in one place, with the reason for it.

These are population averages. The point of the observers (urine colour, body
weight, measured sweat loss) is that the model does not have to be right about
any individual up front -- it gets corrected. But the starting values still
matter, because a badly wrong prior takes many observations to wash out, so
each one below is sourced rather than guessed.

Anything a person might reasonably want to change lives on the profile row and
uses the value here only as its default. Anything with no profile column is a
constant because changing it would mean changing the physiology.
"""

from __future__ import annotations

# -- simulation ------------------------------------------------------------

STEP_MINUTES = 5
"""Integration step. Small enough that a 20-minute gastric time constant is
resolved properly, large enough that a month of history is a few thousand
steps."""

DEFAULT_LOOKBACK_DAYS = 3
"""How far back `current_state` starts the ledger. The observers dominate
within about a day, so anchoring further back buys accuracy that is already
paid for and costs time on every page load."""


# -- baseline losses -------------------------------------------------------

INSENSIBLE_ML_PER_KG_H = 0.44
"""Respiratory water plus transepidermal diffusion at rest in temperate
conditions. 0.44 mL/kg/h is about 790 mL/day for a 75 kg adult, which matches
the usual textbook split of ~450 mL skin and ~300 mL respiratory."""

INSENSIBLE_HEAT_COEFF = 0.03
INSENSIBLE_HEAT_BASE_C = 22.0
INSENSIBLE_HEAT_MAX_MULTIPLE = 2.0
"""Above about 22 C insensible loss climbs: breathing is faster, skin is warmer
and a little non-exercise sweating starts. 3% per degree, capped at double,
covers a hot room without pretending it is exercise."""

OBLIGATORY_URINE_ML_PER_KG_H = 0.55
"""The floor. The kidney must clear a fixed daily solute load and cannot
concentrate urine past roughly 1200 mOsm/kg, so about 1000 mL/day for a 75 kg
adult leaves regardless of how dry you are. This floor is what makes the
deficit keep growing when you stop drinking."""

FECAL_ML_PER_DAY = 150.0
"""Small, constant, and omitting it biases the whole ledger wet."""

TARGET_URINE_ML_PER_KG_DAY = 20.0
"""NOT a loss term -- this is only used to project a daily intake *target*.
Someone genuinely well hydrated passes far more than the obligatory floor, and
a target computed from the floor would tell a healthy person to drink one litre
a day. The ledger uses the floor because that is the physiology; the target
uses this because that is the goal. Keeping them separate is deliberate."""


# -- diuresis --------------------------------------------------------------

DIURESIS_HALFLIFE_MIN = 60.0
"""When there is surplus body water the kidneys shed it with roughly an hour's
half-life. This is what makes urine run pale after a big drink -- it falls out
of the model rather than being special-cased anywhere."""

MAX_DIURESIS_ML_PER_MIN = 15.0
"""Maximum free-water clearance. Roughly 900 mL/h; it is the ceiling that makes
over-drinking dangerous rather than merely wasteful."""

ALCOHOL_DIURESIS_ML_PER_G = 10.0
ALCOHOL_DIURESIS_TAU_MIN = 60.0
"""Ethanol suppresses vasopressin: about 10 mL of extra urine per gram, which
is roughly 100 mL per standard drink, released over a couple of hours."""

CAFFEINE_DIURESIS_ML_PER_MG = 0.0
CAFFEINE_DIURESIS_THRESHOLD_MG = 300.0
"""Zero on purpose. Habitual caffeine users show no meaningful net diuresis,
and coffee's hydration index is close to water's. The threshold and a non-zero
coefficient are here so someone who genuinely reacts can set one, but the
default must not encode folklore."""


# -- intake and absorption -------------------------------------------------

GASTRIC_TAU_MIN = 20.0
"""First-order gastric emptying. A swallowed drink is not body water yet."""

ABSORPTION_CAP_ML_PER_H = 800.0
"""The hard ceiling on how fast fluid can actually cross into the body. This
constant is load-bearing: it is the reason a litre drunk at once does not fix a
litre deficit, and therefore the reason the advice is a schedule instead of a
number. Trained athletes reach higher during exercise; the profile can raise
it."""

MAX_BOLUS_ML = 350.0
"""Largest single dose the planner will ask for. More than this at once is
uncomfortable and mostly ends up waiting in the stomach anyway."""

FOOD_WATER_ML_PER_DAY = 700.0
"""Water eaten rather than drunk -- roughly a fifth to a quarter of total
intake on a normal diet. Trickled across waking hours only, since you are not
eating at 3am. Overridden by logged meals when they exist."""

METABOLIC_WATER_ML_PER_KCAL = 0.12
"""Oxidising fuel produces water. Negligible sitting still, but about 360 mL
across a 3000 kcal ride, which is not nothing. Applied to resting metabolic
rate and activity burn alike, so it covers the ~250 mL/day resting figure
without a second constant."""


# -- sweat -----------------------------------------------------------------

LATENT_HEAT_SWEAT_KJ_PER_L = 2426.0
"""Energy to evaporate a litre of sweat at skin temperature."""

KCAL_TO_KJ = 4.184

HEAT_FRACTION_COLD_C = 5.0
HEAT_FRACTION_COLD = 0.43
HEAT_FRACTION_TEMPERATE_C = 20.0
HEAT_FRACTION_TEMPERATE = 0.65
HEAT_FRACTION_HOT_C = 35.0
HEAT_FRACTION_HOT = 0.85
"""Fraction of metabolic energy that must be shed by *evaporation*, which is
not the same as the fraction that becomes heat.

About 78% of the burn becomes heat, but only part of that heat needs sweat --
the rest leaves dry, by convection and radiation, and how much depends
entirely on the air. At 20 C there is a 15 degree gradient from skin to air
doing a fifth of the work for free. At 35 C the skin and the air are the same
temperature, dry loss stops, and radiant load may even add to what sweat has
to carry.

Calibrated against a documented case rather than assembled from first
principles: a 3.5 hour marathon at ~700 kcal/h in 18 C air produces about 3 L
of sweat, which these anchors reproduce to within a few percent. An earlier
version omitted the dry-loss term entirely and read 20% high at every
temperature."""

EVAP_EFFICIENCY_BEST = 0.92
EVAP_EFFICIENCY_WORST = 0.45
"""How much of the sweat produced actually evaporates. The rest drips off. Note
the direction this pushes: in humidity, *less* evaporates, so the body produces
*more* sweat to shed the same heat, and all of it is still lost fluid. Humid
conditions therefore raise the estimate, not lower it."""

MAX_SWEAT_RATE_ML_PER_H = 2500.0
"""Physiological ceiling. Anything above this is a data error, not an athlete."""

SWEAT_CALIBRATION_MIN = 0.5
SWEAT_CALIBRATION_MAX = 2.0
"""Bounds on the personal multiplier fitted from measured pre/post weights, so
one mis-typed weight cannot send the model somewhere absurd."""

MIN_PAIRS_FOR_CALIBRATION = 4
"""Do not fit a personal sweat rate from fewer than this many weight pairs."""


# -- sodium ----------------------------------------------------------------

SWEAT_SODIUM_MMOL_PER_L = 40.0
"""Default sweat sodium concentration, about 920 mg/L. Real range is wide --
20 to 80 mmol/L -- which is why the profile asks about salt crust on skin and
white stains on dark clothing."""

MG_PER_MMOL_SODIUM = 22.99

SODIUM_REPLACE_MG_PER_L_LOW = 300.0
SODIUM_REPLACE_MG_PER_L_HIGH = 700.0
"""Sodium to pair with each litre of replacement fluid. The high end is for
salty sweaters and long sessions."""

SODIUM_TRIGGER_SWEAT_ML_4H = 1200.0
SODIUM_TRIGGER_PLANNED_ML_4H = 1500.0
SODIUM_TRIGGER_DAILY_GAP_MG = 1500.0
"""Any one of these means the advice should mention sodium rather than just
water."""

POST_EXERCISE_REPLACE_FRACTION = 1.35
"""Replace 135% of a measured sweat deficit: some of what you drink leaves as
urine before it is retained. 125-150% is the usual guidance; the midpoint is
the default."""


# -- hyponatraemia guard ---------------------------------------------------

OVERDRINK_ML_PER_H = 1000.0
OVERDRINK_SODIUM_MG_PER_L = 200.0
"""Drinking more than a litre an hour of fluid this dilute, while not actually
in deficit, is the direction that causes exercise-associated hyponatraemia.
This warning fires on the *opposite* of what a hydration app normally nags
about, and it stays on by default -- in endurance settings this failure mode
hurts more people than dehydration does."""


# -- deficit thresholds ----------------------------------------------------

DEFICIT_PCT_NOTICEABLE = 1.0
DEFICIT_PCT_SIGNIFICANT = 2.0
DEFICIT_PCT_SEVERE = 3.0
"""Percent of body mass. 1% is where endurance performance and cognition start
to measurably slip, 2% is clearly impaired, 3% is where it stops being a
training question."""


# -- planning --------------------------------------------------------------

NOCTURIA_CUTOFF_H = 2.0
"""Do not schedule fluid inside this many hours of bedtime unless the deficit
is severe. Being woken at 3am is a real cost and the app should not cause it
for the sake of a rounding error."""

PRELOAD_ML_PER_KG_4H = 6.0
PRELOAD_ML_PER_KG_2H = 4.0
"""Pre-exercise loading: 5-7 mL/kg four hours out, another 3-5 mL/kg two hours
out if urine is still dark."""

PLAN_HORIZON_H = 6.0
"""How far ahead the dose schedule is generated."""


# -- urine observer --------------------------------------------------------

TRUST_URINE = 0.6
"""Ceiling on how much a single urine reading can move the ledger, before its
own timing confidence is applied. A colour chart read by eye under a bathroom
light is informative, not authoritative."""

URINE_COLOUR_DEFICIT_PCT = {
    1: -0.4,
    2: -0.1,
    3: 0.3,
    4: 0.8,
    5: 1.3,
    6: 1.8,
    7: 2.4,
    8: 3.0,
}
"""Armstrong 8-point chart to deficit as percent of body mass. 1 and 2 are
negative because pale urine means a surplus being shed, not merely 'fine'."""

TRUST_WEIGHT = 0.85
"""Morning body weight against its own trend is the strongest observer there
is -- it is an actual measurement of body water, not a proxy for one."""

WEIGHT_TREND_HALFLIFE_DAYS = 7.0
"""Half-life of the mass EWMA that separates real mass change from fluid. Fat
does not arrive overnight; two pounds between Tuesday and Wednesday is water."""

MAX_PLAUSIBLE_FLUID_SWING_KG = 3.0
"""A deviation from trend larger than this is a scale error, a different scale,
or clothes -- not body water. Ignore it as an observer."""
