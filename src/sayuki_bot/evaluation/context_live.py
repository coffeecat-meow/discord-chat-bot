"""C3 bridge: a keyless receive-path worker, driven by the proven bounded pipe loop."""

import asyncio
import concurrent.futures
from pathlib import Path
import threading

from .legacy_v4 import StopSignal, _stream_flow_worker
from .live import BudgetStopped
from sayuki_bot.llm.client import response_shape


async def run_context_worker(
    *,
    case,
    version_root,
    harness_root,
    transport,
    replicate,
    pacing="real",
    timeout=240,
    journal=None,
):
    loop = asyncio.get_running_loop()
    inflight = set()
    lock = threading.Lock()
    stop = StopSignal()
    finished = asyncio.Event()
    holder = {}
    partial = {}

    async def complete(record):
        response = await transport.call({**record["request"], "messages": record["messages"]})
        choice = response.choices[0] if response.choices else None
        return {
            "kind": "model_response",
            "text": getattr(getattr(choice, "message", None), "content", None),
            "response_shape": response_shape(response),
            "finish_reason": getattr(choice, "finish_reason", None),
            "usage": response.usage.model_dump() if response.usage else {},
        }

    def model_request(record, remaining):
        future = asyncio.run_coroutine_threadsafe(complete(record), loop)
        with lock:
            inflight.add(future)
        try:
            return future.result(timeout=remaining)
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            raise asyncio.CancelledError()
        except BudgetStopped as exc:
            return {"kind": "model_stopped", "reason": str(exc)}
        except Exception as exc:
            future.cancel()
            return {"kind": "model_stopped", "reason": type(exc).__name__}
        finally:
            with lock:
                inflight.discard(future)

    def abandon():
        with lock:
            for future in tuple(inflight):
                future.cancel()

    model_request.abandon = abandon

    def progress(record):
        partial.clear()
        partial.update(record["result"])
        if journal:
            journal({"kind": "case-progress", **transport.identity, "partial": dict(partial)})

    def drive():
        try:
            holder["value"] = _stream_flow_worker(
                {
                    "case": case,
                    "version_root": str(version_root),
                    "harness_root": str(harness_root),
                    "model": transport.model,
                    "replicate": replicate,
                    "pacing": pacing,
                    "case_timeout": timeout,
                },
                Path(version_root),
                timeout=timeout,
                on_model_request=model_request,
                on_progress=progress,
                stop_event=stop,
                worker_script=Path(harness_root) / "scripts/context_live_worker.py",
            )
        except BaseException as exc:
            holder["error"] = exc
        finally:
            loop.call_soon_threadsafe(finished.set)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    try:
        await finished.wait()
        if "error" in holder:
            raise holder["error"]
        values = holder["value"]["results"]
        if not values:
            raise RuntimeError("Worker returned no case record")
        return values[0]
    except BaseException:
        stop.set()
        abandon()
        await asyncio.to_thread(thread.join, 10)
        leftover = await asyncio.to_thread(stop.confirm_cleanup, 1)
        if journal:
            journal(
                {
                    "kind": "case-interrupted",
                    **transport.identity,
                    "partial": dict(partial),
                    "cleanup_confirmed": not leftover and not thread.is_alive(),
                }
            )
        raise
