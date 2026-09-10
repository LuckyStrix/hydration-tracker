"""Passwords, API tokens, sessions and CSRF.

Single user, on a tailnet. That justifies keeping this small, but not keeping
it sloppy: the app holds a health log, and "it is only on my network" stops
being true the first time something else on that network is compromised.

No new dependencies. `hashlib.scrypt` is in the standard library and is a
proper memory-hard KDF, so there is no reason to pull in bcrypt for it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config, db

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16

PASSWORD_HASH_KEY = "auth.password_hash"


# -- passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    """scrypt with a random salt, stored as `scrypt$n$r$p$salt$hash`."""
    salt = os.urandom(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False
    # Constant-time, so a wrong password cannot be narrowed down by timing.
    return hmac.compare_digest(derived.hex(), hash_hex)


def password_is_set(connection: sqlite3.Connection) -> bool:
    return db.get_setting(connection, PASSWORD_HASH_KEY) is not None


def set_password(connection: sqlite3.Connection, password: str) -> None:
    if len(password) < 8:
        from .errors import ValidationError

        raise ValidationError("choose at least eight characters")
    with db.transaction(connection):
        db.set_setting(connection, PASSWORD_HASH_KEY, hash_password(password))


def check_password(connection: sqlite3.Connection, password: str) -> bool:
    return verify_password(password, db.get_setting(connection, PASSWORD_HASH_KEY))


def bootstrap_password(connection: sqlite3.Connection) -> None:
    """Adopt HYDRATION_PASSWORD on first start, if one was provided."""
    if config.PASSWORD and not password_is_set(connection):
        set_password(connection, config.PASSWORD)


# -- sessions --------------------------------------------------------------

def create_session(connection: sqlite3.Connection, user_agent: str | None = None) -> str:
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with db.transaction(connection):
        connection.execute(
            """
            INSERT INTO session (id, created_at, last_seen_at, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                db.to_iso(now),
                db.to_iso(now),
                db.to_iso(now + timedelta(days=config.SESSION_DAYS)),
                (user_agent or "")[:200],
            ),
        )
    return session_id


def validate_session(connection: sqlite3.Connection, session_id: str | None) -> bool:
    """Check a session and slide its expiry forward.

    The heartbeat is deliberately not wrapped in a transaction of its own per
    request -- it is a single statement, and a page view is not a change worth
    serialising against the sync thread.
    """
    if not session_id:
        return False
    now = db.utcnow()
    row = connection.execute(
        "SELECT expires_at FROM session WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row["expires_at"] <= now:
        return False
    connection.execute(
        "UPDATE session SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (now, db.to_iso(datetime.now(timezone.utc) + timedelta(days=config.SESSION_DAYS)), session_id),
    )
    return True


def destroy_session(connection: sqlite3.Connection, session_id: str | None) -> None:
    if not session_id:
        return
    with db.transaction(connection):
        connection.execute("DELETE FROM session WHERE id = ?", (session_id,))


def purge_expired_sessions(connection: sqlite3.Connection) -> int:
    with db.transaction(connection):
        cursor = connection.execute("DELETE FROM session WHERE expires_at <= ?", (db.utcnow(),))
    return cursor.rowcount


# -- api tokens ------------------------------------------------------------

def _token_hash(token: str) -> str:
    """SHA-256, not scrypt.

    Deliberate: these are 256-bit random strings, not passwords. There is no
    dictionary to attack, so the only thing a slow KDF would buy is a slow
    request on every sensor push from Home Assistant.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(connection: sqlite3.Connection, label: str) -> str:
    """Mint a bearer token. Returned once, in clear, and never again."""
    token = secrets.token_urlsafe(32)
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO api_token (label, token_hash, created_at) VALUES (?, ?, ?)",
            (label.strip() or "unnamed", _token_hash(token), db.utcnow()),
        )
    return token


def check_token(connection: sqlite3.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = connection.execute(
        "SELECT id, revoked_at FROM api_token WHERE token_hash = ?", (_token_hash(token),)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return False
    connection.execute("UPDATE api_token SET last_used_at = ? WHERE id = ?", (db.utcnow(), row["id"]))
    return True


def revoke_token(connection: sqlite3.Connection, token_id: int) -> None:
    with db.transaction(connection):
        connection.execute(
            "UPDATE api_token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (db.utcnow(), token_id),
        )


def list_tokens(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM api_token WHERE revoked_at IS NULL ORDER BY created_at DESC"
    ).fetchall()


# -- csrf ------------------------------------------------------------------

def csrf_token_for(session_id: str) -> str:
    """Derived from the session rather than stored.

    A token tied to the session needs no table and no cleanup, and it cannot
    be replayed into a different session.
    """
    return hmac.new(_csrf_key(), session_id.encode(), hashlib.sha256).hexdigest()


def csrf_valid(session_id: str | None, token: str | None) -> bool:
    if not session_id or not token:
        return False
    return hmac.compare_digest(csrf_token_for(session_id), token)


_CSRF_KEY_SETTING = "auth.csrf_key"
_csrf_key_cache: bytes | None = None


def _csrf_key() -> bytes:
    global _csrf_key_cache
    if _csrf_key_cache is None:
        raise RuntimeError("CSRF key not loaded; call load_csrf_key() at startup")
    return _csrf_key_cache


def load_csrf_key(connection: sqlite3.Connection) -> None:
    """Load or mint the per-installation CSRF key.

    Persisted rather than generated per process, so restarting the container
    does not invalidate every open form.
    """
    global _csrf_key_cache
    stored = db.get_setting(connection, _CSRF_KEY_SETTING)
    if stored is None:
        stored = secrets.token_hex(32)
        with db.transaction(connection):
            db.set_setting(connection, _CSRF_KEY_SETTING, stored)
    _csrf_key_cache = bytes.fromhex(stored)
