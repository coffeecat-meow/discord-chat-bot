"""Typed, source-bound self memory. Core persona has no mutation route here."""

from __future__ import annotations

import hashlib
import json
import re

from .evidence import source_rows, sources_visible, validate_sources

CATEGORIES = {"preference", "conversation"}
# Conservative validation of preference quotes, not a general semantic oracle.
PREFERENCE = re.compile(
    r"(?:喜歡|偏好|愛喝|愛吃|討厭|不喜歡|\b(?:like|prefer|love|dislike)\b)", re.I
)
CORE = re.compile(
    r"(?:年齡|\d+\s*歲|身分|身份|外觀|髮|眼睛|性格|角色設定|角色卡|覆寫|改寫|忽略.*(?:規則|指令)|\b(?:age|identity|personality|hair|eyes|override)\b)",
    re.I,
)
PAST = re.compile(
    r"(?:昨晚|昨天|前天|上週|上周|上次|去年|去過|去了|\b(?:yesterday|last night)\b)", re.I
)


def handle(value):
    match = re.fullmatch(r"self:([1-9][0-9]*)", str(value))
    if not match:
        raise ValueError("自身條目須用讀取結果的 self:ID，不猜舊key")
    return int(match[1])


class SelfMemory:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def target(self, entry_id, guild_id, channels, bot_id):
        row = self.db.connection.execute(
            "SELECT * FROM memories WHERE id=? AND kind='self' AND subject_id=? AND guild_id=?",
            (handle(entry_id), str(bot_id), str(guild_id)),
        ).fetchone()
        if row is None or row["channel_id"] not in {str(c) for c in channels}:
            raise PermissionError("自身條目不在可讀範圍")
        dependencies = source_rows(self.db, row["id"])
        if not sources_visible(self.db, dependencies, guild_id, channels):
            raise PermissionError("自身條目的來源已不可讀")
        return row

    def write(self, event, bot_id, category, content, evidence, channels, *, entry_id=None):
        if category not in CATEGORIES:
            raise ValueError("自身記憶只接受 preference 或 conversation；固定核心不能改寫")
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            raise ValueError("自身內容須為1–2000字元")
        evidence = tuple({e.id: e for e in evidence}.values())
        if not 1 <= len(evidence) <= 4:
            raise ValueError("自身記憶須明確指定1–4則來源")
        if not self.db.memory_enabled(event.guild_id, bot_id):
            raise PermissionError("自身記憶已停用")
        dependencies = validate_sources(self.db, event, evidence, channels)
        if any(e.created_at > self.clock() for e in dependencies):
            raise ValueError("自身來源時間在未來")
        own = [e for e in evidence if str(e.author_id) == str(bot_id)]
        if category == "preference":
            if (
                len(content) > 200
                or not PREFERENCE.search(content)
                or CORE.search(content)
                or PAST.search(content)
            ):
                raise ValueError("偏好須是簡短原話，不能混入核心改寫或過去日常；可縮小引用")
            if not any(content in e.text for e in own):
                raise ValueError("偏好須逐字引用你自己的來源訊息，不能用群友轉述或模型摘要")
        elif (
            len(evidence) < 2
            or not own
            or not any(str(e.author_id) != str(bot_id) for e in evidence)
        ):
            raise ValueError("對話經歷至少需你與群友各一則原訊息；單獨即興日常不是經歷")
        old = self.target(entry_id, event.guild_id, channels, bot_id) if entry_id else None
        key = (
            old["key"]
            if old
            else "self:"
            + hashlib.sha256(
                json.dumps(
                    [category, content, sorted(e.id for e in evidence)], ensure_ascii=False
                ).encode()
            ).hexdigest()[:24]
        )
        with self.db.connection:
            if old:
                self.db.connection.execute(
                    "UPDATE memories SET value=?,source_id=?,source_author=?,updated_at=?,active=1 WHERE id=?",
                    (
                        content,
                        str(event.id),
                        str(event.author_id),
                        self.clock().isoformat(),
                        old["id"],
                    ),
                )
                identifier = old["id"]
            else:
                self.db.connection.execute(
                    """INSERT INTO memories(guild_id,channel_id,subject_id,kind,key,value,source_id,source_author,updated_at,basis)
                    VALUES(?,?,?,'self',?,?,?,?,?,'unknown') ON CONFLICT(guild_id,channel_id,subject_id,kind,key)
                    WHERE kind!='profile' DO UPDATE SET value=excluded.value,source_id=excluded.source_id,
                    source_author=excluded.source_author,updated_at=excluded.updated_at,active=1""",
                    (
                        str(event.guild_id),
                        str(event.channel_id),
                        str(bot_id),
                        key,
                        content,
                        str(event.id),
                        str(event.author_id),
                        self.clock().isoformat(),
                    ),
                )
                identifier = self.db.connection.execute(
                    "SELECT id FROM memories WHERE guild_id=? AND channel_id=? AND subject_id=? AND kind='self' AND key=?",
                    (str(event.guild_id), str(event.channel_id), str(bot_id), key),
                ).fetchone()[0]
            self.db.connection.execute(
                "INSERT INTO self_memory_meta VALUES(?,?,?) ON CONFLICT(memory_id) DO UPDATE SET category=excluded.category,evidence_ids=excluded.evidence_ids",
                (identifier, category, json.dumps([str(e.id) for e in evidence])),
            )
            self.db.connection.execute(
                "DELETE FROM memory_sources WHERE memory_id=?", (identifier,)
            )
            self.db.connection.executemany(
                "INSERT INTO memory_sources VALUES(?,?)",
                [(identifier, str(e.id)) for e in dependencies],
            )
        return {"status": "ok", **self.record(identifier, event.guild_id, channels, bot_id)}

    def delete(self, event, bot_id, entry_id, channels):
        validate_sources(self.db, event, [], channels)
        row = self.target(entry_id, event.guild_id, channels, bot_id)
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE memories SET active=0,value='' WHERE id=?", (row["id"],)
            )
        return {"status": "ok", "operation": "delete", "id": f"self:{row['id']}"}

    def record(self, identifier, guild_id, channels, bot_id):
        row = self.target(f"self:{identifier}", guild_id, channels, bot_id)
        if not row["active"] or not self.db.memory_enabled(guild_id, bot_id):
            raise PermissionError("自身條目未啟用")
        if row["source_author"] and not self.db.memory_enabled(guild_id, row["source_author"]):
            raise PermissionError("自身來源作者已停用記憶")
        meta = self.db.connection.execute(
            "SELECT * FROM self_memory_meta WHERE memory_id=?", (identifier,)
        ).fetchone()
        result = {
            "id": f"self:{identifier}",
            "category": "legacy_unclassified",
            "updated_at": row["updated_at"],
        }
        if meta is None:
            result.update(
                value=row["value"][:600],
                truncated=len(row["value"]) > 600,
                interpretation="未分類舊自身資料；不認證為經歷，也不能覆寫固定核心。",
            )
            # A legacy anchor is still a dependency when one exists.
            if row["source_id"]:
                anchors = self.db.connection.execute(
                    "SELECT * FROM events WHERE id=?", (row["source_id"],)
                ).fetchall()
                if not anchors or not sources_visible(
                    self.db, source_rows_for_events(anchors), guild_id, channels
                ):
                    raise PermissionError("舊自身來源已不可讀")
            return result
        result["category"] = meta["category"]
        ids = json.loads(meta["evidence_ids"])
        if not ids or len(ids) > 4:
            raise ValueError("Invalid self evidence")
        quotes = [
            self.db.connection.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone()
            for identifier in ids
        ]
        if any(q is None for q in quotes) or not sources_visible(
            self.db, source_rows_for_events(quotes), guild_id, channels
        ):
            raise PermissionError("自身原文不可讀")
        result["sources"] = [
            {
                "message_id": "msg_" + q["id"],
                "author_id": q["author_id"],
                "self": q["author_id"] == str(bot_id),
                "channel_id": q["channel_id"],
                "time": q["created_at"],
                "text": q["text"][:600],
                "truncated": len(q["text"]) > 600,
            }
            for q in quotes
        ]
        if meta["category"] == "preference":
            if not any(q["author_id"] == str(bot_id) and row["value"] in q["text"] for q in quotes):
                raise ValueError("Preference quote is stale")
            result["value"] = row["value"]
        result["interpretation"] = (
            "自身偏好原話，不改變固定核心。"
            if meta["category"] == "preference"
            else "只證實曾有這段對話，不證明話中描述的私人活動真的發生；依原文解讀。"
        )
        return result

    def context(self, guild_id, channels, bot_id, *, budget=2000, limit=12, query=""):
        budget, limit = max(400, min(20000, int(budget))), max(1, min(100, int(limit)))
        channels = tuple(str(c) for c in channels)
        output = {"entries": [], "omitted": 0, "scan_limited": False}
        if not channels or bot_id is None:
            return output
        rows = self.db.connection.execute(
            f"""SELECT id,key,value FROM memories WHERE kind='self' AND active=1 AND subject_id=?
            AND guild_id=? AND channel_id IN ({",".join("?" for _ in channels)}) ORDER BY updated_at DESC,id DESC LIMIT 501""",
            (str(bot_id), str(guild_id), *channels),
        ).fetchall()
        output["scan_limited"] = len(rows) > 500
        for row in rows[:500]:
            try:
                record = self.record(row["id"], guild_id, channels, bot_id)
            except (ValueError, PermissionError, TypeError, KeyError):
                continue
            if query:
                from .service import terms

                if not terms(query) & terms(
                    row["key"] + " " + row["value"] + json.dumps(record, ensure_ascii=False)
                ):
                    continue
            if len(output["entries"]) >= limit:
                output["omitted"] += 1
                continue
            trial = {**output, "entries": [*output["entries"], record]}
            # Reserve enough room for the maximum omission counter.
            if len(json.dumps({**trial, "omitted": 500}, ensure_ascii=False)) <= budget:
                output["entries"].append(record)
            else:
                output["omitted"] += 1
        return output


def source_rows_for_events(rows):
    return [
        {
            "guild_id": r["guild_id"],
            "channel_id": r["channel_id"],
            "author_id": r["author_id"],
            "deleted": r["deleted"],
        }
        for r in rows
    ]
