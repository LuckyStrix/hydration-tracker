-- Hydration tracker schema.
--
-- Conventions that hold everywhere in this file:
--
--   * Timestamps are ISO-8601 UTC strings ('2026-09-10T14:30:00+00:00'). They
--     sort lexicographically, which every range query here relies on. Local
--     time exists only where a human reads it.
--   * Quantities are metric: millilitres, kilograms, degrees Celsius,
--     milligrams. Litres and Fahrenheit exist only in the templates.
--   * Nothing is deleted. A correction sets `voided_at` on the old row and
--     inserts a new one, because a health log you can silently rewrite is a
--     health log you cannot trust later.
--   * `source` records where a row came from, so a run of odd data can be
--     traced to the sensor or the integration that produced it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- The person the model is about. Exactly one row, id 1 -- enforced by the
-- CHECK rather than by convention, because 'there is only one profile' is an
-- assumption the entire model makes and a second row would silently halve it.
CREATE TABLE IF NOT EXISTS profile (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    display_name            TEXT    NOT NULL DEFAULT 'me',
    body_mass_kg            REAL    NOT NULL DEFAULT 75.0,
    height_cm               REAL    NOT NULL DEFAULT 178.0,
    sex                     TEXT    NOT NULL DEFAULT 'male'
                                    CHECK (sex IN ('male', 'female', 'other')),
    birth_year              INTEGER NOT NULL DEFAULT 1995,
    timezone                TEXT    NOT NULL DEFAULT 'America/New_York',

    wake_hour               REAL    NOT NULL DEFAULT 7.0,
    bed_hour                REAL    NOT NULL DEFAULT 23.0,

    -- How salty this person's sweat is. The single most useful thing to get
    -- right for electrolyte advice, and the settings page asks about it in
    -- plain terms (salt crust on skin, white marks on dark clothing).
    sweat_sodium_mmol_l     REAL    NOT NULL DEFAULT 40.0,

    -- Fitted from measured pre/post activity weights once there are enough of
    -- them. 1.0 means 'no personal data yet, using the population model'.
    sweat_calibration       REAL    NOT NULL DEFAULT 1.0,
    sweat_calibration_n     INTEGER NOT NULL DEFAULT 0,

    -- Fitted from the end-of-day question. Scales insensible loss and
    -- obligatory urine, so a person whose baseline genuinely runs higher or
    -- lower than the population average stops being told otherwise every day.
    baseline_loss_scale     REAL    NOT NULL DEFAULT 1.0,
    feedback_n              INTEGER NOT NULL DEFAULT 0,

    absorption_cap_ml_h     REAL    NOT NULL DEFAULT 800.0,
    food_water_ml_day       REAL    NOT NULL DEFAULT 700.0,
    caffeine_diuresis_ml_mg REAL    NOT NULL DEFAULT 0.0,
    trust_urine             REAL    NOT NULL DEFAULT 0.6,

    default_temp_c          REAL    NOT NULL DEFAULT 21.0,
    default_humidity_pct    REAL    NOT NULL DEFAULT 45.0,

    -- Which unit the entry boxes start in, 'l' or 'oz'. Entry only: storage
    -- is millilitres and the display is litres either way. No CHECK, because
    -- ALTER TABLE ADD COLUMN carries constraints unevenly and a fresh database
    -- differing from a migrated one is worse than validating in one function
    -- (`units.volume_to_ml`, which every entry path goes through).
    volume_entry_unit       TEXT    NOT NULL DEFAULT 'l',

    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL
);


-- What a drink is made of, per litre. Seeded in db.py with the common ones.
CREATE TABLE IF NOT EXISTS beverage (
    id                  INTEGER PRIMARY KEY,
    name                TEXT    NOT NULL UNIQUE,

    -- Beverage Hydration Index: retention relative to still water at 1.0.
    -- Milk and oral rehydration solution beat water; nothing sensible is far
    -- below it.
    hydration_index     REAL    NOT NULL DEFAULT 1.0 CHECK (hydration_index > 0),

    sodium_mg_per_l     REAL    NOT NULL DEFAULT 0.0,
    potassium_mg_per_l  REAL    NOT NULL DEFAULT 0.0,
    caffeine_mg_per_l   REAL    NOT NULL DEFAULT 0.0,
    alcohol_pct         REAL    NOT NULL DEFAULT 0.0,
    kcal_per_l          REAL    NOT NULL DEFAULT 0.0,

    -- Riboflavin colours urine for hours and has nothing to do with hydration,
    -- so a void logged soon after one of these is close to worthless as a
    -- measurement. urine.py reads this flag through the void context.
    is_multivitamin     INTEGER NOT NULL DEFAULT 0,

    -- Ordering on the quick-log buttons; lower sorts first.
    sort_order          INTEGER NOT NULL DEFAULT 100,
    archived_at         TEXT
);


