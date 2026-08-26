"""OpenAI-compatible chat-completions adapter (1st-priority backend, §4.1)."""

from __future__ import annotations

import json

import httpx

from .base import Completion, Message, ModelBackend, ToolCall, ToolSpec


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

        response = await self._post(f"{self.base_url}/chat/completions", payload)
        response.raise_for_status()
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
        return Completion(text=message.get("content"), tool_calls=tool_calls)

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

    async def _post(self, url: str, payload: dict):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if isinstance(self._client, httpx.AsyncClient):
            return await self._client.post(url, json=payload, headers=headers)
        return self._client.post(url, json=payload, headers=headers)
