"""Per-gate entry signal (PHASE_ENTRY_SIGNAL_DESIGN).

P2 opens on ``PROPOSE:``, P5 on ``FINAL:``; voting before the signal exists is
soft-blocked so a gate never sits at N/N unable to move.
"""

from __future__ import annotations

import json

import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.agent.system_prompt import render_system_prompt
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P2_SPLIT, P5_SUBMIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import _PHASE_GATE_ENTRY, _inject_protocol_gate_state


class Quiet(ModelBackend):
    async def complete(self, messages, tools=None):
        return Completion(text=None)


async def _gate(server, participants, phase, thread, **kw):
    protocol = CollaborationProtocol(server, participants=list(participants))
    protocol.bind_gate(phase, thread, **kw)
    tid = await server.create_thread(thread, participants=list(participants))
    gate = protocol.gate_for(phase)
    gate.bind_to_thread(tid)
    protocol.phase_manager._phase = phase
    server.subscribe(gate.on_message)
    return protocol, gate, tid


def test_phase_table_matches_design():
    assert _PHASE_GATE_ENTRY[P2_SPLIT] == (True, "PROPOSE:")
    assert _PHASE_GATE_ENTRY[P5_SUBMIT] == (True, "FINAL:")


@pytest.mark.asyncio
async def test_p5_does_not_open_without_final():
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    _, gate, tid = await _gate(
        server, agents, P5_SUBMIT, "submission",
        require_proposal=True, entry_prefix="FINAL:",
    )
    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: ship it")
    assert gate.approvals == set(agents)
    assert not gate.is_open          # no FINAL: draft exists

    await server.send_message(tid, author="a1", content="FINAL: the answer is 165")
    assert gate.is_open              # late draft opens on the votes already cast


@pytest.mark.asyncio
async def test_p5_propose_is_not_the_entry_signal():
    """PROPOSE: no longer counts on a FINAL: gate — that is the whole point."""
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    _, gate, tid = await _gate(
        server, agents, P5_SUBMIT, "submission",
        require_proposal=True, entry_prefix="FINAL:",
    )
    await server.send_message(tid, author="a1", content="PROPOSE: submit 165")
    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: ok")
    assert not gate.has_proposal
    assert not gate.is_open


@pytest.mark.asyncio
async def test_p2_still_opens_on_propose():
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    _, gate, tid = await _gate(
        server, agents, P2_SPLIT, "plan", require_proposal=True
    )
    await server.send_message(tid, author="a1", content="PROPOSE: split it")
    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: ok")
    assert gate.is_open


@pytest.mark.asyncio
async def test_approve_before_entry_signal_is_soft_blocked():
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    protocol, gate, tid = await _gate(
        server, agents, P5_SUBMIT, "submission",
        require_proposal=True, entry_prefix="FINAL:",
    )
    agent = AgentLoop(agent_id="a1", backend=Quiet(), server=server)
    agent.current_phase = P5_SUBMIT
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_needs_signal == "FINAL:"

    before = len(server.snapshot()["messages"])
    out = json.loads(
        await agent._execute_tool(
            "send_message", {"thread": tid, "content": "APPROVE: ship"}
        )
    )
    assert out["error"] == "entry_signal_required"
    assert out["needs"] == "FINAL:"
    assert len(server.snapshot()["messages"]) == before   # vote never landed

    # posting the draft clears the block
    await agent._execute_tool(
        "send_message", {"thread": tid, "content": "FINAL: 165"}
    )
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_needs_signal is None
    ok = json.loads(
        await agent._execute_tool(
            "send_message", {"thread": tid, "content": "APPROVE: ship"}
        )
    )
    assert "error" not in ok
    assert "a1" in gate.approvals


@pytest.mark.asyncio
async def test_free_form_phase_has_no_entry_signal():
    """P3/P4 keep the old behaviour: the first APPROVE doubles as the proposal."""
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    protocol, gate, tid = await _gate(
        server, agents, P2_SPLIT, "plan", require_proposal=False
    )
    agent = AgentLoop(agent_id="a1", backend=Quiet(), server=server)
    agent.current_phase = P2_SPLIT
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_needs_signal is None

    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: ok")
    assert gate.is_open


@pytest.mark.asyncio
async def test_inject_resets_entry_signal_on_gateless_phase():
    from agent_augury.core.protocol.phases import P1_EXPLORE

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, _, _ = await _gate(
        server, ["a1", "a2"], P5_SUBMIT, "submission",
        require_proposal=True, entry_prefix="FINAL:",
    )
    agent = AgentLoop(agent_id="a1", backend=Quiet(), server=server)
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_needs_signal == "FINAL:"

    protocol.phase_manager._phase = P1_EXPLORE
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_needs_signal is None
    assert agent.gate_entry_prefix is None


def test_prompt_mentions_the_gate_entry_signal():
    p5 = render_system_prompt(
        "a1", phase=P5_SUBMIT,
        gate_thread_id="thread-4", gate_thread_name="submission",
        gate_entry_prefix="FINAL:",
    )
    assert "FINAL:" in p5
    assert "`FINAL:` / `APPROVE:` only to this" in p5

    p2 = render_system_prompt(
        "a1", phase=P2_SPLIT,
        gate_thread_id="thread-1", gate_thread_name="plan",
        gate_entry_prefix="PROPOSE:",
    )
    assert "`PROPOSE:` / `APPROVE:` only to this" in p2
