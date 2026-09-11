"""Slack Incoming Webhook observe mirror (M6 spike).

Same contract as DiscordWebhookMirror: enqueue on Wire events, ``flush()``
posts to a webhook. Failures are recorded, never raised into Core.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Slack text practical limit; leave headroom for mrkdwn.
_MAX_CONTENT = 3000


class SlackWebhookMirror:
    """Enqueue-on-event, flush-to-Slack-webhook. Observation must never raise."""

    def __init__(
        self,
        webhook_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self.outbox: list[str] = []
        self.errors: list[Exception] = []

    def enqueue_text(self, text: str) -> None:
        if not text:
            return
        if len(text) > _MAX_CONTENT:
            text = text[:_MAX_CONTENT] + "…"
        self.outbox.append(text)

    async def flush(self) -> int:
        """Post every queued line. Swallows HTTP failures (records them)."""
        sent = 0
        while self.outbox:
            content = self.outbox.pop(0)
            try:
                response = await self._client.post(
                    self.webhook_url,
                    json={"text": content},
                )
                response.raise_for_status()
                sent += 1
            except Exception as exc:  # noqa: BLE001 — observation must not kill sessions
                self.errors.append(exc)
        return sent

    async def aclose(self) -> None:
        if self._owns_client and hasattr(self._client, "aclose"):
            await self._client.aclose()

    @staticmethod
    def format_message_line(event: dict[str, Any]) -> str:
        author = event.get("author") or event.get("agent_id") or "?"
        thread = event.get("thread_id") or "?"
        content = event.get("content") or ""
        return f"`[{thread}]` *{author}*: {content}"

    @classmethod
    def from_env(cls, url_env: str) -> SlackWebhookMirror | None:
        url = os.environ.get(url_env)
        if not url:
            return None
        return cls(webhook_url=url)


def slack_from_config(spec: dict[str, Any] | None) -> SlackWebhookMirror | None:
    """Build Slack observe mirror from config; None when disabled / env missing."""
    if not spec:
        return None
    if spec.get("enabled") is False:
        return None
    # mode defaults to observe; reject interact until M6+inbound exists
    mode = spec.get("mode", "observe")
    if mode not in (None, "observe"):
        raise ValueError(
            f"slack.mode={mode!r} not supported in M6 (observe-only spike)"
        )
    url_env = spec.get("url_env")
    if not url_env:
        # allow nested webhook: { webhook: { url_env: ... } }
        webhook = spec.get("webhook")
        if isinstance(webhook, dict):
            url_env = webhook.get("url_env")
    if not url_env:
        raise ValueError("slack requires 'url_env' (or webhook.url_env)")
    return SlackWebhookMirror.from_env(str(url_env))
