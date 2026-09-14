"""Session Gateway — in-proc fan-out bus for Augury Wire messages.

Design: docs/architecture/MULTI_FRONT_DESIGN.md (M1).
Wire schemas: schemas/wire/.
"""

from __future__ import annotations

from .bridge import PendingApproval, PendingQuestion, SessionBridge
from .bus import SessionGateway, SurfaceMode, SurfaceSubscription
from .headless import HeadlessRunner, emit_startup_warnings, run_headless_session
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
    "HeadlessRunner",
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
    "run_headless_session",
    "run_stdio_bridge",
    "translate_core_event",
]
