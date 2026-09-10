"""Settings, sign-in, and the Garmin/Home Assistant plumbing pages."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import config, db, security, service, units
from ..errors import ValidationError
from . import deps

router = APIRouter()


# -- authentication --------------------------------------------------------

@router.get("/setup")
def setup_page(request: Request):
    """First run. Offered only while no password exists."""
    if security.password_is_set(deps.connection()):
        return deps.redirect("/login")
    return deps.render(request, "setup.html")


@router.post("/setup")
def setup(request: Request, password: str = Form(...), confirm: str = Form(...)):
    conn = deps.connection()
    if security.password_is_set(conn):
        # Otherwise this route would let anyone reset the password of a running
        # installation, which is the whole of the authentication defeated.
        return deps.redirect("/login", "A password is already set.", "error")
    if password != confirm:
        return deps.redirect("/setup", "Those did not match.", "error")
    security.set_password(conn, password)
    return _sign_in(conn, request, "/")


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    conn = deps.connection()
    if not security.password_is_set(conn):
        return deps.redirect("/setup")
    if deps.is_signed_in(request):
        return deps.redirect(deps.safe_path(next))
    return deps.render(request, "login.html", next=deps.safe_path(next))


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    conn = deps.connection()
    target = deps.safe_path(next)

    # Checked before the password, not after: an attempt that is refused for
    # being too soon must not also tell you whether the guess was right.
    waiting = security.login_lock_remaining_s(conn)
    if waiting > 0:
        return deps.redirect(
            f"/login?next={target}",
            f"Too many attempts. Try again in {math.ceil(waiting)} seconds.",
            "error",
        )

    if not security.check_password(conn, password):
        locked_for = security.record_failed_login(conn)
        message = "That is not the password."
        if locked_for:
            message += f" Locked for {math.ceil(locked_for)} seconds."
        return deps.redirect(f"/login?next={target}", message, "error")

    security.clear_login_failures(conn)
    return _sign_in(conn, request, target)


def _sign_in(conn, request: Request, target: str) -> RedirectResponse:
    session_id = security.create_session(conn, request.headers.get("user-agent"))
    response = deps.redirect(target, "Signed in.", "good")
    response.set_cookie(
        config.SESSION_COOKIE,
        session_id,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        # Only when something in front is terminating TLS. Setting it on plain
        # HTTP over the tailnet would mean the browser drops the cookie and
        # sign-in silently never sticks.
        secure=config.BEHIND_PROXY,
    )
    return response


@router.post("/logout")
def logout(request: Request):
    security.destroy_session(deps.connection(), request.cookies.get(config.SESSION_COOKIE))
    response = deps.redirect("/login", "Signed out.")
    response.delete_cookie(config.SESSION_COOKIE)
    return response


# -- settings --------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request):
    conn = deps.connection()
    row = service.profile_row(conn)
    from ..maintenance import backup_status
    from ..sync import sync_status

    return deps.render(
        request,
        "settings.html",
        row=row,
        mass_lb=units.kg_to_lb(row["body_mass_kg"]),
        beverages=service.list_beverages(conn, include_archived=True),
        tokens=security.list_tokens(conn),
        caffeine_options=_caffeine_options(row["caffeine_diuresis_ml_mg"]),
        new_token=request.query_params.get("token"),
        sync=sync_status(conn),
        backups=backup_status(conn),
        garmin_configured=bool(config.GARMIN_EMAIL and config.GARMIN_PASSWORD),
        saltiness=_saltiness_options(row["sweat_sodium_mmol_l"]),
    )


SALTINESS = (
    (20.0, "Barely", "No salt crust, no white marks on dark kit, no cramping."),
    (40.0, "Average", "Occasional white marks after a long hot session."),
    (60.0, "Salty", "Salt visible on skin or clothing after most hard sessions."),
    (80.0, "Very salty", "Gritty salt on the face, white-stained kit, cramps late in long efforts."),
)
"""The profile asks in plain observable terms rather than millimoles per litre,
because a number nobody can estimate produces a default nobody changes -- and
this is the single most useful thing to get right for electrolyte advice."""


CAFFEINE_SENSITIVITY = (
    (0.0, "No noticeable effect", "The usual case, and the default. Habitual drinkers show no "
                                 "meaningful net loss, and coffee hydrates about as well as water."),
    (0.5, "Noticeable", "A third coffee costs you more fluid than the volume you drank "
                        "explains."),
    (1.0, "Strong", "Caffeine is clearly a diuretic for you, and a heavy morning leaves you dry "
                    "by lunchtime."),
)
"""Asked in observable terms, like the sweat saltiness above. 'Millilitres of
urine per milligram of caffeine' is a number nobody can estimate about
themselves, and a number nobody can estimate is a default nobody changes."""


def _caffeine_options(current: float) -> list[dict]:
    closest = min(CAFFEINE_SENSITIVITY, key=lambda entry: abs(entry[0] - current))[0]
    return [
        {"value": value, "label": label, "hint": hint, "selected": value == closest}
        for value, label, hint in CAFFEINE_SENSITIVITY
    ]


def _saltiness_options(current: float) -> list[dict]:
    closest = min(SALTINESS, key=lambda entry: abs(entry[0] - current))[0]
    return [
        {"value": value, "label": label, "hint": hint, "selected": value == closest}
        for value, label, hint in SALTINESS
    ]


@router.post("/settings/profile")
def save_profile(
    request: Request,
    display_name: str = Form("me"),
    mass_lb: str = Form(...),
    height_cm: str = Form(...),
    sex: str = Form("male"),
    birth_year: int = Form(...),
    timezone_name: str = Form(...),
    wake_hour: float = Form(7.0),
    bed_hour: float = Form(23.0),
    sweat_sodium_mmol_l: float = Form(40.0),
    absorption_cap_ml_h: float = Form(800.0),
    trust_urine: float = Form(0.6),
    food_water_ml_day: float = Form(700.0),
    caffeine_diuresis_ml_mg: float = Form(0.0),
    default_temp_f: str = Form("70"),
    default_humidity_pct: float = Form(45.0),
    volume_entry_unit: str = Form("l"),
):
    pounds = units.parse_optional_float(mass_lb)
    if pounds is None:
        raise ValidationError("body weight is needed -- the whole model is scaled by it")
    if volume_entry_unit not in units.VOLUME_UNITS:
        raise ValidationError(f"{volume_entry_unit!r} is not a fluid unit this app knows")
    service.save_profile(
        deps.connection(),
        display_name=display_name.strip() or "me",
        body_mass_kg=units.lb_to_kg(pounds),
        height_cm=float(units.parse_optional_float(height_cm) or 178.0),
        sex=sex,
        birth_year=int(birth_year),
        timezone=timezone_name.strip(),
        wake_hour=wake_hour,
        bed_hour=bed_hour,
        sweat_sodium_mmol_l=sweat_sodium_mmol_l,
        absorption_cap_ml_h=absorption_cap_ml_h,
        trust_urine=trust_urine,
        food_water_ml_day=food_water_ml_day,
        caffeine_diuresis_ml_mg=caffeine_diuresis_ml_mg,
        default_temp_c=units.f_to_c(units.parse_optional_float(default_temp_f) or 70.0),
        default_humidity_pct=default_humidity_pct,
        volume_entry_unit=volume_entry_unit,
    )
    return deps.redirect("/settings", "Profile saved.", "good")


@router.post("/settings/password")
def change_password(request: Request, current: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    conn = deps.connection()
    if not security.check_password(conn, current):
        return deps.redirect("/settings", "The current password is wrong.", "error")
    if password != confirm:
        return deps.redirect("/settings", "Those did not match.", "error")
    security.set_password(conn, password)
    return deps.redirect("/settings", "Password changed.", "good")


# -- beverages -------------------------------------------------------------

@router.post("/settings/beverage")
def save_beverage(
    request: Request,
    beverage_id: str = Form(""),
    name: str = Form(...),
    hydration_index: float = Form(1.0),
    sodium_mg_per_l: float = Form(0.0),
    potassium_mg_per_l: float = Form(0.0),
    caffeine_mg_per_l: float = Form(0.0),
    alcohol_pct: float = Form(0.0),
    kcal_per_l: float = Form(0.0),
):
    conn = deps.connection()
    if hydration_index <= 0:
        raise ValidationError("hydration index has to be greater than zero")
    values = (name.strip(), hydration_index, sodium_mg_per_l, potassium_mg_per_l,
              caffeine_mg_per_l, alcohol_pct, kcal_per_l)
    with db.transaction(conn):
        if beverage_id:
            conn.execute(
                """
                UPDATE beverage SET name = ?, hydration_index = ?, sodium_mg_per_l = ?,
                       potassium_mg_per_l = ?, caffeine_mg_per_l = ?, alcohol_pct = ?, kcal_per_l = ?
                 WHERE id = ?
                """,
                (*values, int(beverage_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO beverage (name, hydration_index, sodium_mg_per_l, potassium_mg_per_l,
                                      caffeine_mg_per_l, alcohol_pct, kcal_per_l)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
    return deps.redirect("/settings", f"Saved {name}.", "good")


@router.post("/settings/beverage/archive")
def archive_beverage(request: Request, beverage_id: int = Form(...)):
    """Archive rather than delete -- past drinks still point at it."""
    conn = deps.connection()
    with db.transaction(conn):
        conn.execute(
            "UPDATE beverage SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (db.utcnow(), beverage_id),
        )
    return deps.redirect("/settings", "Archived. Existing entries keep it.", "good")


# -- api tokens ------------------------------------------------------------

@router.post("/settings/token")
def create_token(request: Request, label: str = Form("Home Assistant")):
    token = security.issue_token(deps.connection(), label)
    # Handed back once, in the query string of a redirect this app serves to
    # itself over the tailnet. It is never stored in clear.
    return deps.redirect(f"/settings?token={token}", "Token created -- copy it now.", "good")


@router.post("/settings/token/revoke")
def revoke_token(request: Request, token_id: int = Form(...)):
    security.revoke_token(deps.connection(), token_id)
    return deps.redirect("/settings", "Token revoked.", "good")


# -- garmin ----------------------------------------------------------------

@router.post("/settings/sync")
def sync_now(request: Request):
    from ..sync import run_sync_once

    result = run_sync_once(deps.connection())
    kind = "good" if result.get("ok") else "error"
    return deps.redirect("/settings", result.get("message", "Sync finished."), kind)


@router.post("/settings/backup")
def backup_now(request: Request):
    """Take one on demand, alongside the scheduled ones.

    Restoring is deliberately not offered here. It replaces the whole database,
    which is not a thing to put one click away from a page you visit to change
    your bedtime -- `hydration restore` asks for confirmation and takes a copy
    of what it is about to overwrite.
    """
    from ..maintenance import run_backup_once

    result = run_backup_once(deps.connection())
    if not result.get("ok"):
        return deps.redirect("/settings", f"Backup failed: {result.get('message')}", "error")
    pruned = result.get("pruned") or 0
    message = f"Backed up to {result['path']}."
    if pruned:
        message += f" Pruned {pruned} older."
    return deps.redirect("/settings", message, "good")
