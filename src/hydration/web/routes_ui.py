"""The pages a person looks at, and the forms that write to them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, Response

from .. import charts, config, db, portability, reports, service, units
from ..errors import ValidationError
from ..model import plan as P
from . import deps

router = APIRouter()

QUICK_VOLUMES_L = (0.25, 0.35, 0.5, 0.75, 1.0)
"""The quick-log buttons. Litres, because that is what was asked for, and a
short list because a long one is slower to use than typing."""


@router.get("/healthz")
def healthz():
    """Liveness for the container healthcheck. Touches the database, because an
    app that answers while its database is unreachable is not healthy."""
    deps.connection().execute("SELECT 1 FROM profile WHERE id = 1").fetchone()
    return PlainTextResponse("ok")


# -- today -----------------------------------------------------------------

@router.get("/")
def today(request: Request):
    conn = deps.connection()
    now = datetime.now(timezone.utc)
    tz = deps.profile_timezone(conn)

    timeline, plan = service.current_state(conn, now=now)
    service.record_recommendation(conn, plan)

    start_of_day = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    entries = reports.timeline_entries(conn, start_of_day.astimezone(timezone.utc), now)

    return deps.render(
        request,
        "today.html",
        plan=plan,
        timeline=timeline,
        profile=timeline.profile,
        entries=entries,
        beverages=service.list_beverages(conn),
        quick_volumes=QUICK_VOLUMES_L,
        deficit_chart=charts.deficit_chart(
            timeline, timeline.corrections, tz, body_mass_kg=timeline.profile.body_mass_kg
        ),
        daily_pct=min(
            100, round(100 * plan.daily_intake_ml / plan.daily_target_ml) if plan.daily_target_ml else 0
        ),
        sweat_24h_ml=timeline.delta("sweat_ml", 24.0),
        gauge=_gauge(plan),
    )


def _gauge(plan: P.Plan) -> dict:
    """The dial on the front page.

    Zero is euhydrated and the needle runs to 3% -- past that the number stops
    being a training metric, and stretching the scale to fit would make the
    normal range unreadable to flatter an emergency.
    """
    pct = plan.deficit_pct
    fraction = min(max(pct / 3.0, -0.2), 1.0)
    return {
        "pct": pct,
        "fraction": fraction,
        "dash": max(0.0, min(1.0, fraction)) * 100.0,
        "tone": {
            "ok": "good",
            "add_sodium": "warning",
            "drink": "warning",
            "drink_urgent": "critical",
            "slow_down": "serious",
        }.get(plan.status, "good"),
    }


# -- logging ---------------------------------------------------------------

@router.get("/log")
def log_page(request: Request):
    conn = deps.connection()
    return deps.render(
        request,
        "log.html",
        beverages=service.list_beverages(conn),
        quick_volumes=QUICK_VOLUMES_L,
        colours=range(1, 9),
        now_local=datetime.now(deps.profile_timezone(conn)).strftime("%Y-%m-%dT%H:%M"),
    )


def _parse_local(raw: str | None, tz) -> datetime | None:
    """Read a datetime-local field as the wall clock the person typed.

    The browser sends '2026-09-10T14:30' with no zone. Treating that as UTC
    would file a 2pm drink at 10am, so it is localised to the profile's zone
    before being converted for storage.
    """
    if not raw:
        return None
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"{raw!r} is not a valid date and time") from None
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


@router.post("/log/drink")
def log_drink(
    request: Request,
    beverage: str = Form(...),
    volume_l: str = Form(...),
    at: str = Form(""),
    sodium_mg: str = Form(""),
    note: str = Form(""),
):
    conn = deps.connection()
    litres = units.parse_optional_float(volume_l)
    if litres is None:
        raise ValidationError("how much did you drink?")
    service.log_intake(
        conn,
        beverage=beverage,
        volume_ml=litres * 1000.0,
        at=_parse_local(at, deps.profile_timezone(conn)),
        sodium_mg=units.parse_optional_float(sodium_mg),
        note=note.strip() or None,
    )
    return deps.redirect(deps.safe_path(request.headers.get("referer", "/").rsplit(request.base_url.netloc, 1)[-1] or "/"),
                         f"Logged {litres:.2f} L of {beverage.lower()}.", "good")


@router.post("/log/void")
def log_void(
    request: Request,
    colour: int = Form(...),
    at: str = Form(""),
    volume_l: str = Form(""),
    urgency: str = Form(""),
    note: str = Form(""),
):
    conn = deps.connection()
    litres = units.parse_optional_float(volume_l)
    service.log_void(
        conn,
        colour=colour,
        at=_parse_local(at, deps.profile_timezone(conn)),
        volume_ml=litres * 1000.0 if litres else None,
        urgency=int(urgency) if urgency else None,
        note=note.strip() or None,
    )
    return deps.redirect("/", f"Logged a void at colour {colour}.", "good")


@router.post("/log/weight")
def log_weight(
    request: Request,
    mass_lb: str = Form(...),
    context: str = Form("morning"),
    at: str = Form(""),
):
    conn = deps.connection()
    pounds = units.parse_optional_float(mass_lb)
    if pounds is None:
        raise ValidationError("what did the scale say?")
    service.log_weight(
        conn,
        mass_kg=units.lb_to_kg(pounds),
        context=context,
        at=_parse_local(at, deps.profile_timezone(conn)),
    )
    return deps.redirect("/", f"Logged {pounds:.1f} lb.", "good")


@router.post("/log/symptom")
def log_symptom(request: Request, kind: str = Form(...), severity: int = Form(2), note: str = Form("")):
    service.log_symptom(deps.connection(), kind=kind, severity=severity, note=note.strip() or None)
    return deps.redirect("/", f"Noted: {kind}.", "good")


@router.post("/log/meal")
def log_meal(
    request: Request,
    label: str = Form(""),
    water_l: str = Form(""),
    sodium_mg: str = Form(""),
):
    litres = units.parse_optional_float(water_l) or 0.0
    service.log_meal(
        deps.connection(),
        label=label.strip() or None,
        water_ml=litres * 1000.0,
        sodium_mg=units.parse_optional_float(sodium_mg) or 0.0,
    )
    return deps.redirect("/", "Logged a meal.", "good")


@router.post("/log/activity")
def log_activity(
    request: Request,
    name: str = Form(""),
    at: str = Form(...),
    duration_min: float = Form(...),
    kcal: str = Form(""),
    sweat_l: str = Form(""),
):
    conn = deps.connection()
    started = _parse_local(at, deps.profile_timezone(conn))
    if started is None:
        raise ValidationError("when did it start?")
    sweat_litres = units.parse_optional_float(sweat_l)
    service.record_activity(
        conn,
        started_at=started,
        duration_s=duration_min * 60.0,
        name=name.strip() or None,
        kcal=units.parse_optional_float(kcal),
        sweat_ml_reported=sweat_litres * 1000.0 if sweat_litres else None,
    )
    return deps.redirect("/activities", "Activity recorded.", "good")


@router.post("/log/feel")
def log_feel(request: Request, verdict: str = Form(...), note: str = Form("")):
    """The end-of-day question.

    Worth more than it looks: it is the only reading that can see what no
    sensor here reaches, and a run of them shifts the model's baseline rather
    than just being recorded.
    """
    conn = deps.connection()
    service.log_feedback(conn, verdict=verdict, note=note.strip() or None)
    scale = service.load_profile(conn).baseline_loss_scale
    return deps.redirect(
        "/",
        f"Noted. Baseline losses now at {scale:.2f}x the population average.",
        "good",
    )


@router.get("/export/hydration.json")
def export_json(request: Request):
    """Everything, in a form the importer can put back.

    A download rather than a page, and a plain GET so it needs no JavaScript.
    """
    payload = portability.export_json(deps.connection())
    stamp = datetime.now(deps.profile_timezone(deps.connection())).strftime("%Y%m%d")
    return Response(
        payload,
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="hydration-{stamp}.json"'},
    )


@router.get("/export/entries.csv")
def export_csv(request: Request):
    payload = portability.export_csv(deps.connection())
    stamp = datetime.now(deps.profile_timezone(deps.connection())).strftime("%Y%m%d")
    return Response(
        payload,
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="hydration-{stamp}.csv"'},
    )


@router.post("/import")
async def import_data(request: Request):
    """Merge an exported file back in.

    Reads the upload by hand rather than through a typed parameter, because the
    CSRF middleware has already consumed the multipart body and re-parsing it
    is the reliable way to get both the token and the file.
    """
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise ValidationError("choose a file to import")

    raw = await upload.read()
    if len(raw) > 64 * 1024 * 1024:
        raise ValidationError("that file is larger than this importer will accept")

    payload = portability.parse_export(raw)
    counts = portability.import_payload(
        deps.connection(), payload, restore_profile=bool(form.get("restore_profile"))
    )
    added = sum(counts.values())
    detail = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()) if count)
    message = f"Imported {added} new entries ({detail})." if added else "Nothing new -- already imported."
    return deps.redirect("/settings", message, "good")


@router.post("/log/retract")
def retract(request: Request, table: str = Form(...), row_id: int = Form(...), back: str = Form("/")):
    service.void_entry(deps.connection(), table, row_id, reason="retracted from the web UI")
    return deps.redirect(deps.safe_path(back), "Entry retracted. It stays in the history, marked.", "good")


# -- history ---------------------------------------------------------------

@router.get("/history")
def history(request: Request, days: int = 14):
    conn = deps.connection()
    tz = deps.profile_timezone(conn)
    days = max(1, min(int(days), 180))

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    timeline = service.timeline_for(conn, start=start, end=end)
    summaries = reports.daily_summaries(conn, start, end, tz)

    return deps.render(
        request,
        "history.html",
        days=days,
        ranges=(1, 3, 7, 14, 30, 90),
        profile=timeline.profile,
        summaries=summaries,
        totals={
            "intake_ml": sum(day["total_ml"] for day in summaries),
            "sweat_ml": sum(day["sweat_ml"] for day in summaries),
            "voids": sum(day["voids"] for day in summaries),
            "sodium_gap_mg": sum(
                max(0.0, day["sodium_sweat_mg"] - day["sodium_in_mg"]) for day in summaries
            ),
        },
        deficit_chart=charts.deficit_chart(
            timeline, timeline.corrections, tz, body_mass_kg=timeline.profile.body_mass_kg
        ),
        intake_chart=charts.intake_by_beverage_chart(summaries, tz),
        sweat_chart=charts.sweat_vs_intake_chart(summaries, tz),
        urine_chart=charts.urine_chart(reports.void_points(conn, start, end, tz), tz, start, end),
        weight_chart=charts.weight_chart(reports.weight_points(conn, start, end, tz), tz),
        entries=reports.timeline_entries(conn, max(start, end - timedelta(days=3)), end),
    )


@router.get("/activities")
def activities(request: Request):
    conn = deps.connection()
    rows = reports.recent_activities(conn)
    profile = service.load_profile(conn)
    return deps.render(
        request,
        "activities.html",
        profile=profile,
        activities=[
            {
                "row": row,
                "post_target_ml": P.post_exercise_target_ml(row["sweat_ml_used"] or 0.0),
                "sodium_lost_mg": (row["sweat_ml_used"] or 0.0)
                / 1000.0
                * profile.sweat_sodium_mmol_l
                * 22.99,
            }
            for row in rows
        ],
        now_local=datetime.now(deps.profile_timezone(conn)).strftime("%Y-%m-%dT%H:%M"),
    )


@router.get("/insights")
def insights(request: Request):
    conn = deps.connection()
    tz = deps.profile_timezone(conn)
    profile = service.load_profile(conn)
    row = service.profile_row(conn)
    return deps.render(
        request,
        "insights.html",
        profile=profile,
        bias=reports.model_bias(conn),
        by_hour=reports.deficit_by_hour(conn, tz),
        calibration={
            "factor": row["sweat_calibration"],
            "count": row["sweat_calibration_n"],
            "baseline_scale": row["baseline_loss_scale"],
            "feedback_n": row["feedback_n"],
        },
    )
