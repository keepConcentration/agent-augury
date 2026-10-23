"""Anthropic Messages API adapter (native ``type: anthropic``)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from ..model_listing import extract_model_ids
from .base import Completion, Message, ModelBackend, ToolCall, ToolSpec
from .errors import (
    CHAT_CONNECT_TIMEOUT_S,
    CHAT_READ_TIMEOUT_S,
    classify_http,
    describe_request_error,
    network_error,
)

logger = logging.getLogger(__name__)

ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192


class AnthropicBackend(ModelBackend):
    """POSTs ``{base_url}/v1/messages`` with an API key; parses text/tool_use.

    AgentLoop keeps an OpenAI-shaped conversation; this adapter converts at the
    boundary only (system peel, tool_use / tool_result blocks).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | httpx.AsyncClient | None = None,
        timeout: float = CHAT_READ_TIMEOUT_S,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=CHAT_CONNECT_TIMEOUT_S)
        )
        self._owns_client = client is None

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Completion:
        system, api_messages = _to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_map_tool(t) for t in tools]

        last_error = ""
        for attempt in range(3):
            try:
                response = await self._post(f"{self.base_url}/v1/messages", payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if (
                    isinstance(exc.response.status_code, int)
                    and 500 <= exc.response.status_code < 600
                    and attempt < 2
                ):
                    await asyncio.sleep(2**attempt)
                    continue
                body = exc.response.text if exc.response is not None else ""
                code = status if isinstance(status, int) else None
                return Completion(error=classify_http(code, body, model=self.model))
            except httpx.RequestError as exc:
                last_error = describe_request_error(exc)
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                    continue
                return Completion(
                    error=network_error(
                        f"{last_error} (retried 3 times)", model=self.model
                    )
                )
            else:
                break
        else:
            return Completion(
                error=network_error(
                    f"{last_error} (retried 3 times)", model=self.model
                )
            )

        data = response.json()
        return _parse_response(data)

    async def list_models(self) -> list[str] | None:
        """Fetch model IDs from ``GET /v1/models`` (best-effort)."""
        try:
            # Default page size is 20; 1000 is the API max.
            response = await self._get(f"{self.base_url}/v1/models?limit=1000")
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — list is best-effort
            logger.debug("list_models failed: %s", exc)
            return None
        try:
            data = response.json()
        except (ValueError, httpx.DecodingError):
            logger.debug("list_models returned non-JSON")
            return None
        return extract_model_ids(data.get("data") or [])

    async def aclose(self) -> None:
        if self._owns_client and hasattr(self._client, "aclose"):
            await self._client.aclose()
        elif self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get(self, url: str):
        headers = self._headers()
        if isinstance(self._client, httpx.AsyncClient):
            return await self._client.get(url, headers=headers)
        return self._client.get(url, headers=headers)

    async def _post(self, url: str, payload: dict):
        headers = self._headers()
        if isinstance(self._client, httpx.AsyncClient):
            return await self._client.post(url, json=payload, headers=headers)
        return self._client.post(url, json=payload, headers=headers)


def _map_tool(spec: ToolSpec) -> dict[str, Any]:
    """Internal ``{name, description, schema}`` → Anthropic tool JSON."""
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "input_schema": spec.get("schema", {"type": "object", "properties": {}}),
    }


def _to_anthropic_messages(
    messages: list[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """OpenAI-shaped history → ``(system, messages)`` for Messages API."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        if role == "system":
            text = msg.get("content")
            if isinstance(text, str) and text:
                system_parts.append(text)
            i += 1
            continue
        if role == "tool":
            blocks: list[dict[str, Any]] = []
            while i < n and messages[i].get("role") == "tool":
                t = messages[i]
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": t.get("tool_call_id") or "",
                        "content": t.get("content") if t.get("content") is not None else "",
                    }
                )
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            blocks = []
            content = msg.get("content")
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                raw_args = fn.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        parsed = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        parsed = {}
                elif isinstance(raw_args, dict):
                    parsed = raw_args
                else:
                    parsed = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": parsed if isinstance(parsed, dict) else {},
                    }
                )
            # Anthropic rejects empty non-final content; an empty turn says nothing.
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            i += 1
            continue
        # user (and any other role treated as user text)
        content = msg.get("content")
        if content:
            out.append({"role": "user", "content": content})
        i += 1

    # A trailing assistant turn is read as prefill (continue that text, or 400
    # on models without prefill); an empty list is a 400. End on a user turn.
    if not out or out[-1]["role"] == "assistant":
        out.append({"role": "user", "content": "(continue)"})

    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


def _parse_response(data: dict[str, Any]) -> Completion:
    """Anthropic Messages response → ``Completion``."""
    if data.get("stop_reason") == "max_tokens":
        # A cut-off tool_use may carry incomplete input; leave a trace.
        logger.warning(
            "anthropic reply hit max_tokens (model=%s); output may be truncated",
            data.get("model"),
        )
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            piece = block.get("text")
            if isinstance(piece, str) and piece:
                text_parts.append(piece)
        elif btype == "tool_use":
            raw_input = block.get("input")
            args = raw_input if isinstance(raw_input, dict) else {}
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=args,
                )
            )
    text = "".join(text_parts) if text_parts else None
    usage_raw = data.get("usage")
    usage = None
    if isinstance(usage_raw, dict):
        usage = {
            "input_tokens": usage_raw.get("input_tokens"),
            "output_tokens": usage_raw.get("output_tokens"),
        }
    return Completion(text=text, tool_calls=tool_calls, usage=usage)
