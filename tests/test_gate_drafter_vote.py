"""Drafter self-vote, empty messages, and signals written as prose.

All three come from the 2026-09-17 live run, where two gates stalled until a
human told the last agent to speak.
"""

from __future__ import annotations

import json

import pytest

from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P1_EXPLORE, P2_SPLIT, P5_SUBMIT
from agent_augury.core.server import MessageServer


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


@pytest.mark.asyncio
async def test_drafter_does_not_have_to_approve_its_own_draft():
    """P5 live: a2 posted FINAL:, the other three approved, and a2 waited for
    votes it already had. Posting the draft now counts as a2's own vote."""
    server = MessageServer()
    agents = ["a1", "a2", "a3", "a4"]
    for a in agents:
        server.register_agent(a)
    _, gate, tid = await _gate(
        server, agents, P5_SUBMIT, "submission",
        require_proposal=True, entry_prefix="FINAL:",
    )
    await server.send_message(tid, author="a2", content="FINAL: the answer is 34")
    assert gate.approvals == {"a2"}
    for a in ("a1", "a3", "a4"):
        await server.send_message(tid, author=a, content="APPROVE: looks right")
    assert gate.is_open  # no fourth round trip needed


@pytest.mark.asyncio
async def test_redraft_recollects_votes_including_the_authors():
    """A replacement draft clears votes; the author votes for the new one."""
    server = MessageServer()
    agents = ["a1", "a2"]
    for a in agents:
        server.register_agent(a)
    _, gate, tid = await _gate(
        server, agents, P2_SPLIT, "plan",
        require_proposal=True, entry_prefix="PROPOSE:",
    )
    await server.send_message(tid, author="a1", content="PROPOSE: split A/B")
    await server.send_message(tid, author="a2", content="APPROVE:")
    assert gate.is_open

    gate2 = gate
    gate2.opened_at_seq = None  # simulate a reopened gate for the redraft path
    gate2.approvals = {"a1", "a2"}
    await server.send_message(tid, author="a1", content="PROPOSE: split C/D")
    assert gate2.approvals == {"a1"}  # a2 must look at the new text


@pytest.mark.asyncio
async def test_empty_send_message_is_refused():
    """Live run seq 9: an empty broadcast woke every peer and said nothing."""
    server = MessageServer()
    server.register_agent("a1")
    tid = await server.create_thread("plan", participants=["a1"])
    loop = AgentLoop("a1", server, Quiet())
    out = json.loads(await loop._execute_tool(
        "send_message", {"thread": tid, "content": "   "}
    ))
    assert out["error"] == "empty_message"
    assert server.snapshot()["messages"] == []


@pytest.mark.asyncio
async def test_ready_in_reply_text_gets_a_nudge():
    """Live run `43addf1b`: agent-4 wrote READY: in its reply and never sent
    it, so P1 sat at 3/4 and the turn ended with the phase never advancing.

    P1 has no gate, so the P2-P5 branch of the nudge could not see this.
    """
    server = MessageServer()
    server.register_agent("a4")
    human = await server.create_thread("human", participants=["a4"])
    loop = AgentLoop("a4", server, Quiet())
    loop.current_phase = P1_EXPLORE
    loop.gate_open = False
    loop.gate_thread_id = None      # P1 binds no gate
    loop.gate_entry_prefix = None

    nudge = loop._unsent_signal_nudge("READY: I solved it, the answer is 392", [])
    assert nudge is not None and human in nudge

    loop.ready_states = {"a4"}      # already counted -> silence
    assert loop._unsent_signal_nudge("READY: again", []) is None


@pytest.mark.asyncio
async def test_signal_in_reply_text_gets_a_nudge():
    """Live run: a4 wrote APPROVE: in its reply and never sent it, hanging P2."""
    server = MessageServer()
    server.register_agent("a4")
    tid = await server.create_thread("plan", participants=["a4"])
    loop = AgentLoop("a4", server, Quiet())
    loop.current_phase = P2_SPLIT
    loop.gate_open = False
    loop.gate_thread_id = tid
    loop.gate_entry_prefix = "PROPOSE:"

    nudge = loop._unsent_signal_nudge("APPROVE: the result 34 is correct", [])
    assert nudge is not None and tid in nudge

    # already voted -> silence
    loop.gate_approvals = {"a4"}
    assert loop._unsent_signal_nudge("APPROVE: again", []) is None

    # actually sent it -> silence
    loop.gate_approvals = frozenset()
    sent = [ToolCall(id="1", name="send_message", arguments={})]
    assert loop._unsent_signal_nudge("APPROVE: ok", sent) is None


def test_terminal_phases_say_the_protocol_is_over():
    """Live `a858cd97`: a follow-up question ran 39 steps of imitated protocol.

    At COMPLETED the phase block was empty, so the only thing steering the
    agent was a conversation full of PROPOSE:/APPROVE:/FINAL: from the run
    that had just ended.
    """
    from agent_augury.core.agent.system_prompt import render_system_prompt
    from agent_augury.core.protocol.phases import COMPLETED, REJECTED

    for phase in (COMPLETED, REJECTED):
        block = render_system_prompt("a1", phase=phase)
        assert f"**{phase}**" in block, f"{phase} renders no phase block"
        assert "closed history" in block


def test_every_gated_phase_says_how_to_end_it():
    """Live run `63fec483`: P3 took 13 messages where P4/P5 took 4 each.

    Its prompt block never named the signal that closes the phase, so agents
    filled the gap with status pings and acknowledgements until someone
    happened to APPROVE: on the 10th message.
    """
    from agent_augury.core.agent.system_prompt import render_system_prompt

    for phase in ("P2_SPLIT", "P3_EXECUTE", "P4_REVIEW", "P5_SUBMIT"):
        block = render_system_prompt("a1", phase=phase)
        assert "APPROVE:" in block, f"{phase} never says how to close its gate"


@pytest.mark.asyncio
async def test_spent_protocol_drops_the_previous_turns_assignment():
    """Live `a858cd97`: a follow-up question arrived with the protocol COMPLETED.

    The phase block is empty at COMPLETED, so the stale P2 line was the ONLY
    protocol text left in the prompt -- agents kept "verifying" the previous
    turn's arithmetic and leaked its answer into the new one.
    """
    from agent_augury.core.agent.system_prompt import render_system_prompt
    from agent_augury.core.protocol.phases import COMPLETED
    from agent_augury.core.session import _inject_protocol_gate_state

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol._assignments = {"a2": "괄호 계산 검증 - (7-3)=4"}
    protocol._submitter_id = "a1"

    agent = AgentLoop("a2", server, Quiet())

    protocol.phase_manager._phase = P2_SPLIT
    _inject_protocol_gate_state(agent, protocol)
    assert agent.assignment == "괄호 계산 검증 - (7-3)=4"   # live during the run

    protocol.phase_manager._phase = COMPLETED
    _inject_protocol_gate_state(agent, protocol)
    assert agent.assignment is None
    assert agent.submitter_id is None
    agent.current_phase = COMPLETED
    prompt = render_system_prompt(
        "a2", phase=COMPLETED,
        assignment=agent.assignment, submitter_id=agent.submitter_id,
    )
    assert "assigned share" not in prompt
