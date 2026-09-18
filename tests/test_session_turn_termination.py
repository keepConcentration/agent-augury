"""Session turn termination (SESSION_TURN_TERMINATION_DESIGN.md)."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import COMPLETED, P1_EXPLORE, P5_SUBMIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session, _on_protocol_gate_open
from agent_augury.gateway.turn_done import derive_turn_done_reason, publish_turn_done
from agent_augury.gateway.types import make_event


class ScriptBackend(ModelBackend):
    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


class CaptureGateway:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_protocol_reaching_completed_ends_the_turn():
    """B: a terminal reached DURING the run stops the agents right away.

    The old version of this test pre-set COMPLETED before ``_run_impl``, which
    is the *resume* case (see below) — it locked in a bug where a finished
    session could never be resumed.
    """
    server = MessageServer()
    server.register_agent("a1")
    protocol = CollaborationProtocol(server, participants=["a1"])
    protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=True,
                       entry_prefix="FINAL:")
    tid = await server.create_thread("submission", participants=["a1"])
    gate = protocol.gate_for(P5_SUBMIT)
    gate.bind_to_thread(tid)
    protocol.phase_manager._phase = P5_SUBMIT
    protocol.on_gate_open(lambda ph: _on_protocol_gate_open(session, ph))
    # phase was set directly, so wire the gate's on_open callback by hand
    protocol._setup_gate_for_phase(P5_SUBMIT)

    backend = ScriptBackend([
        # One participant, so posting the draft is already unanimous: the gate
        # opens on FINAL: alone and the extra APPROVE: is never reached.
        Completion(tool_calls=[ToolCall(id="t1", name="send_message",
                                        arguments={"thread": tid,
                                                   "content": "FINAL: 98"})]),
    ] + [Completion(text="still talking")] * 5)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=50)
    session.protocol = protocol
    session._setup_done = True
    session._output_task = asyncio.create_task(session._output_consumer())

    steps = await session._run_impl(initial_prompt="go")
    await session.close()

    assert protocol.phase == COMPLETED
    assert backend.calls == 1          # FINAL: opens the gate, then it breaks
    assert steps == 1                  # one turn, no follow-up chatter


async def _spent_session(script):
    """A session whose protocol finished an earlier round, ready for a follow-up."""
    server = MessageServer()
    server.register_agent("a1")
    protocol = CollaborationProtocol(server, participants=["a1"])
    protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=True,
                       entry_prefix="FINAL:")
    sub_tid = await server.create_thread("submission", participants=["a1"])
    human_tid = await server.create_thread("human", participants=["a1"])
    gate = protocol.gate_for(P5_SUBMIT)
    gate.bind_to_thread(sub_tid)

    # ...and it finished: gate opened, split recorded, phase terminal.
    gate.approvals.add("a1")
    gate.opened_at_seq = 99
    protocol._assignments = {"a1": "괄호 계산 검증"}
    protocol.submitter_id = "a1"
    protocol._gate_open_fired.add(P5_SUBMIT)
    protocol.phase_manager._phase = COMPLETED

    backend = ScriptBackend(script)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=30)
    session.protocol = protocol
    session._setup_done = True
    protocol.on_gate_open(lambda ph: _on_protocol_gate_open(session, ph))
    session._output_task = asyncio.create_task(session._output_consumer())
    return session, protocol, gate, backend, human_tid, sub_tid


def _say(tid, text):
    return Completion(tool_calls=[ToolCall(
        id="t", name="send_message",
        arguments={"thread": tid, "content": text})])


@pytest.mark.asyncio
async def test_followup_question_opens_a_new_light_round():
    """A COMPLETED protocol from an earlier turn must not kill the next run.

    Live repro (`694182e`): every follow-up came back as ``session: 0 steps``.
    Since FOLLOWUP_TURN_PROTOCOL_DESIGN it does more than run -- the follow-up
    opens a fresh ``light`` round (P1 -> P5) so the answer gets reviewed
    instead of landing as N unread drafts.
    """
    session, protocol, gate, backend, human_tid, sub_tid = await _spent_session([])
    backend.script = [
        _say(human_tid, "READY: done"),
        _say(sub_tid, "FINAL: the follow-up answer"),
    ]

    steps = await session._run_impl(initial_prompt="why did you skip that?")
    await session.close()

    assert backend.calls >= 1, "follow-up never reached the agents"
    assert steps >= 1
    assert protocol.mode == "light"
    # P1 -> P5 -> COMPLETED: one participant, so FINAL: is already unanimous.
    assert protocol.phase == COMPLETED
    assert gate.is_open
    finals = [m for m in session.server.snapshot()["messages"]
              if m["content"].startswith("FINAL:")]
    assert len(finals) == 1


@pytest.mark.asyncio
async def test_a_bare_resume_does_not_open_a_round():
    """No new question, no new round -- only a real prompt starts one."""
    session, protocol, _gate, _backend, _h, _s = await _spent_session(
        [Completion(text=None)]
    )
    steps = await session._run_impl(initial_prompt=None)
    await session.close()

    assert protocol.phase == COMPLETED      # untouched -- no round opened
    assert protocol.mode == "full"          # and the mode was not switched
    assert steps <= 2                       # free-form, closed by D-prime


def test_begin_round_clears_last_rounds_state():
    """Gates reset in place: MessageServer has no unsubscribe (§4.3)."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    gate = protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=True,
                              entry_prefix="FINAL:")
    gate.approvals.update(["a1", "a2"])
    gate.draft_author = "a1"
    gate.opened_at_seq = 42
    gate._proposal_received = True
    protocol._assignments = {"a1": "old share"}
    protocol.submitter_id = "a1"
    protocol._ready_states.update(["a1", "a2"])
    protocol._gate_open_fired.add(P5_SUBMIT)
    protocol.phase_manager._phase = COMPLETED

    subscribers_before = len(server._subscribers)
    protocol.begin_round(mode="light")

    assert protocol.phase == P1_EXPLORE
    assert protocol.mode == "light"
    assert gate.approvals == set()
    assert gate.draft_author is None
    assert gate.opened_at_seq is None
    assert not gate.has_proposal
    assert protocol._assignments == {}
    assert protocol.submitter_id is None
    assert protocol.ready_states == frozenset()
    # Without this the gate reopens but the phase never advances.
    assert P5_SUBMIT not in protocol._gate_open_fired
    assert len(server._subscribers) == subscribers_before, "gate re-subscribed"


