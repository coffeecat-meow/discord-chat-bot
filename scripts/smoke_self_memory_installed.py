"""Run with non-editable wheel python -I; synthetic SQLite, no SDK/network."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

import sayuki_bot
from sayuki_bot.context.compiler import SELF_POLICY
from sayuki_bot.conversation.models import Event
from sayuki_bot.memory.governance import Governance
from sayuki_bot.memory.service import MemoryService
from sayuki_bot.storage.database import Database


def main():
    origin = Path(sayuki_bot.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve())
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="self-installed-") as directory:
        path = Path(directory) / "scene.sqlite3"
        db = Database(path)
        try:
            memory = MemoryService(db)
            memory.clock = lambda: now
            bot = Event(100, 30, 20, 99, "紗月", "我比較喜歡合作遊戲", now)
            user = Event(101, 30, 20, 10, "群友", "下次一起玩吧", now)
            for event in (bot, user):
                db.record_event(event)
            result = memory.own.write(user, 99, "preference", bot.text, [bot], [20])
            assert result["category"] == "preference" and result["sources"][0]["self"] is True
            assert memory.self_context(30, [21], 99)["entries"] == []
            assert len(memory.self_context(30, [20, 21], 99)["entries"]) == 1
            try:
                memory.own.write(user, 99, "core", "25歲黑髮", [bot], [20])
            except ValueError:
                pass
            else:
                raise AssertionError("Core mutation unexpectedly accepted")
            assert "固定核心" in SELF_POLICY and "輕量日常RP" in SELF_POLICY
        finally:
            db.close()
        db = Database(path)
        try:
            memory = MemoryService(db)
            assert memory.self_context(30, [20], 99)["entries"][0]["value"] == bot.text
            Governance(db).set_enabled(30, 10, False)
            assert memory.self_context(30, [20], 99)["entries"] == []
            Governance(db).set_enabled(30, 10, True)
            db.invalidate_event(100, deleted=True)
            assert memory.self_context(30, [20], 99)["entries"] == []
            with db.connection:
                db.connection.executemany(
                    "INSERT INTO memories(guild_id,channel_id,subject_id,kind,key,value,updated_at) "
                    "VALUES('30','20','99','self',?,?,?)",
                    [(f"old-{i}", "舊資料" * 500, now.isoformat()) for i in range(40)],
                )
            context = memory.self_context(30, [20], 99)
            assert len(json.dumps(context, ensure_ascii=False)) <= 2000
            assert context["omitted"] > 0
            assert all(r["category"] == "legacy_unclassified" for r in context["entries"])
            print(json.dumps({"package": str(origin), "typed_source_write": "passed",
                "fixed_core_no_write_route": "passed", "shared_scope": "passed",
                "reopen_metadata": "passed", "privacy_and_invalidation": "passed",
                "legacy_budget": "passed", "paid_calls": 0, "discord_calls": 0}, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
