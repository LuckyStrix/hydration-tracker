# Hydration tracker — working notes

A personal hydration tracker. Runs in Docker Desktop on a Windows PC, reached
over Tailscale. Home Assistant feeds it; Garmin feeds it; the website is where
the history and the answer live.

## The rule

> **Every mutation goes through `service.py`. Nothing else writes to the
> tables.**

The web layer parses and renders. The model computes. `service.py` is the only
place that decides what a row means, and the only place that turns stored rows
into the dataclasses the model consumes (`build_events`). Keeping that
translation in one function is what stops "what was logged" and "what it
implies" drifting apart.

## The model is the product

Everything outside `src/hydration/model/` is data entry and presentation, where
a bug is visible. A bug *inside* it produces a plausible-looking number that is
quietly wrong, which is worse. So:

- The model is a **pure function** — dataclasses in, a `Timeline` out, no
  database, no clock beyond what it is handed. That is what makes it testable
  without a browser, and `tests/test_model.py` is the most important file in the
  suite.
- **Every constant is in `model/constants.py` with the reason for its value.**
  Not the source it came from — the reason. Several encode a bug that was
  actually hit.
- The **ledger and the observers are separate ideas.** `balance.py` accumulates;
  `urine.py` and the weight observer correct. Neither is trusted alone.

## Things that are load-bearing

- **The absorption cap (`ABSORPTION_CAP_ML_PER_H`).** It is why the advice is a
  schedule rather than a number. Remove it and the whole planner collapses into
  "drink the deficit", which is wrong and uncomfortable.
- **`sweat_ml_estimated_raw` is a separate column on purpose.** The personal
  sweat factor is fitted from measured-against-*raw* pairs. Fit it against the
  calibrated estimate and each refit measures how well the last refit worked; it
  converges on 1.0 and silently erases itself. Covered by
  `test_calibration_does_not_feed_back_on_itself`.
- **A sample's timestamp is the state *as of* that instant.** `simulate` records
  the initial state before any step and labels each step's result with the time
  it *ends*. Labelling with the start time reads one step into the future and
  makes the absorption cap look violated when it is not.
- **Activity overlap queries use the stored `ended_at`**, never SQLite's
  `datetime()`. That function emits `2026-06-15 11:00:00` — space separator, no
  zone — which does not compare against the ISO-8601 strings every other
  timestamp uses, so the query silently matches nothing.
- **Past sweat must not enter the forward maintenance rate.** It is already in
  the deficit. An earlier version told a rider who had lost 2.1 L and was 2.2 L
  down to drink 3.6 L.
- **`db.transaction()` uses BEGIN IMMEDIATE.** The sync thread and a browser
  request genuinely do write at the same time. Do not make it deferred.
- **`deps.walk_routes` follows `_IncludedRouter.original_router`.** FastAPI
  wraps every `include_router` in an object with no `.path` and no `.routes`.
  Reading `app.routes` directly finds five objects and makes the authorisation
  tests pass vacuously — `test_the_route_walk_finds_the_routes` exists to catch
  that.
- **CSRF reads the body with `request.body()`, not `request.form()`.** Starlette
  only replays a body it saw read through `body()`; using `form()` in middleware
  leaves every downstream route seeing an empty form.
- **Every response leaves through `_finish()`.** Including the early refusals.
  Returning one directly skips the security headers.

## Conventions

- **Python 3.11**, FastAPI, stdlib `sqlite3`, hand-written SQL, no ORM. Note
  3.11 — nested same-quote f-strings are a 3.12 feature and will not compile.
- **Storage is metric and UTC.** Millilitres, kilograms, Celsius, ISO-8601 UTC
  strings that sort lexicographically. Display is litres, pounds, Fahrenheit and
  local time. The conversion happens in `units.py` and the Jinja filters in
  `web/deps.py`, and nowhere else. A conversion that leaks inward is how a column
  ends up holding a mix of both with no way to tell which is which.
