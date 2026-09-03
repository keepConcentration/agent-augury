"""Internal message server — the SSOT for threads/messages/mentions.

DESIGN.md §3.4 (schema & primitives), §3.5.2 (A model: send→inbox push,
step() drains as single consumer), §3.5.3 (broadcast fan-out rules).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Callable


class MessageServer:
    """In-process, memory-backed message server (v0.1a state store).

    Single asyncio event loop assumed; no locks needed beyond cooperative
    scheduling (§3.5.4).
    """

    def __init__(self) -> None:
        self._agents: set[str] = set()
        self._threads: dict[str, dict[str, Any]] = {}
        # messages in global send order; each carries an int `seq` for ordering
        self._messages: list[dict[str, Any]] = []
        # message_id -> message dict index (O(1) lookup for drain_inbox)
        self._message_index: dict[str, dict[str, Any]] = {}
        self._inboxes: dict[str, asyncio.Queue[str]] = {}
        self._thread_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        # v0.3: event stream for broadcast logging (thread/message/resource)
        self._event_subscribers: list[Callable[[dict[str, Any]], None]] = []

    # -- registration -------------------------------------------------------

    def register_agent(self, agent_id: str) -> None:
        """Idempotent, synchronous state setup (no IO involved)."""
        if agent_id in self._agents:
            return
        self._agents.add(agent_id)
        self._inboxes[agent_id] = asyncio.Queue()

    # -- primitives ---------------------------------------------------------

    async def create_thread(self, name: str, *, participants: list[str]) -> str:
        # Return existing thread with the same name if it exists
        for thread in self._threads.values():
            if thread["name"] == name:
                # Update participants to include any new agents
                existing = set(thread["participants"])
                new = set(participants)
                added = new - existing
                if added:
                    # Register new participants FIRST (inbox) — otherwise
                    # a later send to them would KeyError on the missing inbox.
                    for p in sorted(added):
                        self.register_agent(p)
                    thread["participants"] = sorted(existing | new)
                    # Emit an event so broadcast observers see the expansion
                    # (D7 — reuse with a participant change must be observable).
                    self._emit_event({
                        "type": "create_thread",
                        "thread_id": thread["thread_id"],
                        "name": name,
                        "participants": list(thread["participants"]),
                        "reused": True,
                        "timestamp": int(time.time()),
                    })
                return thread["thread_id"]
        for p in participants:
            self.register_agent(p)
        thread_id = f"thread-{next(self._thread_ids)}"
        self._threads[thread_id] = {
            "thread_id": thread_id,
            "name": name,
            "participants": list(participants),
        }
        self._emit_event({
            "type": "create_thread",
            "thread_id": thread_id,
            "name": name,
            "participants": list(participants),
            "timestamp": int(time.time()),
        })
        return thread_id

    async def send_message(
        self,
        thread_id: str,
        *,
        author: str,
        content: str,
        mentions: list[str] | None = None,
    ) -> str:
        """Append a message and return immediately (fire-and-forget).

        Delivery (§3.5.3): non-empty mentions → participants ∩ mentions;
        empty mentions → broadcast to participants minus the author.
        Always pushes to targets' inboxes.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"no such thread: {thread_id}")
        if author not in thread["participants"]:
            raise ValueError(f"author {author!r} is not a participant of {thread_id}")

        participants = thread["participants"]
        if mentions:
            targets = [a for a in participants if a in mentions]
        else:
            targets = [a for a in participants if a != author]

        message = {
            "message_id": f"msg-{next(self._message_ids)}",
            "thread_id": thread_id,
            "author": author,
            "content": content,
            "mentions": list(mentions or []),
            "delivered_to": targets,
            "created_at": int(time.time()),
            "seq": len(self._messages),
        }
        self._messages.append(message)
        self._message_index[message["message_id"]] = message

        for target in targets:
            self._inboxes[target].put_nowait(message["message_id"])

        for subscriber in self._subscribers:
            subscriber(message)

        self._emit_event({
            "type": "send_message",
            "message_id": message["message_id"],
            "thread_id": thread_id,
            "author": author,
            "content": content,
            "mentions": list(mentions or []),
            "delivered_to": targets,
            "timestamp": int(time.time()),
        })
        return message["message_id"]

    # -- subscriptions (gate / mirrors) --------------------------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a synchronous observer invoked on every sent message."""
        self._subscribers.append(callback)

    def subscribe_events(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a synchronous observer for all broadcast events.

        Events are emitted for: create_thread, send_message, read_resource.
        Each event is a dict with at least ``type`` and ``timestamp`` keys.
        """
        self._event_subscribers.append(callback)

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Fire an event to all event subscribers (best-effort, no raise)."""
        for subscriber in self._event_subscribers:
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001 — subscriber must not break server
                pass

    # -- inbox consumption (single consumer: step()) ------------------------

    def inbox_size(self, agent_id: str) -> int:
        self._require_agent(agent_id)
        return self._inboxes[agent_id].qsize()

    async def drain_inbox(self, agent_id: str) -> list[dict[str, Any]]:
        """Drain the caller's inbox FIFO. The only inbox consumer is step()."""
        self._require_agent(agent_id)
        q = self._inboxes[agent_id]
        out: list[dict[str, Any]] = []
        while not q.empty():
            mid = q.get_nowait()
            msg = self._message_index.get(mid)
            if msg is not None:
                out.append(dict(msg))
        return out

    # -- views --------------------------------------------------------------

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"no such thread: {thread_id}")
        return dict(thread)

    def snapshot(self) -> dict[str, Any]:
        """Read-only-ish view of full state (read_resource tool backing)."""
        return {
            "agents": sorted(self._agents),
            "threads": [dict(t) for t in self._threads.values()],
            "messages": [dict(m) for m in self._messages],
        }

    # -- internals ----------------------------------------------------------

    def _require_agent(self, agent_id: str) -> None:
        if agent_id not in self._agents:
            raise KeyError(f"unknown agent: {agent_id}")
