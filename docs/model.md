# The model

This is the part worth reading. Everything else in the application is data entry
and presentation; this is where the answers come from.

The design in one sentence: **keep a running water-balance ledger, then correct
it against things that can actually be measured.** A ledger on its own drifts,
because every constant in it is a population average applied to one person. A
single measurement on its own is noise. Blending them is considerably better
than either.

Every constant named below lives in
[`src/hydration/model/constants.py`](../src/hydration/model/constants.py) with
the reasoning for its value.

---

## State

Two compartments, integrated in five-minute steps:

| Variable | Meaning |
|---|---|
| `gut_ml` | Fluid swallowed but not yet absorbed |
| `deficit_ml` | Body water owed, relative to euhydrated. Positive means dry |

The deficit is reported as **percent of body mass**, which is the standard unit
in the literature: 1% is where endurance performance and cognition start to
measurably slip, 2% is clearly impaired, 3% stops being a training question.

Two compartments rather than one is not a flourish. It is what makes the
absorption limit representable, and that limit is what turns the output from a
number into a schedule.

---

## Losses

### Insensible loss

Respiratory water plus transepidermal diffusion:

```
insensible = 0.44 mL/kg/h × mass × (1 + 0.03 × max(0, heat_index_°C − 22))
```

capped at twice the baseline. 0.44 mL/kg/h is about 790 mL/day for a 75 kg
adult, matching the usual split of ~450 mL through skin and ~300 mL through
breathing. Above roughly 22 °C it climbs: breathing is faster, skin is warmer,
and a little non-exercise sweating begins.

A further 150 mL/day is lost in faeces — small, constant, and omitting it biases
the whole ledger wet.

### Urine

The kidney must clear a fixed daily solute load and cannot concentrate urine
past roughly 1200 mOsm/kg, so a floor of

```
obligatory = 0.55 mL/kg/h × mass
```

leaves regardless of how dry you are — about 1000 mL/day at 75 kg. **This floor
is what makes the deficit keep growing when you stop drinking.**

On top of the floor, surplus is shed with roughly an hour's half-life, capped at
15 mL/min of free-water clearance:

```
if deficit < 0:
    urine += min(−deficit × (1 − 2^(−Δt/60min)), 15 mL/min × Δt)
```

This is why urine runs pale after a big drink. It falls out of the model rather
than being special-cased anywhere, which is a good sign that the shape is right.

### Alcohol

Ethanol suppresses vasopressin: about **10 mL of extra urine per gram**, roughly
100 mL per standard drink, released over a couple of hours.

The interesting consequence is that four beers come out roughly *neutral* —
1.4 L of fluid more than covers their own diuresis, which is why low-strength
beer scores close to water on the beverage hydration index. Four shots of
spirits carry the same ethanol with almost no water and leave you clearly worse
off. Both behaviours are pinned by tests.

### Caffeine

**Zero by default**, and that is a deliberate position rather than an oversight.
Habitual caffeine users show no meaningful net diuresis, and coffee's hydration
index sits close to water's. A threshold and a coefficient exist so someone who
genuinely reacts can set one, but the default must not encode folklore.

### Sweat

