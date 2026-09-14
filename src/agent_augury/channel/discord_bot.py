"""Discord bot adapter — N개 discord.Client in a single asyncio loop.

Outbound: send-only observation (SSOT — MessageServer is truth).
Inbound (M5): optional ``inbound=True`` registers ``on_message`` and forwards
channel text to a Gateway handler as Wire ``human.*`` commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

from ..bot_token_env import (
    DEFAULT_SECRETS_ENV_PATH,
    discord_token_shape_hint,
    looks_like_discord_bot_token,
    normalize_discord_token,
)

log = logging.getLogger(__name__)

# Discord message limit is 2000; we stay under with headroom.
_MAX_CONTENT = 1800
# Shared-channel pacing: Discord allows ~5 msg / 5s per channel.
_CHANNEL_MIN_INTERVAL_S = 1.05
_MAX_SEND_RETRIES = 4

InboundHandler = Callable[..., None]
_MENTION_RE = re.compile(r"<@!?\d+>")

# One lock + last-send time per Discord channel (shared by N bots).
_channel_gates: dict[int, asyncio.Lock] = {}
_channel_last_send: dict[int, float] = {}

# Deduplicate the same Discord message across N inbound bots (e.g. "1" approve).
_inbound_seen: dict[str, float] = {}
_INBOUND_DEDUP_TTL_S = 5.0


@dataclass
class OutboundMessage:
    """One Discord channel.send payload (optional button View)."""

    content: str
    view: Any | None = None
    view_factory: Any | None = None  # callable → View (created on send loop)


def _inbound_already_seen(message_id: str) -> bool:
    """True if this Discord message id was already handled by another bot."""
    now = time.monotonic()
    # Opportunistic prune
    stale = [k for k, t in _inbound_seen.items() if now - t > _INBOUND_DEDUP_TTL_S]
    for k in stale:
        _inbound_seen.pop(k, None)
    if message_id in _inbound_seen:
        return True
    _inbound_seen[message_id] = now
    return False


def _channel_gate(channel_id: int) -> asyncio.Lock:
    lock = _channel_gates.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_gates[channel_id] = lock
    return lock


def split_discord_content(content: str, *, limit: int = _MAX_CONTENT) -> list[str]:
    """Split long prose into Discord-safe chunks (prefer newline boundaries)."""
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = limit
        piece = rest[:cut].rstrip()
        if piece:
            chunks.append(piece)
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


class DiscordBotError(RuntimeError):
    """Raised when a Discord bot cannot start (missing/invalid token, etc.)."""


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
        token_env: str | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.channel_id = channel_id
        self.inbound = bool(inbound)
        self.token_env = token_env or "(token)"
        self._token = token
        self._inbound_handler: InboundHandler | None = None

        if intents is None:
            intents = discord.Intents.default()
            if self.inbound:
                # Required to read message bodies in guild channels.
                intents.message_content = True

        self._client = discord.Client(intents=intents)
        self._ready = asyncio.Event()
        self._outbox: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None
        self._gateway_task: asyncio.Task | None = None
        self._interaction_handler: Callable[[discord.Interaction], Any] | None = None

        @self._client.event
        async def on_ready() -> None:
            self._ready.set()
            log.info("bot %s ready as %s", self.agent_id, self._client.user)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            handler = self._interaction_handler
            if handler is None:
                return
            try:
                result = handler(interaction)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("bot %s interaction handler failed", self.agent_id)

        if self.inbound:
            @self._client.event
            async def on_message(message: discord.Message) -> None:
                self._handle_inbound_message(message)

    def set_inbound_handler(self, handler: InboundHandler | None) -> None:
        """Install/replace the callback used when ``inbound=True``."""
        self._inbound_handler = handler

    def set_interaction_handler(
        self, handler: Callable[[discord.Interaction], Any] | None
    ) -> None:
        """Install Discord component (button) handler for this bot client."""
        self._interaction_handler = handler

    def _handle_inbound_message(self, message: discord.Message) -> None:
        if not self.inbound or self._inbound_handler is None:
            return
        if not accept_inbound_message(
            message,
            channel_id=self.channel_id,
            bot_user_id=getattr(self._client.user, "id", None),
        ):
            return
        mid = str(getattr(message, "id", "") or "")
        if mid and _inbound_already_seen(mid):
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
                message_id=mid,
            )
        except TypeError:
            # Older handlers without message_id kwarg.
            self._inbound_handler(
                content,
                user_id=str(message.author.id),
                channel_id=int(message.channel.id),
            )
        except Exception:
            log.exception("bot %s inbound handler failed", self.agent_id)

    # -- lifecycle -----------------------------------------------------------

    async def _teardown_start_failure(self) -> None:
        if self._gateway_task is not None:
            if not self._gateway_task.done():
                self._gateway_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._gateway_task
            self._gateway_task = None
        if self._sender_task is not None:
            self._sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender_task
            self._sender_task = None
        self._ready.clear()

    async def start(self) -> None:
        """Log in to Discord and wait until ``on_ready`` (gateway keeps running)."""
        if self._gateway_task is not None and not self._gateway_task.done():
            return

        token = normalize_discord_token(self._token or "")
        self._token = token
        if not token:
            if looks_like_discord_bot_token(self.token_env):
                suggested = f"BOT_TOKEN_{self.agent_id.replace('-', '_').upper()}"
                raise DiscordBotError(
                    f"Discord bot for agent {self.agent_id!r}: `token_env` in your "
                    f"config looks like a bot token, not an environment variable name.\n"
                    f"  Edit the YAML: set token_env to e.g. {suggested!r}, then add\n"
                    f"    {suggested}=<your Bot Token from Discord Developer Portal>\n"
                    f"  to `.env` (or the shell). YAML must store the env *name* only."
                )
            raise DiscordBotError(
                f"Discord bot for agent {self.agent_id!r}: "
                f"environment variable {self.token_env!r} is unset or empty.\n"
                f"  Put the bot token in `.env` (or the shell), e.g.\n"
                f"    {self.token_env}=your_discord_bot_token\n"
                f"  Then re-run. (YAML only stores the env *name*, not the secret.)"
            )

        self._ready.clear()
        self._sender_task = asyncio.create_task(self._sender_loop())
        self._gateway_task = asyncio.create_task(self._client.start(token))

        ready_wait = asyncio.create_task(self.wait_ready(timeout=30.0))
        gateway = self._gateway_task
        done, _pending = await asyncio.wait(
            {ready_wait, gateway},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if gateway in done:
            ready_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_wait
            exc = gateway.exception()
            if exc is not None:
                await self._teardown_start_failure()
                if isinstance(exc, discord.LoginFailure):
                    shape = discord_token_shape_hint(token)
                    extra = f"\n  Hint: {shape}" if shape else ""
                    raise DiscordBotError(
                        f"Discord bot for agent {self.agent_id!r}: login failed "
                        f"(env {self.token_env!r}).\n"
                        f"  Check `{self.token_env}` — Developer Portal → Bot → Token "
                        f"(not OAuth Client Secret).\n"
                        f"  If you run from a git checkout, also check "
                        f"`{Path.cwd() / '.env'}` vs `{DEFAULT_SECRETS_ENV_PATH}` "
                        f"(wizard tokens live in the latter; stale repo `.env` used to win).\n"
                        f"  Discord said: {exc}{extra}"
                    ) from exc
                raise DiscordBotError(
                    f"Discord bot for agent {self.agent_id!r}: gateway failed ({exc!r})"
                ) from exc
            await self._teardown_start_failure()
            raise DiscordBotError(
                f"Discord bot for agent {self.agent_id!r}: gateway stopped before on_ready "
                f"(env {self.token_env!r})."
            )

        ready_exc = ready_wait.exception()
        if ready_exc is None:
            return

        await self._teardown_start_failure()
        if isinstance(ready_exc, asyncio.TimeoutError):
            raise DiscordBotError(
                f"Discord bot for agent {self.agent_id!r}: timed out waiting for on_ready "
                f"(env {self.token_env!r})."
            ) from ready_exc
        raise DiscordBotError(
            f"Discord bot for agent {self.agent_id!r}: failed waiting for on_ready ({ready_exc!r})"
        ) from ready_exc

    async def close(self) -> None:
        """Cancel sender and close the gateway connection."""
        if self._sender_task:
            self._sender_task.cancel()
            self._sender_task = None
        await self._client.close()
        if self._gateway_task is not None:
            if not self._gateway_task.done():
                self._gateway_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gateway_task
            self._gateway_task = None

    async def wait_ready(self, timeout: float = 30.0) -> None:
        """Wait until on_ready fires (gateway handshake complete)."""
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    # -- send path -----------------------------------------------------------

    def enqueue(
        self,
        content: str,
        *,
        view: Any | None = None,
        view_factory: Any | None = None,
    ) -> None:
        """Queue one or more Discord messages (long text is split; view on last)."""
        chunks = split_discord_content(content, limit=_MAX_CONTENT)
        if not chunks and (view is not None or view_factory is not None):
            chunks = ["🔐 Approval needed"]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self._outbox.put_nowait(
                OutboundMessage(
                    content=chunk,
                    view=view if last else None,
                    view_factory=view_factory if last else None,
                )
            )

    async def flush(self, timeout: float = 30.0) -> None:
        """Wait until the outbox is empty (best-effort)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._outbox.empty():
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)

    async def _sender_loop(self) -> None:
        """Drain outbox → channel.send() with shared-channel pacing + 429 retry."""
        await self._ready.wait()
        channel = self._client.get_channel(self.channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self.channel_id)

        while True:
            item = await self._outbox.get()
            if isinstance(item, str):
                item = OutboundMessage(content=item)
            await self._send_with_retry(channel, item)

    async def _send_with_retry(self, channel: Any, item: OutboundMessage) -> None:
        gate = _channel_gate(self.channel_id)
        for attempt in range(_MAX_SEND_RETRIES):
            async with gate:
                last = _channel_last_send.get(self.channel_id, 0.0)
                now = asyncio.get_running_loop().time()
                wait = _CHANNEL_MIN_INTERVAL_S - (now - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    kwargs: dict[str, Any] = {"content": item.content}
                    view = item.view
                    if view is None and item.view_factory is not None:
                        view = item.view_factory()
                    if view is not None:
                        kwargs["view"] = view
                    await channel.send(**kwargs)
                    _channel_last_send[self.channel_id] = (
                        asyncio.get_running_loop().time()
                    )
                    return
                except discord.HTTPException as exc:
                    retry_after = getattr(exc, "retry_after", None)
                    if exc.status == 429 and attempt + 1 < _MAX_SEND_RETRIES:
                        delay = float(retry_after) if retry_after is not None else 1.5
                        log.warning(
                            "bot %s rate-limited; retry in %.1fs",
                            self.agent_id,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    log.warning("bot %s send failed: %s", self.agent_id, exc)
                    print(
                        f"warning: Discord bot {self.agent_id!r} failed to send "
                        f"({exc})",
                        flush=True,
                    )
                    return
                except Exception as exc:
                    log.exception("bot %s unexpected send error", self.agent_id)
                    print(
                        f"warning: Discord bot {self.agent_id!r} send error: {exc}",
                        flush=True,
                    )
                    return

    def route_approval(self, content: str, view: Any) -> None:
        """Enqueue an approval prompt with Approve/Deny buttons."""
        self.enqueue(content, view=view)


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

    def route_event(
        self,
        agent_id: str,
        content: str,
        *,
        view: Any | None = None,
        view_factory: Any | None = None,
    ) -> None:
        """Enqueue a message to the bot for *agent_id* (no-op if unknown)."""
        bot = self._bots.get(agent_id)
        if bot:
            bot.enqueue(content, view=view, view_factory=view_factory)

    async def start_all(self) -> None:
        """Login every bot concurrently (asyncio.gather)."""
        bots = list(self._bots.values())
        if not bots:
            return
        results = await asyncio.gather(
            *(bot.start() for bot in bots),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if not errors:
            return
        # Prefer our wrapped DiscordBotError; otherwise surface the first failure.
        for err in errors:
            if isinstance(err, DiscordBotError):
                raise err
        raise DiscordBotError(str(errors[0])) from errors[0]

    async def stop_all(self) -> None:
        """Close every bot concurrently."""
        await asyncio.gather(*(bot.close() for bot in self._bots.values()))

    async def flush(self, timeout: float = 30.0) -> None:
        """Wait for all bot outboxes to drain."""
        await asyncio.gather(*(bot.flush(timeout=timeout) for bot in self._bots.values()))

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
    """Legacy log-line formatter (tests / TUI parity). Chat bots use ``chat_surface_format``."""
    from .chat_surface_format import format_core_event_log_line

    return format_core_event_log_line(event)
