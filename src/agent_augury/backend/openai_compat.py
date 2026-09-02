"""OpenAI-compatible chat-completions adapter (1st-priority backend, §4.1)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..model_listing import extract_model_ids
from .base import Completion, Message, ModelBackend, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class OpenAICompatBackend(ModelBackend):
    """POSTs {base_url}/chat/completions with an API key; parses text/tool_calls."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Completion:
        payload: dict = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
        }
        if tools:
            payload["tools"] = [self._map_tool(t) for t in tools]

        try:
            response = await self._post(f"{self.base_url}/chat/completions", payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # HTTP error (4xx/5xx) — return error text so the agent can
            # surface it to the user instead of crashing the session.
            detail = exc.response.text[:500] if exc.response is not None else ""
            return Completion(
                text=(
                    f"[backend error] HTTP {exc.response.status_code if exc.response is not None else '?'}"
                    f" from {self.base_url}/chat/completions. "
                    f"The provider endpoint may be unavailable or the model '{self.model}' may not exist. "
                    f"Detail: {detail}"
                )
            )
        except httpx.RequestError as exc:
            # Network-level error (DNS, connection refused, timeout, etc.)
            return Completion(
                text=(
                    f"[backend error] Network error calling {self.base_url}/chat/completions: {exc}. "
                    f"Please check your internet connection and the base URL."
                )
            )

        data = response.json()

        message = data["choices"][0]["message"]
        tool_calls = [
            ToolCall(
                id=c["id"],
                name=c["function"]["name"],
                arguments=json.loads(c["function"]["arguments"]),
            )
            for c in message.get("tool_calls") or []
        ]
        usage = data.get("usage")
        return Completion(text=message.get("content"), tool_calls=tool_calls, usage=usage)

    async def list_models(self) -> list[str] | None:
        """Fetch available model IDs from the /models endpoint.

        Returns None if the endpoint is unavailable, returns an empty list
        if the call succeeded but no models were reported.
        """
        try:
            response = await self._get(f"{self.base_url}/models")
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

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _map_tool(spec: ToolSpec) -> dict:
        """Internal {name, description, schema} → OpenAI function-tool JSON."""
        return {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("schema", {"type": "object", "properties": {}}),
            },
        }

    async def _get(self, url: str):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if isinstance(self._client, httpx.AsyncClient):
            return await self._client.get(url, headers=headers)
        return self._client.get(url, headers=headers)

    async def _post(self, url: str, payload: dict):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if isinstance(self._client, httpx.AsyncClient):
            return await self._client.post(url, json=payload, headers=headers)
        return self._client.post(url, json=payload, headers=headers)
