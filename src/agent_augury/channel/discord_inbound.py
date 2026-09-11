"""Discord inbound HITL (M5) — opt-in only.

When ``bots[].inbound: true``, channel messages become Wire ``human.*``
commands on an interact Gateway surface. Default remains observe-only (D2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from agent_augury.gateway.bridge import SessionBridge
from agent_augury.gateway.bus import SessionGateway, SurfaceSubscription
from agent_augury.gateway.types import make_command

from .discord_bot import BotManager

INBOUND_SURFACE = "discord-inbound"


def attach_discord_inbound(
    gateway: SessionGateway,
    bridge: SessionBridge,
    bot_manager: BotManager,
) -> bool:
    """Enable Gateway interact surface + per-bot on_message handlers.

    Returns True if at least one bot had ``inbound=True``.
    """
    inbound_bots = [b for b in bot_manager.adapters() if b.inbound]
    if not inbound_bots:
        return False

    if INBOUND_SURFACE not in gateway.surfaces():
        gateway.attach(
            SurfaceSubscription(
                name=INBOUND_SURFACE,
                mode="interact",
                family="chat",
                on_event=None,
            )
        )

    for bot in inbound_bots:
        agent_id = bot.agent_id

        def _on_inbound(
            content: str,
            *,
            user_id: str,
            channel_id: int,
            _agent_id: str = agent_id,
        ) -> None:
            dispatch_discord_inbound(
                gateway,
                bridge,
                content,
                agent_id=_agent_id,
                user_id=user_id,
                channel_id=channel_id,
            )

        bot.set_inbound_handler(_on_inbound)
    return True


def dispatch_discord_inbound(
    gateway: SessionGateway,
    bridge: SessionBridge,
    content: str,
    *,
    agent_id: str,
    user_id: str,
    channel_id: int,
) -> dict[str, Any]:
    """Map one Discord user message to ``human.answer`` or ``human.send``."""
    text = (content or "").strip()
    if not text:
        return {"ok": False, "error": "empty content"}

    source = {
        "surface": "discord",
        "mode": "interact",
        "user": str(user_id),
        "channel": str(channel_id),
    }
    cmd_id = str(uuid4())
    pending = bridge.pending
    if pending is not None:
        cmd = make_command(
            "human.answer",
            id=cmd_id,
            content=text,
            thread_id=pending.thread_id or None,
            question_id=pending.question_id or None,
            source=source,
        )
    else:
        fields: dict[str, Any] = {
            "content": text,
            "mentions": [agent_id],
            "source": source,
        }
        if bridge.recent_thread:
            fields["thread_id"] = bridge.recent_thread
        cmd = make_command("human.send", id=cmd_id, **fields)

    return gateway.dispatch(cmd, surface=INBOUND_SURFACE)


InboundHandler = Callable[..., None]
