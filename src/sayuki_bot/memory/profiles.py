"""Profile revisions and scoped resolution; no model calls or Discord dependencies."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from .evidence import source_rows, sources_visible


class Profiles:
    def __init__(self, database):
        self.db = database

    @staticmethod
    def resolution_query(guild_id, channels):
        """A correction only applies where its source channel may be disclosed.

        Inactive successors still withdraw the old fact. Deleting a correction's
        source must not silently restore the preference it corrected.
        """
        channels = tuple(str(c) for c in channels)
        marks = ",".join("?" for _ in channels) or "NULL"
        return (
            f"""WITH RECURSIVE replaced(id) AS (
                SELECT r.old_id FROM profile_revisions r JOIN memories m ON m.id=r.new_id
                WHERE m.guild_id=? AND m.channel_id IN ({marks})
                UNION
                SELECT r.old_id FROM profile_revisions r JOIN replaced p ON r.new_id=p.id
            )""",
            (str(guild_id), *channels),
        )

    def superseded(self, guild_id, channels):
        query, parameters = self.resolution_query(guild_id, channels)
        rows = self.db.connection.execute(query + " SELECT id FROM replaced", parameters)
        return {r[0] for r in rows}

    def write(
        self,
        event,
        subject_id,
        key,
        value,
        *,
        operation,
        context,
        channels,
        target_id,
        relation,
        expires_at,
        updated_at,
        now,
        basis,
        evidence,
    ):
        if not isinstance(context, str) or len(context) > 100:
            raise ValueError("Invalid profile context")
        if operation == "set" and not value.strip():
            raise ValueError("Profile value cannot be empty")
        if relation not in {"replace", "context"} or (target_id is None and relation != "replace"):
            raise ValueError("Invalid profile relation")
        if target_id is not None and (not str(target_id).isdigit() or int(target_id) <= 0):
            raise ValueError("Invalid profile target")
        channels = {str(c) for c in channels}
        if str(event.channel_id) not in channels:
            raise PermissionError("Source channel is not readable")
        marks = ",".join("?" for _ in channels)
        rows = self.db.connection.execute(
            f"""SELECT m.*,e.created_at AS source_time FROM memories m
            LEFT JOIN events e ON e.id=m.source_id WHERE m.guild_id=? AND m.subject_id=?
            AND m.kind='profile' AND m.channel_id IN ({marks})""",
            (str(event.guild_id), str(subject_id), *channels),
        ).fetchall()
        hidden = self.superseded(event.guild_id, channels)
        context = context.strip()
        target = None
        if target_id is not None:
            target = next((r for r in rows if r["id"] == int(target_id)), None)
            if target is None or (
                target["source_author"]
                and not self.db.memory_enabled(event.guild_id, target["source_author"])
            ):
                raise PermissionError("Profile target is outside readable scope")
            if not sources_visible(
                self.db, source_rows(self.db, target["id"]), event.guild_id, channels
            ):
                raise PermissionError("Profile evidence is outside readable scope")
            if (
                target["id"] in hidden
                or not target["active"]
                or (target["expires_at"] and datetime.fromisoformat(target["expires_at"]) <= now)
            ):
                return {"status": "ignored", "reason": "target_no_longer_current"}
            if str(event.author_id) != str(subject_id):
                return {"status": "ignored", "reason": "requires_subject_statement"}
            key = target["key"]
            if relation == "replace":
                if context and context != target["context"]:
                    raise ValueError("Replacement must preserve the target context")
                context = target["context"]
            elif not context or context == target["context"] or operation != "set":
                raise ValueError("Context branch requires a distinct, explicit context")
        family = [r for r in rows if r["key"] == key and r["context"] == context]
        # Late batches cannot overwrite a newer statement, even if an older local
        # version was already replaced from another readable channel.
        for prior in [*family, *([target] if target is not None else [])]:
            if prior["source_time"] and (
                datetime.fromisoformat(prior["source_time"]),
                int(prior["source_id"]) if str(prior["source_id"]).isdigit() else 0,
            ) > (event.created_at, event.id):
                return {"status": "ignored", "reason": "older_source"}
            if (
                str(event.author_id) != str(subject_id)
                and prior["source_author"] == str(subject_id)
                and prior["basis"] in {"self_report", "unknown"}
            ):
                return {
                    "status": "ignored",
                    "reason": "third_party_cannot_replace_subject_statement",
                }
        if basis != "self_report" and any(
            r["basis"] == "self_report" for r in [*family, *([target] if target else [])]
        ):
            return {"status": "ignored", "reason": "unconfirmed_cannot_replace_subject_statement"}
        predecessors = [
            r
            for r in family
            if r["channel_id"] == str(event.channel_id)
            and r["id"] not in hidden
            and (
                basis == "self_report"
                or (
                    r["basis"] == basis
                    and (basis != "reported" or r["source_author"] == str(event.author_id))
                )
            )
        ]
        if target is not None and relation == "replace" and target not in predecessors:
            predecessors.append(target)
        # Repeating the same extraction is idempotent, but a new source is kept.
        duplicate = next(
            (
                r
                for r in predecessors
                if r["source_id"] == str(event.id)
                and r["value"] == (value if operation == "set" else "")
                and bool(r["active"]) == (operation == "set")
                and r["basis"] == basis
                and {s["event_id"] for s in source_rows(self.db, r["id"])}
                == {str(e.id) for e in evidence}
            ),
            None,
        )
        if duplicate and len(predecessors) == 1:
            return {"status": "ok", "memory_id": duplicate["id"], "key": key}
        with self.db.connection:
            cursor = self.db.connection.execute(
                """INSERT INTO memories
                (guild_id,channel_id,subject_id,kind,key,value,source_id,source_author,
                 updated_at,expires_at,active,context,basis) VALUES(?,?,?,'profile',?,?,?,?,?,?,?,?,?)""",
                (
                    str(event.guild_id),
                    str(event.channel_id),
                    str(subject_id),
                    key,
                    value if operation == "set" else "",
                    str(event.id),
                    str(event.author_id),
                    updated_at,
                    expires_at,
                    int(operation == "set"),
                    context,
                    basis,
                ),
            )
            identifier = cursor.lastrowid
            self.db.connection.executemany(
                "INSERT INTO memory_sources VALUES(?,?)",
                [(identifier, str(e.id)) for e in evidence],
            )
            self.db.connection.executemany(
                "INSERT INTO profile_revisions VALUES(?,?)",
                [(r["id"], identifier) for r in predecessors],
            )
            # Derived summaries are disposable caches. Remove ones based on a
            # withdrawn statement instead of feeding stale preferences back in.
            sources = {r["source_id"] for r in predecessors}
            if sources:
                for summary in self.db.connection.execute("SELECT * FROM summaries").fetchall():
                    if sources.intersection(json.loads(summary["source_ids"])):
                        self.db.connection.execute(
                            "DELETE FROM summaries WHERE guild_id=? AND channel_id=?",
                            (summary["guild_id"], summary["channel_id"]),
                        )
        return {
            "status": "ok",
            "operation": operation,
            "memory_id": identifier,
            "subject_id": str(subject_id),
            "key": key,
            "context": context,
            "basis": basis,
            "source_ids": [str(e.id) for e in evidence],
        }

    @staticmethod
    def annotate(rows):
        """Different contexts coexist; unresolved same-key statements stay explicit."""
        groups = defaultdict(list)
        for item in rows:
            if item["kind"] == "profile":
                groups[(item["subject_id"], item["key"], item["context"])].append(item)
        for items in groups.values():
            conflict = len({" ".join(r["value"].casefold().split()) for r in items}) > 1
            for item in items:
                item["conflict"] = conflict
        return rows
