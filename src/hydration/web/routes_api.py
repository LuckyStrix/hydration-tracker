"""The Home Assistant API.

Two directions. Home Assistant posts what it observes -- drinks, voids, the
ambient sensors, weight -- and polls `/status` for one line telling you what to
do about it, which is what ends up on a dashboard card or comes out of a
speaker.

Every route here authenticates with a bearer token, never a session cookie, and
every one accepts the units a person actually thinks in: litres, Fahrenheit and
pounds, alongside the metric names for anything that would rather send those.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db, service, units
from ..errors import HydrationError, NotFound, ValidationError
from . import deps

router = APIRouter(prefix="/api/v1")

HASS_STATE_LIMIT = 255
"""A Home Assistant entity state is capped at 255 characters. The headline is
normally well inside that, but a long sodium clause on a long plan could reach
it, and an over-long state is rejected by HASS with an error that gives no hint
why -- so it is truncated here where the reason is visible."""


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


async def _body(request: Request) -> dict:
    """Accept a JSON body or a form post.

    Home Assistant's `rest_command` sends JSON when given a payload and a
    content type, and form-encoded when not. Both arrive here, and failing on
    the second would be a confusing thing to debug from the YAML end.
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            raise ValidationError("body was not valid JSON") from None
        if not isinstance(payload, dict):
            raise ValidationError("expected a JSON object")
        return payload
    form = await request.form()
    return {key: value for key, value in form.items()}


def _time(payload: dict) -> datetime | None:
    raw = payload.get("at") or payload.get("timestamp")
    if not raw:
        return None
    try:
        return db.from_iso(str(raw))
    except ValueError:
        raise ValidationError(f"{raw!r} is not an ISO-8601 timestamp") from None


def _volume_ml(payload: dict, *names: str) -> float | None:
    """Read a volume given in whichever unit the caller found convenient."""
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            value = units.parse_optional_float(payload[name])
            if value is None:
                continue
            return value if name.endswith("_ml") else value * 1000.0
    return None


def _temp_c(payload: dict) -> float | None:
    if payload.get("temp_c") not in (None, ""):
        return units.parse_optional_float(payload["temp_c"])
    for name in ("temp_f", "temperature", "temp"):
        if payload.get(name) not in (None, ""):
            return units.f_to_c(units.parse_optional_float(payload[name]))
    return None


def _mass_kg(payload: dict) -> float | None:
    if payload.get("mass_kg") not in (None, ""):
        return units.parse_optional_float(payload["mass_kg"])
    for name in ("mass_lb", "weight_lb", "weight"):
        if payload.get(name) not in (None, ""):
            return units.lb_to_kg(units.parse_optional_float(payload[name]))
    return None


# -- inputs ----------------------------------------------------------------

@router.post("/intake")
async def log_intake(request: Request):
    payload = await _body(request)
    volume = _volume_ml(payload, "volume_ml", "volume_l", "litres", "liters", "volume")
    if volume is None:
        raise ValidationError("send volume_l (or volume_ml)")
    row_id = service.log_intake(
        deps.connection(),
        beverage=payload.get("beverage") or payload.get("drink") or "Water",
        volume_ml=volume,
        at=_time(payload),
        sodium_mg=units.parse_optional_float(payload.get("sodium_mg")),
        caffeine_mg=units.parse_optional_float(payload.get("caffeine_mg")),
        note=payload.get("note"),
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id, "logged_l": round(volume / 1000, 3)})


@router.post("/void")
async def log_void(request: Request):
    payload = await _body(request)
    colour = payload.get("colour", payload.get("color"))
    if colour in (None, ""):
        raise ValidationError("send colour, 1-8 from the chart")
    row_id = service.log_void(
        deps.connection(),
        colour=int(float(colour)),
        at=_time(payload),
        volume_ml=_volume_ml(payload, "volume_ml", "volume_l"),
        urgency=int(payload["urgency"]) if payload.get("urgency") not in (None, "") else None,
        note=payload.get("note"),
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id})


@router.post("/weight")
async def log_weight(request: Request):
    payload = await _body(request)
    mass = _mass_kg(payload)
    if mass is None:
        raise ValidationError("send mass_lb (or mass_kg)")
    row_id = service.log_weight(
        deps.connection(),
        mass_kg=mass,
        at=_time(payload),
        context=payload.get("context") or "morning",
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id})


