"""M1 — internal message server primitives.

Spec refs: DESIGN.md §2.1, §3.4, §3.5.2, §3.5.3.
"""

import asyncio

import pytest

from agent_augury.server import MessageServer


# ---------------------------------------------------------------------------
# create_thread
# ---------------------------------------------------------------------------


async def test_create_thread_returns_id_and_records_participants():
    server = MessageServer()
    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])
    assert isinstance(tid, str)
    thread = server.get_thread(tid)
    assert thread["name"] == "plan"
    assert thread["participants"] == ["agent-1", "agent-2"]


async def test_create_thread_auto_registers_unknown_participants():
    """Server-level convenience: referencing an agent creates its inbox.
    Config-typo validation is the session layer's job, not the server's."""
    server = MessageServer()
    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])
    assert isinstance(tid, str)
    # now addressable
    assert server.inbox_size("agent-1") == 0


async def test_create_thread_reuse_returns_same_id():
    """Same-name create_thread reuses the existing thread (D7 behavior)."""
    server = MessageServer()
    tid1 = await server.create_thread("plan", participants=["agent-1"])
    tid2 = await server.create_thread("plan", participants=["agent-1"])
    assert tid1 == tid2
    assert len(server.snapshot()["threads"]) == 1


async def test_create_thread_reuse_expands_participants_and_emits_event():
    """D7 — reuse with a participant change must register newcomers and emit
    a create_thread event so broadcast observers see the expansion."""
    server = MessageServer()
    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])
    events = []
    server.subscribe_events(events.append)

    tid2 = await server.create_thread("plan", participants=["agent-1", "agent-2", "agent-3"])

    assert tid2 == tid
    assert server.get_thread(tid)["participants"] == ["agent-1", "agent-2", "agent-3"]
    # New participant must be registered (addressable inbox) — regression guard
    assert server.inbox_size("agent-3") == 0
    # Exactly one create_thread event with reused=True and the expanded list
    ct_events = [e for e in events if e["type"] == "create_thread"]
    assert len(ct_events) == 1
    assert ct_events[0]["reused"] is True
    assert ct_events[0]["thread_id"] == tid
    assert ct_events[0]["participants"] == ["agent-1", "agent-2", "agent-3"]


async def test_create_thread_reuse_no_event_when_participants_unchanged():
    """D7 — reuse with the same participant set must NOT emit an event."""
    server = MessageServer()
    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])
    events = []
    server.subscribe_events(events.append)

    tid2 = await server.create_thread("plan", participants=["agent-1", "agent-2"])

    assert tid2 == tid
    assert events == []


# ---------------------------------------------------------------------------
# send_message: push to inbox, fire-and-forget
# ---------------------------------------------------------------------------


async def test_send_message_pushes_to_mentioned_agents_inbox():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])

    mid = await server.send_message(
        tid, author="agent-1", content="hello", mentions=["agent-2"]
    )

    assert isinstance(mid, str)
    # author must NOT receive its own message
    assert server.inbox_size("agent-1") == 0
    # mention target has exactly one queued message
    assert server.inbox_size("agent-2") == 1


async def test_broadcast_fans_out_to_all_participants_except_author():
    server = MessageServer()
    for a in ("agent-1", "agent-2", "agent-3"):
        server.register_agent(a)
    tid = await server.create_thread("t", participants=["agent-1", "agent-2", "agent-3"])

    await server.send_message(tid, author="agent-1", content="news", mentions=[])

    # empty mentions => broadcast; author excluded
    assert server.inbox_size("agent-1") == 0
    assert server.inbox_size("agent-2") == 1
    assert server.inbox_size("agent-3") == 1


async def test_mentions_outside_participants_are_ignored():
    """§3.5.3 — mentions pointing outside participants are rejected from delivery."""
    server = MessageServer()
    for a in ("agent-1", "agent-2", "agent-out"):
        server.register_agent(a)
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])

    await server.send_message(
        tid, author="agent-1", content="x", mentions=["agent-out"]
    )

    # effective delivery = participants ∩ mentions = {} → nobody receives
    assert server.inbox_size("agent-out") == 0
    assert server.inbox_size("agent-2") == 0


async def test_send_message_from_non_participant_is_rejected():
    server = MessageServer()
    for a in ("agent-1", "agent-2", "agent-3"):
        server.register_agent(a)
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    with pytest.raises(ValueError):
        await server.send_message(tid, author="agent-3", content="intrude", mentions=[])


# ---------------------------------------------------------------------------
# drain_inbox (single consumer)
# ---------------------------------------------------------------------------


async def test_drain_returns_fifo_and_clears_queue():
    server = MessageServer()
    for a in ("agent-1", "agent-2"):
        server.register_agent(a)
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])

    m1 = await server.send_message(tid, author="agent-1", content="first", mentions=["agent-2"])
    m2 = await server.send_message(tid, author="agent-1", content="second", mentions=["agent-2"])

    drained = await server.drain_inbox("agent-2")
    assert [m["message_id"] for m in drained] == [m1, m2]
    assert [m["author"] for m in drained] == ["agent-1", "agent-1"]
    assert drained[0]["content"] == "first"
    # queue is now empty
    assert await server.drain_inbox("agent-2") == []


async def test_unknown_agent_operations_are_rejected():
    server = MessageServer()
    with pytest.raises(KeyError):
        server.inbox_size("ghost")
    with pytest.raises(KeyError):
        await server.drain_inbox("ghost")


async def test_snapshot_readonly_view():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="m", mentions=["agent-2"])

    snap = server.snapshot()
    threads = snap["threads"]
    messages = snap["messages"]
    assert len(threads) == 1 and threads[0]["thread_id"] == tid
    assert len(messages) == 1
