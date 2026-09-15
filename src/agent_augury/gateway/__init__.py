"""Session Gateway — in-proc fan-out bus for Augury Wire messages.

Design: docs/architecture/MULTI_FRONT_DESIGN.md (M1).
Wire schemas: schemas/wire/.
"""

from __future__ import annotations

from typing import Any

from .bridge import BridgeBindOptions, PendingApproval, PendingQuestion, SessionBridge
from .host import bootstrap_gateway_host
from .register import register_chat_surface, register_ui_surface
from .bus import SessionGateway, SurfaceMode, SurfaceSubscription
from .stdio import JsonlStdioBridge, decode_line, encode_line, run_stdio_bridge
from .translate import translate_core_event
from .types import (
    COMMAND_TYPES,
    EVENT_TYPES,
    WireCommand,
    WireEvent,
    WireMessage,
    WireResult,
    make_command,
    make_event,
    make_result,
)

# headless imports Session — keep lazy to avoid session↔gateway circular import
_HEADLESS_EXPORTS = frozenset(
    {"HeadlessRunner", "emit_startup_warnings", "run_headless_session"}
)


def __getattr__(name: str) -> Any:
    if name in _HEADLESS_EXPORTS:
        from . import headless

        return getattr(headless, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BridgeBindOptions",
    "COMMAND_TYPES",
    "EVENT_TYPES",
    "HeadlessRunner",
    "bootstrap_gateway_host",
    "JsonlStdioBridge",
    "PendingApproval",
    "PendingQuestion",
    "SessionBridge",
    "SessionGateway",
    "SurfaceMode",
    "SurfaceSubscription",
    "WireCommand",
    "WireEvent",
    "WireMessage",
    "WireResult",
    "decode_line",
    "emit_startup_warnings",
    "encode_line",
    "make_command",
    "make_event",
    "make_result",
    "register_chat_surface",
    "register_ui_surface",
    "run_headless_session",
    "run_stdio_bridge",
    "translate_core_event",
]
