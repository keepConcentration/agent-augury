"""Session Gateway — in-proc fan-out bus for Augury Wire messages.

Design: docs/architecture/MULTI_FRONT_DESIGN.md (M1).
Wire schemas: schemas/wire/.
"""

from __future__ import annotations

from .bridge import PendingQuestion, SessionBridge
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

__all__ = [
    "COMMAND_TYPES",
    "EVENT_TYPES",
    "JsonlStdioBridge",
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
    "encode_line",
    "make_command",
    "make_event",
    "make_result",
    "run_stdio_bridge",
    "translate_core_event",
]
