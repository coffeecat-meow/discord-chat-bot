"""Real Go probe through installed ModelClient. Go-only config copy, isolated DB.

Usage: python -I go_probe.py <deploy_root> <probe_dir>
Prints only non-secret fields. Hard cap: 3 HTTP requests, opencode.ai only.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import openai
from dotenv import dotenv_values

from sayuki_bot.config import load_config
from sayuki_bot.llm.client import ModelClient
from sayuki_bot.llm.routing import route
from sayuki_bot.storage.database import Database

root, probe = Path(sys.argv[1]), Path(sys.argv[2])
MAX_CALLS = 3
calls = []

# Only the Go key enters this process; OR key is deliberately absent.
for k in [k for k in os.environ if k in ("OPENROUTER_API_KEY", "OPENCODE_API_KEY", "LLM_API_KEY")]:
    del os.environ[k]
go_key = dotenv_values(root / ".env").get("OPENCODE_API_KEY") or ""
assert go_key, "OPENCODE_API_KEY empty"
os.environ["OPENCODE_API_KEY"] = go_key


async def on_request(request):
    if request.url.host != "opencode.ai":
        raise RuntimeError(f"probe refused non-Go host {request.url.host}")
    if len(calls) >= MAX_CALLS:
        raise RuntimeError("probe call budget exhausted")
    body = json.loads(request.content or b"{}")
    calls.append(
        {
            "method": request.method,
            "host": request.url.host,
            "path": request.url.path,
            "user_agent": request.headers.get("user-agent"),
            "x_opencode_session_present": "x-opencode-session" in request.headers,
            "authorization_present": "authorization" in request.headers,
            "body_model": body.get("model"),
            "body_keys": sorted(k for k in body if k != "messages"),
            "max_tokens": body.get("max_tokens"),
            "temperature": body.get("temperature"),
            "thinking": body.get("thinking"),
            "reasoning_effort": body.get("reasoning_effort"),
            "reasoning": body.get("reasoning"),
        }
    )


async def on_response(response):
    await response.aread()
    rec = calls[-1]
    rec["status"] = response.status_code
    try:
        data = response.json()
    except Exception:
        rec["response_json"] = False
        return
    rec["response_model"] = data.get("model")
    ch = (data.get("choices") or [{}])[0]
    rec["response_finish_reason"] = ch.get("finish_reason")
    if "error" in data:
        err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
        rec["error_type"] = err.get("type") or err.get("code")
        rec["error_message"] = str(err.get("message", ""))[:160]


real_factory = openai.AsyncOpenAI


def observed_factory(**options):
    # Same official options (key, base_url, timeout, max_retries, headers); adds observer only.
    options["http_client"] = httpx.AsyncClient(
        timeout=options.get("timeout"),
        event_hooks={"request": [on_request], "response": [on_response]},
    )
    return real_factory(**options)


openai.AsyncOpenAI = observed_factory

MESSAGES = [
    {"role": "system", "content": "Connectivity probe. Reply with exactly: OK"},
    {"role": "user", "content": "Reply with exactly OK and nothing else."},
]


def transient(exc):
    return isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, TimeoutError)) or (
        isinstance(exc, openai.APIStatusError) and exc.status_code >= 500
    )


async def one(mc, role, turn):
    try:
        g = await mc.generate(MESSAGES, turn_id=turn, role=role)
        return {"ok": True, "text": g.text.strip()[:40], "text_chars": len(g.text.strip()),
                "finish_reason": g.finish_reason,
                "usage_tokens": {k: g.usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}
    except Exception as exc:
        return {"ok": False, "error_class": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None), "transient": transient(exc)}


async def main():
    config = load_config(probe / "config/runtime.toml")
    assert config.path("database").resolve().is_relative_to(probe.resolve())
    for r in ("chat", "attention"):
        assert route(config, r).host == "opencode.ai"
        assert not route(config, r, "fallback").enabled
    db = Database(config.path("database"))
    mc = ModelClient(config, db, go_key)
    print("sdk_client_base_url:", str(mc.client.base_url))
    results = []
    try:
        res = await one(mc, "chat", "probe-chat-1")
        results.append(("chat#1", res))
        if not res["ok"] and res["transient"]:
            res = await one(mc, "chat", "probe-chat-2")
            results.append(("chat#2 (retry)", res))
        if res["ok"] and len(calls) < MAX_CALLS:
            results.append(("attention#1", await one(mc, "attention", "probe-attention-1")))
    finally:
        await mc.close()
    for name, res in results:
        print(name, json.dumps(res, ensure_ascii=False))
    print("http_calls:", len(calls))
    for c in calls:
        print(" ", json.dumps(c, ensure_ascii=False))
    for row in db.connection.execute("SELECT turn_id,role,model,round(duration_ms) d,data FROM usage ORDER BY id"):
        d = json.loads(row["data"])
        print(" usage", row["turn_id"], row["role"], row["model"], f'{row["d"]}ms',
              {k: d.get(k) for k in ("route", "endpoint", "finish_reason", "error_type")})
    for row in db.connection.execute("SELECT turn_id,stage FROM traces ORDER BY id"):
        print(" trace", row["turn_id"], row["stage"])
    db.close()


asyncio.run(main())
