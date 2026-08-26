"""Backend construction from config specs."""

from __future__ import annotations

import os
from typing import Any

from .backend.base import Completion, ModelBackend, ToolCall
from .backend.fake import FakeModelBackend
from .backend.nous_portal import NousPortalBackend
from .backend.openai_compat import OpenAICompatBackend


def _completion_from_spec(entry: Any) -> Completion:
    """Config entry → Completion. String = final text; dict may hold tool_calls."""
    if isinstance(entry, str):
        return Completion(text=entry)
    if isinstance(entry, dict):
        calls = [
            ToolCall(id=f"cfg-{i}", name=c["name"], arguments=dict(c.get("arguments") or {}))
            for i, c in enumerate(entry.get("tool_calls") or [])
        ]
        return Completion(text=entry.get("text"), tool_calls=calls)
    raise ValueError(f"cannot interpret script entry: {entry!r}")


def _api_key_from_env(spec: dict[str, Any]) -> str:
    env_name = spec.get("api_key_env")
    if not env_name:
        raise ValueError("real backends require 'api_key_env' (env variable name)")
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(
            f"environment variable {env_name!r} is not set — cannot build backend"
        )
    return value


def build_backend(spec: dict[str, Any]) -> ModelBackend:
    btype = spec.get("type")
    if btype == "fake":
        return FakeModelBackend([_completion_from_spec(e) for e in spec.get("script", [])])
    if btype == "openai":
        base_url = spec.get("base_url")
        if not base_url:
            raise ValueError("openai backend requires 'base_url'")
        return OpenAICompatBackend(
            base_url=base_url,
            api_key=_api_key_from_env(spec),
            model=spec["model"],
        )
    if btype == "nous":
        return NousPortalBackend(
            api_key=_api_key_from_env(spec),
            model=spec["model"],
            base_url=spec.get("base_url"),
        )
    raise ValueError(f"unknown backend type: {btype!r}")