@router.post("/env")
async def log_environment(request: Request):
    """Ambient conditions. The most valuable passive input there is -- it costs
    nothing to send and it drives both the baseline losses and the sweat
    estimate."""
    payload = await _body(request)
    temp_c = _temp_c(payload)
    humidity = units.parse_optional_float(payload.get("humidity", payload.get("humidity_pct")))
    if temp_c is None or humidity is None:
        raise ValidationError("send temp_f (or temp_c) and humidity")
    row_id = service.log_environment(
        deps.connection(),
        temp_c=temp_c,
        humidity_pct=humidity,
        at=_time(payload),
        location=payload.get("location") or "indoor",
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id})


@router.post("/symptom")
async def log_symptom(request: Request):
    payload = await _body(request)
    kind = payload.get("kind") or payload.get("symptom")
    if not kind:
        raise ValidationError("send a symptom kind, e.g. headache or cramp")
    row_id = service.log_symptom(
        deps.connection(),
        kind=str(kind),
        severity=int(payload.get("severity") or 2),
        at=_time(payload),
        note=payload.get("note"),
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id})


@router.post("/meal")
async def log_meal(request: Request):
    payload = await _body(request)
    row_id = service.log_meal(
        deps.connection(),
        label=payload.get("label"),
        water_ml=_volume_ml(payload, "water_ml", "water_l") or 0.0,
        sodium_mg=units.parse_optional_float(payload.get("sodium_mg")) or 0.0,
        at=_time(payload),
        source=payload.get("source") or "hass",
    )
    return _json({"ok": True, "id": row_id})


# -- output ----------------------------------------------------------------

@router.get("/status")
def status(request: Request):
    """One line to display, and the numbers behind it as attributes.

    Shaped for a Home Assistant REST sensor: `headline` becomes the entity
    state and everything else becomes attributes, so a dashboard card and a
    spoken announcement can both be built from this without any templating
    gymnastics at the other end.
    """
    conn = deps.connection()
    now = datetime.now(timezone.utc)
    timeline, plan = service.current_state(conn, now=now)
    service.record_recommendation(conn, plan)

    def rounded(value: float, places: int = 2) -> float:
        # `or 0.0` collapses negative zero. Python rounds -0.001 to -0.0, which
        # serialises as "-0.0" and reads to a person as a broken number.
        return round(value, places) or 0.0

    next_dose = plan.next_dose
    return _json(
        {
            "headline": plan.headline[:HASS_STATE_LIMIT],
            "status": plan.status,
            "deficit_l": rounded(plan.deficit_ml / 1000),
            "deficit_pct": rounded(plan.deficit_pct),
            "next_dose_l": rounded(next_dose.volume_ml / 1000) if next_dose else 0.0,
            "next_dose_at": db.to_iso(next_dose.at) if next_dose else None,
            "planned_total_l": rounded(plan.total_planned_ml / 1000),
            "sodium_mg": round(plan.sodium.recommended_mg),
            "sodium_gap_mg": round(plan.sodium.gap_mg),
            "daily_intake_l": rounded(plan.daily_intake_ml / 1000),
            "daily_target_l": rounded(plan.daily_target_ml / 1000),
            "daily_pct": round(
                100 * plan.daily_intake_ml / plan.daily_target_ml if plan.daily_target_ml else 0
            ),
            "sweat_24h_l": rounded(timeline.delta("sweat_ml", 24.0) / 1000),
            "detail": plan.detail,
            "flags": plan.medical_flags,
            "updated_at": db.to_iso(now),
        }
    )


@router.get("/beverages")
def beverages():
    """So the Home Assistant dropdown can be built from the real catalogue
    rather than a hand-copied list that drifts out of date."""
    rows = service.list_beverages(deps.connection())
    return _json({"beverages": [row["name"] for row in rows]})


# -- errors ----------------------------------------------------------------

def install_error_handlers(app) -> None:
    """Turn the application's own errors into JSON with a useful status.

    Without this a bad payload from a rest_command surfaces as a 500 and an
    HTML traceback, which is close to undebuggable from the YAML end.
    """

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return _json({"ok": False, "error": str(exc)}, status=400)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return _json({"ok": False, "error": str(exc)}, status=404)

    @app.exception_handler(HydrationError)
    async def _conflict(request: Request, exc: HydrationError):
        return _json({"ok": False, "error": str(exc)}, status=409)
