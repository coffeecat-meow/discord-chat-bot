"""Real-model transport, pairing and budget control for the two-version comparison.

The offline entries stay the default: this module is only reached when a caller
asks for live mode explicitly. What it adds is the part an offline replay cannot
provide — a real completion behind the same flow both versions already run, with
every request, setting, usage figure and stop reason written down as it happens.

Three properties this file exists to keep:

* **The key stays in this process.** 4.6 talks to the transport directly; 4.0
  runs in its isolated child and asks the parent for each completion over the
  pipe, so the child never holds a key or reaches the network. No key is written
  to a report, a checkpoint, a command line or an error message.
* **The batch can stop and stay stopped.** Request count, reported spend and
  unknown usage are all stop conditions, and the signal is a ``BaseException`` so
  4.0's own ``except Exception`` retry cannot absorb it into fallback prose.
* **Nothing is rounded up to look finished.** Missing usage is recorded as
  unknown rather than zero, and an unverified provider is reported as unverified
  rather than assumed.

Field names follow the current OpenRouter documentation: usage accounting is
returned automatically (the old ``usage: {include: true}`` switch is deprecated),
provider routing uses ``order``/``allow_fallbacks``/``max_price``, and the serving
provider is confirmed through ``GET /api/v1/generation`` (``provider_name``),
because the chat-completion response schema does not document a provider field.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4
from sayuki_bot.llm.client import response_shape

# The plan's ceiling for one batch; a smaller limit can be passed for a smoke.
DEFAULT_MAX_CALLS = 48
DEFAULT_STOP_COST = 0.25
# Price ceiling carried over from the existing single-version evaluation.
MAX_PRICE = {"prompt": 0.3, "completion": 1.2}


class BudgetStopped(BaseException):
    """A batch-level stop: request ceiling, reported spend, or unknown usage."""


@dataclass
class LiveBudget:
    """Counts every real model call, including continuations and retries."""

    max_calls: int = DEFAULT_MAX_CALLS
    stop_cost: float = DEFAULT_STOP_COST
    attempted_calls: int = 0
    reported_cost: float = 0.0
    unknown_usage: bool = False
    stopped_reason: str | None = None

    def blocking_reason(self) -> str | None:
        if self.stopped_reason:
            return self.stopped_reason
        if self.attempted_calls >= self.max_calls:
            return f"request ceiling reached ({self.attempted_calls}/{self.max_calls})"
        if self.reported_cost >= self.stop_cost:
            return f"reported spend reached the stop threshold (${self.reported_cost:.4f})"
        if self.unknown_usage:
            return "required usage or cost was not reported for an earlier call"
        return None

    def check(self) -> None:
        reason = self.blocking_reason()
        if reason:
            self.stopped_reason = reason
            raise BudgetStopped(reason)

    def record_attempt(self) -> None:
        self.attempted_calls += 1

    def record_usage(self, record: dict) -> None:
        """Take the normalised usage record, so budget and report never disagree.

        Keep whatever cost was reported even when the record is incomplete, then
        stop: a batch cannot keep spending once it can no longer account for what
        it spent. Optional extras (cached, reasoning) are not required, and a
        genuine 0 is a number, not a gap.
        """
        cost = record.get("cost")
        if isinstance(cost, (int, float)):
            self.reported_cost += float(cost)
        if record.get("status") != "reported":
            self.unknown_usage = True

    def snapshot(self) -> dict:
        return {
            "attempted_calls": self.attempted_calls,
            "max_calls": self.max_calls,
            "reported_cost": round(self.reported_cost, 6),
            "stop_cost": self.stop_cost,
            "unknown_usage": self.unknown_usage,
            "stopped_reason": self.stopped_reason,
        }


# What the batch must have to keep accounting for itself. Cached and reasoning
# counts are extras: not every provider reports them, and their absence is not a
# reason to stop.
REQUIRED_USAGE = ("prompt_tokens", "completion_tokens", "cost")


def usage_record(usage: dict | None) -> dict:
    """Pull the documented usage fields; absent numbers stay unknown, never zero."""
    if not usage:
        return {
            "status": "unknown",
            "reason": "the response carried no usage object",
            "missing": list(REQUIRED_USAGE),
        }
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    cost_details = usage.get("cost_details") or {}
    record = {
        "status": "reported",
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": prompt_details.get("cached_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "cost": usage.get("cost"),
        "upstream_inference_cost": cost_details.get("upstream_inference_cost"),
    }
    # `is None`, not falsiness: a reported 0 is a real number.
    missing = [name for name in REQUIRED_USAGE if record[name] is None]
    if missing:
        record["status"] = "partial"
        record["missing"] = missing
        record["reason"] = "required usage fields were not reported: " + ", ".join(missing)
    return record


def provider_routing(provider: str | None) -> dict:
    """Pin one provider and switch fallbacks off, per the routing documentation."""
    routing = {"max_price": dict(MAX_PRICE)}
    if provider:
        routing["order"] = [provider]
        routing["allow_fallbacks"] = False
    return routing


@dataclass
class LiveTransport:
    """SDK-shaped transport shared by both versions.

    4.6's ``ModelClient`` calls ``chat.completions.create`` on it; 4.0's isolated
    child reaches the same ``call()`` through the parent proxy, so both versions
    get identical routing, accounting and stop behaviour.
    """

    client: object
    model: str
    budget: LiveBudget
    provider: str | None = None
    journal: object = None
    verify_provider: bool = False
    identity: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    stopped: str | None = None

    def __post_init__(self):
        self.chat = SimpleNamespace(completions=self)

    def for_case(self, *, journal=None, **identity) -> LiveTransport:
        """A transport bound to one case/version/replicate.

        The budget is shared so the ceiling applies across the whole batch;
        `journal` is explicit rather than swallowed into the identity.
        """
        bound = LiveTransport(
            client=self.client,
            model=self.model,
            budget=self.budget,
            provider=self.provider,
            journal=journal or self.journal,
            verify_provider=self.verify_provider,
            identity={**self.identity, **identity},
        )
        # `requests` stays per-bound: sharing it would make 4.6 record whatever
        # 4.0 sent last. The ledger is the batch-wide view.
        return bound

    async def create(self, **kwargs):
        return await self.call(kwargs)

    async def call(self, kwargs: dict):
        from sayuki_bot.evaluation.legacy_v4 import request_fingerprint

        round_number = len(self.requests) + 1
        # One id per request, so several events can be related back to one call.
        request_id = uuid4().hex
        try:
            self.budget.check()
        except BudgetStopped as stop:
            self.stopped = str(stop)
            self._write(
                {
                    "event": "completion",
                    "request_id": request_id,
                    "round": round_number,
                    "status": "not_sent",
                    "reason": str(stop),
                    "settings": {
                        name: kwargs.get(name)
                        for name in ("model", "max_tokens", "temperature", "extra_body")
                    },
                    "fingerprint": request_fingerprint(kwargs),
                }
            )
            raise

        kwargs = dict(kwargs)
        extra = dict(kwargs.get("extra_body") or {})
        extra["provider"] = provider_routing(self.provider)
        kwargs["extra_body"] = extra
        # Taken after the routing is injected, so the fingerprint describes what
        # was actually sent rather than what was proposed.
        fingerprint = request_fingerprint(kwargs)
        settings = {
            name: kwargs.get(name) for name in ("model", "max_tokens", "temperature", "extra_body")
        }

        self.budget.record_attempt()
        started = perf_counter()
        try:
            response = await self.client.chat.completions.create(**kwargs)
        except BaseException as exc:
            # The call may or may not have been billed, so the batch stops here.
            self.budget.unknown_usage = True
            self.stopped = f"{type(exc).__name__}: {exc}"
            self._write(
                {
                    "event": "completion",
                    "request_id": request_id,
                    "round": round_number,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "settings": settings,
                    "fingerprint": fingerprint,
                    "usage": {"status": "unknown", "reason": "the request raised"},
                }
            )
            raise
        duration_ms = round((perf_counter() - started) * 1000, 3)
        usage = response.usage.model_dump() if getattr(response, "usage", None) else {}
        record = usage_record(usage)
        self.budget.record_usage(record)
        choice = response.choices[0] if response.choices else None
        self.requests.append(fingerprint)
        # Only the documented lookup awaits anything, so only it can be cancelled
        # mid-verification. Without it there is no window to protect and the
        # provider (unverified) rides along on the single completion event.
        inline_provider = None if self.verify_provider else await self._provider_of(response)
        # The completion is saved *before* verification is even attempted: a
        # cancellation while waiting on metadata must not erase a request that
        # already happened and was already charged.
        self._write(
            {
                "event": "completion",
                "request_id": request_id,
                "round": round_number,
                "status": "ok",
                "provider": inline_provider,
                "duration_ms": duration_ms,
                "settings": settings,
                "fingerprint": fingerprint,
                "response_id": getattr(response, "id", None),
                "model_returned": getattr(response, "model", None),
                "finish_reason": getattr(choice, "finish_reason", None),
                "usage": record,
                # The raw model text is kept apart from whatever was delivered.
                "raw_text": getattr(getattr(choice, "message", None), "content", None),
                "response_shape": response_shape(response),
            }
        )
        if inline_provider is not None:
            return response
        try:
            provider_seen = await self._provider_of(response)
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Record that verification never finished, then let the stop travel.
            self._write(
                {
                    "event": "verification",
                    "request_id": request_id,
                    "round": round_number,
                    "status": "cancelled",
                    "provider": {
                        "requested": self.provider,
                        "reported": None,
                        "verified": False,
                        "source": "verification was cancelled",
                    },
                }
            )
            raise
        self._write(
            {
                "event": "verification",
                "request_id": request_id,
                "round": round_number,
                "status": "ok",
                "provider": provider_seen,
            }
        )
        return response

    async def _provider_of(self, response) -> dict:
        """Which provider served this, and can we actually confirm it?

        The chat-completion schema does not document a provider field, so an
        attribute the SDK happens to expose is reported as unverified. The
        documented route is GET /api/v1/generation, used only when asked for.
        """
        reported = getattr(response, "provider", None)
        result = {
            "requested": self.provider,
            "reported": reported,
            "verified": False,
            "source": "response attribute (not in the documented schema)"
            if reported
            else "not reported",
        }
        identifier = getattr(response, "id", None)
        if self.verify_provider and identifier:
            try:
                metadata = await self._generation_metadata(identifier)
            except (asyncio.CancelledError, KeyboardInterrupt):
                # Control exceptions are not verification failures: a cancelled
                # batch must stop rather than quietly move to the next request.
                raise
            except Exception as exc:  # verification must never fail the batch
                result["verification_error"] = f"{type(exc).__name__}: {exc}"
                return result
            name = (metadata or {}).get("provider_name")
            if name:
                result.update(reported=name, verified=True, source="GET /api/v1/generation")
                result["native_tokens"] = {
                    key: metadata.get(key)
                    for key in (
                        "native_tokens_prompt",
                        "native_tokens_completion",
                        "native_tokens_reasoning",
                        "native_tokens_cached",
                    )
                }
                result["total_cost"] = metadata.get("total_cost")
        return result

    async def _generation_metadata(self, identifier):
        fetch = getattr(self.client, "generation_metadata", None)
        if fetch is None:
            return None
        return await fetch(identifier)

    def _write(self, entry: dict) -> None:
        if not self.journal:
            return
        self.journal(
            {
                "kind": "model-call",
                **self.identity,
                **entry,
                "budget": self.budget.snapshot(),
            }
        )


def pair_schedule(cases, replicates: int = 1, *, start_with: str = "4.0") -> list[dict]:
    """Interleave the two versions per case: AB, then BA, then AB…

    Identity is (case_id, pair_id, replicate, version); rounds are counted
    separately, because one case is not one model request.
    """
    if replicates < 1:
        raise ValueError("replicates must be at least 1")
    other = "4.6" if start_with == "4.0" else "4.0"
    schedule = []
    for case in cases:
        for replicate in range(1, replicates + 1):
            first, second = (start_with, other) if replicate % 2 else (other, start_with)
            pair_id = f"{case['id']}#r{replicate}"
            for position, version in enumerate((first, second), start=1):
                schedule.append(
                    {
                        "case_id": case["id"],
                        "pair_id": pair_id,
                        "replicate": replicate,
                        "version": version,
                        "position": position,
                        "order": f"{first}->{second}",
                    }
                )
    return schedule


def blind_documents(report: dict, *, seed: str | None = None) -> tuple[dict, dict]:
    """A reading copy with no version labels, and the answer key in a separate file.

    X and Y are shuffled per case, so neither letter belongs to a fixed version,
    and the mapping is never stored next to the transcripts.
    """
    shuffler = random.Random(seed or report.get("run_id", ""))
    reading, mapping = [], []
    for case in report.get("cases", []):
        metrics = case.get("metrics") or {}
        partial = case.get("partial") or {}
        sides = ["v40", "v46"]
        shuffler.shuffle(sides)
        labelled = {}
        for label, side in zip(("X", "Y"), sides):
            # A stopped or failed case keeps its evidence under `partial`; a side
            # that did deliver must still appear, and each side keeps its own
            # status rather than inheriting the other's failure.
            side_metrics = metrics.get(side) or {}
            side_partial = partial.get(side) or {}
            segments = side_metrics.get("sent_segments") or side_partial.get("sent_segments", [])
            labelled[label] = {
                "segments": segments,
                "raw_model_text": (case.get("raw_model_text") or {}).get(side, []),
                "tool_sequence": side_metrics.get("tool_sequence", []),
                "delivery_targets": side_metrics.get("delivery_targets", []),
                "status": case.get(f"{side}_harness_status") or case.get("status"),
                "delivered": bool(segments),
                # A person marks the first real answer; "我找一下" is not one.
                "substantive_answer_index": None,
            }
            mapping.append(
                {
                    "case_id": case.get("case_id"),
                    "pair_id": case.get("pair_id"),
                    "replicate": case.get("replicate"),
                    "label": label,
                    "version": "4.0" if side == "v40" else "4.6",
                }
            )
        reading.append(
            {
                "case_id": case.get("case_id"),
                "pair_id": case.get("pair_id"),
                "replicate": case.get("replicate"),
                "task": case.get("task"),
                **labelled,
            }
        )
    return (
        {
            "run_id": report.get("run_id"),
            "note": "版本標籤已移除，X／Y 每個案例各自洗牌；答案在另一份映射檔，不在此檔內。",
            "cases": reading,
        },
        {"run_id": report.get("run_id"), "mapping": mapping},
    )


def settings_baseline(config, model: str) -> dict:
    """The shared main-chat settings both versions must show at the SDK boundary."""
    return {
        "model": model,
        "max_tokens": config.get("model", "max_output_tokens", 8192),
        "temperature": config.get("model", "temperature", 0.65),
        "reasoning_effort": config.get("model", "reasoning_effort", "") or None,
        "background_extraction": bool(config.get("memory", "background_enabled", False)),
    }


def json_safe(value):
    """Best-effort JSON encoding for evidence that must never lose a record."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def ledger_requests(ledger) -> list[dict]:
    """One record per request, however many events it produced.

    A request writes a completion event and, when verification runs, a second
    event for the provider. Reconstruction, the summary and the fairness check
    all work from this merged view so a request is never counted twice.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in ledger:
        if item.get("kind") != "model-call":
            continue
        key = (
            item.get("request_id")
            or f"{item.get('pair_id')}:{item.get('version')}:{item.get('round')}"
        )
        if key not in merged:
            merged[key] = {"request_id": key}
            order.append(key)
        entry = merged[key]
        if item.get("event") == "verification":
            entry["provider"] = item.get("provider")
            entry["verification_status"] = item.get("status")
            continue
        if item.get("provider") is not None:
            entry["provider"] = item["provider"]
        for name in (
            "version",
            "pair_id",
            "replicate",
            "round",
            "status",
            "usage",
            "finish_reason",
            "response_id",
            "settings",
            "duration_ms",
            "reason",
            "raw_text",
            "response_shape",
            "error_type",
        ):
            if name in item:
                entry[name] = item[name]
        entry.setdefault("provider", None)
    return [merged[key] for key in order]


def provider_evidence(ledger, *, requested=None) -> dict:
    """Did one provider actually serve both versions, and can we prove it?

    Three separate questions, deliberately not collapsed into one flag:

    * `metadata_obtained` — did the documented lookup return a provider at all?
    * `provider_consistent` — did every sent round, on both versions and every
      continuation, report the same provider?
    * `provider_matches_request` — is that provider the slug we asked for? A slug
      and a display name need not be the same string, and no documented mapping
      exists, so anything short of an exact match stays unverified rather than
      being assumed.

    Getting a provider name back is not by itself evidence that the comparison
    was fair, which is what the earlier version implied.
    """
    sent = [item for item in ledger_requests(ledger) if item.get("status") == "ok"]
    names, unverified, versions = [], 0, {}
    for item in sent:
        provider = item.get("provider") or {}
        versions.setdefault(item.get("version"), []).append(provider.get("reported"))
        if provider.get("verified") and provider.get("reported"):
            names.append(provider["reported"])
        else:
            unverified += 1
    distinct = sorted({name for name in names if name})
    metadata_obtained = bool(names)
    consistent = bool(names) and unverified == 0 and len(distinct) == 1
    matches = bool(consistent and requested and distinct[0] == requested)
    reasons = []
    if not sent:
        reasons.append("no request was sent, so there is nothing to compare")
    if unverified:
        reasons.append(f"{unverified} of {len(sent)} sent round(s) have an unverified provider")
    if len(distinct) > 1:
        reasons.append(f"rounds reported different providers: {', '.join(distinct)}")
    if consistent and not matches:
        reasons.append(
            "the reported provider name does not match the requested slug and no "
            "documented slug-to-name mapping exists, so the pin stays unverified"
        )
    if len(versions) < 2 and sent:
        reasons.append("only one version produced a sent round")
    return {
        "metadata_obtained": metadata_obtained,
        "provider_consistent": consistent,
        "provider_matches_request": matches,
        "requested": requested,
        "reported": distinct,
        "sent_rounds": len(sent),
        "unverified_rounds": unverified,
        "by_version": versions,
        "reasons": reasons,
    }


def fairness_verdict(provider: dict, settings: dict) -> dict:
    """Combine the two independent questions without letting either stand in for
    the other: settings agreeing says nothing about which provider ran them."""
    settings_consistent = bool(settings.get("comparable"))
    reasons = list(provider["reasons"])
    if not settings_consistent:
        reasons.append("request settings were not identical across the compared rounds")
    # The pin being verified is part of the verdict, not an aside. Two versions
    # reporting the same display name is a real but weaker fact: it stays visible
    # as `provider_consistent` while the headline flag remains false, because the
    # conservative policy is to treat an unmapped slug as unverified.
    routing_verified = provider["provider_matches_request"]
    return {
        "metadata_obtained": provider["metadata_obtained"],
        "settings_consistent": settings_consistent,
        "provider_consistent": provider["provider_consistent"],
        "provider_matches_request": routing_verified,
        "routing_verified": routing_verified,
        "comparable": settings_consistent and provider["provider_consistent"] and routing_verified,
        "provider": provider,
        "reasons": reasons,
    }


def live_limits(provider_verified: bool, stopped_reason: str | None) -> list[str]:
    """Limits that actually apply to a live run, not the offline replay's."""
    limits = [
        "真實模型回覆：模型時間是實測值，但樣本極小，不足以宣稱任何版本更快或更省。",
        "工具與 Discord 動作仍隔離：外部邊界一律被哨兵拒絕，不支援的工具標記在案例載入時就被拒。",
        "`--order`／`--start-with` 只決定每一對的先後；同案例 AB／BA 已交錯，但重複觀測次數仍很少。",
        "X／Y 閱讀稿每案洗牌且答案另存一檔；第一個實質答案仍須由人工標註，程式不判定。",
        "4.0 的 `digest_pending` 前置摘要沒有進入 E 層，因此不能稱完整端到端。",
    ]
    if not provider_verified:
        limits.append(
            "指定路由未核實或供應商不一致：不能宣稱兩版在同一供應商下公平比較，"
            "時間與費用差異可能來自路由而非版本。"
            "兩版回傳相同名稱只是較弱的觀測，slug 與 display name 沒有文件化對應時仍視為未核實。"
        )
    if stopped_reason:
        limits.append(f"本批提前停止（{stopped_reason}），涵蓋的案例不完整。")
    return limits


