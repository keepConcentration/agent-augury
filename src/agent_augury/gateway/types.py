"""Augury Wire message types (M0/M1).

Matches ``schemas/wire/*.schema.json``. Validation is structural (required
keys + known type names); full JSON Schema engines are optional later.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Literal

WireDir = Literal["event", "cmd", "result"]

EVENT_TYPES = frozenset({
    "session.started",
    "session.phase",
    "agent.step",
    "tool",
    "human.question",
    "thread.created",
    "message",
    "log",
    "session.ended",
    "error",
    "read_resource",
})

COMMAND_TYPES = frozenset({
    "session.start",
    "session.interrupt",
    "session.quit",
    "human.send",
    "human.answer",
    "human.skip",
})

HUMAN_COMMAND_TYPES = frozenset({
    "human.send",
    "human.answer",
    "human.skip",
})

WireMessage = dict[str, Any]
WireEvent = dict[str, Any]
WireCommand = dict[str, Any]
WireResult = dict[str, Any]


class WireError(ValueError):
    """Invalid wire message."""


def make_event(type_: str, **fields: Any) -> WireEvent:
    if type_ not in EVENT_TYPES:
        raise WireError(f"unknown event type: {type_}")
    msg: WireEvent = {"dir": "event", "type": type_}
    msg.update(fields)
    return msg


def make_command(type_: str, id: str, **fields: Any) -> WireCommand:
    if type_ not in COMMAND_TYPES:
        raise WireError(f"unknown command type: {type_}")
    if not id:
        raise WireError("command id is required")
    msg: WireCommand = {"dir": "cmd", "type": type_, "id": id}
    msg.update(fields)
    return msg


def make_result(id: str, *, ok: bool, **fields: Any) -> WireResult:
    if not id:
        raise WireError("result id is required")
    msg: WireResult = {"dir": "result", "id": id, "ok": ok}
    msg.update(fields)
    return msg


def validate_message(msg: Mapping[str, Any]) -> WireMessage:
    """Return a shallow copy after basic structural checks."""
    if not isinstance(msg, Mapping):
        raise WireError("message must be a mapping")
    direction = msg.get("dir")
    if direction not in ("event", "cmd", "result"):
        raise WireError(f"invalid dir: {direction!r}")
    out: MutableMapping[str, Any] = dict(msg)
    if direction == "event":
        t = out.get("type")
        if t not in EVENT_TYPES:
            raise WireError(f"unknown event type: {t!r}")
    elif direction == "cmd":
        t = out.get("type")
        if t not in COMMAND_TYPES:
            raise WireError(f"unknown command type: {t!r}")
        if not out.get("id"):
            raise WireError("cmd requires id")
        if t in ("human.send", "human.answer") and "content" not in out:
            raise WireError(f"{t} requires content")
    else:  # result
        if not out.get("id"):
            raise WireError("result requires id")
        if "ok" not in out:
            raise WireError("result requires ok")
    return dict(out)
