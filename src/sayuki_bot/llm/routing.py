"""Configured primary/fallback routes, independent for chat, light and vision."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit


GO_URL = "https://opencode.ai/zen/go/v1"
ROUTER_URL = "https://openrouter.ai/api/v1"
ROLES = {"chat", "attention", "memory", "vision", "task"}


@dataclass(frozen=True)
class Route:
    slot: str
    base_url: str
    model: str
    api_key_env: str
    api_format: str
    enabled: bool = True

    @property
    def client_identity(self):
        return self.base_url.rstrip("/"), self.api_key_env, self.api_format

    @property
    def host(self):
        return urlsplit(self.base_url).hostname


def route(config, role="chat", slot="primary"):
    if role not in ROLES or slot not in {"primary", "fallback"}:
        raise ValueError("Unknown model role or route slot")
    model = config.sections.get("model", {})
    routes = model.get("routes", {})
    group = role if role in routes else "light" if role in {"attention", "memory"} else role
    parent = routes.get("chat", {}).get(slot, {})
    own = routes.get(group, {}).get(slot, {}) if group != "chat" else {}
    settings = {**parent, **{k: v for k, v in own.items() if v != ""}}
    legacy_model = model.get(role) or model.get("chat", "")
    # Only pre-route configurations inherit the historical single endpoint.
    # New examples specify every endpoint explicitly; provider order is data.
    legacy = not routes
    base_url = settings.get("base_url") or (model.get("base_url", ROUTER_URL) if legacy else "")
    name = settings.get("model") or legacy_model
    key_env = settings.get("api_key_env") or (
        model.get("api_key_env", "LLM_API_KEY") if legacy else ""
    )
    enabled = slot == "primary" or bool(parent or own) and settings.get("enabled", True)
    fmt = settings.get("api_format", "auto")
    if fmt == "auto":
        host = urlsplit(base_url).hostname
        fmt = (
            "opencode"
            if host == "opencode.ai"
            else "openrouter"
            if host == "openrouter.ai"
            else "openai"
        )
    return Route(slot, base_url, name, key_env, fmt, enabled)


def primary_key(config, environ=None):
    environ = os.environ if environ is None else environ
    selected = route(config)
    value = environ.get(selected.api_key_env, "")
    # Accept the user's existing Go key spelling without falling back to an
    # unrelated LLM_API_KEY that may belong to a different provider.
    if not value and selected.api_key_env == "OPENCODE_API_KEY":
        value = environ.get("opencode_api_key", "")
    return value


def validate_routes(config):
    groups = config.sections.get("model", {}).get("routes", {})
    if not isinstance(groups, dict) or set(groups) - (ROLES | {"light"}):
        raise ValueError("model.routes contains an unknown model group")
    for group, slots in groups.items():
        if not isinstance(slots, dict) or set(slots) - {"primary", "fallback"}:
            raise ValueError(f"model.routes.{group} requires primary/fallback tables")
        for slot, values in slots.items():
            if not isinstance(values, dict) or set(values) - {
                "base_url",
                "model",
                "api_key_env",
                "api_format",
                "enabled",
            }:
                raise ValueError(f"Unknown setting in model.routes.{group}.{slot}")
            for key, value in values.items():
                if key == "enabled":
                    if type(value) is not bool:
                        raise ValueError("model route enabled must be a boolean")
                    if slot == "primary" and not value:
                        raise ValueError("Primary routes cannot be disabled")
                elif not isinstance(value, str):
                    raise ValueError(f"model route {key} must be a string")
    for role in ROLES:
        for slot in ("primary", "fallback"):
            selected = route(config, role, slot)
            if not selected.enabled:
                continue
            endpoint = urlsplit(selected.base_url)
            # Accessing port also rejects malformed/out-of-range port strings.
            if endpoint.port is not None and endpoint.port == 0:
                raise ValueError(f"Invalid {role} {slot} model endpoint port")
            if (
                not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.scheme != "https"
                and not (
                    endpoint.scheme == "http"
                    and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError(f"Invalid {role} {slot} model endpoint")
            if groups and (
                not selected.model.strip()
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", selected.api_key_env)
            ):
                raise ValueError(f"Configure {role} {slot} model and api_key_env")
            if selected.api_format not in {"opencode", "openrouter", "openai"}:
                raise ValueError("Unknown model route api_format")