- **Nothing is deleted.** Corrections set `voided_at` and insert a new row. A
  health log you can silently rewrite is one you cannot trust later.
- **No JavaScript, anywhere.** The CSP is `script-src 'none'`. Charts are
  server-rendered SVG built in `charts.py`; hover tooltips are `<title>`
  children, which browsers show natively. No `style=` attributes either — the
  CSP drops them, which is why meter widths are classes (`.w-45`).
- **Chart colour lives in `app.css`, not in `charts.py`.** Marks carry class
  names; the stylesheet themes them. One render is correct in light and dark.
  The palette passes the colourblind-separation gates — re-run the validator
  before changing a hex.
- **Errors**: `ValidationError`, `ConflictError`, `NotFound` become flash
  messages or JSON. Anything else is a real bug and should 500.

- **Silence is not dehydration.** After `UNLOGGED_GRACE_H` with no manual entry,
  the routine baseline stops being applied and the estimate relaxes toward
  `UNLOGGED_PRIOR_PCT`. Sweat is exempt -- a synced ride is real evidence. Without
  this the ledger reached ~20% of body mass over a fortnight and raised medical
  flags about it. Covered by `test_silence_is_not_read_as_dehydration`.
- **Engagement and evidence are two different signals.** Engagement (manual logs
  only) drives the regression; evidence (manual logs *plus* activities) drives
  the confidence level. Merging them made a hard ride read as "not enough data"
  the moment it finished.
- **Both fitted parameters must read an un-fitted ledger.** `sweat_calibration`
  fits from `sweat_ml_estimated_raw`; `baseline_loss_scale` fits from a timeline
  built with `apply_feedback=False`. Either one reading back its own influence
  converges on a no-op and silently erases itself.
- **The baseline fit is damped and uses the app's own lookback.** It is a
  feedback controller: undamped it oscillated into its bounds, and fitted
  against a different window than the app displays it moved the wrong way
  entirely. Both are regression-tested.
- **`build_void_contexts` runs once per simulation, not once per void.** The
  per-void scan was O(voids x events) and cost ten seconds on a year of history.
- **Schema changes need an entry in `db.MIGRATIONS`.** `CREATE TABLE IF NOT
  EXISTS` does nothing to an existing table, so a new column never reaches a
  database that already has data. Append, never reorder, and always give a
  DEFAULT.
- **A migration whose DEFAULT is a placeholder needs the backfill too** -- the
  fourth element of the tuple. `ended_at` arrived defaulted to `''`, which sorts
  below every real timestamp, so every activity older than the upgrade dropped
  out of the ledger without a word. Backfills run on every `init()`, not only on
  the start that adds the column, so a database that took an earlier
  backfill-less version is repaired rather than left broken.
- **The observer loop in `simulate` binds `event_index`, never `index`.**
  `index` is the step counter, and `sweat_rate`/`kcal_rate` are keyed by it.
  Rebinding it rewound the counter to a position in the *event list*, after
  which every step read the rate tables at the wrong index -- a ride's sweat
  landed hours late, or past `end` and so not at all, and it got worse the more
  diligently the log was kept. Covered by
  `test_a_well_kept_log_does_not_lose_a_rides_sweat`.
- **An activity's conditions are filled in field by field.** Garmin reports a
  ride temperature and never a humidity; replacing the pair because one was
  missing threw away the only reading taken where the sweating happened and
  substituted the sensor at home. Temperature is what the heat-fraction anchors
  move most on.
- **A hand-logged activity has no external id**, so the unique index cannot
  catch a double submit -- and two copies of a ride is two copies of its sweat.
  `record_activity` guards it on (provider, started_at) instead.
- **The sweat calibration re-fits from the service layer, not only from the
  sync.** A pre/post pair almost always lands after the session is already in
  the table, and it only ever re-fitted at the end of a Garmin run -- so anyone
  logging by hand stayed on the population model however many sessions they
  weighed. `log_weight` refreshes the activity; `log_manual_activity` re-fits.
