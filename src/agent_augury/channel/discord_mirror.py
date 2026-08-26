"""Read-only Discord observation mirror (D3 unblocks in v0.1b).

Pull-based: server subscription enqueues formatted lines; ``flush()`` posts
them to a Discord webhook. The core NEVER reads anything back from Discord
(§3.3 — channels are views, not protocol participants).
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class DiscordWebhookMirror:
    """Enqueue-on-message, flush-to-webhook. Observation must never raise."""

    def __init__(self, webhook_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self.outbox: list[str] = []
        self.errors: list[Exception] = []

    # -- server subscription ---------------------------------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        self.enqueue(message)

    def enqueue(self, message: dict[str, Any]) -> None:
        self.outbox.append(self.format_line(message))

    # -- flushing ----------------------------------------------------------------

    async def flush(self) -> int:
        """Post every queued line. Swallows HTTP failures (records them)."""
        sent = 0
        while self.outbox:
            content = self.outbox.pop(0)
            try:
                response = await self._client.post(
                    self.webhook_url, json={"content": content}
                )
                response.raise_for_status()
                sent += 1
            except Exception as exc:  # noqa: BLE001 — observation must not kill sessions
                self.errors.append(exc)
        return sent

    async def aclose(self) -> None:
        if self._owns_client and hasattr(self._client, "aclose"):
            await self._client.aclose()

    # -- formatting ---------------------------------------------------------------

    @staticmethod
    def format_line(message: dict[str, Any]) -> str:
        return f"`[{message['thread_id']}]` **{message['author']}**: {message['content']}"

    @classmethod
    def from_env(cls, url_env: str) -> "DiscordWebhookMirror | None":
        url = os.environ.get(url_env)
        if not url:
            return None
        return cls(webhook_url=url)


def mirror_from_config(spec: dict[str, Any] | None) -> DiscordWebhookMirror | None:
    """Build a mirror from config; returns None when disabled / env missing."""
    if not spec:
        return None
    mtype = spec.get("type")
    if mtype == "discord_webhook":
        return DiscordWebhookMirror.from_env(spec["url_env"])
    raise ValueError(f"unknown mirror type: {mtype!r}")
