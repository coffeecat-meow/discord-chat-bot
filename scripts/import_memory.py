"""Run with the installed package (or PYTHONPATH=src). Dry-run by default.

O7-A lifecycle:

* The source and destination must be different files, including through
  symbolic or hard links.
* With ``--apply`` the same DatabaseLock as the Bot covers path spellings and
  device/inode identity, so symlinks and hard links cannot sidestep the lock.
* The destination is never opened for writing while importing. It is copied:
  the current schema (or no destination) into memory; supported older schemas
  into a private temporary file that the normal ``Database`` upgrades. A
  dry-run therefore never upgrades or touches the real file. An unknown schema,
  or a SQLite file that is not a 4.6 database, is refused before anything runs.
* Only a completed import is written back, in one SQLite backup step. On any
  failure the destination is left as it was, resources are closed, and a
  one-line JSON status is printed to stderr.
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from sayuki_bot.memory.migration import (
    read_v4,
    read_v5,
    import_records,
    import_graph,
    read_only_source,
)
from sayuki_bot.storage.database import Database, SCHEMA_VERSION

SUPPORTED_SCHEMAS = tuple(range(SCHEMA_VERSION + 1))


def _same_file(source, destination):
    if destination.resolve() == source.resolve():
        return True
    try:
        return destination.exists() and os.path.samefile(source, destination)
    except OSError:
        return False


def _destination_schema(path):
    """(user_version, has_4_6_memories_table) read without writing anything."""
    with read_only_source(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    return version, tables


def _lock_paths(destination):
    """The Bot locks `<configured path>.lock`; cover the given and resolved path.

    Two spellings of one directory (``/var`` and ``/private/var`` on macOS) name
    the same lock file, and locking it twice from one process would conflict
    with itself, so paths are compared with their directory resolved.
    """
    from sayuki_bot.storage.instance_lock import database_lock_paths
    return [str(path) for path in database_lock_paths(destination)]


def _fail(status, message, code=1):
    status.update({"status": "failed", "error": message})
    print(json.dumps(status, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--format", choices=["v4", "v4-self", "v5", "v5-events"], required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--guild-id")
    parser.add_argument("--channel-id")
    parser.add_argument("--bot-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.format.startswith("v4") and (not args.guild_id or not args.channel_id):
        parser.error("V4 requires explicit destination scope")
    if args.format.startswith("v4") and not (
        str(args.guild_id).isdigit() and str(args.channel_id).isdigit()
    ):
        parser.error("V4 destination scope must be numeric Discord IDs")
    if args.apply and not args.destination:
        parser.error("--apply requires --destination")
    if args.destination and _same_file(args.source, args.destination):
        parser.error("Source and destination must differ")

    schema_before = None
    if args.destination and args.destination.exists():
        try:
            schema_before, tables = _destination_schema(args.destination)
        except sqlite3.DatabaseError as exc:
            parser.error(f"Destination is not a readable SQLite database: {exc}")
        if schema_before not in SUPPORTED_SCHEMAS:
            parser.error(f"Unsupported destination schema {schema_before}; refusing to import")
        if tables and "memories" not in tables:
            parser.error(
                "Destination schema is not a Sayuki 4.6 database (no memories table); refusing"
            )

    status = {"status": "running", "destination_written": False}
    from sayuki_bot.storage.instance_lock import DatabaseLock

    locks = []
    db = None
    report = None
    try:
        if args.destination and args.apply:
            lock = DatabaseLock(args.destination)
            lock.acquire()
            locks.append(lock)
        with tempfile.TemporaryDirectory(prefix="sayuki-import-") as work:
            if schema_before in range(1, SCHEMA_VERSION) or (schema_before == 0 and args.destination.exists()):
                # An isolated copy is upgraded by the normal Database code; the
                # real destination is only ever replaced by a finished import.
                copy = Path(work) / "destination.sqlite3"
                with read_only_source(args.destination) as source:
                    target = sqlite3.connect(copy)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                db = Database(copy)
            else:
                db = Database(":memory:")
                # A lifetime lock may create an empty inode for a new database.
                # Only copy a destination observed before acquiring that lock.
                if schema_before is not None:
                    with read_only_source(args.destination) as source:
                        source.backup(db.connection)
            if args.format == "v5-events":
                report = import_graph(db, args.source, apply=args.apply)
            else:
                records = (
                    read_v5(args.source)
                    if args.format == "v5"
                    else read_v4(
                        args.source,
                        args.guild_id,
                        args.channel_id,
                        permanent=args.format == "v4-self",
                        bot_id=args.bot_id,
                    )
                )
                report = import_records(db, records, apply=args.apply)
            if args.apply:
                args.destination.parent.mkdir(parents=True, exist_ok=True)
                target = sqlite3.connect(args.destination)
                try:
                    db.connection.backup(target)
                finally:
                    target.close()
                status["destination_written"] = True
            if schema_before is not None or args.apply:
                after = db.connection.execute("PRAGMA user_version").fetchone()[0]
                report["destination_schema"] = {
                    "before": schema_before,
                    "after": after if args.apply else schema_before,
                }
            db.close()
            db = None
    except SystemExit:
        raise
    except BaseException as exc:
        partial = getattr(exc, "import_report", None)
        if partial is not None:
            status["partial_report"] = partial
        _fail(status, f"{type(exc).__name__}: {exc}")
    finally:
        if db is not None:
            db.close()
        for lock in reversed(locks):
            lock.release()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run()
