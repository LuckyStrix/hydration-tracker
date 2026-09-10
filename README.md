# Hydration tracker

[![CI](https://github.com/LuckyStrix/hydration-tracker/actions/workflows/ci.yml/badge.svg)](https://github.com/LuckyStrix/hydration-tracker/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

A self-hosted hydration tracker that answers one question well: **how much should
I drink, over what period, and does it need salt?**

> Drink 0.35 L now, then 0.35 L every 45 min until 7:15 PM. Add 1200 mg sodium.

That sentence is the whole output. It goes on the web page, and it goes to Home
Assistant as a single sensor state you can put on a dashboard or have a speaker
read out.

It is not a drink counter. It keeps a running water-balance ledger — intake,
sweat, urine, insensible loss, the lot — and then *corrects that ledger against
things it can actually measure*: the colour of your last void, weighted by when
it happened; your morning weight against its own trend; Garmin's per-activity
sweat loss. A ledger on its own drifts, because every constant in it is a
population average applied to one person. A single reading on its own is noise.
Blending the two is the whole design.

Logging happens wherever it is convenient — a Home Assistant dashboard, an NFC
tag, a phone browser, your watch. The website is where the history lives and
where the answer is read.

## What it looks at

| Input | Where it comes from | Why it matters |
|---|---|---|
| Drinks | Home Assistant, the web form, an NFC tag | Volume × the drink's hydration index, absorbed on a realistic curve |
| Urine colour | Home Assistant or the 8-swatch picker | The cheapest hydration measurement there is — if you weight it by timing |
| Body weight | Garmin Index scale, or by hand | The strongest reading available; a pre/post pair *measures* a session's sweat outright |
| Activities | Garmin, or entered by hand | Sweat loss, calories, conditions |
| Temperature & humidity | Home Assistant sensors | Drives baseline losses and the sweat estimate. Costs nothing and needs no attention |
| Meals, symptoms | The log page | Sodium you already ate; cramps and headaches that are often salt, not water |
| How the day felt | One tap on the Today page | The only reading that sees what no sensor can. A run of them shifts the model's baseline, not just the log |

## The parts that are not obvious

**Timing decides what a urine colour is worth.** The first void of the morning
is dark in a perfectly hydrated person — vasopressin runs high overnight and the
kidney concentrates on purpose. A pale sample twenty minutes after a large drink
is the drink, passing through. A multivitamin makes the colour meaningless for
hours. So each reading carries a confidence, and the same colour 6 moves the
estimate by 0.73 L as a fresh mid-afternoon sample and by 0.12 L taken right
after a big glass of water.

**You cannot absorb more than about 800 mL an hour.** This is why the answer is a
schedule and not a number. Told to fix a 1.2 L deficit, the obvious advice is
"drink 1.2 L", and the result is a full stomach, an unchanged deficit and a trip
to the bathroom.

**Humidity raises fluid loss, not lowers it.** Sweat that cannot evaporate does
not cool you, so the body makes more of it — and all of it is still lost water.

**It warns in both directions.** Almost every hydration app nags one way only.
Drinking large volumes of dilute fluid while not actually in deficit is how
exercise-associated hyponatraemia happens, and in endurance settings that has
killed more people than dehydration. That warning is on by default, and it
replaces the drinking plan rather than sitting beside it.

**It knows the difference between "you did not drink" and "you did not log".**
This is the failure that sinks most self-tracking: a ledger left to integrate on
its own responded to a week away by reporting a deficit nobody could survive, and
then raised medical warnings about it. Six hours after the last entry the
estimate stops accumulating and relaxes toward an ordinary day — because a person
with access to water does not passively dehydrate — while a synced ride is still
believed, because sweat is real whether or not anyone was logging. When it is
running on a guess it says so, and it stops raising flags it cannot support.

**It tells you when it is wrong about you.** The Insights page shows how far the
ledger sits from what your own readings said, and after four weighed sessions it
fits a personal sweat rate and stops using the population average. Without that
the advice would be unfalsifiable, which is not an acceptable place to leave
something giving health guidance.

## Running it

Anywhere Docker runs. Developed against Docker Desktop on Windows and reached
over Tailscale, but there is nothing host-specific in the image.

```bash
git clone https://github.com/LuckyStrix/hydration-tracker.git
cd hydration-tracker
cp .env.example .env          # copy .env.example .env on Windows
docker compose up -d --build
```

Then open `http://<host>:8080` and set a password. That is the whole setup —
Garmin and Home Assistant are both optional and can be added later.

To update:

```bash
git pull && docker compose up -d --build
```

The database is in a named volume, so it survives rebuilds.

### Reaching it from elsewhere

Over a tailnet, `http://<machine-name>:8080` works as-is. If you put TLS in
front of it — `tailscale serve`, or any reverse proxy — set
`HYDRATION_BEHIND_PROXY=1`, which turns on secure cookies and forwarded-header
handling.

Leave that off for plain HTTP. With it on, the browser drops the session cookie
over HTTP and sign-in silently never sticks.

### Where the database lives, and why it matters

The database is in a **named Docker volume**, not a bind-mounted host
directory. That is deliberate, and both reasons bite in practice:

- On Docker Desktop, a bind mount crosses a filesystem translation layer where
  SQLite's file locking is unreliable. A named volume is a real Linux
  filesystem and behaves.
- Anything watching a host directory — a backup agent, a folder-syncing tool, an
  editor's indexer — will eventually copy the database mid-write and end up with
  a file that may not open.

So back it up with the command rather than by copying the file. That uses
`VACUUM INTO`, which takes a read lock and writes a complete, defragmented
database — consistent by construction, and safe for anything to pick up.

**This happens on its own.** The app takes one every 24 hours into `./backups/`
and keeps the newest 14, because a backup you have to remember to take is a
backup you will not have. The Settings page shows when the last one ran. To
take one now, or to change the schedule:

```powershell
docker compose exec hydration hydration backup            # now
docker compose exec hydration hydration backup --keep 14  # and prune old ones
```

`HYDRATION_BACKUP_HOURS`, `HYDRATION_BACKUP_KEEP` and `HYDRATION_BACKUP_AUTOMATIC`
in `.env` control the schedule.

### Putting one back

```powershell
docker compose exec hydration hydration restore /backups/hydration-20260615T120000Z.db
```

It asks for confirmation, takes a copy of the current database first, and
refuses a file that is not a hydration backup or does not pass an integrity
check — restoring the wrong file should cost a minute, not a history. The copy
goes through SQLite's own backup API rather than over the file, so it works
while the app is running. Restart the container afterwards anyway.

If you do point `/data` at a host directory, keep it on a local disk. SQLite
locking does not work reliably over a network share or a mapped drive.

### Garmin

Put your credentials in `.env`. If your account has multi-factor on, the first
login needs a code, and a background thread has nobody to ask — so do it once,
by hand:

```powershell
docker compose exec hydration hydration garmin-login
```

The cached tokens refresh themselves indefinitely after that. If a sync fails,
the Settings page says why rather than failing silently.

**If you get a 401 from Garmin**, in order of likelihood:

- `GARMIN_EMAIL` is your Garmin **display username** rather than the email
  address the account was registered with. Garmin refuses a username with
  exactly this error and no hint that that is the problem.
- A stray space or a quote around a value in `.env`. Both are trimmed now, but
  a `#` in an unquoted password is still truncated by some parsers — quote it.
- The password is genuinely wrong, or the account is locked. Sign in at
  [connect.garmin.com](https://connect.garmin.com) to tell those apart.
- Multi-factor is on and this is the first login. Run `hydration garmin-login`.

After editing `.env`, run `docker compose up -d` rather than `restart` — a
restart reuses the container's existing environment and your change will not
take effect, which looks exactly like the credentials still being wrong.

A refused login backs off — an hour, then longer, up to six hours — instead of
retrying on the usual interval. Ninety-six failed logins a day is how an
account gets locked, and that would be a worse problem than the typo that
started it. **Sync now** on the Settings page ignores the backoff.

This uses the unofficial `garminconnect` client, because Garmin has no public
API for an individual. It talks to the same endpoints the phone app uses, which
works well but is somebody else's private interface — so every call is written to
survive field names moving, and a sync failure can never break the app or the
ledger.

### Home Assistant

Everything you need is in [`hass/`](hass/README.md) — a drop-in package with the
REST commands, the dashboard helpers, the ambient-sensor automation, and the
sensor that carries the one-line plan.

## Command line

```
hydration serve           # the web server (what the container runs)
hydration status          # print the current plan
hydration garmin-login    # first Garmin login, answering MFA
hydration sync            # pull from Garmin now
hydration backup          # consistent database copy into ./backups
hydration backup --keep N # ...and delete all but the newest N
hydration restore FILE    # put a backup back (asks first; --force to skip)
hydration set-password    # reset the web password
```

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q      # pyproject sets pythonpath = ["src"]
.venv/bin/python -m ruff check src tests
```

The lint rule set is deliberately narrow — pyflakes and bugbear, which find
mistakes, not the formatting rules, which would churn a layout that is a
choice. CI runs both on 3.11 and 3.12, builds the image, and checks that a
backup taken inside it can be restored.

The model is a pure function — dataclasses in, a timeline out, no database — so
almost all of it is testable without a browser. That is deliberate: the model is
where the risk lives. Everything else is data entry and presentation, where a bug
is visible.

There is no JavaScript anywhere, and the CSP has `script-src 'none'`. Charts are
server-rendered SVG; every control is a form. The pages work with scripting
disabled and print correctly.

## Your data stays yours

Export the whole log as **JSON** (complete and reversible) or **CSV** (one flat
table for a spreadsheet), and import it back on another machine. Importing the
same file twice adds nothing the second time, so pulling your history across is
safe to repeat.

API tokens, the password hash and the CSRF key are deliberately excluded. An
export is a file that gets emailed and left in cloud storage; it must not be a
way in.

## How it is put together

```
src/hydration/
  model/          the ledger, the observers, the planner — pure functions, no I/O
    constants.py    every tunable number, with the reason for its value
    balance.py      the two-compartment ledger
    sweat.py        sweat estimation from calories and conditions
    urine.py        urine colour and body weight as weighted observations
    electrolytes.py sodium, and the hyponatraemia guard
    plan.py         state -> a drinking schedule
  service.py      the only module that writes to the database
  portability.py  export and import
  reports.py      read-only aggregation for history and insights
  charts.py       server-rendered SVG
  providers/      Garmin
  web/            routing and rendering
  templates/  static/  schema.sql
hass/             drop-in Home Assistant package
docs/             the model, and how to tune it
```

The layering is the design: **the web layer parses and renders, the model
computes, and `service.py` is the only place that decides what a row means.**

## Documentation

| | |
|---|---|
| [`docs/model.md`](docs/model.md) | How the model works — every term, the equations, the validation cases, and the reasoning. Start here if you want to know whether to believe it. |
| [`docs/tuning.md`](docs/tuning.md) | What to change when the model does not suit you, and what is not actually a tuning problem. |
| [`hass/README.md`](hass/README.md) | Home Assistant setup, the dashboard card, NFC tags, and the full HTTP API. |
| [`CLAUDE.md`](CLAUDE.md) | Working notes: the invariants, and the things that will bite you if you change them. |

## This is not medical software

It models population averages and corrects them with your own readings. It gives
hydration guidance, not diagnoses, and it cannot see anything a doctor would. It
flags a short, specific list of things that mean *stop using an app* — several
very dark voids in a row, no urine for eight hours with symptoms, an estimated
deficit past 3% of body mass — and deliberately flags nothing else, because a list
that warns about everything gets dismissed and then warns about nothing.
