"""Chat surface handler typing (registration via ``attach_*`` + ``register_chat_surface``)."""

from __future__ import annotations

from typing import Protocol

from agent_augury.gateway.bus import SurfaceMode
from agent_augury.gateway.types import WireEvent

__all__ = ["ChannelAdapter", "SurfaceMode"]


class ChannelAdapter(Protocol):
    """Wire event handler shape for chat surfaces (tests / future thin wrappers).

    Production Discord/Slack use ``channels/*/observe.py`` ``attach_*`` helpers;
    platform mirror/bot classes are send sinks, not adapters themselves.
    """

    name: str
    mode: SurfaceMode

    def on_wire_event(self, event: WireEvent) -> None:
        """Handle one Wire event. Must not raise into Core/Gateway."""
        ...