- **The sync cursor only moves when everything landed.** Moving it regardless
  gave a failed activity one more chance inside the two-day overlap and then
  stepped over it for good.
- **`/import` has its own body limit.** A year of drinking exports to about a
  megabyte, and holding it to the ordinary form ceiling meant the export could
  not be read back -- the restore path failing on the size of the thing being
  restored. `app._body_limit` is the one place that decides.
- **Backups are taken by the app, on a timer** (`maintenance.py`), because a
  backup you have to remember is a backup you will not have. `hydration restore`
  is the other half, and it goes through SQLite's backup API rather than over the
  file: the live database may well be open in another process.
- **`record_recommendation` compares substance, never the headline.** The
  headline carries a clock time -- "...every 20 min until 5:40 PM" -- which
  advances with the wall clock, so every headline differs from the one a minute
  before it. Home Assistant polls `/api/v1/status` every 60 s, and comparing the
  strings wrote 1440 rows a day: exactly the duplicate-burial the check exists
  to prevent, arriving through the door it was watching.
- **`daily_summaries` widens its query to the local midnight that opens the
  first bucket.** `start` is an instant, so without it the earliest bar held
  only the hours after "now minus N days" and was drawn full height beside
  complete days. The route asks for `days - 1` so the chart has `days` bars.
- **The ledger blends urine through `urine.blend`**, and passes
  `profile.trust_urine` in. The arithmetic was open-coded in `_apply_observer`
  as well, and the copy in `urine.py` read the *constant* -- so the two agreed
  only at the default, and the tests were pinning the path the app did not take.
- **Caffeine is a real term now, defaulting to nothing.** The chain -- catalogue
  figures, per-drink override, profile column, threshold constant, event field,
  state slot -- all existed and reached `_apply_intake`, where nothing read it.
  It is a threshold effect on a *running load* with caffeine's own half-life, so
  the third coffee does something and the first does not. `caffeine_diuresis_ml_mg`
  at 0.0 leaves the ledger bit-for-bit as it was.
- **A correction reports the number it actually used.** The weight observer
  quoted the trend *after* this morning's reading had been folded into it --
  explaining a shift with a figure that had no part in producing it.

## Things that will bite you

- **Garmin is somebody else's private API.** `providers/garmin.py` searches
  payloads for known keys rather than indexing fixed paths, and probes for method
  names, because both move. Nothing in there may raise into a request.
- **The first Garmin login needs a human** if MFA is on. `hydration
  garmin-login`, once. A background thread has nobody to ask.
- **A refused Garmin login backs off; a transient one does not.** They are
  different problems: a 401 does not fix itself until somebody edits `.env`, and
  retrying it every fifteen minutes is 96 failed logins a day against Garmin's
  SSO -- which is how a typo in a password becomes a locked account, caused by
  us. `AUTH_FAILURE_MARKERS` decides which kind it was, and over-matching is the
  safe direction.
- **Credentials are read through `config._credential`**, which trims whitespace
  and matched outer quotes. A `.env` is edited by hand; a trailing space is
  invisible in every editor and produces a 401 indistinguishable from a wrong
  password.
- **The database lives in a Docker volume, not a bind mount.** SQLite locking is
  unreliable across Docker Desktop's filesystem translation layer, and anything
  watching a host directory will eventually copy the file mid-write. Backups get
  out via `VACUUM INTO` (`hydration backup`), never by copying the file.
- **`_validated_time` refuses future timestamps.** Tests anchored to today's date
  fail for every hour later than the moment the suite runs; use a fixed past date.
- **The Home Assistant entity state cap is 255 characters.** The headline is
  truncated to fit, because HASS rejects an over-long state with an error that
  gives no hint why.
- **Lint is `ruff check src tests`**, and the rule set is deliberately narrow
  (`E4`, `E7`, `E9`, `F`, `B`). The formatting and import-ordering rules are off
  on purpose: the layout here is a choice, and turning them on would bury the
  findings that matter under a few hundred that do not.
