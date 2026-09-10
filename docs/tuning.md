# Tuning the model

The Insights page compares what the ledger believed against what your own
readings said. A persistent bias in one direction means a constant does not suit
you. This is how to fix it.

Read this only if the bias is consistent over a couple of weeks *and* larger than
about 0.3 L. Below that it is noise, and chasing it will make the model worse.

## The ledger reads consistently drier than your readings

You are being told you are behind when your urine and your morning weight say you
are fine. In order of likelihood:

1. **Your insensible loss is lower than the default.** A sedentary indoor life in
   a cool house sits below `INSENSIBLE_ML_PER_KG_H = 0.44`. Try 0.38.
2. **You eat more water than the default assumes.** `food_water_ml_day` on the
   profile is 700 mL. A diet heavy in fruit, soup or salad is closer to 1000.
3. **Your obligatory urine is lower.** `OBLIGATORY_URINE_ML_PER_KG_H = 0.55` is an
   average; a low-protein, low-salt diet clears less solute and needs less water
   to do it.

## The ledger reads consistently wetter than your readings

You are being told you are fine while your urine says otherwise.

1. **Your sweat is being underestimated.** Most likely if you train. Weigh
   yourself immediately before and after four sessions and let the app fit your
   own factor — that is the fix, and it is automatic.
2. **Your house is warmer or drier than the sensor says**, or the sensor is
   somewhere unrepresentative. Check `default_temp_c` and where the Home
   Assistant sensor actually is.
3. **You are logging drinks you did not finish.** Common, and easy to miss.

## The constants and where they live

All of them are in `src/hydration/model/constants.py`, each with the reasoning
for its value. The ones most worth touching are on the Settings page and should
be changed there rather than in code:

| Setting | Default | Change it when |
|---|---|---|
| Sweat sodium | 40 mmol/L | Salt crust on your skin or white marks on dark kit → higher |
| Absorption cap | 800 mL/h | You are a trained endurance athlete and tolerate more during exercise |
| Trust in urine colour | 0.6 | Your bathroom light is bad, or you are unusually consistent about reading the chart |
| Food water | 700 mL/day | Your diet is much wetter or drier than average |

## Why the personal sweat factor is fitted from a separate column

The activity table stores `sweat_ml_estimated_raw` — the population model with no
personal factor — alongside the calibrated estimate. The fit reads the raw column.

This matters. Fitting the factor against an already-calibrated estimate makes each
refit measure how well the *previous* refit worked. It converges on 1.0, and the
model silently reverts to the population average while still reporting that it is
calibrated. If you change how calibration works, keep the two columns separate.

## Things that are not tuning problems

- **A single wildly wrong reading.** The model already discards weight deviations
  over 3 kg from trend, and sweat measurements above a physiological ceiling,
  because a bad measurement outranks both models and is worse than none.
- **Waking up mildly dehydrated.** Everyone does. Losses run all night and you are
  not drinking. A morning deficit around 0.5% of body mass is normal.
- **Being told to drink at zero deficit.** That is the maintenance rate, and it is
  correct: "drink nothing" is true for exactly as long as it takes to become false.
