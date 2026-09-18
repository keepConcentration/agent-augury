"""Publish ``session.turn_done`` after each Core ``run()`` (SESSION_TURN_TERMINATION)."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_augury.core.protocol.phases import COMPLETED, REJECTED
from agent_augury.core.session import Session

from .types import make_event


def derive_turn_done_reason(
    session: Session,
    steps: int,
    run_exc: BaseException | None,
) -> str:
    """Map session state to Wire ``reason`` (stdio·headless 동일)."""
    if run_exc is not None and not isinstance(run_exc, asyncio.CancelledError):
        return "error"
    if session.interrupted():
        return "interrupted"
    protocol = session.protocol
    if protocol is not None:
        phase = protocol.phase
        if phase == REJECTED:
            return "protocol_rejected"
        if phase == COMPLETED:
            return "protocol_completed"
    if getattr(session, "_gate_deadlock", False):
        return "gate_deadlock"
    if session.max_steps and steps >= session.max_steps:
        return "max_steps"
    return "idle"


def publish_turn_done(
    gateway: Any,
    session: Session,
    steps: int,
    *,
    run_exc: BaseException | None = None,
) -> None:
    """Best-effort ``session.turn_done`` — never raise (Wire must not mask run errors)."""
    reason = derive_turn_done_reason(session, steps, run_exc)
    fields: dict[str, Any] = {"reason": reason, "steps": steps}
    if session.protocol is not None:
        fields["phase"] = session.protocol.phase
    try:
        gateway.publish(make_event("session.turn_done", **fields))
    except Exception:  # noqa: BLE001, S110 — never break Core for Wire
        pass
