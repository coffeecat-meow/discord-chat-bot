"""Fallback simulation: installed ModelClient + real AsyncOpenAI SDK + httpx.MockTransport.

Usage: python -I fallback_sim.py <candidate_toml> <verify_dir>
No network, no real keys (fake keys set in-process; real .env never read).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import openai

FAKE = {"OPENCODE_API_KEY": "fake-go-key-sim", "OPENROUTER_API_KEY": "fake-or-key-sim"}
for k in list(os.environ):
    if k in ("OPENCODE_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY", "opencode_api_key"):
        del os.environ[k]
os.environ.update(FAKE)

from sayuki_bot.config import load_config  # noqa: E402
from sayuki_bot.llm.client import ModelClient  # noqa: E402
from sayuki_bot.storage.database import Database  # noqa: E402

candidate, vdir = Path(sys.argv[1]), Path(sys.argv[2])
config = load_config(candidate)
# Redirect only the DB to an isolated file; routes stay exactly as the candidate.
config.sections["paths"]["database"] = str((vdir / "fallback-sim.sqlite3").resolve())
assert "sayuki.sqlite3" not in config.sections["paths"]["database"].split("/")[-1]

seen = []
mode = {"go": "ok", "or": "ok"}


def ok(model):
    return httpx.Response(200, json={
        "id": "sim", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": f"reply-from-{model}"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


async def handler(request):
    body = json.loads(request.content)
    host = request.url.host
    auth = request.headers.get("authorization", "")
    seen.append({
        "host": host, "model": body.get("model"),
        "key": "go" if auth == f"Bearer {FAKE['OPENCODE_API_KEY']}" else
               "or" if auth == f"Bearer {FAKE['OPENROUTER_API_KEY']}" else "other",
        "ua": request.headers.get("user-agent", "")[:24],
        "session_hdr": "x-opencode-session" in request.headers,
        "thinking": body.get("thinking"), "reasoning_effort": body.get("reasoning_effort"),
        "reasoning": body.get("reasoning"), "max_tokens": body.get("max_tokens"),
    })
    side = "go" if host == "opencode.ai" else "or" if host == "openrouter.ai" else None
    if side is None:
        raise AssertionError(f"unexpected host {host}")
    m = mode[side]
    if m == "ok":
        return ok(body.get("model"))
    if m == "connect":
        raise httpx.ConnectError("simulated connect failure", request=request)
    if m == "503":
        return httpx.Response(503, json={"error": {"message": "simulated outage"}})
    if m == "hang":
        await asyncio.sleep(3600)
    raise AssertionError(m)


real_factory = openai.AsyncOpenAI


def mock_factory(**options):
    options["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return real_factory(**options)


openai.AsyncOpenAI = mock_factory
MSG = [{"role": "user", "content": "synthetic"}]
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


async def run(db, mc, role="chat", turn="t"):
    seen.clear()
    db.connection.execute("DELETE FROM traces")
    db.connection.commit()
    try:
        g = await mc.generate(MSG, turn_id=turn, role=role)
        return g, None
    except BaseException as exc:  # noqa: BLE001
        return None, exc


def stages(db):
    return [(r["stage"], json.loads(r["data"])) for r in db.connection.execute("SELECT stage,data FROM traces ORDER BY id")]


async def main():
    db = Database(config.path("database"))
    mc = ModelClient(config, db, FAKE["OPENCODE_API_KEY"])

    # 1. primary success: no fallback call
    mode["go"], mode["or"] = "ok", "ok"
    g, e = await run(db, mc)
    check("1 primary success -> Go only, OR not called",
          e is None and [s["host"] for s in seen] == ["opencode.ai"] and g.text == "reply-from-deepseek-v4.1-flash",
          f"hosts={[s['host'] for s in seen]}")

    # 2. primary connection failure -> exactly one OR call
    mode["go"], mode["or"] = "connect", "ok"
    g, e = await run(db, mc)
    hosts = [s["host"] for s in seen]
    st = stages(db)
    check("2 Go connect error -> one OR fallback, success from OR",
          e is None and hosts == ["opencode.ai", "openrouter.ai"] and g.text == "reply-from-deepseek/deepseek-v4.1-flash:floor",
          f"hosts={hosts}")
    fo = [d for s, d in st if s == "model_failover"]
    check("2b model_failover trace recorded, no key inside",
          len(fo) == 1 and fo[0].get("from_endpoint") == "https://opencode.ai/zen/go/v1"
          and fo[0].get("to_endpoint") == "https://openrouter.ai/api/v1"
          and "fake-" not in json.dumps(st),
          json.dumps(fo[0] if fo else {}, ensure_ascii=False))
    go_req, or_req = seen[0], seen[1]
    check("2c Go request: own model/key/UA/session header, no OR fields",
          go_req["model"] == "deepseek-v4.1-flash" and go_req["key"] == "go"
          and go_req["ua"].startswith("sayuki-discord/4.6") and go_req["session_hdr"] and go_req["reasoning"] is None)
    check("2d OR request: own model/key, no Go UA/session header",
          or_req["model"] == "deepseek/deepseek-v4.1-flash:floor" and or_req["key"] == "or"
          and not or_req["ua"].startswith("sayuki-discord") and not or_req["session_hdr"]
          and or_req["thinking"] is None, json.dumps(or_req))

    # 3. primary API 503 -> one OR call
    mode["go"], mode["or"] = "503", "ok"
    g, e = await run(db, mc)
    hosts = [s["host"] for s in seen]
    fo = [d for s, d in stages(db) if s == "model_failover"]
    check("3 Go HTTP 503 -> one OR fallback", e is None and hosts == ["opencode.ai", "openrouter.ai"]
          and fo and fo[0].get("status_code") == 503, f"hosts={hosts} status={fo[0].get('status_code') if fo else None}")

    # 4. per-format params on fallback: attention and memory roles
    mode["go"], mode["or"] = "connect", "ok"
    g, e = await run(db, mc, role="attention")
    check("4a attention: Go thinking.disabled / OR reasoning.enabled=false",
          e is None and seen[0]["thinking"] == {"type": "disabled"} and seen[1]["reasoning"] == {"enabled": False}
          and seen[1]["thinking"] is None, f"go={seen[0]['thinking']} or={seen[1]['reasoning']}")
    g, e = await run(db, mc, role="memory")
    check("4b memory: Go reasoning_effort=low / OR reasoning.effort=low, 8192 tokens",
          e is None and seen[0]["reasoning_effort"] == "low" and seen[1]["reasoning"] == {"effort": "low"}
          and seen[1]["reasoning_effort"] is None and seen[0]["max_tokens"] == seen[1]["max_tokens"] == 8192,
          f"go={seen[0]['reasoning_effort']} or={seen[1]['reasoning']}")

    # 5. cancellation -> no fallback
    mode["go"], mode["or"] = "hang", "ok"
    seen.clear()
    task = asyncio.create_task(mc.generate(MSG, turn_id="cancel", role="chat"))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    check("5 cancel during Go call -> CancelledError, OR not called",
          cancelled and [s["host"] for s in seen] == ["opencode.ai"], f"hosts={[s['host'] for s in seen]}")

    # 6. both fail -> raises, no fake success
    mode["go"], mode["or"] = "connect", "503"
    g, e = await run(db, mc)
    st = [s for s, _ in stages(db)]
    check("6 Go+OR both fail -> exception, no reply, fallback_failed trace",
          g is None and e is not None and "model_failover" in st and "model_fallback_failed" in st,
          f"error={type(e).__name__} traces={st}")

    await mc.close()
    db.close()
    print(f"SUMMARY {sum(ok for _, ok in results)}/{len(results)} passed")
    sys.exit(0 if all(ok for _, ok in results) else 1)


asyncio.run(main())
