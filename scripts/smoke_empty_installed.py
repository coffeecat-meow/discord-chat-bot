"""Use wheel python -I: real engine/model, synthetic SDK and SQLite, no network."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import sayuki_bot
from sayuki_bot.config import load_config
from sayuki_bot.conversation.engine import Engine
from sayuki_bot.conversation.models import Event, Turn
from sayuki_bot.conversation.repair import RepairState
from sayuki_bot.llm.client import EmptyCompletion, ModelClient
from sayuki_bot.storage.database import Database


async def main():
    origin = Path(sayuki_bot.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve())
    with tempfile.TemporaryDirectory(prefix="empty-installed-") as directory:
        root = Path(directory)
        (root / "persona.txt").write_text("合成測試角色")
        (root / "runtime.toml").write_text(
            '[paths]\npersona="persona.txt"\ndatabase="scene.sqlite3"\n'
            '[model]\nchat="synthetic-model"\n'
        )
        cfg = load_config(root / "runtime.toml")
        db = Database(":memory:")
        try:
            def response(text):
                return NS(choices=[NS(message=NS(content=text), finish_reason="stop")],
                          usage=NS(model_dump=lambda: {"completion_tokens": 1}))

            create = AsyncMock(side_effect=[response(None), response("嗨")])
            model = ModelClient(cfg, db, "synthetic-placeholder",
                                NS(chat=NS(completions=NS(create=create))))
            engine = Engine(cfg, None, model, None, NS(db=db), NS(channel=lambda *args: None))
            event = Event(1, 30, 20, 10, "群友", "嗨", datetime.now(timezone.utc))
            turn = Turn(event, None, [], "mention")
            generated = await engine.generate_reply(turn, [{"role": "user", "content": "嗨"}], RepairState())
            assert generated.text == "嗨" and create.await_count == 2 and turn.empty_retries == 1
            rows = db.connection.execute("SELECT data FROM usage").fetchall()
            assert len(rows) == 2 and json.loads(rows[0]["data"])["response_shape"]["content_type"] == "null"
            create.side_effect = [response(None), response(None), response("不該第三次")]
            create.reset_mock()
            try:
                await engine.generate_reply(Turn(event, None, [], "mention"), [], RepairState())
            except EmptyCompletion:
                pass
            else:
                raise AssertionError("Exhausted empty recovery must fail")
            assert create.await_count == 2
            print(json.dumps({"package": str(origin), "empty_then_text": "passed",
                              "bounded_failed_retry": "passed", "usage_and_shape": "passed",
                              "real_model_calls": 0, "discord_calls": 0}, ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