See [below](#sweat-estimation) — it is the largest and most interesting term.

---

## Gains

### Drinks

```
gut_ml += volume_ml × hydration_index
```

The hydration index is the Beverage Hydration Index: retention relative to still
water at 1.0. Milk and oral rehydration solution beat water because their sodium
and slower gastric emptying reduce the urine that follows.

Applying it at the gut is a simplification — strictly it describes retention an
hour or two later, not absorption — but it puts the effect in the right direction
and roughly the right size. Modelling retention properly would mean modelling
osmolality, and nothing here would use it.

### Absorption

Fluid moves from gut to body by first-order emptying with a 20-minute time
constant, **hard-capped at 800 mL/h**:

```
emptied = min(gut_ml × (1 − e^(−Δt/20min)),  800 mL/h × Δt)
```

This constant is load-bearing. Without it, a litre swallowed is a litre absorbed
within the hour and the advice collapses to "drink the deficit". With it, the
answer has to be a schedule.

### Food and metabolism

- **Food water**: 700 mL/day by default, trickled across waking hours only. You
  are not eating at 3am, and spreading it over the full day makes the model read
  wet at breakfast and dry at bedtime.
- **Metabolic water**: 0.12 mL per kcal oxidised, applied to resting metabolic
  rate (Mifflin–St Jeor) and activity burn alike. Negligible sitting still; about
  360 mL across a 3000 kcal ride.

---

## Sweat estimation

Physical rather than a lookup table. Exercise burns energy, most of it becomes
heat, and shedding heat means evaporating sweat:

```
heat_kJ          = kcal × 4.184 × heat_fraction(temp)
litres_to_evap   = heat_kJ / 2426                        # latent heat of vaporisation
litres_produced  = litres_to_evap / evap_efficiency(temp, humidity)
```

capped at a physiological 2.5 L/h.

### `heat_fraction` — the share that must evaporate

Not the same as the share that becomes heat. About 78% of the burn becomes heat,
but only part of *that* needs sweat; the rest leaves dry, by convection and
radiation, and how much depends entirely on the air:

| Air temperature | Fraction needing evaporation |
|---|---|
| 5 °C | 0.43 |
| 20 °C | 0.65 |
| 35 °C | 0.85 |

At 20 °C a 15-degree skin-to-air gradient does a fifth of the work for free. At
35 °C skin and air are the same temperature, dry loss stops, and radiant load may
add to what sweat has to carry.

These anchors were **fitted to a documented case, not assembled from first
principles**: a 3.5-hour marathon at ~700 kcal/h in 18 °C air produces about 3 L
of sweat, which they reproduce to within a few percent. An earlier version
omitted the dry-loss term and read about 20% high at every temperature.

### `evap_efficiency` — and the term people get backwards

Sweat only cools you when it turns to vapour; what drips off is lost fluid that
did no work. The driving force is the vapour-pressure gradient from saturated
skin (~35 °C) to the surrounding air:

```
gradient   = P_sat(35°C) − RH × P_sat(air)
efficiency = clamp(0.92 × gradient / gradient_at_20°C_50%RH,  0.45,  0.92)
```

**Humidity raises fluid loss.** When sweat cannot evaporate, the body produces
more of it to shed the same heat, and all of it still leaves the body. Getting
this backwards is the most common error in sweat estimation.

### Validation

| Case | Model | Expected |
|---|---|---|
| Marathon, 2450 kcal / 3.5 h, 18 °C, 60% RH | 2.90 L | ~3.0 L |
| Hard hour, 1000 kcal, 25 °C, 50% RH | 1.48 L | 1.2–1.7 L |
| Hot and humid, 1000 kcal, 32 °C, 80% RH | 2.50 L | at the 2.5 L/h ceiling |
| Easy hour, 600 kcal, 10 °C, 60% RH | 0.57 L | modest |

### Which sweat figure gets used

Three sources, kept side by side and never collapsed into one:

1. **Measured** — a pre/post weight pair. An actual measurement of fluid lost,
   and it beats both models. The arithmetic adds back whatever was drunk during
   the session, or every bottle taken on the ride reads as sweat that never
   happened.
2. **Reported** — Garmin's own figure. It has the heart-rate trace and the
   device's temperature, which this application does not.
3. **Estimated** — the formula above.

A measurement outside physiological bounds is discarded rather than trusted: it
outranks both models, so a bad one is worse than none.

### Personal calibration

After four weighed sessions, a personal multiplier is fitted by least squares
through the origin — the ratio of totals, because the quantity is a scale factor
and an intercept would let one short activity drag the line. Bounded to
[0.5, 2.0] so a single mistyped weight cannot send the model somewhere absurd.

> **The fit must read the uncalibrated estimate.** The activity table stores
> `sweat_ml_estimated_raw` for exactly this. Fitting against an already-calibrated
> estimate makes each refit measure how well the *previous* refit worked; it
> converges on 1.0 and the model silently reverts to the population average while
> still reporting that it is calibrated.

---

## Observers

### Urine colour, weighted by timing

The cheapest hydration measurement there is, and the one most often misread —
because *when* the sample was produced changes what it means far more than most
trackers admit.

Colour maps to deficit on the Armstrong 8-point chart:

| Colour | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| Deficit, % body mass | −0.4 | −0.1 | 0.3 | 0.8 | 1.3 | 1.8 | 2.4 | 3.0 |

1 and 2 are negative because pale urine means a surplus being shed, not merely
"fine".

Then the timing decides how much that reading is worth:

| Context | Confidence | Why |
|---|---|---|
| 1.5–5 h since the last void | **0.90** | Bladder contents reflect recent kidney output |
| Unremarkable timing | 0.75 | — |
| First void after sleep | **0.35** | Overnight urine is concentrated by design — *and* read one shade lighter |
| Within 60 min post-exercise | 0.50 | Concentrated from sweating even when body water is fine |
| More than 5 h since the last void | 0.50 | Integrates too long a window |
| Within 60 min of a ≥300 mL drink | **0.15** | Dilution artifact — a pale sample here means nothing |
| Within 6 h of a multivitamin | **0.10** | Riboflavin fluoresces yellow regardless of hydration |

Rules are checked worst-first and the lowest applicable confidence wins. These
compromise a reading rather than average out: a sample that is *both* twenty
minutes after a litre of water *and* the first of the morning is not more
informative for being doubly confounded.

The blend:

```
w       = confidence × trust_urine       # trust_urine defaults to 0.6
deficit = (1 − w) × ledger + w × observed
```

In practice, the same colour 6 moves the estimate by **0.73 L** as a fresh
mid-afternoon sample and by **0.12 L** taken right after a big drink.

Note that the first-morning case gets **two** separate corrections — read one
shade lighter *and* trusted less. Those are two different problems: the reading
is biased dark, and it is also noisy.

### Morning weight

Stronger, because it is an actual measurement of mass rather than a proxy. Over
a day or two essentially all mass change is fluid.

The trend is what makes it work. A 7-day half-life EWMA is "true mass"; fast
deviations from it are water:

```
deficit_ml = (trend_kg − measured_kg) × 1000
```

Trusted at 0.85. A deviation larger than 3 kg is **discarded** — that is a
different scale, or clothes, or a typo, and treating it as a three-litre deficit
would be actively harmful.

Pre- and post-activity weights are deliberately **not** read as absolute
hydration state. They measure a change, and they are paired into a measured
sweat loss elsewhere; reading them both ways would count that sweat twice.

---

## Sodium

What is tracked is the **acute gap**, not total dietary sodium. A normal diet
supplies several grams a day and the kidney matches excretion to intake without
help. What it does not cover is a two-litre sweat loss.

```
loss = sweat_L × sweat_sodium_mmol_L × 22.99 mg/mmol
gap  = loss − (sodium in drinks + sodium in logged meals)
```

Default sweat sodium is 40 mmol/L (≈920 mg/L), but the real range is 20–80, which
is why the settings page asks in observable terms — salt crust on skin, white
marks on dark clothing, cramping late in long efforts — rather than asking for a
number nobody can estimate.

Sodium is recommended when any of: more than 1.2 L of sweat in the last four
hours; more than 1.5 L of replacement fluid planned; a daily gap over 1500 mg; or
cramps or headache logged alongside real sweating. The dose is 300–700 mg per
litre of replacement fluid, scaled across that band by how salty your sweat is.

Not logging meals biases this toward recommending sodium. That is the safe
direction and easily dismissed.

### The guard that points the other way

Almost every hydration app nags in one direction only. Drinking large volumes of
dilute fluid while **not** in deficit is how exercise-associated hyponatraemia
happens, and in endurance settings it has killed more people than dehydration.

Three conditions must hold together, because any one alone is fine:

- more than 1 L drunk in the last hour,
- that fluid carrying under 200 mg/L of sodium,
- and `deficit ≤ 0`.

Someone genuinely two litres down who drinks fast is doing the right thing and
must not be told off for it. When the guard does fire it **replaces** the
drinking plan rather than sitting beside it — handing someone a schedule and a
stop-drinking warning simultaneously is worse than useless.

---

## Turning state into a plan

The deficit is a number; "you are 1.2 L down" is not advice.

```
target = deficit − gut_ml + preload + maintenance × window
```

- **`− gut_ml`** — fluid already swallowed is on its way. Asking for it again is
  how you over-drink on the app's own advice.
- **`preload`** — 5–7 mL/kg four hours before a known session, 3–5 mL/kg two hours
  out if still dark.
- **`maintenance`** — ongoing baseline losses per waking hour, so a zero deficit
  still gets a plan. "Drink nothing" is true for exactly as long as it takes to
  become false.

> Sweat is excluded from maintenance. Sweat that has already happened is already
> in the deficit; adding it here asks you to replace it twice. An earlier version
> did exactly that and told a rider who had lost 2.1 L and was 2.2 L down to drink
> 3.6 L.

Then three constraints shape the schedule:

1. Never faster than the absorption cap — the excess would only sit in your
   stomach.
2. No single dose above 350 mL.
3. Nothing scheduled within two hours of bedtime, unless the deficit is severe
   (>3%). An app that costs you sleep to fix a rounding error has made your day
   worse. The cutoff is a comfort rule, not a safety rule, and it loses to one.

The scheduler then picks the **longest** interval whose doses stay under the
bolus cap — the fewest interruptions that still gets the fluid in — and emits a
one-line headline, which is what Home Assistant displays:

> *Drink 0.35 L now, then 0.25 L every 45 min until 7:15 PM. Add 1200 mg sodium.*

### A cross-check

For a rider 2.2 L down after a 2.1 L sweat loss, the planner produces 2.78 L
across the afternoon. The independent post-exercise heuristic — replace 135% of
what was lost, because some of what you drink leaves as urine before it is
retained — gives 2.83 L. Two methods built on different reasoning, agreeing to
within 2%.

---

## What the model deliberately does not do

- **Diagnose anything.** It flags a short, specific list of things that mean stop
  using an app: several very dark voids in a row, no urine for eight hours with
  symptoms, an estimated deficit past 3%. It flags nothing else, because a list
  that warns about everything gets dismissed and then warns about nothing.
- **Count logged void volumes against the ledger.** Modelled urine output already
  covers them; counting a logged volume as well would take the same water out
  twice. They are kept to check the model's urine term against reality.
- **Pretend to precision it does not have.** These are population averages
  corrected by a handful of noisy readings. The Insights page exists to show you
  how well that is working, and [`tuning.md`](tuning.md) covers what to change
  when it is not.
