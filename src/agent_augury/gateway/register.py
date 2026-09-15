"""Surface registration SSOT (chat + UI families).

Design: docs/architecture/GATEWAY_SURFACE_BINDING_DESIGN.md (G3).
"""

from __future__ import annotations

from collections.abc import Callable

from .bus import DEFAULT_CHAT_MAILBOX_MAX, SessionGateway, SurfaceMode, SurfaceSubscription
from .types import WireEvent

EventHandler = Callable[[WireEvent], None]


def _guarded(handler: EventHandler) -> EventHandler:
    def on_event(event: WireEvent) -> None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001, S110 — chat observe must not break fan-out
            pass

    return on_event


def register_chat_surface(
    gateway: SessionGateway,
    *,
    name: str,
    mode: SurfaceMode,
    on_event: EventHandler | None,
    event_types: frozenset[str] | None,
    mailbox_max: int = DEFAULT_CHAT_MAILBOX_MAX,
) -> None:
    """Attach one chat-family surface (Discord/Slack observe or interact shell)."""
    wrapped = _guarded(on_event) if on_event is not None else None
    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode=mode,
            family="chat",
            on_event=wrapped,
            event_types=event_types,
            mailbox_max=mailbox_max if mailbox_max > 0 else None,
        )
    )


def register_ui_surface(
    gateway: SessionGateway,
    *,
    name: str,
    mode: SurfaceMode,
    on_event: EventHandler,
    event_types: frozenset[str] | None = None,
) -> None:
    """Attach one UI-family surface (Ink JSONL, headless stderr, …).

    Sync delivery (no mailbox) so JSONL order stays tight with cmd results;
    bus still isolates exceptions (D5).
    """
    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode=mode,
            family="ui",
            on_event=on_event,
            event_types=event_types,
            mailbox_max=None,
        )
    )
