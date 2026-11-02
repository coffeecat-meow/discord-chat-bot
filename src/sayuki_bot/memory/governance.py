"""Explicit guild-scoped memory controls and export/erasure."""

import json
from sayuki_bot.storage.database import utcnow


class Governance:
    def __init__(self, database):
        self.db = database

    def set_enabled(self, guild_id, user_id, enabled):
        with self.db.connection:
            self.db.connection.execute(
                "INSERT INTO privacy VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET enabled=excluded.enabled",
                (str(guild_id), str(user_id), int(enabled)),
            )
            if not enabled:
                ids = {
                    r[0]
                    for r in self.db.connection.execute(
                        "SELECT id FROM events WHERE guild_id=? AND author_id=?",
                        (str(guild_id), str(user_id)),
                    )
                }
                for row in self.db.connection.execute(
                    "SELECT rowid,source_ids FROM summaries WHERE guild_id=?", (str(guild_id),)
                ).fetchall():
                    if ids.intersection(json.loads(row["source_ids"])):
                        self.db.connection.execute(
                            "DELETE FROM summaries WHERE rowid=?", (row["rowid"],)
                        )

    def allow_import_restore(self, guild_id, user_id, *, confirmed=False):
        """Only the explicit self-consent command calls this, never a tool."""
        if confirmed is not True:
            raise ValueError("Explicit confirmation is required to restore erased imports")
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE import_erasure SET restore_allowed=1 WHERE guild_id=? AND user_id=?",
                (str(guild_id), str(user_id)),
            )
        return {
            "legacy_import_restore_allowed": True,
            "memory_enabled": self.db.memory_enabled(guild_id, user_id),
        }

    def export(self, guild_id, user_id):
        guild_id, user_id = str(guild_id), str(user_id)
        memories = [
            dict(r)
            for r in self.db.connection.execute(
                """SELECT * FROM memories WHERE guild_id=? AND (subject_id=? OR source_author=?
                OR (kind='self' AND id IN (SELECT s.memory_id FROM memory_sources s JOIN events e
                ON e.id=s.event_id WHERE e.guild_id=? AND e.author_id=?)))""",
                (guild_id, user_id, user_id, guild_id, user_id),
            )
        ]
        identifiers = {r["id"] for r in memories}
        return {
            "observations": [
                dict(r)
                for r in self.db.connection.execute(
                    "SELECT o.* FROM observations o JOIN events e ON e.id=o.event_id WHERE e.guild_id=? AND e.author_id=?",
                    (guild_id, user_id),
                )
            ],
            "events": [
                dict(r)
                for r in self.db.connection.execute(
                    "SELECT * FROM events WHERE guild_id=? AND author_id=?", (guild_id, user_id)
                )
            ],
            "memories": memories,
            "self_memory_meta": [
                dict(r)
                for r in self.db.connection.execute("SELECT * FROM self_memory_meta")
                if r["memory_id"] in identifiers
            ],
            "memory_sources": [
                dict(r)
                for r in self.db.connection.execute("SELECT * FROM memory_sources")
                if r["memory_id"] in identifiers
            ],
            "profile_revisions": [
                dict(r)
                for r in self.db.connection.execute(
                    "SELECT r.* FROM profile_revisions r JOIN memories m ON m.id=r.old_id WHERE m.guild_id=?",
                    (guild_id,),
                )
                if r["old_id"] in identifiers and r["new_id"] in identifiers
            ],
            "reminders": [
                dict(r)
                for r in self.db.connection.execute(
                    "SELECT * FROM reminders WHERE guild_id=? AND user_id=?", (guild_id, user_id)
                )
            ],
        }

    def erase(self, guild_id, user_id):
        guild_id, user_id = str(guild_id), str(user_id)
        with self.db.connection:
            self.db.connection.execute(
                "INSERT INTO import_erasure VALUES(?,?,?,0) ON CONFLICT(guild_id,user_id) DO UPDATE SET cleared_at=excluded.cleared_at,restore_allowed=0",
                (guild_id, user_id, utcnow()),
            )
            ids = {
                r[0]
                for r in self.db.connection.execute(
                    "SELECT id FROM events WHERE guild_id=? AND author_id=?", (guild_id, user_id)
                )
            }
            # Erase derived content even when this user supplied a secondary
            # source rather than the anchor statement or profile subject.
            for identifier in ids:
                self.db.invalidate_event(identifier, deleted=True)
            memories = self.db.connection.execute(
                "DELETE FROM memories WHERE guild_id=? AND (subject_id=? OR source_author=?)",
                (guild_id, user_id, user_id),
            ).rowcount
            events = self.db.connection.execute(
                "DELETE FROM events WHERE guild_id=? AND author_id=?", (guild_id, user_id)
            ).rowcount
            for identifier in ids:
                self.db.connection.execute(
                    "DELETE FROM observations WHERE event_id=?", (identifier,)
                )
            for table in ["summaries", "memory_jobs"]:
                column = "source_ids" if table == "summaries" else "event_ids"
                rows = self.db.connection.execute(
                    f"SELECT rowid,{column} FROM {table} WHERE guild_id=?", (guild_id,)
                ).fetchall()
                for row in rows:
                    if ids.intersection(json.loads(row[column])):
                        self.db.connection.execute(
                            f"DELETE FROM {table} WHERE rowid=?", (row["rowid"],)
                        )
            # Keep unknown receipt state; remove identifying reminder content.
            self.db.connection.execute(
                "DELETE FROM reminders WHERE guild_id=? AND user_id=? AND status!='unknown'",
                (guild_id, user_id),
            )
            self.db.connection.execute(
                "UPDATE reminders SET user_id='',content='' WHERE guild_id=? AND user_id=? AND status='unknown'",
                (guild_id, user_id),
            )
            self.set_enabled(guild_id, user_id, False)
        return {
            "events_deleted": events,
            "memories_deleted": memories,
            "future_memory_enabled": False,
        }
