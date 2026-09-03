"""P1 — persistence: aiosqlite-backed MessageServer state save/restore.

Spec refs: DESIGN.md §3.5.4 D5 (server state: memory → aiosqlite transition).
"""

from __future__ import annotations

import gc
import os
import shutil
import tempfile

import pytest

from agent_augury.server import MessageServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_path(tmpdir: str, name: str) -> str:
    return os.path.join(tmpdir, f"{name}.db")


@pytest.fixture
def tmp_db_dir():
    """Create a temp directory that is cleaned up after the test.

    Uses ignore_errors=True to handle Windows file-locking delays.
    """
    d = tempfile.mkdtemp(prefix="augury-persist-")
    yield d
    gc.collect()
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. 저장→복구 왕복: threads + messages survive a restart
# ---------------------------------------------------------------------------


async def test_persistence_roundtrip_restores_threads_and_messages(tmp_db_dir):
    """Create threads/messages, then reopen the same db_path → state restored."""
    db_path = _make_db_path(tmp_db_dir, "roundtrip")

    # First session: create threads and send messages
    s1 = MessageServer(db_path=db_path)
    t1 = await s1.create_thread("design", participants=["a1", "a2"])
    t2 = await s1.create_thread("review", participants=["a2", "a3"])
    m1 = await s1.send_message(t1, author="a1", content="hello design", mentions=["a2"])
    m2 = await s1.send_message(t2, author="a2", content="hello review", mentions=["a3"])

    snap1 = s1.snapshot()
    assert len(snap1["threads"]) == 2
    assert len(snap1["messages"]) == 2

    # Close first session (releases file lock on Windows)
    await s1.close()

    # Second session: same db_path → must restore
    s2 = MessageServer(db_path=db_path)
    await s2.load()
    snap2 = s2.snapshot()
    assert len(snap2["threads"]) == 2
    assert len(snap2["messages"]) == 2

    # Threads restored with same IDs and participants
    t1_restored = s2.get_thread(t1)
    assert t1_restored["name"] == "design"
    assert t1_restored["participants"] == ["a1", "a2"]
    t2_restored = s2.get_thread(t2)
    assert t2_restored["name"] == "review"
    assert t2_restored["participants"] == ["a2", "a3"]

    # Messages restored with same IDs, content, seq
    restored_msgs = {m["message_id"]: m for m in snap2["messages"]}
    assert m1 in restored_msgs
    assert m2 in restored_msgs
    assert restored_msgs[m1]["content"] == "hello design"
    assert restored_msgs[m1]["author"] == "a1"
    assert restored_msgs[m2]["content"] == "hello review"
    assert restored_msgs[m2]["author"] == "a2"

    await s2.close()


# ---------------------------------------------------------------------------
# 2. 카운터 이어짐: thread-N / msg-N counters continue from max+1
# ---------------------------------------------------------------------------


async def test_persistence_counters_continue_after_restart(tmp_db_dir):
    """After restart, new thread/message IDs must not collide with restored ones."""
    db_path = _make_db_path(tmp_db_dir, "counters")

    # First session: create thread-1, thread-2, msg-1, msg-2
    s1 = MessageServer(db_path=db_path)
    await s1.create_thread("t1", participants=["a1"])
    await s1.create_thread("t2", participants=["a1"])
    t = await s1.create_thread("t-send", participants=["a1", "a2"])
    await s1.send_message(t, author="a1", content="m1", mentions=["a2"])
    await s1.send_message(t, author="a2", content="m2", mentions=["a1"])

    # Close first session
    await s1.close()

    # Second session: same db_path
    s2 = MessageServer(db_path=db_path)
    await s2.load()
    # New thread must be thread-4 (continues from max+1, not restarting at 1)
    new_t = await s2.create_thread("t3", participants=["a1"])
    assert new_t == "thread-4", f"expected thread-4, got {new_t}"
    # New message must be msg-3 (not msg-1)
    new_m = await s2.send_message(
        new_t, author="a1", content="m3", mentions=[]
    )
    assert new_m == "msg-3", f"expected msg-3, got {new_m}"

    await s2.close()


# ---------------------------------------------------------------------------
# 3. 메모리 폴백: db_path=None → pure memory mode (backward compatible)
# ---------------------------------------------------------------------------


async def test_persistence_none_is_pure_memory():
    """db_path=None must keep pure-memory behavior (no DB, no persistence)."""
    s = MessageServer(db_path=None)
    t = await s.create_thread("mem", participants=["a1", "a2"])
    m = await s.send_message(t, author="a1", content="in-memory", mentions=["a2"])

    snap = s.snapshot()
    assert len(snap["threads"]) == 1
    assert len(snap["messages"]) == 1
    assert snap["messages"][0]["content"] == "in-memory"

    # No DB file created (since db_path is None, _db stays None)
    assert s._db is None


# ---------------------------------------------------------------------------
# 4. seq 보존: message sequence order is preserved after restart
# ---------------------------------------------------------------------------


async def test_persistence_seq_order_preserved(tmp_db_dir):
    """seq values must be preserved in original send order after restart."""
    db_path = _make_db_path(tmp_db_dir, "seq")

    s1 = MessageServer(db_path=db_path)
    t = await s1.create_thread("seq-test", participants=["a1", "a2"])
    # Send 5 messages in order
    for i in range(5):
        await s1.send_message(
            t, author="a1", content=f"msg-{i}", mentions=["a2"]
        )

    # Verify seq in first session
    snap1 = s1.snapshot()
    seqs1 = [m["seq"] for m in snap1["messages"]]
    assert seqs1 == [0, 1, 2, 3, 4]

    # Close first session
    await s1.close()

    # Restart
    s2 = MessageServer(db_path=db_path)
    await s2.load()
    snap2 = s2.snapshot()
    seqs2 = [m["seq"] for m in snap2["messages"]]
    assert seqs2 == [0, 1, 2, 3, 4], f"seq not preserved: {seqs2}"

    # Content order must match seq order
    contents = [m["content"] for m in snap2["messages"]]
    assert contents == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]

    await s2.close()
