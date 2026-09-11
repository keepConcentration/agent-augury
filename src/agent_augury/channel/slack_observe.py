"""Attach Slack webhook mirror as a Gateway observe-only surface (M6)."""

from __future__ import annotations

from agent_augury.gateway.bus import SessionGateway, SurfaceSubscription
from agent_augury.gateway.types import WireEvent

from .discord_observe import format_wire_for_bot
from .slack_mirror import SlackWebhookMirror

_SLACK_TYPES = frozenset({
    "message",
    "thread.created",
    "tool",
    "agent.step",
    "human.question",
    "log",
    "read_resource",
})


def attach_slack_mirror(
    gateway: SessionGateway,
    mirror: SlackWebhookMirror,
    *,
    name: str = "slack-mirror",
) -> None:
    """Subscribe *mirror* to Wire events (observe-only)."""

    def on_event(event: WireEvent) -> None:
        try:
            text = _format_slack_event(event)
            if text:
                mirror.enqueue_text(text)
        except Exception as exc:  # noqa: BLE001 — observation must not kill sessions
            mirror.errors.append(exc)

    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode="observe",
            family="chat",
            on_event=on_event,
            event_types=_SLACK_TYPES,
        )
    )


def _format_slack_event(event: WireEvent) -> str | None:
    if event.get("type") == "message":
        return SlackWebhookMirror.format_message_line(event)
    # Reuse Discord wire→text mapping for tool/step/thread (plain text ok for Slack).
    return format_wire_for_bot(event)
