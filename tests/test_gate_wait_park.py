"""Gate-wait idle park (PROTOCOL_GATE_WAIT_PARK_DESIGN.md)."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P1_EXPLORE, P2_SPLIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session
from agent_augury.gateway import SurfaceSubscription


def _keep_park_alive(session: Session) -> None:
    """Attach a fake interact surface so D12 does not end a headless park."""
    session.gateway.attach(
        SurfaceSubscription(name="test-ui", mode="interact")
    )


class CountingBackend(ModelBackend):
    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


@pytest.mark.asyncio
async def test_gate_wait_parks_without_spam_then_wakes_on_inbox():
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    server.register_human()

    b1 = CountingBackend(
        [
            Completion(text="waiting filler once"),
            Completion(text=None),
        ]
    )
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    # Second agent parks immediately on empty completion; interrupted at end.
    b2 = CountingBackend([Completion(text=None)])
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    session = Session(server=server, agents=[a1, a2], max_steps=0)
    session.protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    session.protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    gate = session.protocol.gate_for(P2_SPLIT)
    assert gate is not None
    gate.bind_to_thread(tid)
    session.protocol.phase_manager._phase = P2_SPLIT
    session.protocol.start = lambda: None  # type: ignore[method-assign]
    _keep_park_alive(session)

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def inject_then_interrupt() -> None:
        # Filler step + one-shot gate-thread nudge, then park.
        for _ in range(200):
            if b1.calls >= 2 and ("a1", P2_SPLIT) in session._gate_thread_nudged:
                await asyncio.sleep(0.15)
                if b1.calls == 2:
                    break
            await asyncio.sleep(0.05)
        assert b1.calls == 2, f"expected park after 2 calls, got {b1.calls}"
        await server.human_send(
            tid, author="human", content="ping a1", mentions=["a1"]
        )
        for _ in range(200):
            if b1.calls >= 3:
                break
            await asyncio.sleep(0.05)
        assert b1.calls == 3
        session.request_interrupt()

    injector = asyncio.create_task(inject_then_interrupt())
    await asyncio.wait_for(session.run(initial_prompt="go"), timeout=5.0)
    await injector
    await session.close()


@pytest.mark.asyncio
async def test_no_protocol_empty_text_still_exits():
    server = MessageServer()
    server.register_agent("a1")
    backend = CountingBackend([Completion(text=None)])
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=10)
    steps = await session.run(initial_prompt="x")
    await session.close()
    assert backend.calls == 1
    assert steps == 1


@pytest.mark.asyncio
async def test_is_gate_waiting_p1_and_closed_gate():
    server = MessageServer()
    server.register_agent("a1")
    agent = AgentLoop(
        agent_id="a1",
        backend=CountingBackend([Completion(text=None)]),
        server=server,
    )
    session = Session(server=server, agents=[agent])
    assert session._is_gate_waiting() is False

    session.protocol = CollaborationProtocol(server, participants=["a1"])
    session.protocol.phase_manager._phase = P1_EXPLORE
    assert session._is_gate_waiting() is True
