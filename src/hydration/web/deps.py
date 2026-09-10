"""Shared request plumbing: templates, filters, auth gates, flash messages.

The display-edge conversions live here as Jinja filters. This is the boundary
the rest of the application is written to respect -- storage is metric and UTC,
and the only place either becomes litres, pounds, Fahrenheit or local time is
in a template calling one of these.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from .. import config, db, security, units

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

PUBLIC_PATHS = {"/login", "/setup", "/healthz", "/static", "/favicon.ico"}
"""Paths reachable without a session. The auth test walks the real route table
and asserts everything else refuses an anonymous caller, so adding a route here
is a deliberate act rather than an oversight."""

API_PREFIX = "/api/"
"""API routes authenticate with a bearer token instead of a session cookie.
They are not public -- they are guarded differently."""

FLASH_COOKIE = "hydration_flash"


# -- connections -----------------------------------------------------------

def connection() -> sqlite3.Connection:
    return db.get(config.DB_PATH)


def profile_timezone(conn: sqlite3.Connection) -> ZoneInfo:
    row = conn.execute("SELECT timezone FROM profile WHERE id = 1").fetchone()
    try:
        return ZoneInfo(row["timezone"] if row else config.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


# -- templates -------------------------------------------------------------

def _make_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        litres=units.format_l,
        litres_number=lambda ml: f"{(ml or 0) / 1000:.2f}",
        pounds=units.format_lb,
        fahrenheit=units.format_f,
        signed_litres=lambda ml: f"{'+' if (ml or 0) >= 0 else '-'}{abs(ml or 0) / 1000:.2f} L",
        pct=lambda value, places=1: f"{value:.{places}f}%",
        signed_pct=lambda value, places=1: f"{value:+.{places}f}%",
        clock=_clock,
        day=_day,
        stamp=_stamp,
        ago=_ago,
        duration=_duration,
    )
    return env


_TZ: ZoneInfo = ZoneInfo("UTC")


def set_display_timezone(tz: ZoneInfo) -> None:
    """Bind the timezone the filters render in.

    Set per request from the profile. A module-level binding is safe here
    because there is exactly one user -- and if that ever stops being true,
    this is the thing that has to change first.
    """
    global _TZ
    _TZ = tz


def _local(moment: datetime | str | None) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, str):
        moment = db.from_iso(moment)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ)


def _clock(moment) -> str:
    local = _local(moment)
    return local.strftime("%-I:%M %p") if local else "--"


def _day(moment) -> str:
    local = _local(moment)
    return local.strftime("%a %-d %b") if local else "--"


def _stamp(moment) -> str:
    local = _local(moment)
    return local.strftime("%a %-d %b, %-I:%M %p") if local else "--"


def _ago(moment) -> str:
    local = _local(moment)
    if local is None:
        return "never"
    seconds = (datetime.now(timezone.utc) - local.astimezone(timezone.utc)).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} h ago"
    return f"{hours / 24:.0f} days ago"


def _duration(seconds: float | None) -> str:
    if not seconds:
        return "--"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"


templates = _make_environment()


def render(request: Request, name: str, **context) -> HTMLResponse:
    conn = connection()
    set_display_timezone(profile_timezone(conn))
    session_id = request.cookies.get(config.SESSION_COOKIE)

    body = templates.get_template(name).render(
        request=request,
        csrf_token=security.csrf_token_for(session_id) if session_id else "",
        flash=_read_flash(request),
        nav=name,
        **context,
    )
    response = HTMLResponse(body)
    if request.cookies.get(FLASH_COOKIE):
        response.delete_cookie(FLASH_COOKIE)
    return response


# -- flash messages --------------------------------------------------------

def _read_flash(request: Request) -> dict | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    kind, _, message = raw.partition("|")
    return {"kind": kind or "info", "message": message}


def flash(response, message: str, kind: str = "info") -> None:
    response.set_cookie(
        FLASH_COOKIE,
        f"{kind}|{message}",
        max_age=30,
        httponly=True,
        samesite="lax",
        secure=config.BEHIND_PROXY,
    )


def redirect(to: str, message: str | None = None, kind: str = "info") -> RedirectResponse:
    response = RedirectResponse(to, status_code=303)
    if message:
        flash(response, message, kind)
    return response


# -- auth ------------------------------------------------------------------

def is_signed_in(request: Request) -> bool:
    return security.validate_session(connection(), request.cookies.get(config.SESSION_COOKIE))


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    # Home Assistant's rest_command can send a token in the query string more
    # easily than a header in some setups; accepted, but the header is better
    # because a URL ends up in logs.
    return request.query_params.get("token")


def is_public(path: str) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in PUBLIC_PATHS)


def walk_routes(app) -> list[tuple[str, set[str]]]:
    """Every path the application actually serves, with its methods.

    Not as simple as reading `app.routes`. FastAPI wraps each `include_router`
    in a `_IncludedRouter` that has **no `.path` and no `.routes`** -- the real
    routes hang off `.original_router`. Reading `.routes` with a `[]` default
    therefore walks straight past every routed path and leaves a caller
    believing the application has five routes, four of which are `/openapi.json`
    and a static mount.

    That matters because the authorisation tests enumerate this. A walk that
    silently returns nothing makes them pass vacuously, which is worse than
    having no tests at all -- so `test_the_route_walk_finds_the_routes` asserts
    the walk still sees the application.
    """
    found: list[tuple[str, set[str]]] = []
    seen: set[int] = set()

    def visit(node) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))

        inner = getattr(node, "original_router", None)
        if inner is not None:
            visit(inner)
            return

        for route in getattr(node, "routes", []):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                found.append((path, set(methods)))
            elif hasattr(route, "original_router") or hasattr(route, "routes"):
                # A nested router or a mount. Static mounts have routes of
                # their own that are not application endpoints, so they are
                # skipped by name rather than walked.
                if getattr(route, "name", None) != "static":
                    visit(route)

    visit(app)
    return found


def safe_path(candidate: str | None, fallback: str = "/") -> str:
    """Refuse a caller-supplied redirect that leaves this site.

    Skipping this is how an open redirect gets in: `?next=https://evil/` on a
    login link is a phishing page wearing your own domain.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    return candidate
