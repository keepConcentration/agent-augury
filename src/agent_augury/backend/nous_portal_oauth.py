"""Nous Portal OAuth Backend — Device Code Flow."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from ..model_listing import extract_model_ids
from ..auth.oauth import (
    NOUS_PORTAL_CONFIG,
    DeviceCodeFlow,
    OAuthProviderConfig,
    TokenResponse,
)
from ..auth.token_store import TokenStore, compute_expires_at, is_token_expiring
from .base import Completion, Message, ModelBackend, OAuthModelBackend, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class NousPortalOAuthBackend(OAuthModelBackend):
    """OpenAI-compatible Nous Portal backend with OAuth device code auth.

    On first call, runs the device code flow to obtain tokens.
    Persists tokens to disk; refreshes automatically when expiring.
    """

    def __init__(
        self,
        model: str,
        base_url: str = "https://inference-api.nousresearch.com/v1",
        config: Optional[OAuthProviderConfig] = None,
        token_store: Optional[TokenStore] = None,
        client: Optional[httpx.Client | httpx.AsyncClient] = None,
        timeout: float = 120.0,
        on_user_code: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._config = config or NOUS_PORTAL_CONFIG
        self._token_store = token_store or TokenStore()
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._on_user_code = on_user_code
        self._token: Optional[TokenResponse] = None

    async def get_access_token(self) -> str:
        """Resolve a valid access token, refreshing or re-authing as needed."""
        if self._token and not is_token_expiring(self._token.expires_at):
            return self._token.access_token

        stored = self._token_store.get_provider_tokens(self._config.id)
        access_token = stored.get("access_token")
        refresh_token = stored.get("refresh_token")
        expires_at = stored.get("expires_at")

        if access_token and not is_token_expiring(expires_at):
            self._token = TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=stored.get("expires_in"),
            )
            return access_token

        if refresh_token:
            try:
                return await self._refresh_token(refresh_token)
            except Exception as exc:
                logger.warning("Token refresh failed, re-authenticating: %s", exc)

        return await self._authenticate()

    async def _refresh_token(self, refresh_token: str) -> str:
        """Refresh an existing token."""
        flow = DeviceCodeFlow(self._config, http_client_factory=self._make_sync_client)
        # Run synchronous refresh in thread
        import asyncio
        refreshed = await asyncio.to_thread(flow.refresh_access_token, refresh_token)
        self._save_token(refreshed)
        return refreshed.access_token

    async def _authenticate(self) -> str:
        """Run full device code flow."""
        flow = DeviceCodeFlow(self._config, http_client_factory=self._make_sync_client)
        import asyncio
        token = await asyncio.to_thread(
            lambda: flow.authenticate(on_user_code=self._on_user_code, open_browser=True)
        )
        self._save_token(token)
        return token.access_token

    def _make_sync_client(self):
        """Create a sync httpx client for auth flows."""
        return httpx.Client(timeout=30.0, headers={"Accept": "application/json"})

    def _save_token(self, token: TokenResponse) -> None:
        """Persist token to store."""
        expires_at = compute_expires_at(token.expires_in)
        self._token = TokenResponse(
            access_token=token.access_token,
            token_type=token.token_type,
            expires_in=token.expires_in,
            refresh_token=token.refresh_token,
            scope=token.scope,
        )
        # Store expires_at as a string for JSON serialization
        self._token_store.set_provider_tokens(
            self._config.id,
            {
                "access_token": token.access_token,
                "token_type": token.token_type,
                "expires_in": token.expires_in,
                "expires_at": expires_at,
                "refresh_token": token.refresh_token,
                "scope": token.scope,
                "obtained_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def list_models(self) -> list[str] | None:
        """Fetch available model IDs from the /models endpoint.

        Returns None if the endpoint is unavailable, returns an empty list
        if the call succeeded but no models were reported.
        """
        try:
            token = await self.get_access_token()
        except Exception as exc:  # noqa: BLE001 — list is best-effort
            logger.debug("list_models: auth failed: %s", exc)
            return None
        client = self._get_async_client()
        try:
            response = await client.get(
                f"{self.base_url}/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
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

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Completion:
        """Call Nous Portal chat completions with OAuth token."""
        token = await self.get_access_token()

        client = self._get_async_client()
        payload: dict = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
        }
        if tools:
            payload["tools"] = [self._map_tool(t) for t in tools]

        response = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
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
        usage = data.get("usage")
        return Completion(text=message.get("content"), tool_calls=tool_calls, usage=usage)

    def _get_async_client(self) -> httpx.AsyncClient:
        if isinstance(self._client, httpx.AsyncClient):
            return self._client
        if self._client is not None:
            return self._client  # type: ignore[return-value]
        self._client = httpx.AsyncClient(timeout=self._timeout, headers={"Accept": "application/json"})
        self._owns_client = True
        return self._client  # type: ignore[return-value]

    @staticmethod
    def _map_tool(spec: ToolSpec) -> dict:
        """Internal tool spec to OpenAI function-tool JSON."""
        return {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("schema", {"type": "object", "properties": {}}),
            },
        }

    async def aclose(self) -> None:
        """Close the HTTP client if owned."""
        if self._owns_client and isinstance(self._client, httpx.AsyncClient):
            await self._client.aclose()
            self._client = None
