"""Channel Adapter protocol (Chat surfaces — observe by default)."""

from __future__ import annotations

from typing import Literal, Protocol

from agent_augury.gateway.types import WireEvent

SurfaceMode = Literal["observe", "interact"]


class ChannelAdapter(Protocol):
    """Platform-specific outbound (and optional inbound) adapter.

    M4: observe-only Discord. Inbound HITL is M5 (opt-in).
    """

    name: str
    mode: SurfaceMode

    def on_wire_event(self, event: WireEvent) -> None:
        """Handle one Wire event. Must not raise into Core/Gateway."""
        ...
