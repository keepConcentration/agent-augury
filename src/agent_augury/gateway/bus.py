"""In-process Session Gateway bus (M1).

Fan-out domain/wire events to N surface subscriptions.
Interactive surfaces may dispatch commands; observe-only chat drops human.*.

A7/D5: per-surface exception isolation; optional bounded mailbox (drop-oldest)
so slow chat handlers do not block Core publish.
Design: docs/architecture/GATEWAY_BACKPRESSURE_DESIGN.md
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
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

log = logging.getLogger(__name__)

SurfaceMode = Literal["interact", "observe"]
EventHandler = Callable[[WireEvent], None]
CommandHandler = Callable[[WireCommand], Any]

# Default for register_chat_surface (A7).
DEFAULT_CHAT_MAILBOX_MAX = 256


@dataclass
class SurfaceSubscription:
    """One attached surface (Ink, Discord observe, fake test client, …)."""

    name: str
    mode: SurfaceMode = "interact"
    family: Literal["ui", "chat"] = "ui"
    on_event: EventHandler | None = None
    # Optional event-type allowlist; None = all events.
    event_types: frozenset[str] | None = None
    # A7: None = deliver inline in publish; int = bounded mailbox (drop-oldest).
    mailbox_max: int | None = None


@dataclass
class SessionGateway:
    """Thin fan-out bus between Core and surfaces.

    Core (or a future bridge) calls :meth:`publish` with wire events.
    Surfaces call :meth:`dispatch` with wire commands.
    ``on_command`` is installed by the host (cli/session bridge) to reach Core.
    """

    on_command: CommandHandler | None = None
    _surfaces: dict[str, SurfaceSubscription] = field(default_factory=dict)
    _mailboxes: dict[str, deque[WireEvent]] = field(default_factory=dict)
    _drop_counts: dict[str, int] = field(default_factory=dict)
    _drain_scheduled: set[str] = field(default_factory=set)

    def attach(self, sub: SurfaceSubscription) -> None:
        if not sub.name:
            raise ValueError("surface name required")
        self._surfaces[sub.name] = sub
        if sub.mailbox_max is not None and sub.mailbox_max > 0:
            self._mailboxes.setdefault(sub.name, deque())

    def detach(self, name: str) -> None:
        self._surfaces.pop(name, None)
        self._mailboxes.pop(name, None)
        self._drain_scheduled.discard(name)

    def surfaces(self) -> list[str]:
        return sorted(self._surfaces)

    def has_interact_surface(self) -> bool:
        """True when at least one ``mode=interact`` surface is attached."""
        return any(sub.mode == "interact" for sub in self._surfaces.values())

    def drop_counts(self) -> dict[str, int]:
        """Per-surface count of events dropped by mailbox overflow (A7)."""
        return dict(self._drop_counts)

    def drain_mailboxes(self) -> None:
        """Deliver all pending mailboxed events (tests / shutdown)."""
        for name in list(self._mailboxes):
            self._drain_mailbox(name)

    def publish(self, event: WireEvent) -> int:
        """Validate and fan-out an event. Returns number of attempted deliveries."""
        msg = validate_message(event)
        if msg["dir"] != "event":
            raise ValueError("publish expects dir=event")
        delivered = 0
        event_type = msg["type"]
        payload = dict(msg)
        for sub in list(self._surfaces.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            if sub.on_event is None:
                continue
            if sub.mailbox_max is not None and sub.mailbox_max > 0:
                self._enqueue_mailbox(sub, payload)
            else:
                self._deliver_safe(sub, payload)
            delivered += 1
        return delivered

    def _enqueue_mailbox(self, sub: SurfaceSubscription, event: WireEvent) -> None:
        box = self._mailboxes.setdefault(sub.name, deque())
        max_n = sub.mailbox_max or 0
        if max_n > 0 and len(box) >= max_n:
            box.popleft()
            self._drop_counts[sub.name] = self._drop_counts.get(sub.name, 0) + 1
            log.warning(
                "gateway mailbox full; dropped oldest event for surface %r (drops=%s)",
                sub.name,
                self._drop_counts[sub.name],
            )
        box.append(dict(event))
        self._schedule_drain(sub.name)

    def _schedule_drain(self, name: str) -> None:
        if name in self._drain_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._drain_mailbox(name)
            return
        self._drain_scheduled.add(name)
        loop.call_soon(self._drain_mailbox, name)

    def _drain_mailbox(self, name: str) -> None:
        self._drain_scheduled.discard(name)
        sub = self._surfaces.get(name)
        box = self._mailboxes.get(name)
        if sub is None or box is None or sub.on_event is None:
            if box is not None:
                box.clear()
            return
        while box:
            self._deliver_safe(sub, box.popleft())

    def _deliver_safe(self, sub: SurfaceSubscription, event: WireEvent) -> None:
        handler = sub.on_event
        if handler is None:
            return
        try:
            handler(event)
        except Exception:  # noqa: BLE001 — D5: never abort fan-out for one surface
            log.exception("surface %r on_event failed", sub.name)

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
        ok = True
        if isinstance(payload, dict):
            extra.update(payload)
            if "ok" in extra:
                ok = bool(extra.pop("ok"))
        return make_result(str(msg["id"]), ok=ok, **extra)

    def publish_raw(self, message: WireMessage) -> int:
        """Publish after validating an arbitrary mapping."""
        msg = validate_message(message)
        if msg["dir"] != "event":
            raise ValueError("publish_raw expects dir=event")
        return self.publish(msg)
