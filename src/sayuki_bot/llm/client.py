"""One completion per generation; tool execution belongs to conversation engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4, uuid5

from sayuki_bot.api import Generation
from sayuki_bot.llm.routing import route, validate_routes
from sayuki_bot.storage import jsonl_log

logger = logging.getLogger(__name__)


def supports_final_tool_reasoning(config, model=None, *, endpoint=None):
    """The route/model for which native final-tool low was actually validated."""
    selected = route(config)
    if (model or selected.model) != "deepseek-v4.1-flash":
        return False
    url = selected.base_url if endpoint is None else endpoint
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "opencode.ai"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.rstrip("/")
            in {"/zen/go/v1", "/inference/go/v1", "/inference/go/openai/v1"}
        )
    except ValueError:
        return False


class EmptyCompletion(ValueError):
    """A completion arrived without a usable reply; it is not deliberate silence."""

    def __init__(self, shape):
        super().__init__("Model completion has no usable text")
        self.shape = shape


def response_shape(response):
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    calls = getattr(message, "tool_calls", None) or []
    names = [str(getattr(getattr(call, "function", None), "name", ""))[:64] for call in calls[:5]]
    old_call = getattr(message, "function_call", None)
    if old_call is not None:
        names.append(str(getattr(old_call, "name", ""))[:64])
    return {
        "choices": len(choices),
        "content_type": "null" if content is None else type(content).__name__,
        "content_chars": len(content) if isinstance(content, str) else None,
        "native_tool_count": len(calls) + int(old_call is not None),
        "native_tool_names": names,
        "reasoning_present": bool(
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_details", None)
            or getattr(message, "reasoning_content", None)
        ),
        "refusal_present": bool(getattr(message, "refusal", None)),
        "finish_reason": getattr(choice, "finish_reason", None),
    }


def reasoning_text(message):
    """Provider reasoning, if returned: DeepSeek/opencode use reasoning_content,
    OpenRouter uses reasoning and/or structured reasoning_details."""
    for field in ("reasoning_content", "reasoning"):
        value = getattr(message, field, None)
        if isinstance(value, str) and value.strip():
            return value
    pieces = []
    for detail in getattr(message, "reasoning_details", None) or []:
        get = detail.get if isinstance(detail, dict) else lambda k, d=detail: getattr(d, k, None)
        text = get("text") or get("summary")
        if isinstance(text, str) and text.strip():
            pieces.append(text)
    return "\n".join(pieces)


class ModelClient:
    def __init__(self, config, database, api_key, client=None, *, fallback_client=None):
        validate_routes(config)
        from openai import AsyncOpenAI

        self._factory = AsyncOpenAI
        self.config, self.db = config, database
        self._primary_route = route(config)
        self._primary_key = api_key
        self._injected_client = client is not None
        self._session_namespace = uuid4()
        self._clients = {}
        self.fallback_client = fallback_client
        if client is None:
            client = self._make_client(self._primary_route, api_key)
        self.client = client

    def _make_client(self, selected, key):
        options = dict(
            api_key=key,
            base_url=selected.base_url,
            # The per-call deadline is enforced in generate(); the SDK limit only
            # has to be long enough for the slowest role (background memory).
            timeout=max(
                self.config.get("model", "timeout_seconds", 90),
                self.config.get("memory", "timeout_seconds", 180),
            ),
            max_retries=0
            if self.config.get("model", "routes")
            else self.config.get("model", "max_retries", 1),
        )
        if selected.api_format == "opencode":
            options["default_headers"] = {"User-Agent": "sayuki-discord/4.6"}
        return self._factory(**options)

    def _key(self, selected):
        value = os.getenv(selected.api_key_env, "")
        if not value and selected.api_key_env == "OPENCODE_API_KEY":
            value = os.getenv("opencode_api_key", "")
        return value

    def _get_client(self, selected):
        if (
            selected.slot == "primary"
            and self._injected_client
            and not self.config.get("model", "routes")
        ):
            # An explicitly injected legacy transport is owned by the caller;
            # tests/adapters may change model settings without rebuilding it.
            return self.client
        if (
            selected.slot == "primary"
            and selected.client_identity == self._primary_route.client_identity
        ):
            return self.client
        if (
            selected.slot == "fallback"
            and self.fallback_client is not None
            and selected.client_identity == route(self.config, slot="fallback").client_identity
        ):
            return self.fallback_client
        identity = selected.client_identity
        if identity not in self._clients:
            key = self._key(selected)
            if (
                not key
                and selected.slot == "primary"
                and selected.api_key_env == self._primary_route.api_key_env
            ):
                key = self._primary_key
            if not key:
                raise ValueError(f"Missing API key environment variable {selected.api_key_env}")
            self._clients[identity] = self._make_client(selected, key)
        return self._clients[identity]

    def _backup_available(self, selected):
        return selected.enabled and (
            selected.client_identity in self._clients
            or bool(self._key(selected))
            or self.fallback_client is not None
            and selected.client_identity == route(self.config, slot="fallback").client_identity
        )

    def _arguments(self, messages, selected, role, phase):
        args = dict(
            model=selected.model,
            messages=messages,
            max_tokens=self.config.get("model", "max_output_tokens", 8192)
            if role == "chat"
            else self.config.get(
                "model",
                "task_max_output_tokens",
                self.config.get("model", "max_output_tokens", 8192),
            )
            if role == "task"
            else self.config.get("model", "memory_max_output_tokens", 8192)
            if role == "memory"
            else self.config.get("model", "attention_max_output_tokens", 512)
            if role == "attention"
            else 2000,
            temperature=self.config.get("model", "temperature", 0.65)
            if role in {"chat", "task"}
            else 0.1,
        )
        effort = (
            self.config.get("model", "memory_reasoning_effort", "low")
            if role == "memory"
            else self.config.get("model", "attention_reasoning_effort", "low")
            if role == "attention"
            else self.config.get(
                "model", "task_reasoning_effort", self.config.get("model", "reasoning_effort", "")
            )
            if role == "task"
            else self.config.get("model", "reasoning_effort", "")
        )
        # Participation is a small admission decision, independent from chat RP.
        # Omitted flags in older configs adopt the same disabled default.
        final_low = (
            role == "chat"
            and phase in {"final_tool", "closeout"}
            and (
                selected.api_format == "opencode"
                and supports_final_tool_reasoning(
                    self.config, selected.model, endpoint=selected.base_url
                )
                or selected.slot == "fallback"
                and selected.api_format == "openrouter"
                and supports_final_tool_reasoning(self.config)
            )
        )
        if final_low and selected.api_format == "opencode":
            args["reasoning_effort"] = "low"
        elif final_low and selected.api_format == "openrouter":
            args["extra_body"] = {"reasoning": {"effort": "low"}}
        elif selected.api_format == "opencode" and selected.model == "deepseek-v4.1-flash":
            # Go forwards DeepSeek's native controls, not OpenRouter's body.
            # RC probe: old attention body timed out/length3/3; native disabled
            # returned usable JSON3/3 with no reasoning_content observed.
            if role == "attention":
                enabled = self.config.get("model", "attention_reasoning_enabled", False)
                args["extra_body"] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
                if enabled and effort:
                    args["reasoning_effort"] = effort
            elif effort and role != "vision":
                args["reasoning_effort"] = effort
        elif role == "attention" and not self.config.get(
            "model", "attention_reasoning_enabled", False
        ):
            args["extra_body"] = {"reasoning": {"enabled": False}}
        elif effort and role != "vision":
            args["extra_body"] = {"reasoning": {"effort": effort}}
        elif role == "attention":
            args["extra_body"] = {"reasoning": {"enabled": True}}
        return args, final_low

    async def generate(self, messages, *, turn_id, role="chat", phase="normal"):
        if phase not in {"normal", "final_tool", "closeout"}:
            raise ValueError("Unknown model generation phase")
        primary, backup = route(self.config, role), route(self.config, role, "fallback")
        if not primary.model:
            raise ValueError("Configure the primary chat model before running")
        total = self.config.get("model", "timeout_seconds", 90)
        if role == "attention":
            total = min(total, self.config.get("conversation", "continuation_timeout_seconds", 8))
        elif role == "memory":
            # Background extraction never blocks chat; its long input plus
            # reasoning routinely exceeded the chat deadline.
            total = self.config.get("memory", "timeout_seconds", 180)
        deadline = perf_counter() + total
        ready = self._backup_available(backup)
        args, final_low = self._arguments(messages, primary, role, phase)
        if phase != "normal":
            self.db.trace(
                turn_id,
                "model_generation_phase",
                {
                    "phase": phase,
                    "role": role,
                    "model": primary.model,
                    "low_applied": final_low,
                    "reasoning_effort": args.get("reasoning_effort"),
                    "extra_body": args.get("extra_body"),
                    "messages_sha256": hashlib.sha256(
                        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                },
            )
        try:
            return await self._attempt(
                primary, args, turn_id, role, total * 0.6 if ready else total, phase=phase
            )
        except Exception as exc:
            # Only remote/transport failures qualify. Control exceptions,
            # programming errors, length and empty-reply recovery stay intact.
            import httpx
            from openai import APIError

            if not isinstance(exc, (APIError, httpx.TransportError, TimeoutError, ConnectionError)):
                raise
            if not ready:
                if backup.enabled:
                    self.db.trace(
                        turn_id,
                        "model_fallback_unavailable",
                        {"role": role, "error_type": type(exc).__name__},
                    )
                    logger.warning(
                        "Primary model failed; configured fallback unavailable (%s)",
                        type(exc).__name__,
                    )
                raise
            detail = {
                "role": role,
                "phase": phase,
                "from_endpoint": primary.base_url,
                "to_endpoint": backup.base_url,
                "error_type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
            }
            self.db.trace(turn_id, "model_failover", detail)
            logger.warning(
                "Model fallback: %s -> %s role=%s error=%s status=%s",
                primary.host,
                backup.host,
                role,
                detail["error_type"],
                detail["status_code"],
            )
            left = deadline - perf_counter()
            if left <= 0:
                raise TimeoutError("Model failover deadline exhausted") from exc
            args, _ = self._arguments(messages, backup, role, phase)
            try:
                return await self._attempt(backup, args, turn_id, role, left, phase=phase)
            except Exception as fallback_error:
                self.db.trace(
                    turn_id,
                    "model_fallback_failed",
                    {"role": role, "error_type": type(fallback_error).__name__},
                )
                logger.error(
                    "Fallback model failed role=%s error=%s", role, type(fallback_error).__name__
                )
                raise

    def _log_invocation(self, selected, args, turn_id, role, phase, started_at, start, *,
                        output="", error=None, usage=None, reasoning=""):
        messages, input_chars, system_chars = jsonl_log.messages_for_log(args.get("messages", []))
        usage = usage or {}
        jsonl_log.write(
            "invocation",
            {
                "log_type": "llm_call",
                "time": started_at,
                "turn_id": turn_id,
                "call_type": role if phase == "normal" else f"{role}:{phase}",
                "route": selected.slot,
                "endpoint": selected.base_url,
                "model": selected.model,
                "max_tokens": args.get("max_tokens"),
                "temperature": args.get("temperature"),
                "reasoning_effort": args.get("reasoning_effort"),
                "extra_body": args.get("extra_body"),
                "success": error is None,
                "duration_ms": round((perf_counter() - start) * 1000, 2),
                "finish_reason": usage.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cost": usage.get("cost"),
                "input_chars_without_system": input_chars,
                "system_prompt_chars": system_chars,
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                ),
                "reasoning_chars": len(reasoning or ""),
                "reasoning": reasoning or "",
                "output_chars": len(output or ""),
                "messages": messages,
                "output": output or "",
                "error": ""
                if error is None
                else f"{type(error).__name__}: {error}"[:1000],
                "status_code": getattr(error, "status_code", None),
            },
        )

    async def _attempt(self, selected, args, turn_id, role, timeout, *, phase="normal"):
        start = perf_counter()
        started_at = jsonl_log.now()
        client = self._get_client(selected)
        if selected.api_format == "opencode":
            # SDK transport metadata stays out of the completion JSON contract.
            # Injected RPC/test SDKs own their own headers; real AsyncOpenAI
            # copies options while sharing the original connection pool.
            with_options = getattr(client, "with_options", None)
            if callable(with_options):
                client = with_options(
                    default_headers={
                        "x-opencode-session": str(uuid5(self._session_namespace, str(turn_id)))
                    }
                )
        try:
            response = await asyncio.wait_for(client.chat.completions.create(**args), timeout)
        except asyncio.CancelledError as exc:
            self._log_invocation(selected, args, turn_id, role, phase, started_at, start, error=exc)
            self.db.record_usage(
                turn_id,
                role,
                selected.model,
                (perf_counter() - start) * 1000,
                {
                    "error_type": "CancelledError",
                    "route": selected.slot,
                    "endpoint": selected.base_url,
                },
            )
            raise
        except Exception as exc:
            self._log_invocation(selected, args, turn_id, role, phase, started_at, start, error=exc)
            self.db.record_usage(
                turn_id,
                role,
                selected.model,
                (perf_counter() - start) * 1000,
                {
                    "error_type": type(exc).__name__,
                    "route": selected.slot,
                    "endpoint": selected.base_url,
                },
            )
            raise
        shape = response_shape(response)
        usage = response.usage.model_dump() if response.usage else {}
        usage.update(
            finish_reason=shape["finish_reason"],
            response_shape=shape,
            route=selected.slot,
            endpoint=selected.base_url,
        )
        self.db.record_usage(turn_id, role, selected.model, (perf_counter() - start) * 1000, usage)
        choice = response.choices[0] if shape["choices"] else None
        content = getattr(getattr(choice, "message", None), "content", None)
        self._log_invocation(
            selected, args, turn_id, role, phase, started_at, start,
            output=content if isinstance(content, str) else "", usage=usage,
            reasoning=reasoning_text(getattr(choice, "message", None)),
        )
        # Keep length handling in the engine; do not retry an exhausted output
        # allowance or reinterpret a provider refusal/filter as deliberate silence.
        if (
            role in {"chat", "task"}
            and shape["finish_reason"] != "length"
            and (not isinstance(content, str) or not content.strip())
        ):
            raise EmptyCompletion(shape)
        return Generation(content or "", shape["finish_reason"] or "stop", usage)

    async def close(self):
        errors, seen = [], set()
        for client in [self.client, self.fallback_client, *self._clients.values()]:
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            try:
                await client.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
