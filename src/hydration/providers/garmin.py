"""Garmin Connect, through the unofficial `garminconnect` client.

Garmin has no public API for an individual, so this talks to the same endpoints
the mobile app uses. That works well and is widely used, but it is somebody
else's private interface: field names move, payload shapes change between
firmware releases, and a login flow can grow a step. Everything here is written
with that in mind:

  * **Nothing raises into the application.** A sync failure is logged and
    surfaced on the settings page. It never breaks a request or the ledger.
  * **Fields are searched for, not indexed.** Garmin nests the same value at
    different paths depending on the endpoint and activity type, so
    `_find_number` walks the payload looking for known keys rather than
    asserting a path that will eventually move.
  * **Method names are probed.** The client library renames things between
    releases; calling the first name that exists beats pinning a version that
    stops getting fixes.

The one thing that genuinely needs a human is the first login when multi-factor
is on, which cannot be answered from a background thread. `hydration
garmin-login` does that once and writes the tokens to the data volume; every
sync afterwards refreshes them on its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

from .. import config
from .base import Activity, WeighIn

log = logging.getLogger("hydration.garmin")


class GarminUnavailable(RuntimeError):
    """Raised inside this module only; `sync.py` turns it into a status
    message. It never reaches a request handler."""


def _client_class():
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise GarminUnavailable(
            "the garminconnect package is not installed in this image"
        ) from exc
    return Garmin


def _call_first(obj: Any, names: Iterable[str], *args, **kwargs):
    """Call whichever of these methods this version of the client actually has.

    The library renames methods between releases. Probing beats pinning an old
    version, because the reason to be on a new one is that it carries the fix
    for whatever Garmin changed last.
    """
    tried = []
    for name in names:
        method: Callable | None = getattr(obj, name, None)
        if callable(method):
            return method(*args, **kwargs)
        tried.append(name)
    raise GarminUnavailable(f"this garminconnect version has none of: {', '.join(tried)}")


def connect(*, interactive: bool = False):
    """Log in, preferring the cached tokens.

    `interactive` is only true when a person is sitting at the CLI, and it is
    what allows the multi-factor prompt. A background sync must never ask a
    question it has nobody to ask.
    """
    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        raise GarminUnavailable("GARMIN_EMAIL and GARMIN_PASSWORD are not set")

    Garmin = _client_class()
    config.GARMIN_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_dir = str(config.GARMIN_TOKEN_DIR)

    try:
        client = Garmin(
            email=config.GARMIN_EMAIL,
            password=config.GARMIN_PASSWORD,
            is_cn=False,
            prompt_mfa=_prompt_mfa if interactive else _refuse_mfa,
        )
    except TypeError:
        # Older releases take positional arguments and have no MFA hook.
        client = Garmin(config.GARMIN_EMAIL, config.GARMIN_PASSWORD)

    try:
        client.login(token_dir)
    except Exception as exc:
        raise GarminUnavailable(f"Garmin login failed: {exc}") from exc
    return client


def _prompt_mfa() -> str:
    return input("Garmin multi-factor code: ").strip()


def _refuse_mfa() -> str:
    raise GarminUnavailable(
        "Garmin is asking for a multi-factor code, which a background sync cannot answer. "
        "Run: docker compose exec hydration hydration garmin-login"
    )


# -- payload archaeology ---------------------------------------------------

SWEAT_KEYS = ("sweatLoss", "sweatLossInML", "waterEstimated", "estimatedSweatLoss")
FLUID_KEYS = ("waterConsumed", "fluidConsumed", "waterIntake")
TEMP_KEYS = ("avgTemperature", "temperature", "airTemperature", "minTemperature")
"""Average first. `minTemperature` sitting ahead of it meant a ride that
started cold was estimated at its coldest moment for its whole length."""

HUMIDITY_KEYS = ("humidity", "relativeHumidity", "avgHumidity", "averageHumidity")
"""Garmin rarely carries humidity, but when it does it belongs to the ride
rather than to the sensor in the hallway at home. Absent it, `record_activity`
falls back per field -- which is why this being empty no longer costs us the
temperature as well."""

KCAL_KEYS = ("calories", "activeKilocalories", "kilocalories")
HR_KEYS = ("averageHR", "avgHr", "averageHeartRate")


def _find_number(
    payload: Any, keys: tuple[str, ...], depth: int = 6, *, positive_only: bool = True
) -> float | None:
    """Search a nested payload for the first of `keys` holding a number.

    Garmin puts the same value under `summaryDTO` on one endpoint and at the
    top level on another, and moves it again for some activity types. Walking
    for the key is uglier than a fixed path and survives a great deal more.

    `positive_only` treats a zero or a negative as "not really there", which is
    right for calories and sweat -- Garmin pads missing numbers with 0 -- and
    wrong for temperature, where sub-zero is a real reading and a winter ride
    is exactly when the sweat estimate should differ most.
    """
    if depth < 0 or payload is None:
        return None
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not positive_only or value > 0:
                    return float(value)
        for value in payload.values():
            found = _find_number(value, keys, depth - 1, positive_only=positive_only)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload[:20]:
            found = _find_number(item, keys, depth - 1, positive_only=positive_only)
            if found is not None:
                return found
    return None


def _parse_time(raw: str | None) -> datetime | None:
    """Garmin timestamps arrive in several shapes and usually without a zone.

    `startTimeGMT` is UTC despite saying nothing about it, which is why that
    field is preferred over `startTimeLocal` -- reading the local one as UTC
    would file every activity hours out of place.
    """
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            moment = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
    return None


def _activity_from(summary: dict, detail: dict | None) -> Activity | None:
    external_id = summary.get("activityId") or summary.get("activityid")
    started = _parse_time(summary.get("startTimeGMT") or summary.get("startTimeLocal"))
    if external_id is None or started is None:
        return None

    merged: dict = {"summary": summary, "detail": detail or {}}
    type_key = summary.get("activityType") or {}
    return Activity(
        external_id=str(external_id),
        started_at=started,
        duration_s=float(summary.get("duration") or summary.get("elapsedDuration") or 0.0),
        name=summary.get("activityName"),
        activity_type=type_key.get("typeKey") if isinstance(type_key, dict) else str(type_key),
        distance_m=summary.get("distance"),
        kcal=_find_number(merged, KCAL_KEYS),
        avg_hr=_find_number(merged, HR_KEYS),
        sweat_ml=_find_number(merged, SWEAT_KEYS),
        fluid_consumed_ml=_find_number(merged, FLUID_KEYS) or 0.0,
        temp_c=_find_number(merged, TEMP_KEYS, positive_only=False),
        humidity_pct=_find_number(merged, HUMIDITY_KEYS),
        raw={"summary": summary},
    )


# -- the two things this provider produces ---------------------------------

def fetch_activities(client, since: datetime, until: datetime | None = None) -> list[Activity]:
    until = until or datetime.now(timezone.utc)
    summaries = _call_first(
        client,
        ("get_activities_by_date", "get_activities_bydate"),
        since.date().isoformat(),
        until.date().isoformat(),
    ) or []

    activities: list[Activity] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        detail = None
        activity_id = summary.get("activityId")
        if activity_id is not None and not _find_number(summary, SWEAT_KEYS):
            # Only pay for the detail call when the summary did not already
            # carry a sweat figure. On a month's backfill that is the
            # difference between tens of requests and hundreds.
            try:
                detail = _call_first(client, ("get_activity", "get_activity_details"), activity_id)
            except Exception as exc:
                log.debug("no detail for activity %s: %s", activity_id, exc)
        built = _activity_from(summary, detail if isinstance(detail, dict) else None)
        if built is not None:
            activities.append(built)
    return activities


def fetch_weigh_ins(client, since: datetime, until: datetime | None = None) -> list[WeighIn]:
    """Body composition from an Index scale, if there is one.

    Garmin reports grams here, which is worth knowing: treating them as
    kilograms would produce a 75000 kg reading, and treating them as pounds a
    plausible-looking wrong one.
    """
    until = until or datetime.now(timezone.utc)
    try:
        payload = _call_first(
            client,
            ("get_body_composition", "get_weigh_ins", "get_daily_weigh_ins"),
            since.date().isoformat(),
            until.date().isoformat(),
        )
    except GarminUnavailable:
        return []
    except Exception as exc:
        log.info("weigh-in fetch failed: %s", exc)
        return []

    entries = []
    if isinstance(payload, dict):
        entries = payload.get("dateWeightList") or payload.get("dailyWeightSummaries") or []
    elif isinstance(payload, list):
        entries = payload

    results: list[WeighIn] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        grams = entry.get("weight") or entry.get("weightInGrams")
        stamp = entry.get("date") or entry.get("timestampGMT") or entry.get("calendarDate")
        if not grams:
            continue
        moment = _timestamp_or_date(stamp)
        if moment is None:
            continue
        mass_kg = float(grams) / 1000.0
        if not 20.0 < mass_kg < 400.0:
            continue
        results.append(WeighIn(at=moment, mass_kg=mass_kg))
    return results


def _timestamp_or_date(raw) -> datetime | None:
    """Garmin mixes epoch milliseconds and date strings in the same field."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    parsed = _parse_time(str(raw))
    if parsed is not None:
        return parsed
    try:
        return datetime.combine(date.fromisoformat(str(raw)), datetime.min.time(), tzinfo=timezone.utc)
    except ValueError:
        return None


def push_hydration(client, when: date, total_ml: float) -> bool:
    """Write the day's intake back to Garmin so the watch widget agrees.

    Off unless HYDRATION_GARMIN_WRITE_BACK is set. A sync that both reads and
    writes the same figure can argue with itself, and the watch is not the
    system of record here.
    """
    if not config.GARMIN_WRITE_BACK:
        return False
    try:
        _call_first(client, ("add_hydration_data", "set_hydration_data"), value_in_ml=total_ml, cdate=when.isoformat())
        return True
    except Exception as exc:
        log.info("hydration write-back failed (non-fatal): %s", exc)
        return False
