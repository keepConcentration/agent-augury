"""Attach Discord mirror/bots as Gateway observe-only surfaces (M4).

Behavior parity with pre-Gateway wiring:

- webhook mirror: formatted lines from ``message`` Wire events
  (was ``server.subscribe(mirror.on_message)``)
- per-agent bots: chat-surface text from tool/thread/message Wire events
  (was ``BotManager.route_event`` inside ``Session._on_server_event``)
"""

from __future__ import annotations

from typing import Any

from agent_augury.gateway.bus import SessionGateway, SurfaceSubscription
from agent_augury.gateway.types import WireEvent

from .chat_surface_format import format_wire_for_chat_surface
from .discord_approval import ToolApprovalView, approval_prompt_text
from .discord_bot import BotManager
from .discord_mirror import DiscordWebhookMirror

_MIRROR_TYPES = frozenset({"message"})
_BOT_TYPES = frozenset({
    # Chat UX: agent prose + HITL. Skip high-volume tool/read_resource noise so
    # shared channels (N bots) do not 429-drop the messages users care about.
    "message",
    "thread.created",
    "agent.step",
    "human.question",
    "approval.request",
    "approval.resolved",
    "approval.expired",
    "tool.denied",
    "log",
})


def attach_discord_mirror(
    gateway: SessionGateway,
    mirror: DiscordWebhookMirror,
    *,
    name: str = "discord-mirror",
) -> None:
    """Subscribe *mirror* to Wire ``message`` events (observe-only)."""

    def on_event(event: WireEvent) -> None:
        try:
            if event.get("type") != "message":
                return
            # D7: ask_user also emits human.question — skip the radio duplicate.
            if str(event.get("content") or "").startswith("[ask-user]"):
                return
            mirror.enqueue(_wire_to_mirror_message(event))
        except Exception as exc:  # noqa: BLE001 — observation must not kill sessions
            mirror.errors.append(exc)

    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode="observe",
            family="chat",
            on_event=on_event,
            event_types=_MIRROR_TYPES,
        )
    )


def attach_discord_bots(
    gateway: SessionGateway,
    bot_manager: BotManager,
    *,
    name: str = "discord-bots",
) -> None:
    """Subscribe *bot_manager* to Wire events (observe-only, per-agent route)."""

    def on_event(event: WireEvent) -> None:
        try:
            agent_id = event.get("agent_id") or event.get("author")
            if not agent_id:
                return
            recipient = str(agent_id)
            if event.get("type") == "approval.request":
                aid = str(event.get("approval_id") or "").strip()
                if not aid:
                    return
                body = approval_prompt_text(event, recipient_agent_id=recipient)
                aid_local = aid
                bot_manager.route_event(
                    recipient,
                    body,
                    view_factory=lambda a=aid_local: ToolApprovalView(a),
                )
                return
            content = format_wire_for_bot(event, recipient_agent_id=recipient)
            if not content:
                return
            bot_manager.route_event(recipient, content)
        except Exception:  # noqa: BLE001, S110 — never break Core
            pass

    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode="observe",
            family="chat",
            on_event=on_event,
            event_types=_BOT_TYPES,
        )
    )


def _wire_to_mirror_message(event: WireEvent) -> dict[str, Any]:
    author = event.get("author") or event.get("agent_id") or "?"
    return {
        "thread_id": event.get("thread_id") or "",
        "author": author,
        "content": event.get("content") or "",
    }


def format_wire_for_bot(
    event: WireEvent,
    *,
    recipient_agent_id: str | None = None,
) -> str | None:
    """Format a Wire event for Discord/Slack chat surfaces."""
    return format_wire_for_chat_surface(
        event,
        recipient_agent_id=recipient_agent_id,
    )
