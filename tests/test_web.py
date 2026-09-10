"""End-to-end tests for the web layer.

The two that matter most are the enumerating ones. They walk the application's
real route table rather than a list kept by hand, so a route added later
without a guard fails the build instead of quietly shipping. A test that
enumerates is only as good as its enumeration, so `test_the_route_walk_finds_the_routes`
fails if the walk stops seeing the application.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hydration import config, db, security
from hydration.web import deps


PASSWORD = "a-good-enough-password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "PASSWORD", None)
    monkeypatch.setattr(config, "SYNC_ENABLED", False)
    db.close()

    from hydration.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client
    db.close()


@pytest.fixture
def signed_in(client):
    conn = deps.connection()
    security.set_password(conn, PASSWORD)
    response = client.post("/login", data={"password": PASSWORD, "next": "/"})
    assert response.status_code == 303
    return client


@pytest.fixture
def token(client):
    return security.issue_token(deps.connection(), "test")


def _csrf(client) -> str:
    session_id = client.cookies.get(config.SESSION_COOKIE)
    return security.csrf_token_for(session_id)


def _routes(app):
    """The application's real route table, via the shared walk.

    Uses `deps.walk_routes` rather than reading `app.routes` directly, because
    reading it directly finds five objects -- FastAPI hides the real routes
    behind `_IncludedRouter.original_router`. See the guard test below.
    """
    return deps.walk_routes(app)


# -- the enumerating guards ------------------------------------------------

def test_the_route_walk_finds_the_routes(client):
    """Guards the two tests below. Both are vacuous if the walk returns
    nothing, and a passing vacuous test is worse than no test."""
    routes = _routes(client.app)
    assert len(routes) > 25, f"only found {len(routes)} routes; the walk is broken"
    assert any(path == "/api/v1/status" for path, _ in routes)
    assert any(path == "/settings" for path, _ in routes)


def test_every_page_is_public_by_declaration_or_refuses_anonymous(client):
    """No route may be reachable signed-out unless it is on PUBLIC_PATHS."""
    for path, methods in _routes(client.app):
        if path.startswith(deps.API_PREFIX) or "{" in path:
            continue
        method = "GET" if "GET" in methods else "POST"
        response = client.request(method, path)
        if deps.is_public(path):
            assert response.status_code != 303 or "/login" not in response.headers.get("location", ""), (
                f"{path} is declared public but redirects to login"
            )
        else:
            assert response.status_code in (303, 403), f"{method} {path} served an anonymous caller"
            if response.status_code == 303:
                assert response.headers["location"].startswith(("/login", "/setup"))


def test_every_api_route_refuses_a_bad_token(client):
    for path, methods in _routes(client.app):
        if not path.startswith(deps.API_PREFIX):
            continue
        method = "GET" if "GET" in methods else "POST"
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}):
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 401, f"{method} {path} accepted {headers}"


def test_a_valid_token_does_not_open_the_web_pages(client, token):
    """The two authentication schemes are separate. A sensor token is not a
    sign-in, and must not become one."""
    response = client.get("/settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 303


def test_a_session_does_not_open_the_api(signed_in):
    """And the reverse: a browser session is not a bearer token."""
    assert signed_in.post("/api/v1/intake", json={"volume_l": 0.5}).status_code == 401


# -- security headers ------------------------------------------------------

def test_every_response_carries_the_security_headers(client, signed_in):
    """Including the refusals -- an early return that skips `_finish` would
    send a 403 with no CSP at all."""
    for response in (
        client.get("/login"),
        client.get("/api/v1/status"),          # 401, no token
        signed_in.get("/"),
        signed_in.post("/log/drink", data={}),  # 403, no CSRF
    ):
        assert "script-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"


def test_the_pages_contain_no_script_at_all(signed_in):
    """The CSP forbids it, so a stray <script> would fail silently in the
    browser rather than loudly here."""
    for path in ("/", "/log", "/history", "/activities", "/insights", "/settings"):
        body = signed_in.get(path).text
        assert "<script" not in body.lower(), f"{path} contains a script tag"
        assert "onclick=" not in body.lower()
        assert 'style="' not in body.lower(), f"{path} has an inline style the CSP will drop"


# -- csrf ------------------------------------------------------------------

def test_a_post_without_a_csrf_token_is_refused(signed_in):
    response = signed_in.post("/log/drink", data={"beverage": "Water", "volume_l": "0.5"})
    assert response.status_code == 403


def test_a_post_with_the_csrf_token_is_accepted(signed_in):
    response = signed_in.post(
        "/log/drink",
        data={"beverage": "Water", "volume_l": "0.5", "csrf_token": _csrf(signed_in)},
    )
    assert response.status_code == 303
    assert deps.connection().execute("SELECT count(*) FROM intake").fetchone()[0] == 1


def test_csrf_middleware_does_not_eat_the_request_body(signed_in):
    """Starlette only replays a body it saw read through `body()`. Reading the
    form with `form()` in the middleware leaves the route seeing an empty form,
    and every field arrives as missing."""
    signed_in.post(
        "/log/drink",
        data={"beverage": "Water", "volume_l": "0.75", "csrf_token": _csrf(signed_in)},
    )
    volume = deps.connection().execute("SELECT volume_ml FROM intake").fetchone()[0]
    assert volume == 750.0, "the route did not receive the posted body"


# -- the home assistant api ------------------------------------------------

def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_hass_can_log_a_drink_in_litres(client, token):
    response = client.post("/api/v1/intake", json={"beverage": "Water", "volume_l": 0.5}, headers=_auth(token))
    assert response.status_code == 200 and response.json()["ok"]
    assert deps.connection().execute("SELECT volume_ml FROM intake").fetchone()[0] == 500.0


def test_hass_can_log_a_void_with_either_spelling(client, token):
    assert client.post("/api/v1/void", json={"colour": 5}, headers=_auth(token)).status_code == 200
    assert client.post("/api/v1/void", json={"color": 4}, headers=_auth(token)).status_code == 200
    assert deps.connection().execute("SELECT count(*) FROM void").fetchone()[0] == 2


def test_hass_sends_fahrenheit_and_pounds(client, token):
    """The units a person actually thinks in, converted at the edge."""
    client.post("/api/v1/env", json={"temp_f": 72.0, "humidity": 45}, headers=_auth(token))
    client.post("/api/v1/weight", json={"mass_lb": 176.4}, headers=_auth(token))
    conn = deps.connection()
    assert conn.execute("SELECT temp_c FROM environment").fetchone()[0] == pytest.approx(22.22, abs=0.01)
    assert conn.execute("SELECT mass_kg FROM body_weight").fetchone()[0] == pytest.approx(80.0, abs=0.02)


def test_a_form_encoded_post_works_too(client, token):
    """Home Assistant's rest_command sends form-encoded when given no content
    type, and failing on that would be baffling to debug from the YAML end."""
    response = client.post(
        "/api/v1/intake", data={"beverage": "Water", "volume_l": "0.33"}, headers=_auth(token)
    )
    assert response.status_code == 200


def test_a_bad_payload_returns_json_not_a_traceback(client, token):
    response = client.post("/api/v1/intake", json={"volume_l": "not a number"}, headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "error" in response.json()


def test_an_unknown_beverage_is_a_404_with_a_reason(client, token):
    response = client.post(
        "/api/v1/intake", json={"beverage": "unobtainium", "volume_l": 0.3}, headers=_auth(token)
    )
    assert response.status_code == 404
    assert "unobtainium" in response.json()["error"]


def test_the_status_endpoint_fits_a_hass_sensor(client, token):
    from hydration.web.routes_api import HASS_STATE_LIMIT

    payload = client.get("/api/v1/status", headers=_auth(token)).json()
    assert len(payload["headline"]) <= HASS_STATE_LIMIT, "a HASS entity state is capped at 255"
    for key in ("headline", "status", "deficit_l", "next_dose_l", "sodium_mg", "daily_target_l", "flags"):
        assert key in payload
    assert payload["status"] in {"ok", "drink", "drink_urgent", "add_sodium", "slow_down", "unknown"}


def test_the_status_endpoint_reflects_what_was_just_logged(client, token):
    before = client.get("/api/v1/status", headers=_auth(token)).json()["daily_intake_l"]
    client.post("/api/v1/intake", json={"beverage": "Water", "volume_l": 0.8}, headers=_auth(token))
    after = client.get("/api/v1/status", headers=_auth(token)).json()["daily_intake_l"]
    assert after == pytest.approx(before + 0.8, abs=0.01)


def test_a_revoked_token_stops_working(client, token):
    conn = deps.connection()
    assert client.get("/api/v1/status", headers=_auth(token)).status_code == 200
    token_id = conn.execute("SELECT id FROM api_token").fetchone()[0]
    security.revoke_token(conn, token_id)
    assert client.get("/api/v1/status", headers=_auth(token)).status_code == 401


# -- pages render ----------------------------------------------------------

def test_every_page_renders(signed_in):
    for path in ("/", "/log", "/history", "/history?days=30", "/activities", "/insights", "/settings"):
        response = signed_in.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert "<html" in response.text


def test_the_pages_render_with_real_data(signed_in, client, token):
    """Charts and aggregates are where an empty database hides bugs."""
    for colour in (3, 5, 7):
        client.post("/api/v1/void", json={"colour": colour}, headers=_auth(token))
    client.post("/api/v1/intake", json={"beverage": "Coffee", "volume_l": 0.3}, headers=_auth(token))
    client.post("/api/v1/intake", json={"beverage": "Water", "volume_l": 0.5}, headers=_auth(token))
    client.post("/api/v1/weight", json={"mass_lb": 170}, headers=_auth(token))
    client.post("/api/v1/env", json={"temp_f": 78, "humidity": 60}, headers=_auth(token))

    body = signed_in.get("/history?days=7").text
    assert "<svg" in body
    assert "Urine colour" in body

    today = signed_in.get("/").text
    assert "<svg" in today


# -- authentication flow ---------------------------------------------------

def test_first_run_asks_for_a_password(client):
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/setup")


def test_setup_cannot_be_used_to_reset_an_existing_password(client):
    """Otherwise anyone reaching the app could take it over -- the whole of the
    authentication defeated by one unguarded route."""
    security.set_password(deps.connection(), PASSWORD)
    response = client.post("/setup", data={"password": "attacker-chosen", "confirm": "attacker-chosen"})
    assert response.status_code == 303
    assert security.check_password(deps.connection(), PASSWORD), "the original password must stand"


def test_a_wrong_password_does_not_sign_you_in(client):
    security.set_password(deps.connection(), PASSWORD)
    client.post("/login", data={"password": "wrong", "next": "/"})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_signing_out_ends_the_session(signed_in):
    signed_in.post("/logout", data={"csrf_token": _csrf(signed_in)})
    assert signed_in.get("/", follow_redirects=False).status_code == 303


def test_an_open_redirect_is_refused(client):
    """`?next=https://evil/` on a login link is a phishing page wearing your
    own domain."""
    security.set_password(deps.connection(), PASSWORD)
    response = client.post("/login", data={"password": PASSWORD, "next": "https://evil.example/"})
    assert response.headers["location"] == "/"


def test_healthz_needs_no_session_and_touches_the_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


def test_the_status_endpoint_never_reports_negative_zero(client, token):
    """Python rounds -0.001 to -0.0, which serialises as "-0.0" and reads to a
    person on a dashboard as a broken number."""
    payload = client.get("/api/v1/status", headers=_auth(token)).json()
    for key in ("deficit_l", "deficit_pct", "next_dose_l", "daily_intake_l"):
        assert str(payload[key]) != "-0.0", f"{key} came back as negative zero"


# -- the end-of-day question ----------------------------------------------

def test_the_feel_buttons_record_and_refit(signed_in):
    response = signed_in.post(
        "/log/feel", data={"verdict": "a_bit_dry", "csrf_token": _csrf(signed_in)}
    )
    assert response.status_code == 303
    assert deps.connection().execute("SELECT count(*) FROM feedback").fetchone()[0] == 1


def test_hass_can_send_how_the_day_felt(client, token):
    response = client.post("/api/v1/feel", json={"verdict": "about_right"}, headers=_auth(token))
    assert response.status_code == 200 and response.json()["ok"]


def test_a_nonsense_verdict_over_the_api_is_a_400(client, token):
    response = client.post("/api/v1/feel", json={"verdict": "splendid"}, headers=_auth(token))
    assert response.status_code == 400


def test_the_status_endpoint_reports_its_own_confidence(client, token):
    """So a dashboard can show a guess differently from a checked figure."""
    payload = client.get("/api/v1/status", headers=_auth(token)).json()
    assert payload["confidence"] in {"good", "fair", "stale"}
    assert payload["confidence_reason"]


def test_an_empty_database_says_it_does_not_know(client, token):
    """Rather than asserting a deficit from nothing, which is how this used to
    manufacture alarming figures."""
    payload = client.get("/api/v1/status", headers=_auth(token)).json()
    assert payload["confidence"] == "stale"
    assert payload["status"] == "unknown"
    assert payload["flags"] == []


# -- export and import over the web ---------------------------------------

def test_the_json_export_downloads(signed_in, client, token):
    client.post("/api/v1/intake", json={"beverage": "Water", "volume_l": 0.4}, headers=_auth(token))
    response = signed_in.get("/export/hydration.json")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["format"] == "hydration-tracker-export"


def test_the_csv_export_downloads(signed_in, client, token):
    client.post("/api/v1/intake", json={"beverage": "Water", "volume_l": 0.4}, headers=_auth(token))
    response = signed_in.get("/export/entries.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "at,kind,what" in response.text


def test_exports_are_not_public(client):
    """They are the entire health log."""
    for path in ("/export/hydration.json", "/export/entries.csv"):
        assert client.get(path).status_code == 303


def test_a_file_can_be_imported_back(signed_in, client, token):
    import json

    client.post("/api/v1/intake", json={"beverage": "Coffee", "volume_l": 0.3}, headers=_auth(token))
    exported = signed_in.get("/export/hydration.json").text

    conn = deps.connection()
    conn.execute("DELETE FROM intake")
    assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 0

    response = signed_in.post(
        "/import",
        data={"csrf_token": _csrf(signed_in)},
        files={"file": ("hydration.json", exported, "application/json")},
    )
    assert response.status_code == 303
    assert conn.execute("SELECT count(*) FROM intake").fetchone()[0] == 1


def test_importing_a_junk_file_is_refused_without_a_traceback(signed_in):
    response = signed_in.post(
        "/import",
        data={"csrf_token": _csrf(signed_in)},
        files={"file": ("nope.json", b"{not json", "application/json")},
    )
    assert response.status_code in (303, 400)
    assert response.status_code != 500
