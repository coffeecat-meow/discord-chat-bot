"""Source-scoped profile and event updates through one mutation service."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from sayuki_bot.memory.profiles import Profiles
from sayuki_bot.memory.evidence import BASIS_NOTES, source_rows, sources_visible, validate_sources


def terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        result.add(word)
        result.update(word[i : i + 2] for i in range(len(word) - 1))
    return result


class MemoryService:
    def __init__(self, database, *, clock=None):
        self.db = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.profiles = Profiles(database)
        from .self_memory import SelfMemory

        self.own = SelfMemory(database, lambda: self.clock())

    def update(
        self,
        event,
        subject_id,
        kind,
        key,
        value,
        *,
        operation="set",
        ttl_hours=None,
        context="",
        allowed_channels=None,
        target_id=None,
        relation="replace",
        basis=None,
        evidence=None,
    ):
        if not self.db.memory_enabled(
            event.guild_id, event.author_id
        ) or not self.db.memory_enabled(event.guild_id, subject_id):
            raise PermissionError("Memory is disabled for source or subject")
        if kind not in {"profile", "event", "self", "culture"}:
            raise ValueError("Unknown memory kind")
        if operation not in {"set", "delete"}:
            raise ValueError("Unknown memory operation")
        if kind == "self":
            raise ValueError("自身記憶須使用分類與明確來源的共用入口，不能寫自由文字覆寫核心")
        if not str(subject_id).isdigit() or not key.strip() or len(key) > 100 or len(value) > 2000:
            raise ValueError("Invalid memory subject/key/value")
        if kind == "event" and key.startswith("event:"):
            # Model-visible event keys are opaque handles, not model-authored
            # index words. Resolve only within the existing mutation scope.
            target = self.db.connection.execute(
                "SELECT key FROM memories WHERE id=? AND kind='event' AND guild_id=? AND channel_id=? AND subject_id=?",
                (
                    key.removeprefix("event:"),
                    str(event.guild_id),
                    str(event.channel_id),
                    str(subject_id),
                ),
            ).fetchone()
            if target is None:
                raise ValueError("Event handle is outside this mutation scope")
            key = target["key"]
        # Sources must still exist; an edit/delete invalidates pending extraction.
        source = self.db.connection.execute(
            "SELECT * FROM events WHERE id=? AND deleted=0", (str(event.id),)
        ).fetchone()
        if (
            not source
            or source["text"] != event.text
            or (source["guild_id"], source["channel_id"], source["author_id"], source["created_at"])
            != (
                str(event.guild_id),
                str(event.channel_id),
                str(event.author_id),
                event.created_at.isoformat(),
            )
        ):
            raise ValueError("Memory source is stale")
        expires = (
            (event.created_at + timedelta(hours=ttl_hours)).astimezone(timezone.utc).isoformat()
            if ttl_hours
            else None
        )
        if kind == "profile":
            basis = basis or (
                "self_report" if str(event.author_id) == str(subject_id) else "reported"
            )
            if basis not in {"self_report", "reported", "inferred"}:
                raise ValueError("Invalid profile basis")
            if basis == "self_report" and str(event.author_id) != str(subject_id):
                raise ValueError("Self report requires the subject's statement")
            channels = [event.channel_id] if allowed_channels is None else allowed_channels
            sources = validate_sources(self.db, event, evidence, channels)
            return self.profiles.write(
                event,
                subject_id,
                key,
                value,
                operation=operation,
                context=context,
                channels=channels,
                target_id=target_id,
                relation=relation,
                expires_at=expires,
                updated_at=self.clock().astimezone(timezone.utc).isoformat(),
                now=self.clock(),
                basis=basis,
                evidence=sources,
            )
        if basis is not None or evidence:
            raise ValueError("Evidence classification applies only to profiles")
        if target_id is not None or context or relation != "replace":
            raise ValueError("Revisions and contexts apply only to profiles")
        prior = self.db.connection.execute(
            """SELECT m.source_author,m.source_id,e.created_at FROM memories m
            LEFT JOIN events e ON e.id=m.source_id WHERE m.guild_id=? AND m.channel_id=?
            AND m.subject_id=? AND m.kind=? AND m.key=?""",
            (str(event.guild_id), str(event.channel_id), str(subject_id), kind, key),
        ).fetchone()
        if kind in {"profile", "event"} and prior:
            if prior["created_at"] and (
                datetime.fromisoformat(prior["created_at"]),
                int(prior["source_id"]) if str(prior["source_id"]).isdigit() else 0,
            ) > (event.created_at, event.id):
                return {"status": "ignored", "reason": "older_source"}
            if str(event.author_id) != str(subject_id) and prior["source_author"] == str(
                subject_id
            ):
                return {
                    "status": "ignored",
                    "reason": "third_party_cannot_replace_subject_statement",
                }
        with self.db.connection:
            self.db.connection.execute(
                """INSERT INTO memories
                (guild_id,channel_id,subject_id,kind,key,value,source_id,source_author,updated_at,expires_at,active)
                VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,subject_id,kind,key)
                WHERE kind!='profile'
                DO UPDATE SET value=excluded.value,source_id=excluded.source_id,
                source_author=excluded.source_author,updated_at=excluded.updated_at,
                expires_at=excluded.expires_at,active=excluded.active""",
                (
                    str(event.guild_id),
                    str(event.channel_id),
                    str(subject_id),
                    kind,
                    key,
                    value if operation == "set" else "",
                    str(event.id),
                    str(event.author_id),
                    self.clock().astimezone(timezone.utc).isoformat(),
                    expires,
                    int(operation == "set"),
                ),
            )
        if kind == "event":
            identifier = self.db.connection.execute(
                "SELECT id FROM memories WHERE guild_id=? AND channel_id=? AND subject_id=? AND kind='event' AND key=?",
                (str(event.guild_id), str(event.channel_id), str(subject_id), key),
            ).fetchone()[0]
            return {
                "status": "ok",
                "operation": operation,
                "subject_id": str(subject_id),
                "key": f"event:{identifier}",
                "source_id": str(event.id),
                "source_author": str(event.author_id),
                "source_time": event.created_at.isoformat(),
                "source_text": event.text[:2000],
                "source_truncated": len(event.text) > 2000,
                "interpretation": "事件摘要僅供檢索；請依來源原文解讀，發話時間不代表事件開始時間。",
            }
        return {"status": "ok", "operation": operation, "subject_id": str(subject_id), "key": key}

    def revise_profile(
        self, event, target_id, relation, context, value, *, allowed_channels, evidence=None
    ):
        row = self.db.connection.execute(
            "SELECT subject_id,key FROM memories WHERE id=? AND kind='profile' AND guild_id=?",
            (target_id, str(event.guild_id)),
        ).fetchone()
        if row is None:
            raise PermissionError("Profile target is unavailable")
        return self.update(
            event,
            row["subject_id"],
            "profile",
            row["key"],
            value,
            target_id=target_id,
            context=context,
            relation="replace" if relation == "delete" else relation,
            operation="delete" if relation == "delete" else "set",
            allowed_channels=allowed_channels,
            evidence=evidence,
        )

    def self_memory(self):
        # Administrative legacy catalogue only. Model input uses self_context.
        return [
            {"key": r["key"], "value": r["value"]}
            for r in self.db.connection.execute(
                "SELECT key,value FROM memories WHERE kind='self' AND active=1 ORDER BY updated_at"
            )
        ]

    def self_context(self, guild_id, channels, bot_id, **options):
        return self.own.context(guild_id, channels, bot_id, **options)

    def sources_current(self, events):
        for event in events:
            row = self.db.connection.execute(
                "SELECT * FROM events WHERE id=? AND deleted=0", (str(event.id),)
            ).fetchone()
            if (
                not row
                or not self.db.memory_enabled(event.guild_id, event.author_id)
                or (row["guild_id"], row["channel_id"], row["author_id"], row["text"])
                != (str(event.guild_id), str(event.channel_id), str(event.author_id), event.text)
            ):
                return False
        return True

    def recall(self, guild_id, channels, *, subjects=(), query="", limit=12):
        channels = tuple(str(c) for c in channels)
        if not channels:
            return []
        marks = ",".join("?" for _ in channels)
        resolution, parameters = self.profiles.resolution_query(guild_id, channels)
        wanted, needles = {str(x) for x in subjects}, terms(query)
        subject_marks = ",".join("?" for _ in wanted)
        subject_filter = f"AND m.subject_id IN ({subject_marks})" if wanted else ""
        rows = self.db.connection.execute(
            resolution
            + f""" SELECT m.*,e.created_at AS source_time,e.text AS source_text,e.deleted AS source_deleted,
            e.guild_id AS source_guild,e.channel_id AS source_channel,e.author_id AS actual_source_author FROM memories m
            LEFT JOIN events e ON m.source_id=e.id WHERE m.guild_id=?
            AND m.channel_id IN ({marks}) AND m.kind!='self' AND m.active=1 AND (m.expires_at IS NULL OR julianday(m.expires_at)>julianday(?))
            AND m.id NOT IN (SELECT id FROM replaced) {subject_filter}
            ORDER BY m.updated_at DESC,m.id DESC LIMIT 500""",
            (*parameters, str(guild_id), *channels, self.clock().isoformat(), *wanted),
        ).fetchall()
        candidates = []
        for row in rows:
            if not self.db.memory_enabled(guild_id, row["subject_id"]) or (
                row["source_author"] and not self.db.memory_enabled(guild_id, row["source_author"])
            ):
                continue
            item = dict(row)
            item.pop("source_text", None)
            item.pop("source_deleted", None)
            for field in ("source_guild", "source_channel", "actual_source_author"):
                item.pop(field, None)
            sources = source_rows(self.db, row["id"]) if row["kind"] == "profile" else []
            if not sources_visible(self.db, sources, guild_id, channels):
                continue
            item["source_ids"] = [s["event_id"] for s in sources] or (
                [row["source_id"]] if row["source_id"] else []
            )
            if row["kind"] == "profile":
                item["basis_note"] = BASIS_NOTES.get(row["basis"], BASIS_NOTES["unknown"])
            if wanted and row["subject_id"] not in wanted:
                continue
            score = len(needles & terms(row["key"] + " " + row["context"] + " " + row["value"]))
            if row["kind"] == "event":
                if row["source_deleted"] or (
                    row["source_text"] is not None
                    and (row["source_guild"], row["source_channel"], row["actual_source_author"])
                    != (row["guild_id"], row["channel_id"], row["source_author"])
                ):
                    continue
                # The old value is a search hint, not a verified paraphrase.
                # Original events remain the canonical text and time source.
                item["_index_text"] = row["key"] + " " + row["value"]
                item["_stored_value"] = row["value"]
                item["key"] = f"event:{row['id']}"
                item["value"] = (row["source_text"] or "")[:2000]
                item["source_available"] = row["source_text"] is not None
                item["source_truncated"] = len(row["source_text"] or "") > 2000
                item["source_note"] = (
                    "來源原文；source_time是發話時間，不是事件開始時間。subject_id是檢索對象，不保證原文主詞；依source_author與正文判讀。"
                    if item["source_available"]
                    else "舊事件索引缺少可核對原文；不能據此確認事件內容，需另查來源。"
                )
                score = max(score, len(needles & terms(row["source_text"] or "")))
            item["score"] = score
            item["third_party"] = row["subject_id"] != row["source_author"]
            candidates.append(item)
        self.profiles.annotate(candidates)
        candidates = [item for item in candidates if not needles or item["score"]]
        return sorted(candidates, key=lambda x: (x["score"], x["updated_at"]), reverse=True)[:limit]

    def search_events(self, guild_id, channels, query, limit=8, *, exclude_ids=()):
        channels = tuple(str(c) for c in channels)
        if not channels or not terms(query):
            return []
        marks = ",".join("?" for _ in channels)
        rows = self.db.connection.execute(
            f"""SELECT e.*,COALESCE(o.value,'') AS observation FROM events e
            LEFT JOIN observations o ON e.id=o.event_id AND o.kind='vision' WHERE e.guild_id=?
            AND e.channel_id IN ({marks}) AND e.deleted=0 ORDER BY e.created_at DESC LIMIT 5000""",
            (str(guild_id), *channels),
        ).fetchall()
        needles = terms(query)
        excluded = {str(value) for value in exclude_ids}
        matched = [
            (
                len(
                    needles
                    & terms(
                        r["text"]
                        + " "
                        + r["observation"]
                        + " "
                        + " ".join(x.get("filename", "") for x in json.loads(r["attachments"]))
                    )
                ),
                r,
            )
            for r in rows
            if r["id"] not in excluded and self.db.memory_enabled(guild_id, r["author_id"])
        ]
        matched = sorted(
            (x for x in matched if x[0]), key=lambda x: (x[0], x[1]["created_at"]), reverse=True
        )[:limit]
        return [
            {
                "id": r["id"],
                "channel_id": r["channel_id"],
                "author_id": r["author_id"],
                "text": r["text"][:1800],
                "image_description": r["observation"][:1500],
                "attachment_names": [x.get("filename", "") for x in json.loads(r["attachments"])],
                "created_at": r["created_at"],
                "url": f"https://discord.com/channels/{r['guild_id']}/{r['channel_id']}/{r['id']}",
                "availability": "stored_index_not_live_verified",
            }
            for _, r in matched
        ]

    def search_context(
        self, guild_id, channels, result, *, bot_id, exclude_ids=(), result_budget=10000
    ):
        """Add readable reply quotations; never infer subjects or write memories."""
        from .search_context import enrich_search_result

        return enrich_search_result(
            self.db,
            guild_id,
            channels,
            result,
            now=self.clock(),
            bot_id=bot_id,
            exclude_ids=exclude_ids,
            result_budget=result_budget,
        )

    def summary(self, guild_id, channel_id, ttl_hours, *, allowed_channels=None):
        row = self.db.connection.execute(
            "SELECT * FROM summaries WHERE guild_id=? AND channel_id=?",
            (str(guild_id), str(channel_id)),
        ).fetchone()
        if (
            not row
            or (self.clock() - datetime.fromisoformat(row["updated_at"])).total_seconds()
            > ttl_hours * 3600
        ):
            return ""
        channels = {
            str(c) for c in ([channel_id] if allowed_channels is None else allowed_channels)
        }
        identifiers = json.loads(row["source_ids"])
        for offset in range(0, len(identifiers), 400):
            batch = identifiers[offset : offset + 400]
            marks = ",".join("?" for _ in batch)
            sources = self.db.connection.execute(
                f"SELECT guild_id,channel_id,author_id,deleted FROM events WHERE id IN ({marks})",
                batch,
            ).fetchall()
            if len(sources) != len(batch) or any(
                s["deleted"]
                or s["guild_id"] != str(guild_id)
                or s["channel_id"] not in channels
                or not self.db.memory_enabled(guild_id, s["author_id"])
                for s in sources
            ):
                return ""
        return row["text"]

    def save_summary(
        self, guild_id, channel_id, text, events, *, expected_version=..., retain_previous=False
    ):
        with self.db.connection:
            previous = self.db.connection.execute(
                "SELECT source_ids,updated_at FROM summaries WHERE guild_id=? AND channel_id=?",
                (str(guild_id), str(channel_id)),
            ).fetchone()
            if (
                expected_version is not ...
                and (previous["updated_at"] if previous else None) != expected_version
            ):
                return False
            for event in events:
                if not self.db.memory_enabled(event.guild_id, event.author_id):
                    return False
                row = self.db.connection.execute(
                    "SELECT text,deleted FROM events WHERE id=?", (str(event.id),)
                ).fetchone()
                if not row or row["deleted"] or row["text"] != event.text:
                    return False
            source_ids = {str(e.id) for e in events}
            if retain_previous and previous:
                source_ids.update(json.loads(previous["source_ids"]))
            self.db.connection.execute(
                """INSERT INTO summaries VALUES(?,?,?,?,?)
                ON CONFLICT(guild_id,channel_id) DO UPDATE SET text=excluded.text,
                source_ids=excluded.source_ids,updated_at=excluded.updated_at""",
                (
                    str(guild_id),
                    str(channel_id),
                    text,
                    json.dumps(sorted(source_ids)),
                    self.clock().astimezone(timezone.utc).isoformat(),
                ),
            )
        return True

    def summary_context(self, guild_id, channel_id, ttl_hours, *, allowed_channels=None):
        text = self.summary(guild_id, channel_id, ttl_hours, allowed_channels=allowed_channels)
        if not text:
            return ""
        row = self.db.connection.execute(
            "SELECT source_ids FROM summaries WHERE guild_id=? AND channel_id=?",
            (str(guild_id), str(channel_id)),
        ).fetchone()
        identifiers = json.loads(row["source_ids"])
        timestamps = []
        for offset in range(0, len(identifiers), 400):
            batch = identifiers[offset : offset + 400]
            marks = ",".join("?" for _ in batch)
            sources = self.db.connection.execute(
                f"SELECT created_at FROM events WHERE id IN ({marks}) AND deleted=0", batch
            ).fetchall()
            timestamps.extend(datetime.fromisoformat(source["created_at"]) for source in sources)
        return {
            "text": text,
            "earliest_source_time": min(timestamps).isoformat() if timestamps else None,
            "latest_source_time": max(timestamps).isoformat() if timestamps else None,
        }
