"""P2 draft commit + SPLIT: none routing (PHASE_MACHINE_ROUTING §2.2–2.4)."""

from __future__ import annotations

import pytest

from agent_augury.core.agent.system_prompt import render_system_prompt
from agent_augury.core.protocol.assignments import parse_split
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import (
    P2_SPLIT,
    P3_EXECUTE,
    P5_SUBMIT,
)
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session, _on_protocol_gate_open

AGENTS = ["a1", "a2", "a3"]


@pytest.mark.asyncio
async def test_assignments_commit_on_gate_open_not_on_propose():
    """§2.3: rival PROPOSE must not stick; only the winning draft commits."""
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=AGENTS)
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    tid = await server.create_thread("plan", participants=AGENTS)
    gate = protocol.gate_for(P2_SPLIT)
    gate.bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)

    await server.send_message(
        tid, author="a1",
        content="PROPOSE: winner\nASSIGN a1: WIN\nASSIGN a2: B\n"
                "ASSIGN a3: C\nSUBMITTER: a1\n",
    )
    # Mid-phase: nothing committed yet (D7 / §2.3).
    assert protocol.assignment_for("a1") is None
    assert protocol.submitter_id is None

    # Rival draft — gate ignores it; must not pollute commit.
    await server.send_message(
        tid, author="a2",
        content="PROPOSE: rival\nASSIGN a1: RIVAL\nSUBMITTER: a2\n",
    )
    assert protocol.assignment_for("a1") is None

    for a in AGENTS:
        if a == "a1":
            continue  # draft author already counted
        await server.send_message(tid, author=a, content="APPROVE: ok")

    assert gate.is_open
    assert protocol.assignment_for("a1") == "WIN"
    assert protocol.submitter_id == "a1"
    assert protocol.split_none is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PROPOSE:\nSPLIT: none\n", True),
        ("**SPLIT: none**", True),
        ("SPLIT = NONE", True),
        ("SPLIT: into three parts", False),
        ("PROPOSE: no split line", False),
        ("APPROVE: ok\n\nSPLIT: none", True),  # body scan; gate opener is separate
    ],
)
def test_parse_split(text, expected):
    assert parse_split(text) is expected


def test_p2_prompt_mentions_split_none():
    prompt = render_system_prompt("a1", phase=P2_SPLIT)
    assert "SPLIT: none" in prompt


@pytest.mark.asyncio
async def test_split_none_routes_p2_to_p5():
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=AGENTS)
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=True, entry_prefix="FINAL:")
    tid = await server.create_thread("plan", participants=AGENTS)
    sub = await server.create_thread("submission", participants=AGENTS)
    gate = protocol.gate_for(P2_SPLIT)
    gate.bind_to_thread(tid)
    protocol.gate_for(P5_SUBMIT).bind_to_thread(sub)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)

    session = Session(server=server, agents=[], max_steps=10)
    session.protocol = protocol
    protocol.on_gate_open(lambda ph: _on_protocol_gate_open(session, ph))

    await server.send_message(
        tid, author="a1",
        content="PROPOSE:\nSPLIT: none\nSUBMITTER: a1\n",
    )
    for a in AGENTS:
        if a == "a1":
            continue
        await server.send_message(tid, author=a, content="APPROVE: ok")

    assert protocol.split_none is True
    assert protocol._assignments == {}
    assert protocol.phase == P5_SUBMIT


@pytest.mark.asyncio
async def test_assign_beats_split_none_e2():
    """E2: SPLIT: none + ASSIGN → still go to P3."""
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=AGENTS)
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    tid = await server.create_thread("plan", participants=AGENTS)
    gate = protocol.gate_for(P2_SPLIT)
    gate.bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)

    session = Session(server=server, agents=[], max_steps=10)
    session.protocol = protocol
    protocol.on_gate_open(lambda ph: _on_protocol_gate_open(session, ph))

    await server.send_message(
        tid, author="a1",
        content=(
            "PROPOSE:\nSPLIT: none\n"
            "ASSIGN a1: still assigned\nASSIGN a2: b\nASSIGN a3: c\n"
            "SUBMITTER: a1\n"
        ),
    )
    for a in AGENTS:
        if a == "a1":
            continue
        await server.send_message(tid, author=a, content="APPROVE: ok")

    assert protocol.split_none is True
    assert protocol.assignment_for("a1") == "still assigned"
    assert protocol.next_phase_after_gate(P2_SPLIT) == P3_EXECUTE
    assert protocol.phase == P3_EXECUTE