CREATE TABLE IF NOT EXISTS intake (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    beverage_id     INTEGER NOT NULL REFERENCES beverage(id),
    volume_ml       REAL    NOT NULL CHECK (volume_ml > 0),

    -- Overrides for when the bottle in hand is not what the catalogue says --
    -- an electrolyte tab in plain water, a double-strength mix. NULL means
    -- 'use the beverage's own figure'.
    sodium_mg       REAL,
    caffeine_mg     REAL,

    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS intake_at ON intake(at) WHERE voided_at IS NULL;


CREATE TABLE IF NOT EXISTS void (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,

    -- Armstrong 8-point urine colour chart.
    colour          INTEGER NOT NULL CHECK (colour BETWEEN 1 AND 8),

    -- Optional, and deliberately not subtracted from the ledger: modelled
    -- urine output already covers it, and counting a logged volume as well
    -- would take the same water out twice. It is kept to check the model's
    -- urine term against reality on the insights page.
    volume_ml       REAL,

    -- Overnight urine is concentrated by design, so this changes both how the
    -- colour is read and how far it is trusted. Derived on insert from the
    -- gap since the previous void and the profile's wake hour, but stored
    -- rather than recomputed so that later edits to wake_hour cannot silently
    -- rewrite history.
    is_first_morning INTEGER NOT NULL DEFAULT 0,

    urgency         INTEGER CHECK (urgency BETWEEN 1 AND 5),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS void_at ON void(at) WHERE voided_at IS NULL;


CREATE TABLE IF NOT EXISTS body_weight (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    mass_kg         REAL    NOT NULL CHECK (mass_kg > 0),

    -- 'morning' is the observer the ledger trusts most. The pre/post pair is
    -- something else entirely: a direct measurement of a single session's
    -- sweat loss. Reading those as absolute hydration state as well would
    -- count that sweat twice, so the model refuses to.
    context         TEXT    NOT NULL DEFAULT 'morning'
                            CHECK (context IN ('morning', 'pre_activity', 'post_activity', 'other')),
    activity_id     INTEGER REFERENCES activity(id),

    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS body_weight_at ON body_weight(at) WHERE voided_at IS NULL;


CREATE TABLE IF NOT EXISTS activity (
    id                  INTEGER PRIMARY KEY,
    provider            TEXT    NOT NULL DEFAULT 'manual',

    -- Garmin's own activity id. The UNIQUE constraint with provider is what
    -- makes re-syncing safe: without it, every sync would add the same ride
    -- again and the ledger would show a sweat loss that never happened.
    external_id         TEXT,

    started_at          TEXT    NOT NULL,
    duration_s          REAL    NOT NULL CHECK (duration_s >= 0),

    -- Stored rather than derived in SQL. SQLite's datetime() emits
    -- '2026-06-15 11:00:00' -- a space separator and no zone -- which does not
    -- compare against the ISO-8601 strings every other timestamp here uses, so
    -- an overlap query built on it silently matches nothing.
    ended_at            TEXT    NOT NULL,
    name                TEXT,
    activity_type       TEXT,
    distance_m          REAL,
    kcal                REAL,
    avg_hr              REAL,

    -- All three kept side by side, never collapsed into one number. The
    -- comparison is what makes the calibration trustworthy rather than magic,
    -- and it is the whole content of the activities page.
    sweat_ml_reported   REAL,   -- Garmin's estimate
    sweat_ml_estimated  REAL,   -- ours, with this person's fitted factor applied
    sweat_ml_measured   REAL,   -- from a pre/post weight pair, when one exists

    -- The same estimate *before* the personal calibration factor. Kept because
    -- the factor is fitted from measured-against-estimated pairs, and fitting
    -- it against an already-calibrated estimate is a feedback loop: each refit
    -- would measure how well the last refit worked, converge on 1.0, and
    -- quietly undo itself. The fit reads this column; everything else reads
    -- the calibrated one.
    sweat_ml_estimated_raw REAL,
    sweat_ml_used       REAL    NOT NULL DEFAULT 0,
    sweat_source        TEXT    NOT NULL DEFAULT 'none',

    fluid_consumed_ml   REAL    NOT NULL DEFAULT 0,
    temp_c              REAL,
    humidity_pct        REAL,

    raw_json            TEXT,
    created_at          TEXT    NOT NULL,
    voided_at           TEXT,
    voided_reason       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS activity_external
    ON activity(provider, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS activity_started ON activity(started_at) WHERE voided_at IS NULL;


-- Ambient conditions, pushed from Home Assistant every quarter hour or so.
CREATE TABLE IF NOT EXISTS environment (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    temp_c          REAL    NOT NULL,
    humidity_pct    REAL    NOT NULL CHECK (humidity_pct BETWEEN 0 AND 100),
    location        TEXT    NOT NULL DEFAULT 'indoor'
                            CHECK (location IN ('indoor', 'outdoor')),
    source          TEXT    NOT NULL DEFAULT 'hass',
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS environment_at ON environment(at);


CREATE TABLE IF NOT EXISTS symptom (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    severity        INTEGER NOT NULL DEFAULT 2 CHECK (severity BETWEEN 1 AND 5),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS symptom_at ON symptom(at) WHERE voided_at IS NULL;


-- Food water and food sodium. Rough by design: a logged 'salty dinner' beats
-- an unlogged one, and precision here would cost more effort than it returns.
CREATE TABLE IF NOT EXISTS meal (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    label           TEXT,
    water_ml        REAL    NOT NULL DEFAULT 0,
    sodium_mg       REAL    NOT NULL DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS meal_at ON meal(at) WHERE voided_at IS NULL;


-- How the day actually felt. A third observer alongside urine colour and body
-- weight, and the only one that can see things no sensor here reaches --
-- thirst, headache, the particular flatness of being under-hydrated.
--
-- One per local day is the intent, but nothing enforces it: changing your mind
-- at 9pm about how 3pm felt is legitimate, and the later answer is simply
-- another reading.
CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    verdict         TEXT    NOT NULL
                            CHECK (verdict IN ('waterlogged', 'a_bit_much', 'about_right',
                                               'a_bit_dry', 'very_dry')),
    note            TEXT,
    source          TEXT    NOT NULL DEFAULT 'web',
    created_at      TEXT    NOT NULL,
    voided_at       TEXT,
    voided_reason   TEXT
);
CREATE INDEX IF NOT EXISTS feedback_at ON feedback(at) WHERE voided_at IS NULL;


-- What the app told you, and when. Written whenever the headline materially
-- changes. This is what lets the history answer 'what did it say, and did
-- following it help' -- without it, the advice is unfalsifiable.
CREATE TABLE IF NOT EXISTS recommendation (
    id              INTEGER PRIMARY KEY,
    at              TEXT    NOT NULL,
    status          TEXT    NOT NULL,
    headline        TEXT    NOT NULL,
    deficit_ml      REAL    NOT NULL,
    deficit_pct     REAL    NOT NULL,
    target_ml       REAL    NOT NULL DEFAULT 0,
    sodium_mg       REAL    NOT NULL DEFAULT 0,
    detail_json     TEXT,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_at ON recommendation(at);


-- Key/value for things with no schema of their own: sync cursors, the login
-- password hash, Garmin's last error.
CREATE TABLE IF NOT EXISTS setting (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);


-- Bearer tokens for Home Assistant. Stored as a hash: a token readable out of
-- the database is a token readable out of a backup.
CREATE TABLE IF NOT EXISTS api_token (
    id              INTEGER PRIMARY KEY,
    label           TEXT    NOT NULL,
    token_hash      TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL,
    last_used_at    TEXT,
    revoked_at      TEXT
);


CREATE TABLE IF NOT EXISTS session (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    user_agent      TEXT
);
CREATE INDEX IF NOT EXISTS session_expires ON session(expires_at);