def summarize_live(report: dict) -> dict:
    """Live-run summary. Deliberately not the offline one: it would claim the
    model time was zero and that no request left the machine."""
    rows = [case for case in report.get("cases", []) if case["status"].startswith("compared")]
    calls = ledger_requests(
        [item for case in report.get("cases", []) for item in case.get("model_calls", [])]
    )
    completed = [item for item in calls if item.get("status") == "ok"]
    failed = [item for item in calls if item.get("status") == "failed"]
    not_sent = [item for item in calls if item.get("status") == "not_sent"]
    # A request that reached the SDK and then failed was attempted, whatever its
    # billing turned out to be; it is not "refused".
    attempted = len(completed) + len(failed)
    return {
        "compared": len(rows),
        "errors": [
            {"case_id": case["case_id"], "v40": case.get("v40_error"), "v46": case.get("v46_error")}
            for case in report.get("cases", [])
            if case["status"] == "error"
        ],
        "incomplete_turns": [case["case_id"] for case in rows if case["status"] != "compared"],
        "model_calls_attempted": attempted,
        "model_calls_completed": len(completed),
        "model_calls_failed": len(failed),
        "model_calls_not_sent": len(not_sent),
        # An empty success set is not completeness, and a failed attempt leaves
        # its billing unknown.
        "usage_complete": bool(completed)
        and not failed
        and all(item.get("usage", {}).get("status") == "reported" for item in completed),
        "settings_comparable_all": all(
            case.get("fairness", {}).get("settings_consistent") for case in rows
        )
        if rows
        else False,
        "provider_consistent_all": all(
            case.get("fairness", {}).get("provider_consistent") for case in rows
        )
        if rows
        else False,
        "fair_comparison": report.get("fair_comparison", False),
        "budget": report.get("budget"),
        "stopped_reason": report.get("stopped_reason"),
    }