def test_begin_round_refuses_a_live_protocol():
    server = MessageServer()
    server.register_agent("a1")
    protocol = CollaborationProtocol(server, participants=["a1"])
    with pytest.raises(ValueError, match="finished protocol"):
        protocol.begin_round(mode="light")


@pytest.mark.asyncio
async def test_text_idle_two_breaks():
    server = MessageServer()
    server.register_agent("a1")
    backend = ScriptBackend(
        [Completion(text="one"), Completion(text="two")]
    )
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=10)

    steps = await session.run(initial_prompt="go")
    await session.close()
    assert steps == 2
    assert backend.calls == 2


@pytest.mark.asyncio
async def test_tool_between_text_resets_idle_streak():
    server = MessageServer()
    server.register_agent("a1")
    backend = ScriptBackend(
        [
            Completion(text="a"),
            Completion(
                text="b",
                tool_calls=[ToolCall(id="t1", name="read_resource", arguments={})],
            ),
            Completion(text="c"),
            Completion(text="d"),
        ]
    )
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=20)

    steps = await asyncio.wait_for(session.run(initial_prompt="go"), timeout=10.0)
    await session.close()
    assert steps == 4


def test_derive_turn_done_reason_error_not_cancelled():
    server = MessageServer()
    session = Session(server=server, agents=[])
    assert (
        derive_turn_done_reason(session, 0, RuntimeError("boom"))
        == "error"
    )
    assert (
        derive_turn_done_reason(session, 0, asyncio.CancelledError())
        != "error"
    )


def test_derive_turn_done_protocol_completed():
    server = MessageServer()
    session = Session(server=server, agents=[])
    session.protocol = CollaborationProtocol(server, participants=["a1"])
    session.protocol.phase_manager._phase = COMPLETED
    assert derive_turn_done_reason(session, 3, None) == "protocol_completed"


def test_publish_turn_done_swallows_publish_errors():
    server = MessageServer()
    session = Session(server=server, agents=[])

    class Broken:
        def publish(self, _event: dict) -> None:
            raise OSError("pipe closed")

    publish_turn_done(Broken(), session, 0, run_exc=RuntimeError("x"))


@pytest.mark.asyncio
async def test_gate_open_snapshot_before_advance():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    session = Session(server=server, agents=[])
    session.protocol = CollaborationProtocol(
        server, participants=["a1", "a2"], mode="light"
    )
    session.protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=False)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate = session.protocol.gate_for(P5_SUBMIT)
    assert gate is not None
    gate.bind_to_thread(tid)
    session.protocol.start()
    for a in ("a1", "a2"):
        await server.send_message(tid, author=a, content="READY: ok")
    assert session.protocol.phase == P5_SUBMIT
    gate.approvals.update(["a1", "a2"])
    gate.opened_at_seq = 99

    captured: list[dict] = []
    session.bridge.publish_core_event = captured.append  # type: ignore[method-assign]

    _on_protocol_gate_open(session, P5_SUBMIT)
    assert session.protocol.phase == COMPLETED
    gate_events = [e for e in captured if e.get("type") == "session.gate"]
    assert len(gate_events) >= 1
    snap = gate_events[0]
    assert snap.get("open") is True
    assert snap.get("phase") == P5_SUBMIT
    assert snap.get("pending") == []


def test_turn_done_event_type_registered():
    make_event("session.turn_done", reason="idle", steps=1)
