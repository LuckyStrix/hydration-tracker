"""Application assembly: middleware, routing, startup.

There is one middleware rather than four, deliberately. Every response that
leaves this application -- including the early refusals for an oversized body,
a missing CSRF token or an anonymous caller -- has to carry the security
headers, and the simplest way to guarantee that is for there to be exactly one
exit through `_finish()`. Returning a response directly from anywhere in here
would skip them, silently.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db, security
from ..errors import ConflictError, HydrationError, NotFound, ValidationError
from . import deps, routes_api, routes_settings, routes_ui

log = logging.getLogger("hydration")

STATIC_DIR = deps.TEMPLATE_DIR.parent / "static"

CSP = "; ".join(
    (
        "default-src 'self'",
        # No JavaScript anywhere in this application. The charts are
        # server-rendered SVG and every control is a form, so there is nothing
        # to allow -- and an XSS with no script sink is a much smaller problem.
        "script-src 'none'",
        "style-src 'self'",
        "img-src 'self' data:",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = db.get(config.DB_PATH)
    db.init(connection)
    security.load_csrf_key(connection)
    security.bootstrap_password(connection)
    security.purge_expired_sessions(connection)
    log.info("hydration tracker ready, database at %s", config.DB_PATH)

    from ..maintenance import start_maintenance, stop_maintenance
    from ..sync import start_sync, stop_sync

    start_sync()
    start_maintenance()
    try:
        yield
    finally:
        stop_sync()
        stop_maintenance()
        db.close()


def create_app() -> FastAPI:
    # No docs and no schema endpoint: nothing consumes them, and an unauthenticated
    # description of every route is a gift to nobody useful.
    app = FastAPI(
        title="Hydration", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(routes_api.router)
    app.include_router(routes_ui.router)
    app.include_router(routes_settings.router)
    routes_api.install_error_handlers(app)
    _install_page_error_handlers(app)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        limit = _body_limit(path)

        if request.method not in SAFE_METHODS:
            declared = request.headers.get("content-length")
            if declared:
                try:
                    too_big = int(declared) > limit
                except ValueError:
                    too_big = True  # a header we cannot read is not one we trust
                if too_big:
                    return _finish(_too_large(limit))

        is_api = path.startswith(deps.API_PREFIX)

        if is_api:
            if not security.check_token(deps.connection(), deps.bearer_token(request)):
                return _finish(
                    JSONResponse({"ok": False, "error": "bad or missing bearer token"}, status_code=401)
                )
        elif not deps.is_public(path):
            if not deps.is_signed_in(request):
                target = "/setup" if not security.password_is_set(deps.connection()) else "/login"
                nxt = deps.safe_path(str(request.url.path))
                return _finish(RedirectResponse(f"{target}?next={nxt}", status_code=303))

        # The header check above is a courtesy to a well-behaved client. A
        # chunked request carries no Content-Length at all, so the only figure
        # worth enforcing is the one that actually arrived -- and after the
        # auth gate, so an anonymous caller cannot make us hold a body at all.
        #
        # Read it with `body()`, never `form()`: Starlette only replays a body
        # it saw read through `body()`, and `form()` here leaves every
        # downstream route seeing an empty form.
        if request.method not in SAFE_METHODS:
            body = await request.body()
            if len(body) > limit:
                return _finish(_too_large(limit, is_api=is_api))

        # CSRF for browser writes.
        if request.method not in SAFE_METHODS and not is_api:
            form = await request.form()
            session_id = request.cookies.get(config.SESSION_COOKIE)
            supplied = form.get("csrf_token")
            if not deps.is_public(path) and not security.csrf_valid(session_id, supplied):
                return _finish(PlainTextResponse("CSRF check failed", status_code=403))

        response = await call_next(request)
        return _finish(response)

    return app


def _body_limit(path: str) -> int:
    """How large a body this path may carry.

    `/import` is the one route whose body is legitimately a whole health log,
    and it has its own ceiling. Everything else is a form.
    """
    return config.IMPORT_MAX_BODY_BYTES if path == "/import" else config.MAX_BODY_BYTES


def _too_large(limit: int, *, is_api: bool = False):
    """Say what the limit was. A bare "too large" sends you looking in the
    wrong place -- which is exactly what it did when an export outgrew the
    import."""
    message = f"Request too large; the limit for this path is {limit // 1024} kB"
    if is_api:
        return JSONResponse({"ok": False, "error": message}, status_code=413)
    return PlainTextResponse(message, status_code=413)


def _finish(response):
    """The single exit. Everything leaving the app passes through here."""
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if config.BEHIND_PROXY:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


def _install_page_error_handlers(app: FastAPI) -> None:
    """Render the application's own errors as pages, not tracebacks.

    A bad number in a form is a message above the field, not a 500. Anything
    that is not one of these three is a real bug and should surface as one.
    """

    async def _page(request: Request, exc: HydrationError, status: int):
        if request.url.path.startswith(deps.API_PREFIX):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)
        referer = deps.safe_path(request.headers.get("referer", "").split(request.base_url.netloc)[-1] or "/")
        return deps.redirect(referer, str(exc), kind="error")

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return _finish(await _page(request, exc, 400))

    @app.exception_handler(NotFound)
    async def _missing(request: Request, exc: NotFound):
        return _finish(await _page(request, exc, 404))

    @app.exception_handler(ConflictError)
    async def _conflict(request: Request, exc: ConflictError):
        return _finish(await _page(request, exc, 409))


app = create_app()
