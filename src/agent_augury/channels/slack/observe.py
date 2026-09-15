"""Attach Slack webhook mirror as a Gateway observe-only surface (M6)."""

from __future__ import annotations

from agent_augury.gateway.bus import SessionGateway
from agent_augury.gateway.register import register_chat_surface
from agent_augury.gateway.types import WireEvent

from ..display import ChatDisplayPolicy
from .mirror import SlackWebhookMirror

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
    display: ChatDisplayPolicy | None = None,
) -> None:
    """Subscribe *mirror* to Wire events (observe-only)."""
    policy = display or ChatDisplayPolicy()

    def on_event(event: WireEvent) -> None:
        try:
            if not policy.allow(event):
                return
            text = _format_slack_event(event, policy=policy)
            if text:
                mirror.enqueue_text(text)
        except Exception as exc:  # noqa: BLE001 — observation must not kill sessions
            mirror.errors.append(exc)

    register_chat_surface(
        gateway,
        name=name,
        mode="observe",
        on_event=on_event,
        event_types=_SLACK_TYPES,
    )


def _format_slack_event(
    event: WireEvent,
    *,
    policy: ChatDisplayPolicy | None = None,
) -> str | None:
    pol = policy or ChatDisplayPolicy()
    if event.get("type") == "message":
        if str(event.get("content") or "").startswith("[ask-user]"):
            return None
        return SlackWebhookMirror.format_message_line(event)
    return pol.format_wire(event)
