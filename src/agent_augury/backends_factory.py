"""Backend construction from config specs."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .auth.oauth import NOUS_PORTAL_CONFIG, DeviceCodeFlow
from .auth.token_store import TokenStore
from .backend.base import Completion, ModelBackend, ToolCall
from .backend.fake import FakeModelBackend
from .backend.nous_portal import NousPortalBackend
from .backend.nous_portal_oauth import NousPortalOAuthBackend
from .backend.openai_compat import OpenAICompatBackend
from .model_listing import extract_model_ids


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
    if btype == "nous_oauth":
        return NousPortalOAuthBackend(
            model=spec["model"],
            base_url=spec.get("base_url", "https://inference-api.nousresearch.com/v1"),
        )
    raise ValueError(f"unknown backend type: {btype!r}")


# -- Model listing helpers (synchronous, for wizard) -------------------------


def _fetch_models_sync(base_url: str, api_key: str) -> list[str] | None:
    """Synchronously fetch model IDs from an OpenAI-compatible /models endpoint."""
    import httpx
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
    except Exception:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return extract_model_ids(data.get("data") or [])


def list_models_openai_compat(base_url: str, api_key: str) -> list[str] | None:
    """List models for an OpenAI-compatible backend. Returns None on failure."""
    return _fetch_models_sync(base_url, api_key)


def list_models_nous_portal(base_url: str, api_key: str) -> list[str] | None:
    """List models for Nous Portal (API key). Returns None on failure."""
    return _fetch_models_sync(base_url, api_key)


def list_models_nous_oauth(base_url: str) -> list[str] | None:
    """List models for Nous Portal (OAuth). Uses stored token or returns None."""
    store = TokenStore()
    tokens = store.get_provider_tokens(NOUS_PORTAL_CONFIG.id)
    access_token = tokens.get("access_token")
    if not access_token:
        return None
    return _fetch_models_sync(base_url, access_token)
