"""Command line: the things that cannot be done from a browser.

Chiefly the first Garmin login. A multi-factor prompt needs a person, and the
sync thread has nobody to ask -- so it happens here, once, and the tokens it
writes to the data volume refresh themselves from then on.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, security, service


def _connection():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = db.get(config.DB_PATH)
    db.init(connection)
    security.load_csrf_key(connection)
    return connection


def cmd_garmin_login(_args) -> int:
    """Log in once, interactively, and cache the tokens.

    This is the only step in the whole system that needs a human, and only the
    first time -- Garmin's multi-factor code cannot be answered by a background
    thread.
    """
    from .providers import garmin

    if not (config.GARMIN_EMAIL and config.GARMIN_PASSWORD):
        print("GARMIN_EMAIL and GARMIN_PASSWORD are not set in the environment.", file=sys.stderr)
        return 1
    try:
        garmin.connect(interactive=True)
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1
    print(f"Logged in. Tokens cached in {config.GARMIN_TOKEN_DIR}.")
    print("The background sync will use them from now on; this should not need running again.")
    return 0


def cmd_sync(_args) -> int:
    from .sync import run_sync_once

    result = run_sync_once(_connection(), interactive=True)
    print(result["message"])
    return 0 if result.get("ok") else 1


def cmd_backup(args) -> int:
    """Write a consistent copy with VACUUM INTO.

    The live database deliberately lives in a Docker volume rather than in a
    bind-mounted host directory. This is how a consistent copy reaches the host
    anyway -- see `db.backup` for why copying the file directly is not the same
    thing.
    """
    connection = _connection()
    directory = args.into or config.BACKUP_DIR
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = db.backup(connection, directory / f"hydration-{stamp}.db")
    print(f"Wrote {written} ({written.stat().st_size / 1024:.0f} kB)")
    if args.keep:
        for removed in db.prune_backups(directory, args.keep):
            print(f"Pruned {removed.name}")
    return 0


def cmd_restore(args) -> int:
    """Put a backup back.

    Destructive, and the one command here that is, so it says exactly what it
    is about to overwrite and takes a copy of the current database first --
    restoring the wrong file should cost a minute, not a history.
    """
    connection = _connection()
    source = args.backup

    # Check the file before anything destructive happens, so a bad one costs
    # nothing at all rather than costing a pointless safety copy first.
    try:
        counts = db.inspect_backup(source)
    except (FileNotFoundError, db.NotABackup) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        print(f"Nothing was changed. Your database is still at {config.DB_PATH}.", file=sys.stderr)
        return 1

    if not args.force:
        current = connection.execute("SELECT count(*) FROM intake").fetchone()[0]
        print(f"This replaces the database at {config.DB_PATH} ({current} drinks logged)")
        print(f"with {source}, which holds {counts['intake']} drinks.")
        if input("Type 'restore' to go ahead: ").strip() != "restore":
            print("Nothing done.")
            return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety = db.backup(
        connection, db.unused_backup_path(config.BACKUP_DIR, f"hydration-{stamp}-before-restore")
    )
    print(f"Current database saved to {safety} first.")

    db.restore(connection, source)

    # The restored file may predate the current schema, so bring it up to date
    # exactly as a container start would.
    db.init(connection)
    print("Restored: " + ", ".join(f"{count} {table}" for table, count in counts.items()))
    print("Restart the application if it is running, so nothing is holding stale state.")
    return 0


def cmd_set_password(_args) -> int:
    connection = _connection()
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Again: "):
        print("Those did not match.", file=sys.stderr)
        return 1
    security.set_password(connection, password)
    print("Password set.")
    return 0


def cmd_status(_args) -> int:
    connection = _connection()
    timeline, plan = service.current_state(connection)
    print(f"Deficit    {plan.deficit_ml / 1000:+.2f} L ({plan.deficit_pct:+.2f}% of body mass)")
    print(f"Status     {plan.status}")
    print(f"Plan       {plan.headline}")
    if plan.detail:
        for line in plan.detail:
            print(f"           - {line}")
    for flag in plan.medical_flags:
        print(f"  FLAG     {flag}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "hydration.web.app:app",
        host=args.host or config.HOST,
        port=args.port or config.PORT,
        log_level="info",
        proxy_headers=config.BEHIND_PROXY,
        forwarded_allow_ips="*" if config.BEHIND_PROXY else None,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="hydration", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("garmin-login", help="log in to Garmin once, answering MFA").set_defaults(fn=cmd_garmin_login)
    subs.add_parser("sync", help="run a Garmin sync now").set_defaults(fn=cmd_sync)
    subs.add_parser("set-password", help="set the web password").set_defaults(fn=cmd_set_password)
    subs.add_parser("status", help="print the current plan").set_defaults(fn=cmd_status)

    backup = subs.add_parser("backup", help="write a consistent database copy")
    backup.add_argument("--into", type=Path, default=None)
    backup.add_argument(
        "--keep", type=int, default=None,
        help="delete all but this many of the backups in the directory afterwards",
    )
    backup.set_defaults(fn=cmd_backup)

    restore = subs.add_parser("restore", help="replace the database with a backup")
    restore.add_argument("backup", type=Path, help="the .db file to restore from")
    restore.add_argument("--force", action="store_true", help="skip the confirmation")
    restore.set_defaults(fn=cmd_restore)

    serve = subs.add_parser("serve", help="run the web server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
