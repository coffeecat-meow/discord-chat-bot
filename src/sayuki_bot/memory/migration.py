"""Explicit offline imports. Source files are never modified.

O7-A: every record goes through the field contract in
``migration_o7a_contract`` before it can reach the database. A record that does
not meet it is reported under a named reason instead of aborting the batch, and
the INSERT column list is fixed rather than taken from the record's keys.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sayuki_bot.storage.database import utcnow
from sayuki_bot.conversation.models import Event
from datetime import timezone

from . import migration_o7a_contract as contract

# Rejection samples kept in a report, so a large bad source cannot bloat it.
MAX_REJECTION_SAMPLES = 200


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            if key not in {"user_id", "id"}:
                yield from flatten(item, f"{prefix}.{key}".strip("."))
    elif isinstance(value, list):
        # Keep each structured event together rather than separating date/source/content.
        for i, item in enumerate(value):
            if isinstance(item, dict):
                yield f"{prefix}[{i}]", json.dumps(item, ensure_ascii=False)
            else:
                yield from flatten(item, f"{prefix}[{i}]")
    elif value is not None and str(value) not in {"", "未知"}:
        yield prefix, str(value)


@contextmanager
def read_only_source(path):
    """Open a legacy SQLite source without creating anything next to it.

    ``mode=ro`` alone still makes SQLite create a ``-shm`` file beside a WAL
    database. Without a ``-wal`` file the main file is complete, so it is read
    as ``immutable``; with one, the pair is copied to a private temporary
    directory and read there, so uncheckpointed rows are not lost either.
    """
    path = Path(path).resolve()
    wal = path.with_name(path.name + "-wal")
    if not wal.exists():
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()
        return
    with tempfile.TemporaryDirectory(prefix="sayuki-import-source-") as directory:
        copy = Path(directory) / path.name
        shutil.copyfile(path, copy)
        shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        connection = sqlite3.connect(copy)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def read_v4(path, guild_id, channel_id, *, permanent=False, bot_id=None):
    content = Path(path).read_text(encoding="utf-8-sig")
    # 4.0 treats an empty memory file as an empty store.
    payload = json.loads(content) if content.strip() else {}
    if permanent:
        if not bot_id:
            raise ValueError("Permanent memory import requires bot_id")
        facts = payload.get("facts", []) if isinstance(payload, dict) else []
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or not fact.get("id"):
                yield {"skip_reason": "missing_field:id", "ref": f"facts[{index}]"}
                continue
            if not isinstance(fact.get("content"), str) or not fact["content"].strip():
                # Never store a placeholder such as "None" as a memory.
                yield {"skip_reason": "missing_field:content", "ref": f"facts[{index}]"}
                continue
            yield dict(
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                subject_id=str(bot_id),
                kind="self",
                key=str(fact["id"]),
                value=fact["content"],
                source_id=None,
                source_author=None,
                updated_at=utcnow(),
                expires_at=None,
                provenance="legacy_v4_self",
            )
        return
    if isinstance(payload, dict) and "users" in payload:
        payload = payload["users"]
    if isinstance(payload, dict) and payload.get("user_id"):
        payload = {str(payload["user_id"]): payload}
    if isinstance(payload, list):
        # 4.0 drops list entries without user_id; report them instead of crashing.
        keyed = {}
        for index, entry in enumerate(payload):
            if isinstance(entry, dict) and entry.get("user_id"):
                keyed[str(entry["user_id"])] = entry
            else:
                keyed[f"#invalid[{index}]"] = entry
        payload = keyed
    if not isinstance(payload, dict):
        raise ValueError("Unsupported V4 memory shape")
    for uid, profile in payload.items():
        if not str(uid).isdigit():
            yield {"skip_reason": "invalid_subject_id", "ref": str(uid)[:40]}
            continue
        if not isinstance(profile, dict):
            yield {"skip_reason": "invalid_profile_shape", "ref": str(uid)}
            continue
        for key, value in flatten(profile):
            kind = (
                "event"
                if key.startswith(("important_events", "recent_events", "gossip_pool"))
                else "profile"
            )
            yield dict(
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                subject_id=str(uid),
                kind=kind,
                key="legacy:" + key,
                value=value,
                source_id=None,
                source_author=None,
                updated_at=utcnow(),
                expires_at=None,
                provenance="legacy_v4_source_unknown",
            )


def read_v5(path):
    with read_only_source(path) as connection:
        privacy_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_v5_privacy'"
        ).fetchone()

        def opted_out(guild_id, user_id):
            if not privacy_table or not user_id:
                return False
            row = connection.execute(
                "SELECT memory_enabled FROM memory_v5_privacy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            return bool(row) and not row[0]

        for row in connection.execute("SELECT * FROM memory_v5_items WHERE status='active'"):
            ref = str(row["id"])
            kind = {
                "semantic_fact": "profile",
                "working_memory": "event",
                "episode": "event",
                "open_loop": "event",
                "culture": "culture",
            }.get(row["kind"])
            # Do not silently turn social scores, affect or persona insight into facts.
            if kind is None:
                yield {"skip_reason": "unmapped_kind:" + row["kind"], "ref": ref}
                continue
            if not row["guild_id"] or not row["channel_id"] or not row["subject_user_id"]:
                yield {"skip_reason": "missing_scope_or_subject", "ref": ref}
                continue
            if opted_out(row["guild_id"], row["subject_user_id"]):
                yield {"skip_reason": "memory_opt_out", "ref": ref}
                continue
            if opted_out(row["guild_id"], row["source_user_id"]):
                # The statement's author stopped memory; their words are not stored.
                yield {"skip_reason": "source_opt_out", "ref": ref}
                continue
            if row["source_channel_id"] and row["source_channel_id"] != row["channel_id"]:
                # Learned in another channel. Importing it here would disclose it
                # in a channel the source was never shared with.
                yield {"skip_reason": "source_scope_mismatch", "ref": ref}
                continue
            yield dict(
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                subject_id=row["subject_user_id"],
                kind=kind,
                key="v5:" + row["id"],
                value=row["content"],
                source_id=row["source_message_id"] or None,
                source_author=row["source_user_id"] or None,
                updated_at=row["updated_at"],
                expires_at=row["expires_at"] or None,
                provenance="legacy_v5:" + row["source_type"],
            )


def _note(report, index, reason, key=None, ref=None):
    report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
    if len(report["rejections"]) < MAX_REJECTION_SAMPLES:
        report["rejections"].append({"index": index, "reason": reason, "key": key, "ref": ref})


def import_records(database, records, *, apply=False):
    from sayuki_bot.memory.profiles import Profiles

    report = {
        "candidates": 0,
        "inserted": 0,
        "unchanged": 0,
        "conflicts": 0,
        "oversized": 0,
        "skipped": {},
        "dry_run": not apply,
        # O7-A diagnostics; the keys above keep their meaning.
        "rejections": [],
        "normalized": {},
    }
    pending = []
    staged = {}
    hidden_by_scope = {}
    for index, item in enumerate(records):
        if isinstance(item, dict) and "skip_reason" in item:
            _note(report, index, item["skip_reason"], ref=item.get("ref"))
            continue
        key = (
            item.get("key") if isinstance(item, dict) and isinstance(item.get("key"), str) else None
        )
        try:
            clean, normalized = contract.normalize(item)
        except contract.Rejected as rejected:
            _note(report, index, rejected.reason, key=key)
            continue
        if not database.memory_enabled(clean["guild_id"], clean["subject_id"]) or (
            clean["source_author"]
            and not database.memory_enabled(clean["guild_id"], clean["source_author"])
        ):
            # An offline import does not get around a destination opt-out.
            _note(report, index, "destination_opt_out", key=key)
            continue
        if not database.legacy_import_allowed(clean["guild_id"], clean["subject_id"]) or (
            clean["source_author"]
            and not database.legacy_import_allowed(clean["guild_id"], clean["source_author"])
        ):
            _note(report, index, "destination_erased", key=key)
            continue
        report["candidates"] += 1
        for field in normalized:
            report["normalized"][field] = report["normalized"].get(field, 0) + 1
        if contract.oversized(clean):
            report["oversized"] += 1
            continue
        identity = contract.identity(clean)
        if identity in staged:
            report["unchanged" if staged[identity] == clean["value"] else "conflicts"] += 1
            continue
        old = contract.existing_rows(database.connection, clean)
        if old:
            scope = (clean["guild_id"], clean["channel_id"])
            if scope not in hidden_by_scope:
                hidden_by_scope[scope] = Profiles(database).superseded(scope[0], [scope[1]])
            current = [r for r in old if r["active"] and r["id"] not in hidden_by_scope[scope]]
            # Historical/tombstoned values must never be reimported as current.
            same = len(current) == 1 and current[0]["value"] == clean["value"]
            report["unchanged" if same else "conflicts"] += 1
            continue
        pending.append(clean)
        staged[identity] = clean["value"]
    report["would_insert"] = len(pending)
    if apply:
        columns = ",".join(contract.INSERT_FIELDS)
        marks = ",".join("?" for _ in contract.INSERT_FIELDS)
        written = 0
        try:
            with database.connection:
                for clean in pending:
                    database.connection.execute(
                        f"INSERT INTO memories({columns}) VALUES({marks})",
                        tuple(clean[field] for field in contract.INSERT_FIELDS),
                    )
                    written += 1
        except BaseException as exc:
            # The transaction rolled back, so nothing from this batch remains.
            exc.import_report = {**report, "inserted": 0, "rolled_back": True}
            raise
        report["inserted"] = written
    return report


def import_graph(database, path, *, apply=False):
    report = {
        "candidates": 0,
        "would_insert": 0,
        "inserted": 0,
        "unchanged": 0,
        "conflicts": 0,
        "skipped": 0,
        "dry_run": not apply,
        # O7-A diagnostics; the keys above keep their meaning.
        "skipped_reasons": {},
        "conflict_reasons": {},
    }

    def skip(reason):
        report["skipped"] += 1
        report["skipped_reasons"][reason] = report["skipped_reasons"].get(reason, 0) + 1

    planned = []
    with read_only_source(path) as source:
        for row in source.execute("SELECT * FROM chat_events ORDER BY timestamp"):
            report["candidates"] += 1
            invalid = next(
                (
                    field
                    for field in ("message_id", "guild_id", "channel_id", "author_id")
                    if not str(row[field]).isdigit()
                ),
                None,
            )
            if invalid:
                skip(f"invalid_id:{invalid}")
                continue
            reply = row["reply_to_message_id"] or None
            if reply is not None and not str(reply).isdigit():
                skip("invalid_id:reply_to_message_id")
                continue
            try:
                timestamp = contract.event_time(row["timestamp"])
            except contract.Rejected as rejected:
                skip(rejected.reason)
                continue
            if not database.memory_enabled(row["guild_id"], row["author_id"]):
                skip("destination_opt_out")
                continue
            if not database.legacy_import_allowed(row["guild_id"], row["author_id"]):
                skip("destination_erased")
                continue
            existing = database.connection.execute(
                "SELECT guild_id,channel_id,author_id,text FROM events WHERE id=?",
                (row["message_id"],),
            ).fetchone()
            if existing:
                same_origin = (
                    existing["guild_id"],
                    existing["channel_id"],
                    existing["author_id"],
                ) == (
                    str(row["guild_id"]),
                    str(row["channel_id"]),
                    str(row["author_id"]),
                )
                if same_origin and existing["text"] == row["content"]:
                    report["unchanged"] += 1
                else:
                    # One message ID in two scopes or from two authors is not the
                    # same message, even if the text happens to match.
                    reason = "identity_mismatch" if not same_origin else "text_differs"
                    report["conflicts"] += 1
                    report["conflict_reasons"][reason] = (
                        report["conflict_reasons"].get(reason, 0) + 1
                    )
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            planned.append(
                Event(
                    int(row["message_id"]),
                    int(row["guild_id"]),
                    int(row["channel_id"]),
                    int(row["author_id"]),
                    row["author_name"] or "",
                    row["content"] or "",
                    timestamp,
                    reply_to=int(reply) if reply else None,
                )
            )
    report["would_insert"] = len(planned)
    if apply:
        # Each event commits on its own (Database.record_event), so an
        # interruption is reported with exactly what was written before it.
        written = []
        try:
            for event in planned:
                database.record_event(event)
                written.append(str(event.id))
                report["inserted"] += 1
        except BaseException as exc:
            exc.import_report = {
                **report,
                "interrupted": True,
                "written_ids": sorted(written),
            }
            raise
    return report
