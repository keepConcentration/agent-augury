"""Internal message server — the SSOT for threads/messages/mentions.

DESIGN.md §3.4 (schema & primitives), §3.5.2 (A model: send→inbox push,
step() drains as single consumer), §3.5.3 (broadcast fan-out rules).

Persistence (§3.5.4 D5): when ``db_path`` is provided, threads and messages
are mirrored to an aiosqlite database. Memory stays the runtime primary
source; DB is the persistence secondary source. ``db_path=None`` keeps
pure-memory mode (backward compatible with v0.1a).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from collections.abc import Callable
from typing import Any

import aiosqlite

# Names reserved for the human participant. Matched case-insensitively so
# an agent id like "Human" or "HUMAN" cannot collide with the user.
# (USER_INTERVENTION_DESIGN.md §3.2).
RESERVED_NAMES = frozenset({"human"})


class ReservedNameError(ValueError):
    """Raised when a reserved name (e.g. 'human') is used as an id."""


async def _create_schema(db: aiosqlite.Connection) -> None:
    """Create the schema tables/indexes one by one."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS threads ("
        "thread_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "participants TEXT NOT NULL)"
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, "
        "author TEXT NOT NULL, content TEXT NOT NULL, "
        "mentions TEXT NOT NULL, delivered_to TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, seq INTEGER NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages(thread_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages(seq)"
    )


