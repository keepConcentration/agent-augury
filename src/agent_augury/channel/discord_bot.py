"""Discord bot adapter — N개 discord.Client in a single asyncio loop.

Outbound: send-only observation (SSOT — MessageServer is truth).
Inbound (M5): optional ``inbound=True`` registers ``on_message`` and forwards
channel text to a Gateway handler as Wire ``human.*`` commands.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

import discord

log = logging.getLogger(__name__)

# Discord message limit is 2000; we stay under with headroom.
_MAX_CONTENT = 1800

InboundHandler = Callable[..., None]
_MENTION_RE = re.compile(r"<@!?\d+>")


class DiscordBotAdapter:
    """One discord.Client bound to one agent + one channel.

    Default: send-only. With ``inbound=True``, messages in ``channel_id``
    (non-bot authors) are forwarded to ``set_inbound_handler``.
    """

    def __init__(
        self,
        agent_id: str,
        token: str,
        channel_id: int,
        *,
        inbound: bool = False,
        intents: discord.Intents | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.channel_id = channel_id
        self.inbound = bool(inbound)
        self._token = token
        self._inbound_handler: InboundHandler | None = None

        if intents is None:
            intents = discord.Intents.default()
            if self.inbound:
                # Required to read message bodies in guild channels.
                intents.message_content = True

        self._client = discord.Client(intents=intents)
        self._ready = asyncio.Event()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None

        @self._client.event
        async def on_ready() -> None:
            self._ready.set()
            log.info("bot %s ready as %s", self.agent_id, self._client.user)

        if self.inbound:
            @self._client.event
            async def on_message(message: discord.Message) -> None:
                self._handle_inbound_message(message)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        """Install/replace the callback used when ``inbound=True``."""
        self._inbound_handler = handler

    def _handle_inbound_message(self, message: discord.Message) -> None:
        if not self.inbound or self._inbound_handler is None:
            return
        if not accept_inbound_message(
            message,
            channel_id=self.channel_id,
            bot_user_id=getattr(self._client.user, "id", None),
        ):
            return
        content = normalize_inbound_content(
            message.content or "",
            bot_user_id=getattr(self._client.user, "id", None),
        )
        if not content:
            return
        try:
            self._inbound_handler(
                content,
                user_id=str(message.author.id),
                channel_id=int(message.channel.id),
            )
        except Exception:
            log.exception("bot %s inbound handler failed", self.agent_id)

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
            except Exception:
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

    def adapters(self) -> list[DiscordBotAdapter]:
        return list(self._bots.values())

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


def accept_inbound_message(
    message: Any,
    *,
    channel_id: int,
    bot_user_id: int | None,
) -> bool:
    """Return True when *message* should be treated as human input."""
    author = getattr(message, "author", None)
    if author is None:
        return False
    if getattr(author, "bot", False):
        return False
    if bot_user_id is not None and getattr(author, "id", None) == bot_user_id:
        return False
    channel = getattr(message, "channel", None)
    return channel is not None and int(getattr(channel, "id", 0)) == int(channel_id)


def normalize_inbound_content(content: str, *, bot_user_id: int | None = None) -> str:
    """Strip bot mentions and collapse whitespace."""
    text = content or ""
    if bot_user_id is not None:
        text = re.sub(rf"<@!?{bot_user_id}>", "", text)
    text = _MENTION_RE.sub("", text)
    return " ".join(text.split()).strip()


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
