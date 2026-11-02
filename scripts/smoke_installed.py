"""Run with a wheel environment's `python -I`: synthetic data, no network."""

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
from sayuki_bot.conversation.models import Event, Turn
from sayuki_bot.context.compiler import ContextCompiler
from sayuki_bot.llm.client import ModelClient
from sayuki_bot.memory.service import MemoryService
from sayuki_bot.storage.database import Database
from sayuki_bot.tools.executor import ToolExecutor


async def main():
    origin = Path(sayuki_bot.__file__).resolve()
    if not origin.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("Expected a non-editable installed wheel, not source imports")
    with tempfile.TemporaryDirectory(prefix="sayuki-installed-") as directory:
        root = Path(directory).resolve()
        (root / "config").mkdir()
        (root / "persona.txt").write_text("合成測試角色")
        source = root / "config/runtime.toml"
        source.write_text(
            '[paths]\npersona="persona.txt"\ndatabase="data/db.sqlite3"\n[model]\nchat="synthetic-model"\n'
        )
        cfg = load_config(source)
        assert cfg.root == root
        db = Database(cfg.path("database"))
        try:
            memory = MemoryService(db)
            event = Event(
                123, 30, 20, 10, "測試群友", "我喜歡Minecraft", datetime.now(timezone.utc)
            )
            db.record_event(event)
            memory.update(event, 10, "profile", "game", "Minecraft")
            assert memory.recall(30, [20], subjects=[10])[0]["value"] == "Minecraft"
            correction = Event(
                124, 30, 20, 10, "測試群友", "更正，是另一個版本", event.created_at,
                reply_to=123,
            )
            db.record_event(correction)
            search = memory.search_context(
                30, [20], {"status": "ok", "messages": memory.search_events(30, [20], "Minecraft")},
                bot_id=99,
            )
            assert search["reply_context"]["records"][0]["message_id"] == "msg_124"
            assert search["messages"][0]["author"] == "測試群友"
            turn = Turn(event, NS(channel=NS(id=20)), [event], bot_id=99)
            messages = ContextCompiler(cfg, memory).compile(turn, [20])
            query = Event(125, 30, 20, 10, "測試群友", "Minecraft的事", event.created_at)
            query_turn = Turn(query, NS(channel=NS(id=20)), [], bot_id=99)
            initial = ContextCompiler(cfg, memory).compile(query_turn, [20])
            context = json.loads(initial[2]["content"].split("\n", 1)[1])
            assert {r["message_id"] for r in context["conversation_sources"]["records"]} == {
                "msg_123", "msg_124"
            }
            response = NS(choices=[NS(message=NS(content="嗨"), finish_reason="stop")], usage=None)
            sdk = NS(
                chat=NS(completions=NS(create=AsyncMock(return_value=response))), close=AsyncMock()
            )
            model = ModelClient(cfg, db, "synthetic-placeholder", client=sdk)
            generation = await model.generate(messages, turn_id=turn.id)
            assert generation.text == "嗨"
            from sayuki_bot.conversation.attention import Attention
            from sayuki_bot.conversation.participation import classify

            from datetime import timedelta
            own = Event(126, 30, 20, 99, "紗月", "過幾天回桃園拿嗎？", event.created_at)
            ack = Event(127, 30, 20, 10, "測試群友", "對啊", event.created_at + timedelta(seconds=1))
            cfg.sections["sleep"] = {"enabled": False}
            # Check classification separately from the timezone-dependent sleep gate.
            from dataclasses import replace
            awake_cfg = replace(cfg, sleep=replace(cfg.sleep, enabled=False))
            assert Attention(awake_cfg).decide(ack, [own, ack], 99, ack.created_at) == "continuation"
            followup = Turn(ack, NS(channel=NS(id=20)), [own, ack], "continuation", bot_id=99)
            response.choices[0].message.content = '{"should_reply":true,"source_ids":[126]}'
            decision = await classify(awake_cfg, model, followup)
            assert decision["should_reply"] and decision["source_ids"] == [126]
            assert sdk.chat.completions.create.call_args.kwargs["max_tokens"] == 512
            from sayuki_bot.context.cross_channel import discover, envelope

            move = Event(128, 30, 20, 10, "測試群友", "我們去 <#21> 聊",
                         event.created_at + timedelta(seconds=2))
            moved = Event(129, 30, 21, 10, "測試群友", "對啊",
                          event.created_at + timedelta(seconds=3))
            db.record_event(own)
            db.record_event(move)
            cross_turn = Turn(moved, NS(channel=NS(id=21)), [], bot_id=99)
            discover(NS(config=awake_cfg, db=db, history={20: [own, move]},
                        access=NS(allowed_channels=lambda _: [20, 21])), cross_turn)
            cross = envelope(cross_turn, cfg.sleep.timezone)
            assert cross["kind"] == "invited_transfer" and cross["invitation_id"] == 128
            access = NS(authorize=lambda *a: None, channel=lambda *a: None)
            executor = ToolExecutor(NS(get_guild=lambda _: None), cfg, db, memory, model, access)
            result = await executor.execute(turn, "TEXT_TRANSFORM", "sha256 | test")
            assert result["status"] == "ok", result
            await model.close()
            print(
                json.dumps(
                    {
                        "package": str(origin),
                        "python": sys.version,
                        "root_from_config": True,
                        "sqlite_and_memory": "passed",
                        "search_reply_context": "passed",
                        "initial_context_sources": "passed",
                        "ordinary_continuation_installed": "passed",
                        "cross_channel_handoff_installed": "passed",
                        "compiler_and_fake_sdk": "passed",
                        "core_tool_without_math": "passed",
                        "model_calls_real": 0,
                        "discord_calls": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