class MessageServer:
    """In-process message server with optional aiosqlite persistence.

    Single asyncio event loop assumed; no locks needed beyond cooperative
    scheduling (§3.5.4).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._agents: set[str] = set()
        self._humans: set[str] = set()
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
        # Persistence state (D5)
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # Background persists, held until done. asyncio keeps only a weak
        # reference to a task, so an unheld one can be garbage-collected
        # mid-write; and without the set ``close()`` has nothing to wait on.
        self._pending_writes: set[asyncio.Task[None]] = set()

    # -- persistence ---------------------------------------------------------

    async def _ensure_db(self) -> None:
        """Lazy-init the DB connection, create schema, and load state.

        Called from every public async method. No-op when ``db_path`` is None
        or the DB is already initialized.
        """
        if self._db is not None or self._db_path is None:
            return
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await _create_schema(self._db)
        await self._db.commit()
        await self._load_from_db()

    async def _load_from_db(self) -> None:
        """Load threads and messages from DB into memory, restoring counters."""
        assert self._db is not None
        # Load threads
        async with self._db.execute(
            "SELECT thread_id, name, participants FROM threads"
        ) as cursor:
            rows = await cursor.fetchall()
            for thread_id, name, participants_json in rows:
                self._threads[thread_id] = {
                    "thread_id": thread_id,
                    "name": name,
                    "participants": json.loads(participants_json),
                }
        # Load messages in seq order
        async with self._db.execute(
            "SELECT message_id, thread_id, author, content, mentions, "
            "delivered_to, created_at, seq FROM messages ORDER BY seq"
        ) as cursor:
            rows = await cursor.fetchall()
            for mid, tid, author, content, men_json, del_json, created_at, seq in rows:
                msg = {
                    "message_id": mid,
                    "thread_id": tid,
                    "author": author,
                    "content": content,
                    "mentions": json.loads(men_json),
                    "delivered_to": json.loads(del_json),
                    "created_at": created_at,
                    "seq": seq,
                }
                self._messages.append(msg)
                self._message_index[mid] = msg
        # Restore thread/message counters to max+1 (avoid ID collision on restart)
        max_thread = 0
        for tid in self._threads:
            if tid.startswith("thread-"):
                try:
                    max_thread = max(max_thread, int(tid[7:]))
                except ValueError:
                    pass
        max_message = 0
        for mid in self._message_index:
            if mid.startswith("msg-"):
                try:
                    max_message = max(max_message, int(mid[4:]))
                except ValueError:
                    pass
        self._thread_ids = itertools.count(max_thread + 1)
        self._message_ids = itertools.count(max_message + 1)

    async def _persist_thread(
        self, thread_id: str, name: str, participants: list[str]
    ) -> None:
        """INSERT or REPLACE a thread row."""
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO threads (thread_id, name, participants) "
            "VALUES (?, ?, ?)",
            (thread_id, name, json.dumps(participants)),
        )
        await self._db.commit()

    async def _persist_message(self, msg: dict[str, Any]) -> None:
        """INSERT a message row."""
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO messages (message_id, thread_id, author, content, "
            "mentions, delivered_to, created_at, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg["message_id"],
                msg["thread_id"],
                msg["author"],
                msg["content"],
                json.dumps(msg["mentions"]),
                json.dumps(msg["delivered_to"]),
                msg["created_at"],
                msg["seq"],
            ),
        )
        await self._db.commit()

    async def close(self) -> None:
        """Flush background writes, then close the DB. Required for shutdown.

        ``set_thread_participants`` persists in the background, so closing
        without draining loses the write: the task wakes to a shut connection
        and dies unobserved, and the next resume reads the pre-change row. A
        roster change made just before shutdown would silently revert.
        """
        if self._pending_writes:
            await asyncio.gather(*tuple(self._pending_writes), return_exceptions=True)
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def load(self) -> None:
        """Explicitly load state from DB into memory.

        Must be called after construction with a ``db_path`` before reading
        state via ``snapshot()``/``get_thread()`` when no mutation method
        (``create_thread``/``send_message``) has been invoked yet.
        """
        await self._ensure_db()

    # -- registration -------------------------------------------------------

    def register_agent(self, agent_id: str) -> None:
        """Idempotent, synchronous state setup (no IO involved)."""
        if agent_id.lower() in RESERVED_NAMES:
            raise ReservedNameError(
                f"'{agent_id}' is a reserved name for the human participant; "
                f"choose another agent id."
            )
        if agent_id in self._humans:
            raise ValueError(f"agent id '{agent_id}' collides with a registered human id")
        if agent_id in self._agents:
            return
        self._agents.add(agent_id)
        self._inboxes[agent_id] = asyncio.Queue()

    def register_human(self, human_id: str = "human") -> None:
        """Register a human participant (separate registry from agents).

        v1.0 supports only the reserved id ``"human"`` (case-insensitive).
        The human gets its own inbox so ``ask_user`` replies and injected
        user messages flow through the same primitives as agent messages.
        """
        if human_id.lower() != "human":
            raise ValueError("human id must be 'human' in v1.0 (reserved namespace)")
        self._humans.add(human_id)
        if human_id not in self._inboxes:
            self._inboxes[human_id] = asyncio.Queue()

    def current_seq(self) -> int:
        """Seq that the next appended message would receive (= len(_messages))."""
        return len(self._messages)

    def drop_inbox_from(self, agent_id: str, thread_ids: set[str]) -> None:
        """Drop unread ids whose message came from *thread_ids*; keep the rest.

        Inbox is one queue per agent (not per thread). A full clear would wipe
        human-thread pokes that DYNAMIC_ROSTER keeps deliverable after demotion.
        """
        self._require_participant(agent_id)
        if not thread_ids:
            return
        q = self._inboxes[agent_id]
        kept: list[str] = []
        while True:
            try:
                mid = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            msg = self._message_index.get(mid)
            if msg is None or msg.get("thread_id") not in thread_ids:
                kept.append(mid)
        for mid in kept:
            q.put_nowait(mid)

    def thread_participants(self, thread_id: str) -> list[str]:
        """Participants of *thread_id*, or empty when there is no such thread."""
        thread = self._threads.get(thread_id)
        return list(thread["participants"]) if thread else []

    def set_thread_participants(self, thread_id: str, agent_ids: list[str]) -> None:
        """Replace thread participants (grow or shrink). Memory sync; DB async.

        New ids are registered (inbox) before being listed. Removed ids drop
        only unread that originated on this thread (``drop_inbox_from``).
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"no such thread: {thread_id}")
        new_ids = list(agent_ids)
        new_set = set(new_ids)
        old_set = set(thread["participants"])
        for p in sorted(new_set - old_set):
            self.register_agent(p)
        removed = old_set - new_set
        for p in removed:
            self.drop_inbox_from(p, {thread_id})
        thread["participants"] = list(new_ids)
        self._schedule_persist_thread(thread_id, thread["name"], list(new_ids))

    def _schedule_persist_thread(
        self, thread_id: str, name: str, participants: list[str]
    ) -> None:
        """Persist when a loop is running; otherwise memory-only (unit tests)."""
        if self._db is None and self._db_path is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._persist_thread_safe(thread_id, name, participants)
        )
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def _persist_thread_safe(
        self, thread_id: str, name: str, participants: list[str]
    ) -> None:
        await self._ensure_db()
        if self._db is not None:
            await self._persist_thread(thread_id, name, participants)

    # -- primitives ---------------------------------------------------------

    async def create_thread(
        self,
        name: str,
        *,
        participants: list[str],
        bootstrap: bool = False,
    ) -> str:
        await self._ensure_db()
        # Return existing thread with the same name if it exists
        for thread in self._threads.values():
            if thread["name"] == name:
                # Update participants to include any new agents
                existing = set(thread["participants"])
                new = set(participants)
                added = new - existing
                # Only the runtime's own bootstrap may widen an existing thread.
                # Knowing the *name* must not be enough to join: names travel in
                # prose, so ingested text can talk an agent into
                # ``create_thread(name="gate-review", participants=["me"])`` and
                # the ``send_message`` participant check below would then pass.
                if added and not bootstrap:
                    added = set()
                if added:
                    # Register new participants FIRST (inbox) — otherwise
                    # a later send to them would KeyError on the missing inbox.
                    for p in sorted(added):
                        self.register_agent(p)
                    thread["participants"] = sorted(existing | new)
                    if self._db is not None:
                        await self._persist_thread(
                            thread["thread_id"], name, thread["participants"]
                        )
                    # Emit an event so broadcast observers see the expansion
                    # (D7 — reuse with a participant change must be observable).
                    evt: dict[str, Any] = {
                        "type": "create_thread",
                        "thread_id": thread["thread_id"],
                        "name": name,
                        "participants": list(thread["participants"]),
                        "reused": True,
                        "timestamp": int(time.time()),
                    }
                    if bootstrap:
                        evt["bootstrap"] = True
                    self._emit_event(evt)
                return thread["thread_id"]
        for p in participants:
            self.register_agent(p)
        thread_id = f"thread-{next(self._thread_ids)}"
        self._threads[thread_id] = {
            "thread_id": thread_id,
            "name": name,
            "participants": list(participants),
        }
        if self._db is not None:
            await self._persist_thread(thread_id, name, list(participants))
        created: dict[str, Any] = {
            "type": "create_thread",
            "thread_id": thread_id,
            "name": name,
            "participants": list(participants),
            "timestamp": int(time.time()),
        }
        if bootstrap:
            created["bootstrap"] = True
        self._emit_event(created)
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
        await self._ensure_db()
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"no such thread: {thread_id}")
        if author not in self._agents:
            raise ValueError(f"author '{author}' is not a registered agent")
        if author not in thread["participants"]:
            raise ValueError(f"author {author!r} is not a participant of {thread_id}")

        participants = thread["participants"]
        if mentions:
            targets = [a for a in participants if a in mentions]
            # humans are not necessarily thread participants; route mentions
            # that target a registered human to its inbox too (§4.2 ask_user).
            for m in mentions:
                if m in self._humans and m not in targets:
                    targets.append(m)
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

        if self._db is not None:
            await self._persist_message(message)

        for target in targets:
            self._inboxes[target].put_nowait(message["message_id"])

        # Surfaces first, then the subscribers that act on the message.
        # A gate subscriber can open a gate and advance the phase, which
        # publishes ``session.phase`` on the same call stack — emitting after
        # them printed every transition ABOVE the message that caused it, so
        # a read of the log had the cause and effect inverted.
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

        for subscriber in self._subscribers:
            subscriber(message)
        return message["message_id"]

    async def human_send(
        self,
        thread_id: str,
        *,
        author: str,
        content: str,
        mentions: list[str] | None = None,
        source: dict[str, Any] | None = None,
    ) -> str:
        """Inject a message from a human participant into the server.

        The human is NOT necessarily a thread participant; its message fans
        out to the thread's agent participants following the same §3.5.3 rule
        as ``send_message`` (empty mentions → all participants; non-empty →
        participants ∩ mentions). Only a registered human id may author here,
        so a user message can never be mistaken for an agent message (§3.2).

        Optional ``source`` (e.g. Discord surface/user/channel) is kept on the
        in-memory message and broadcast event for audit; it is not required
        for delivery.
        """
        await self._ensure_db()
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"no such thread: {thread_id}")
        if author not in self._humans:
            raise ValueError(f"author '{author}' is not a registered human")

        participants = thread["participants"]
        if mentions:
            targets = [a for a in participants if a in mentions]
        else:
            targets = list(participants)

        message: dict[str, Any] = {
            "message_id": f"msg-{next(self._message_ids)}",
            "thread_id": thread_id,
            "author": author,
            "content": content,
            "mentions": list(mentions or []),
            "delivered_to": targets,
            "created_at": int(time.time()),
            "seq": len(self._messages),
        }
        if source:
            message["source"] = dict(source)
        self._messages.append(message)
        self._message_index[message["message_id"]] = message

        if self._db is not None:
            await self._persist_message(message)

        for target in targets:
            self._inboxes[target].put_nowait(message["message_id"])

        # Surfaces first — same ordering as ``send_message`` above.
        event: dict[str, Any] = {
            "type": "send_message",
            "message_id": message["message_id"],
            "thread_id": thread_id,
            "author": author,
            "content": content,
            "mentions": list(mentions or []),
            "delivered_to": targets,
            "timestamp": int(time.time()),
        }
        if source:
            event["source"] = dict(source)
        self._emit_event(event)

        for subscriber in self._subscribers:
            subscriber(message)
        return message["message_id"]

    def inject_agent_notice(
        self,
        agent_id: str,
        content: str,
        *,
        author: str = "human",
    ) -> str:
        """Push a direct inbox notice for *agent_id* (no thread required).

        Used for approval grant/deny/result absorption. The next ``step()``
        drains it as a normal ``[radio]`` block.
        """
        self._require_participant(agent_id)
        message: dict[str, Any] = {
            "message_id": f"msg-{next(self._message_ids)}",
            "thread_id": "_approval",
            "author": author,
            "content": content,
            "mentions": [agent_id],
            "delivered_to": [agent_id],
            "created_at": int(time.time()),
            "seq": len(self._messages),
        }
        self._messages.append(message)
        self._message_index[message["message_id"]] = message
        self._inboxes[agent_id].put_nowait(message["message_id"])
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
            except Exception:  # noqa: BLE001, S110 — subscriber must not break server
                pass

    # -- inbox consumption (single consumer: step()) ------------------------

    def inbox_size(self, agent_id: str) -> int:
        self._require_participant(agent_id)
        return self._inboxes[agent_id].qsize()

    def export_inbox_ids(self) -> dict[str, list[str]]:
        """Snapshot undrained inbox message ids (non-destructive)."""
        out: dict[str, list[str]] = {}
        for agent_id, q in self._inboxes.items():
            ids: list[str] = []
            while True:
                try:
                    ids.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for mid in ids:
                q.put_nowait(mid)
            out[agent_id] = ids
        return out

    def restore_inbox_ids(self, mapping: dict[str, list[str]]) -> None:
        """Replace inboxes with the given message-id lists (missing ids skipped)."""
        for agent_id, ids in mapping.items():
            if agent_id not in self._inboxes:
                continue
            q = self._inboxes[agent_id]
            while True:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            for mid in ids:
                if mid in self._message_index:
                    q.put_nowait(mid)

    async def drain_inbox(self, agent_id: str) -> list[dict[str, Any]]:
        """Drain the caller's inbox FIFO. The only inbox consumer is step()."""
        self._require_participant(agent_id)
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

    def resolve_thread_id(self, ref: str | None) -> str | None:
        """Return a known thread id for *ref* (id or name), or None."""
        if not ref:
            return None
        key = str(ref).strip()
        if not key:
            return None
        if key in self._threads:
            return key
        for thread in self._threads.values():
            if thread["name"] == key:
                return str(thread["thread_id"])
        return None

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

    def _require_participant(self, participant_id: str) -> None:
        """Require an inbox owner — either a registered agent or a human."""
        if participant_id not in self._agents and participant_id not in self._humans:
            raise KeyError(f"unknown participant: {participant_id}")
