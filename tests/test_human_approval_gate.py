"""Human approval gate (after_agents) — HUMAN_APPROVAL_GATE_DESIGN."""

from __future__ import annotations

import pytest

from agent_augury.config import ConfigError
from agent_augury.core.protocol.approval import ConsensusGate
from agent_augury.core.protocol.human_approval import (
    empty_human_approval_map,
    normalize_human_approval,
)
from agent_augury.core.server import MessageServer


def test_normalize_defaults_all_false():
    assert normalize_human_approval({}, config_error=ConfigError) == empty_human_approval_map()
    assert all(
        v is False
        for v in normalize_human_approval(
            {"gates": {"P5_SUBMIT": "submission"}}, config_error=ConfigError
        ).values()
    )


def test_normalize_short_form():
    proto = {
        "gates": {
            "P2_SPLIT": "plan",
            "P5_SUBMIT": "submission",
        },
        "human_approval": {"P5_SUBMIT": True},
    }
    out = normalize_human_approval(proto, config_error=ConfigError)
    assert out["P5_SUBMIT"] is True
    assert out["P2_SPLIT"] is False
    assert out["P3_EXECUTE"] is False


def test_normalize_rejects_p1_and_human_participant():
    with pytest.raises(ConfigError, match="P1_EXPLORE"):
        normalize_human_approval(
            {
                "gates": {"P5_SUBMIT": "submission"},
                "human_approval": {"P1_EXPLORE": True},
            },
            config_error=ConfigError,
        )
    with pytest.raises(ConfigError, match="human"):
        normalize_human_approval(
            {
                "participants": ["a1", "human"],
                "gates": {"P5_SUBMIT": "submission"},
            },
            config_error=ConfigError,
        )


def test_normalize_fills_omitted_keys_false():
    proto = {
        "gates": {"P5_SUBMIT": "submission"},
        "human_approval": {"P5_SUBMIT": True},
    }
    out = normalize_human_approval(proto, config_error=ConfigError)
    assert out["P5_SUBMIT"] is True
    assert out["P2_SPLIT"] is False
    assert out["P3_EXECUTE"] is False
    assert out["P4_REVIEW"] is False

@pytest.mark.asyncio
async def test_after_agents_pending_then_human_approve():
    server = MessageServer()
    server.register_agent("a")
    server.register_agent("b")
    server.register_human()
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=False, await_human_after_agents=True
    )
    pending = []
    gate.on_human_pending(lambda: pending.append(True))
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a", "b"])
    gate.bind_to_thread(tid)

    await server.send_message(tid, author="a", content="APPROVE: ok", mentions=[])
    assert not gate.is_open
    assert not gate.human_pending
    await server.send_message(tid, author="b", content="APPROVE: ok", mentions=[])
    assert gate.human_pending
    assert not gate.is_open
    assert pending == [True]

    # Human APPROVE before pending would be ignored; here we are pending
    await server.human_send(tid, author="human", content="APPROVE: ship it")
    assert gate.is_open
    assert not gate.human_pending


@pytest.mark.asyncio
async def test_human_approve_before_agents_ignored():
    server = MessageServer()
    server.register_agent("a")
    server.register_agent("b")
    server.register_human()
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=False, await_human_after_agents=True
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a", "b"])
    gate.bind_to_thread(tid)

    await server.human_send(tid, author="human", content="APPROVE: early")
    assert not gate.human_pending
    assert not gate.is_open
    await server.send_message(tid, author="a", content="APPROVE: ok", mentions=[])
    await server.send_message(tid, author="b", content="APPROVE: ok", mentions=[])
    assert gate.human_pending
    assert not gate.is_open


@pytest.mark.asyncio
async def test_human_reject_resets_agent_votes():
    server = MessageServer()
    server.register_agent("a")
    server.register_agent("b")
    server.register_human()
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=False, await_human_after_agents=True
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a", "b"])
    gate.bind_to_thread(tid)

    await server.send_message(tid, author="a", content="APPROVE: ok", mentions=[])
    await server.send_message(tid, author="b", content="APPROVE: ok", mentions=[])
    assert gate.human_pending
    await server.human_send(tid, author="human", content="REJECT: redo")
    assert not gate.human_pending
    assert not gate.is_open
    assert gate.approvals == set()


@pytest.mark.asyncio
async def test_without_await_human_opens_immediately():
    server = MessageServer()
    server.register_agent("a")
    server.register_agent("b")
    gate = ConsensusGate(server, thread_name="submission", require_proposal=False)
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a", "b"])
    gate.bind_to_thread(tid)
    await server.send_message(tid, author="a", content="APPROVE: ok", mentions=[])
    await server.send_message(tid, author="b", content="APPROVE: ok", mentions=[])
    assert gate.is_open
