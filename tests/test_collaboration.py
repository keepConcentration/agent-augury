"""v0.2 — P1~P5 collaboration protocol state machine (DESIGN.md §2.3, §6)."""

import pytest

from agent_augury.protocol.approval import ConsensusGate
from agent_augury.protocol.collaboration import CollaborationProtocol
from agent_augury.protocol.phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    REJECTED,
)
from agent_augury.server import MessageServer


def make_protocol(*agents):
    server = MessageServer()
    for a in agents:
        server.register_agent(a)
    return CollaborationProtocol(server, participants=list(agents))


class TestPhaseTransitions:
    """Phase machine enforces valid transitions."""

    def test_start_at_p1(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        assert p.phase == P1_EXPLORE

    def test_valid_chain_p1_to_p5(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        p.advance(P2_SPLIT)
        assert p.phase == P2_SPLIT
        p.advance(P3_EXECUTE)
        assert p.phase == P3_EXECUTE
        p.advance(P4_REVIEW)
        assert p.phase == P4_REVIEW
        p.advance(P5_SUBMIT)
        assert p.phase == P5_SUBMIT
        p.advance(COMPLETED)
        assert p.phase == COMPLETED

    def test_reject_from_any_phase(self):
        for phase in [P1_EXPLORE, P2_SPLIT, P3_EXECUTE, P4_REVIEW, P5_SUBMIT]:
            p = make_protocol("a1", "a2")
            p.start()
            # advance step by step to the target phase
            chain = [P1_EXPLORE, P2_SPLIT, P3_EXECUTE, P4_REVIEW, P5_SUBMIT]
            target_idx = chain.index(phase)
            for i in range(1, target_idx + 1):
                p.advance(chain[i])
            p.reject()
            assert p.phase == REJECTED
            assert p.is_rejected

    def test_invalid_transition_raises(self):
        p = make_protocol("a1", "a2")
        p.start()
        with pytest.raises(ValueError, match="invalid phase transition"):
            p.advance(P3_EXECUTE)  # can't skip P2

    def test_terminal_states_block_further_advance(self):
        p = make_protocol("a1", "a2")
        p.start()
        p.advance(P2_SPLIT)
        p.advance(P3_EXECUTE)
        p.advance(P4_REVIEW)
        p.advance(P5_SUBMIT)
        p.advance(COMPLETED)
        assert p.is_complete
        with pytest.raises(ValueError):
            p.advance(P1_EXPLORE)

    def test_advance_is_idempotent(self):
        p = make_protocol("a1", "a2")
        p.start()
        p.advance(P1_EXPLORE)  # same phase
        assert p.phase == P1_EXPLORE


class TestPhaseCallbacks:
    """Transition callbacks fire in order."""

    def test_on_phase_change_fires_for_each_transition(self):
        p = make_protocol("a1", "a2")
        seen = []
        p.on_phase_change(lambda frm, to: seen.append((frm, to)))
        p.start()
        p.advance(P2_SPLIT)
        p.advance(P3_EXECUTE)
        # start() is idempotent (already P1_EXPLORE), so only 2 advance events
        assert len(seen) == 2
        assert seen[0] == (P1_EXPLORE, P2_SPLIT)
        assert seen[1] == (P2_SPLIT, P3_EXECUTE)


class TestGateBinding:
    """Gates bind to threads and fire open callbacks."""

    def test_bind_gate_creates_gate_for_phase(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        gate = p.bind_gate(P2_SPLIT, "plan")
        assert isinstance(gate, ConsensusGate)
        assert gate.thread_name == "plan"

    def test_bind_gate_for_invalid_phase_raises(self):
        p = make_protocol("a1", "a2")
        with pytest.raises(ValueError, match="no gate slot"):
            p.bind_gate(P1_EXPLORE, "plan")  # P1 has no gate


@pytest.mark.asyncio
async def test_gate_opens_on_unanimous_approval():
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    p = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
    p.start()
    p.bind_gate(P2_SPLIT, "plan")
    p.advance(P2_SPLIT)

    plan = await server.create_thread("plan", participants=["a1", "a2", "a3"])
    await server.send_message(plan, author="a1", content="PROPOSE: v1", mentions=[])
    assert not p.gate_for(P2_SPLIT).is_open

    await server.send_message(plan, author="a1", content="APPROVE: ok", mentions=[])
    assert not p.gate_for(P2_SPLIT).is_open

    await server.send_message(plan, author="a2", content="APPROVE: ok", mentions=[])
    assert not p.gate_for(P2_SPLIT).is_open

    await server.send_message(plan, author="a3", content="APPROVE: ok", mentions=[])
    assert p.gate_for(P2_SPLIT).is_open


class TestP1Finish:
    """Explicit P1 finish API replaces thread-counting heuristics."""

    def test_finish_p1_advances_to_p2(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        assert p.phase == P1_EXPLORE
        # All participants must send READY before P1 can finish
        assert not p.all_ready
        with pytest.raises(RuntimeError, match="Cannot finish P1"):
            p.finish_p1()
        # Simulate all participants sending READY
        for a in ["a1", "a2", "a3"]:
            p._ready_states.add(a)
        assert p.all_ready
        p.finish_p1()
        assert p.phase == P2_SPLIT

    def test_finish_p1_only_from_p1(self):
        p = make_protocol("a1", "a2")
        p.start()
        p.advance(P2_SPLIT)
        with pytest.raises(ValueError, match="finish_p1"):
            p.finish_p1()

    def test_finish_p1_before_start_raises(self):
        p = make_protocol("a1", "a2")
        # Protocol starts at P1_EXPLORE after construction — advance first
        p.advance(P2_SPLIT)
        with pytest.raises(ValueError, match="finish_p1"):
            p.finish_p1()


class TestAssembler:
    """Assembler defaults to first participant."""

    def test_default_assembler(self):
        p = make_protocol("a1", "a2", "a3")
        assert p.assembler_id == "a1"

    def test_bind_assembler(self):
        p = make_protocol("a1", "a2", "a3")
        p.bind_assembler("a2")
        assert p.assembler_id == "a2"


class TestStatus:
    """Status snapshot for observability."""

    def test_status_reflects_current_phase(self):
        p = make_protocol("a1", "a2")
        p.start()
        status = p.status()
        assert status["phase"] == P1_EXPLORE
        assert status["participants"] == ["a1", "a2"]
        assert status["assembler_id"] == "a1"
