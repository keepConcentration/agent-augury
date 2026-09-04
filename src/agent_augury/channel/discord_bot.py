"""Discord bot adapter — N개 discord.Client in a single asyncio loop.

Read-only observation: the bot only SENDS messages to a configured channel.
It never reads or processes inbound Discord messages (SSOT principle —
MessageServer is the single source of truth).

Design (DISCORD_BOT_INTEGRATION.md §4):
  - DiscordBotAdapter: wraps one discord.Client for one agent
  - BotManager: owns N adapters, routes events by agent_id
  - Single asyncio loop, cooperative scheduling (no locks)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

# Discord message limit is 2000; we stay under with headroom.
_MAX_CONTENT = 1800


class DiscordBotAdapter:
    """One discord.Client bound to one agent + one channel.

    Send-only: ``enqueue()`` pushes text; a sender task drains the outbox
    to the Discord channel.  No on_message handler is registered.
    """

    def __init__(
        self,
        agent_id: str,
        token: str,
        channel_id: int,
        *,
        intents: discord.Intents | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.channel_id = channel_id
        self._token = token
        self._client = discord.Client(
            intents=intents or discord.Intents.default(),
        )
        self._ready = asyncio.Event()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None

        @self._client.event
        async def on_ready() -> None:
            self._ready.set()
            log.info("bot %s ready as %s", self.agent_id, self._client.user)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the sender task and log in to Discord."""
        self._sender_task = asyncio.create_task(self._sender_loop())
        await self._client.start(self._token)

    async def close(self) -> None:
        """Cancel sender and close the gateway connection."""
        if self._sender_task:
            self._sender_task.cancel()
        await self._client.close()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Wait until on_ready fires (gateway handshake complete)."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    # -- send path -----------------------------------------------------------

    def enqueue(self, content: str) -> None:
        """Thread-safe-ish: call from the same loop that drives the client."""
        if len(content) > _MAX_CONTENT:
            content = content[:_MAX_CONTENT] + "…"
        self._outbox.put_nowait(content)

    async def _sender_loop(self) -> None:
        """Drain outbox → channel.send(), swallowing HTTP errors."""
        await self._ready.wait()
        channel = self._client.get_channel(self.channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self.channel_id)

        while True:
            content = await self._outbox.get()
            try:
                await channel.send(content)  # type: ignore[union-attr]
            except discord.HTTPException as exc:
                log.warning("bot %s send failed: %s", self.agent_id, exc)
            except Exception:  # noqa: BLE001 — observation must not kill sessions
                log.exception("bot %s unexpected error", self.agent_id)


class BotManager:
    """Owns N DiscordBotAdapter instances and routes events by agent_id.

    The manager is a thin dict wrapper — it does NOT own the asyncio loop.
    All adapters share the loop that calls ``start_all()`` / ``stop_all()``.
    """

    def __init__(self) -> None:
        self._bots: dict[str, DiscordBotAdapter] = {}

    def register(self, bot: DiscordBotAdapter) -> None:
        """Register an adapter.  Re-registering the same agent_id overwrites."""
        self._bots[bot.agent_id] = bot

    def route_event(self, agent_id: str, content: str) -> None:
        """Enqueue a message to the bot for *agent_id* (no-op if unknown)."""
        bot = self._bots.get(agent_id)
        if bot:
            bot.enqueue(content)

    async def start_all(self) -> None:
        """Login every bot concurrently (asyncio.gather)."""
        await asyncio.gather(*(bot.start() for bot in self._bots.values()))

    async def stop_all(self) -> None:
        """Close every bot concurrently."""
        await asyncio.gather(*(bot.close() for bot in self._bots.values()))

    def __len__(self) -> int:
        return len(self._bots)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._bots


def _format_event(event: dict[str, Any]) -> str | None:
    """Convert a MessageServer event dict to a Discord message string.

    Returns None for event types that should not be mirrored.
    """
    event_type = event.get("type")

    if event_type == "create_thread":
        name = event.get("name", "?")
        participants = ", ".join(event.get("participants", []))
        return f"🧵 create_thread **{name}** ({participants})"

    if event_type == "send_message":
        author = event.get("author", "?")
        content = event.get("content", "")
        if len(content) > _MAX_CONTENT:
            content = content[:_MAX_CONTENT] + "…"
        return f"💬 {author}: {content}"

    if event_type == "tool":
        agent_id = event.get("agent_id", "?")
        tool = event.get("tool", "?")
        icons = {
            "read_file": "📖",
            "write_file": "📝",
            "list_directory": "📁",
            "search": "🔍",
            "send_message": "💬",
            "create_thread": "🧵",
            "read_resource": "📊",
        }
        icon = icons.get(tool, "🔧")
        return f"{icon} {agent_id}: {tool}(...)"

    if event_type == "read_resource":
        agent_id = event.get("agent_id", "?")
        threads = event.get("threads", 0)
        messages = event.get("messages", 0)
        return f"📊 {agent_id}: read_resource (threads={threads}, messages={messages})"

    if event_type == "step":
        agent_id = event.get("agent_id", "?")
        result = event.get("result")
        text = getattr(result, "text", None) if result else None
        if text:
            if len(text) > _MAX_CONTENT:
                text = text[:_MAX_CONTENT] + "…"
            return f"💭 {agent_id}: {text}"
        return None

    return None
