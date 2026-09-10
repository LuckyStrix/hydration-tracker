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

## Things that will bite you

- **Garmin is somebody else's private API.** `providers/garmin.py` searches
  payloads for known keys rather than indexing fixed paths, and probes for method
  names, because both move. Nothing in there may raise into a request.
- **The first Garmin login needs a human** if MFA is on. `hydration
  garmin-login`, once. A background thread has nobody to ask.
- **The database lives in a Docker volume, not a bind mount.** SQLite locking is
  unreliable across Docker Desktop's filesystem translation layer, and anything
  watching a host directory will eventually copy the file mid-write. Backups get
  out via `VACUUM INTO` (`hydration backup`), never by copying the file.
- **`_validated_time` refuses future timestamps.** Tests anchored to today's date
  fail for every hour later than the moment the suite runs; use a fixed past date.
- **The Home Assistant entity state cap is 255 characters.** The headline is
  truncated to fit, because HASS rejects an over-long state with an error that
  gives no hint why.
