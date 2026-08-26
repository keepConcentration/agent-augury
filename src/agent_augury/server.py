"""Internal message server — the SSOT for threads/messages/mentions/waits.

DESIGN.md §3.4 (schema & primitives), §3.5.2 (A model: send→inbox push,
step() drains as single consumer), §3.5.3 (broadcast fan-out rules),
§3.5.5 (L2 contrast mode: push off, foreground wait instead).
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Callable

_VALID_MODES = ("L2", "L3")


class MessageServer:
    """In-process, memory-backed message server (v0.1a state store).

    Single asyncio event loop assumed; no locks needed beyond cooperative
    scheduling (§3.5.4).
    """

    def __init__(self) -> None:
        self._agents: set[str] = set()
        self._threads: dict[str, dict[str, Any]] = {}
        # messages in global send order; each carries an int `seq` for cursors
        self._messages: list[dict[str, Any]] = []
        self._inboxes: dict[str, asyncio.Queue[str]] = {}
        self._cursors: dict[str, int] = {}
        self._cond: asyncio.Condition = asyncio.Condition()
        self._mode: str = "L3"
        self._thread_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []

    # -- registration -------------------------------------------------------

    def register_agent(self, agent_id: str) -> None:
        """Idempotent, synchronous state setup (no IO involved)."""
        if agent_id in self._agents:
            return
        self._agents.add(agent_id)
        self._inboxes[agent_id] = asyncio.Queue()
        self._cursors[agent_id] = 0

    # -- primitives ---------------------------------------------------------

    async def create_thread(self, name: str, *, participants: list[str]) -> str:
        for p in participants:
            self.register_agent(p)
        thread_id = f"thread-{next(self._thread_ids)}"
        self._threads[thread_id] = {
            "thread_id": thread_id,
            "name": name,
            "participants": list(participants),
        }
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
        L3 pushes to targets' inboxes; L2 records delivery only (no push).
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

        if self._mode == "L3":
            for target in targets:
                self._inboxes[target].put_nowait(message["message_id"])

        for subscriber in self._subscribers:
            subscriber(message)
        async with self._cond:
            self._cond.notify_all()
        return message["message_id"]

    # -- subscriptions (gate / mirrors) --------------------------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a synchronous observer invoked on every sent message."""
        self._subscribers.append(callback)

    async def wait_for_mention(
        self, agent_id: str, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Foreground blocking receive — L2 contrast mode ONLY (§3.5.5).

        Returns unread messages targeted at the caller (cursor-based,
        unread-only) and advances the caller's cursor past them.
        """
        self._require_agent(agent_id)
        if self._mode != "L2":
            raise RuntimeError(
                "wait_for_mention is only available in L2 contrast mode; "
                "L3 receives via inbox push + step() drain"
            )
        deadline = None if timeout is None else time.monotonic() + timeout
        async with self._cond:
            while True:
                batch = self._unread_for(agent_id)
                if batch:
                    self._cursors[agent_id] = batch[-1]["seq"] + 1
                    return [dict(m) for m in batch]
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"no mention for {agent_id} within timeout")
                try:
                    if remaining is None:
                        await self._cond.wait()
                    else:
                        await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"no mention for {agent_id} within timeout"
                    ) from None

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
            msg = self._by_id(mid)
            if msg is not None:
                out.append(dict(msg))
        return out

    # -- modes / views ------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Switch communication mode (§3.5.5). Only 'listening' differs."""
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

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

    def _unread_for(self, agent_id: str) -> list[dict[str, Any]]:
        cursor = self._cursors[agent_id]
        return [
            m
            for m in self._messages[cursor:]
            if m["seq"] >= cursor and agent_id in m["delivered_to"]
        ]

    def _by_id(self, message_id: str) -> dict[str, Any] | None:
        for m in self._messages:
            if m["message_id"] == message_id:
                return m
        return None
