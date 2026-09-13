"""In-process Session Gateway bus (M1).

Fan-out domain/wire events to N surface subscriptions.
Interactive surfaces may dispatch commands; observe-only chat drops human.*.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .types import (
    HUMAN_COMMAND_TYPES,
    WireCommand,
    WireEvent,
    WireMessage,
    WireResult,
    make_result,
    validate_message,
)

SurfaceMode = Literal["interact", "observe"]
EventHandler = Callable[[WireEvent], None]
CommandHandler = Callable[[WireCommand], Any]


@dataclass
class SurfaceSubscription:
    """One attached surface (Ink, Discord observe, fake test client, …)."""

    name: str
    mode: SurfaceMode = "interact"
    family: Literal["ui", "chat"] = "ui"
    on_event: EventHandler | None = None
    # Optional event-type allowlist; None = all events.
    event_types: frozenset[str] | None = None


@dataclass
class SessionGateway:
    """Thin fan-out bus between Core and surfaces.

    Core (or a future bridge) calls :meth:`publish` with wire events.
    Surfaces call :meth:`dispatch` with wire commands.
    ``on_command`` is installed by the host (cli/session bridge) to reach Core.
    """

    on_command: CommandHandler | None = None
    _surfaces: dict[str, SurfaceSubscription] = field(default_factory=dict)

    def attach(self, sub: SurfaceSubscription) -> None:
        if not sub.name:
            raise ValueError("surface name required")
        self._surfaces[sub.name] = sub

    def detach(self, name: str) -> None:
        self._surfaces.pop(name, None)

    def surfaces(self) -> list[str]:
        return sorted(self._surfaces)

    def publish(self, event: WireEvent) -> int:
        """Validate and fan-out an event. Returns number of deliveries."""
        msg = validate_message(event)
        if msg["dir"] != "event":
            raise ValueError("publish expects dir=event")
        delivered = 0
        event_type = msg["type"]
        for sub in list(self._surfaces.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            if sub.on_event is None:
                continue
            sub.on_event(dict(msg))
            delivered += 1
        return delivered

    def dispatch(self, command: WireCommand, *, surface: str) -> WireResult:
        """Route a command from *surface* toward Core.

        Observe-only subscriptions cannot emit ``human.*`` (D1/D2).
        """
        msg = validate_message(command)
        if msg["dir"] != "cmd":
            raise ValueError("dispatch expects dir=cmd")
        sub = self._surfaces.get(surface)
        if sub is None:
            return make_result(
                str(msg["id"]),
                ok=False,
                error=f"unknown surface: {surface}",
            )
        cmd_type = msg["type"]
        if cmd_type in HUMAN_COMMAND_TYPES and sub.mode != "interact":
            return make_result(
                str(msg["id"]),
                ok=False,
                error=f"surface {surface!r} is observe-only; {cmd_type} rejected",
            )
        if self.on_command is None:
            return make_result(
                str(msg["id"]),
                ok=False,
                error="no command handler installed",
            )
        try:
            payload = self.on_command(dict(msg))
        except Exception as exc:  # noqa: BLE001 — surface gets structured error
            return make_result(str(msg["id"]), ok=False, error=str(exc))
        extra: dict[str, Any] = {}
        if isinstance(payload, dict):
            extra.update(payload)
        return make_result(str(msg["id"]), ok=True, **extra)

    def publish_raw(self, message: WireMessage) -> int:
        """Publish after validating an arbitrary mapping."""
        msg = validate_message(message)
        if msg["dir"] != "event":
            raise ValueError("publish_raw expects dir=event")
        return self.publish(msg)
