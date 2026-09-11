"""Attach Discord mirror/bots as Gateway observe-only surfaces (M4).

Behavior parity with pre-Gateway wiring:

- webhook mirror: formatted lines from ``message`` Wire events
  (was ``server.subscribe(mirror.on_message)``)
- per-agent bots: formatted lines from tool/thread/message Wire events
  (was ``BotManager.route_event`` inside ``Session._on_server_event``)
"""

from __future__ import annotations

from typing import Any

from agent_augury.gateway.bus import SessionGateway, SurfaceSubscription
from agent_augury.gateway.types import WireEvent

from .discord_bot import BotManager, _format_event
from .discord_mirror import DiscordWebhookMirror

_MIRROR_TYPES = frozenset({"message"})
_BOT_TYPES = frozenset({
    "message",
    "thread.created",
    "tool",
    "agent.step",
    "human.question",
    "log",
    "read_resource",
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
            content = format_wire_for_bot(event)
            if not content:
                return
            agent_id = event.get("agent_id") or event.get("author")
            if agent_id:
                bot_manager.route_event(str(agent_id), content)
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


def format_wire_for_bot(event: WireEvent) -> str | None:
    """Format a Wire event the way ``_format_event`` did for Core events."""
    etype = event.get("type")
    if etype == "thread.created":
        return _format_event(
            {
                "type": "create_thread",
                "name": event.get("name", "?"),
                "participants": list(event.get("participants") or []),
            }
        )
    if etype == "message":
        return _format_event(
            {
                "type": "send_message",
                "author": event.get("author") or event.get("agent_id") or "?",
                "content": event.get("content") or "",
            }
        )
    if etype == "tool":
        return _format_event(
            {
                "type": "tool",
                "agent_id": event.get("agent_id", "?"),
                "tool": event.get("tool", "?"),
            }
        )
    if etype == "agent.step":
        result = event.get("result") or {}
        text = result.get("text") if isinstance(result, dict) else None

        class _R:
            pass

        r = _R()
        r.text = text  # type: ignore[attr-defined]
        return _format_event(
            {"type": "step", "agent_id": event.get("agent_id", "?"), "result": r}
        )
    if etype == "read_resource":
        return _format_event(
            {
                "type": "read_resource",
                "agent_id": event.get("agent_id", "?"),
                "threads": event.get("threads", 0),
                "messages": event.get("messages", 0),
            }
        )
    if etype == "human.question":
        q = event.get("question") or ""
        agent = event.get("agent_id") or "?"
        return f"❓ {agent}: {q}"
    if etype == "log" and event.get("text"):
        return str(event["text"])
    return None
