"""Session turn termination (SESSION_TURN_TERMINATION_DESIGN.md)."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import COMPLETED, P5_SUBMIT
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
        Completion(tool_calls=[ToolCall(id="t1", name="send_message",
                                        arguments={"thread": tid,
                                                   "content": "FINAL: 98"})]),
        Completion(tool_calls=[ToolCall(id="t2", name="send_message",
                                        arguments={"thread": tid,
                                                   "content": "APPROVE: ok"})]),
    ] + [Completion(text="still talking")] * 5)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=50)
    session.protocol = protocol
    session._setup_done = True
    session._output_task = asyncio.create_task(session._output_consumer())

    steps = await session._run_impl(initial_prompt="go")
    await session.close()

    assert protocol.phase == COMPLETED
    assert backend.calls == 2          # FINAL: + APPROVE, then the loop breaks
    assert steps == 2


@pytest.mark.asyncio
async def test_spent_protocol_resumes_free_form():
    """A COMPLETED protocol from an earlier turn must not kill the next run.

    Live repro: after the answer was submitted, every follow-up question came
    back as ``session: 0 steps`` because the terminal check fired at the top of
    the very first iteration.
    """
    server = MessageServer()
    server.register_agent("a1")
    protocol = CollaborationProtocol(server, participants=["a1"])
    protocol.phase_manager._phase = COMPLETED

    backend = ScriptBackend([Completion(text="sure, about that...")])
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=50)
    session.protocol = protocol
    session._setup_done = True
    session._output_task = asyncio.create_task(session._output_consumer())

    steps = await session._run_impl(initial_prompt="why did you skip that?")
    await session.close()

    assert protocol.phase == COMPLETED      # not restarted
    assert backend.calls >= 1, "follow-up never reached the agents"
    assert steps >= 1
    # D' still closes the free-form turn instead of looping forever
    assert steps <= 2


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
