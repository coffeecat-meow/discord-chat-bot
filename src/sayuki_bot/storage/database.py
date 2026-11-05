"""Small SQLite persistence boundary. No model or Discord dependencies."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from time import monotonic

SCHEMA_VERSION = 4


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_attachments(attachments) -> list:
    """Attachment identity without Discord's expiring CDN signature (?ex=&is=&hm=).

    A refetched message carries freshly signed URLs; that is not an edit.
    """
    if isinstance(attachments, str):
        attachments = json.loads(attachments or "[]")
    return [
        {**item, "url": str(item.get("url", "")).split("?", 1)[0]}
        if isinstance(item, dict)
        else item
        for item in attachments
    ]


class Database:
    def __init__(self, path: Path | str, *, diagnostic_retention_days=30):
        if type(diagnostic_retention_days) is not int or diagnostic_retention_days <= 0:
            raise ValueError("diagnostic_retention_days must be a positive integer")
        self.diagnostic_retention_days = diagnostic_retention_days
        self._last_diagnostic_cleanup = None
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        try:
            self._initialize()
        except BaseException:
            self.connection.close()
            raise

    def _initialize(self):
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4):
            raise ValueError(f"Unsupported database version {version}")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            author_id TEXT NOT NULL, author_name TEXT NOT NULL, text TEXT NOT NULL,
            created_at TEXT NOT NULL, reply_to TEXT, attachments TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS event_channel ON events(guild_id,channel_id,created_at);
        CREATE INDEX IF NOT EXISTS event_reply ON events(guild_id,channel_id,reply_to);
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            subject_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source_id TEXT, source_author TEXT,
            updated_at TEXT NOT NULL, expires_at TEXT, active INTEGER NOT NULL DEFAULT 1,
            provenance TEXT NOT NULL DEFAULT 'observed',
            UNIQUE(guild_id,channel_id,subject_id,kind,key));
        CREATE TABLE IF NOT EXISTS summaries (
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, text TEXT NOT NULL,
            source_ids TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(guild_id,channel_id));
        CREATE TABLE IF NOT EXISTS receipts (
            key TEXT PRIMARY KEY, turn_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, role TEXT NOT NULL,
            model TEXT NOT NULL, duration_ms REAL NOT NULL, data TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS traces (
            id INTEGER PRIMARY KEY, turn_id TEXT NOT NULL, stage TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL, due_at TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS memory_jobs (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            event_ids TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS privacy (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, enabled INTEGER NOT NULL,
            PRIMARY KEY(guild_id,user_id));
        CREATE TABLE IF NOT EXISTS trusted_guilds (
            guild_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS observations (
            event_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(event_id,kind));
        """)
        if version < 2:
            # Preserve identifiers: imported records and correction links use them.
            self.connection.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE memories RENAME TO memories_v1;
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                subject_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
                value TEXT NOT NULL, source_id TEXT, source_author TEXT,
                updated_at TEXT NOT NULL, expires_at TEXT, active INTEGER NOT NULL DEFAULT 1,
                provenance TEXT NOT NULL DEFAULT 'observed', context TEXT NOT NULL DEFAULT '');
            INSERT INTO memories SELECT *, '' FROM memories_v1;
            DROP TABLE memories_v1;
            PRAGMA user_version=2;
            COMMIT;
            """)
        self.connection.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS memory_current_key
            ON memories(guild_id,channel_id,subject_id,kind,key) WHERE kind!='profile';
        CREATE INDEX IF NOT EXISTS profile_scope
            ON memories(guild_id,subject_id,key,context) WHERE kind='profile';
        CREATE TABLE IF NOT EXISTS profile_revisions (
            old_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            new_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            PRIMARY KEY(old_id,new_id), CHECK(old_id<new_id));
        CREATE INDEX IF NOT EXISTS profile_successor ON profile_revisions(new_id);
        """)
        if version < 3:
            # Old first-person sources can also contain model inference. Do not
            # retroactively label them as verified self-reports.
            self.connection.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE memories ADD COLUMN basis TEXT NOT NULL DEFAULT 'unknown';
            PRAGMA user_version=3;
            COMMIT;
            """)
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS memory_sources (
            memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL, PRIMARY KEY(memory_id,event_id));
        CREATE INDEX IF NOT EXISTS memory_source_event ON memory_sources(event_id);
        CREATE TABLE IF NOT EXISTS self_memory_meta (
            memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            category TEXT NOT NULL CHECK(category IN ('preference','conversation')),
            evidence_ids TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS import_erasure (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, cleared_at TEXT NOT NULL,
            restore_allowed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id,user_id));
        """)
        # Old importers must reject this schema rather than bypass the new
        # persistent erase barrier after recording has been re-enabled.
        self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # A previous process may have sent an action before crashing. Never replay it.
        with self.connection:
            self.connection.execute("UPDATE receipts SET status='unknown' WHERE status='pending'")
            self.connection.execute(
                "UPDATE memory_jobs SET status='pending' WHERE status='running'"
            )
            self.connection.execute("UPDATE reminders SET status='unknown' WHERE status='queued'")

    def close(self):
        self.connection.close()

    def get_state(self, key):
        row = self.connection.execute(
            "SELECT value FROM runtime_state WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_state(self, key, value):
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), utcnow()),
            )

    def record_event(self, event):
        if not self.memory_enabled(event.guild_id, event.author_id):
            return
        attachments = json.dumps(event.attachments)
        previous = self.connection.execute(
            "SELECT text,attachments FROM events WHERE id=?", (str(event.id),)
        ).fetchone()
        if previous and (
            previous["text"] != event.text
            or stable_attachments(previous["attachments"]) != stable_attachments(attachments)
        ):
            self.invalidate_event(event.id, deleted=False)
        with self.connection:
            self.connection.execute(
                """INSERT INTO events
                (id,guild_id,channel_id,author_id,author_name,text,created_at,reply_to,attachments)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                text=excluded.text,attachments=excluded.attachments,author_name=excluded.author_name""",
                (
                    str(event.id),
                    str(event.guild_id),
                    str(event.channel_id),
                    str(event.author_id),
                    event.author_name,
                    event.text,
                    event.created_at.isoformat(),
                    str(event.reply_to) if event.reply_to else None,
                    attachments,
                ),
            )

    def invalidate_event(self, event_id: int, *, deleted: bool):
        with self.connection:
            self.connection.execute("DELETE FROM observations WHERE event_id=?", (str(event_id),))
            if deleted:
                self.connection.execute(
                    "UPDATE events SET deleted=1,text='',attachments='[]' WHERE id=?",
                    (str(event_id),),
                )
            self.connection.execute(
                """UPDATE memories SET active=0,value='' WHERE source_id=? OR id IN
                (SELECT memory_id FROM memory_sources WHERE event_id=?)""",
                (str(event_id), str(event_id)),
            )
            for row in self.connection.execute(
                "SELECT guild_id,channel_id,source_ids FROM summaries"
            ).fetchall():
                if str(event_id) in json.loads(row["source_ids"]):
                    self.connection.execute(
                        "DELETE FROM summaries WHERE guild_id=? AND channel_id=?",
                        (row["guild_id"], row["channel_id"]),
                    )

    def recent(self, guild_id, channel_id, limit=50):
        return list(
            reversed(
                self.connection.execute(
                    """SELECT * FROM events WHERE guild_id=?
            AND channel_id=? AND deleted=0 ORDER BY created_at DESC LIMIT ?""",
                    (str(guild_id), str(channel_id), min(200, max(1, limit))),
                ).fetchall()
            )
        )

    def claim(self, key: str, turn_id: str, kind: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO receipts(key,turn_id,kind,status,updated_at) VALUES(?,?,?,'pending',?)",
                (key, turn_id, kind, utcnow()),
            )
            return cursor.rowcount == 1

    def settle(self, key, status, detail=""):
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError(status)
        with self.connection:
            self.connection.execute(
                "UPDATE receipts SET status=?,detail=?,updated_at=? WHERE key=?",
                (status, str(detail)[:1000], utcnow(), key),
            )

    def record_usage(self, turn_id, role, model, duration_ms, data):
        with self.connection:
            self.connection.execute(
                "INSERT INTO usage(turn_id,role,model,duration_ms,data,created_at) VALUES(?,?,?,?,?,?)",
                (turn_id, role, model, duration_ms, json.dumps(data), utcnow()),
            )
        self._maintain_diagnostics()

    def trace(self, turn_id, stage, data=None):
        with self.connection:
            self.connection.execute(
                "INSERT INTO traces(turn_id,stage,data,created_at) VALUES(?,?,?,?)",
                (turn_id, stage, json.dumps(data or {}, ensure_ascii=False), utcnow()),
            )
        self._maintain_diagnostics()

    def prune_diagnostics(self, *, now=None, retention_days=None):
        days = self.diagnostic_retention_days if retention_days is None else retention_days
        if type(days) is not int or days <= 0:
            raise ValueError("diagnostic retention must be a positive integer")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("diagnostic cleanup clock must be timezone-aware")
        cutoff = (now - timedelta(days=days)).isoformat()
        with self.connection:
            counts = {
                table + "_deleted": self.connection.execute(
                    f"DELETE FROM {table} WHERE julianday(created_at)<julianday(?)", (cutoff,)
                ).rowcount
                for table in ("traces", "usage")
            }
        self._last_diagnostic_cleanup = monotonic()
        return counts

    def _maintain_diagnostics(self):
        if (
            self._last_diagnostic_cleanup is None
            or monotonic() - self._last_diagnostic_cleanup >= 3600
        ):
            self.prune_diagnostics()

    def legacy_import_allowed(self, guild_id, user_id):
        row = self.connection.execute(
            "SELECT restore_allowed FROM import_erasure WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        ).fetchone()
        return row is None or bool(row[0])

    def memory_enabled(self, guild_id, user_id):
        row = self.connection.execute(
            "SELECT enabled FROM privacy WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        ).fetchone()
        return row is None or bool(row[0])

    def observation(self, event_id):
        row = self.connection.execute(
            "SELECT o.value,e.guild_id,e.author_id FROM observations o JOIN events e ON e.id=o.event_id WHERE o.event_id=? AND o.kind='vision' AND e.deleted=0",
            (str(event_id),),
        ).fetchone()
        return (
            row["value"] if row and self.memory_enabled(row["guild_id"], row["author_id"]) else ""
        )

    def save_observation(self, event_id, text, expected_text, expected_attachments):
        row = self.connection.execute(
            "SELECT guild_id,author_id FROM events WHERE id=? AND text=? AND attachments=? AND deleted=0",
            (str(event_id), expected_text, expected_attachments),
        ).fetchone()
        if not row or not self.memory_enabled(row["guild_id"], row["author_id"]):
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO observations VALUES(?,'vision',?,?) ON CONFLICT(event_id,kind) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (str(event_id), text[:6000], utcnow()),
            )
        return True
