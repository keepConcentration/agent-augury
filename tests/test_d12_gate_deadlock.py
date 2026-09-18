"""D12 — all agents parked under headless ends the turn (PHASE_MACHINE_ROUTING §4)."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P2_SPLIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session
from agent_augury.gateway import SurfaceSubscription
from agent_augury.gateway.turn_done import derive_turn_done_reason


class CountingBackend(ModelBackend):
    def __init__(self, script: list[Completion] | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


@pytest.mark.asyncio
async def test_d12_headless_all_park_ends_turn():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)

    agents = [
        AgentLoop(
            agent_id=aid,
            backend=CountingBackend([Completion(text=None)]),
            server=server,
        )
        for aid in ("a1", "a2")
    ]
    session = Session(server=server, agents=agents, max_steps=0)
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol.start = lambda: None  # type: ignore[method-assign]
    session.protocol = protocol

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    steps = await asyncio.wait_for(session.run(initial_prompt="go"), timeout=3.0)
    await session.close()

    assert session._gate_deadlock is True
    assert derive_turn_done_reason(session, steps, None) == "gate_deadlock"


@pytest.mark.asyncio
async def test_d12_interact_surface_keeps_waiting():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)

    agents = [
        AgentLoop(
            agent_id=aid,
            backend=CountingBackend([Completion(text=None)]),
            server=server,
        )
        for aid in ("a1", "a2")
    ]
    session = Session(server=server, agents=agents, max_steps=0)
    session.gateway.attach(
        SurfaceSubscription(name="ink", mode="interact")
    )
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol.start = lambda: None  # type: ignore[method-assign]
    session.protocol = protocol

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def poke() -> None:
        await asyncio.sleep(0.3)
        assert session._gate_deadlock is False
        session.request_interrupt()

    injector = asyncio.create_task(poke())
    await asyncio.wait_for(session.run(initial_prompt="go"), timeout=3.0)
    await injector
    await session.close()
    assert session._gate_deadlock is False


@pytest.mark.asyncio
async def test_d12_counts_live_agents_not_configured_ones():
    """One agent already exited; the rest must still be able to trip D12.

    Comparing `len(_parked_agents)` against `len(self.agents)` looks right but
    hangs exactly when it matters: an agent that broke out on a backend error
    is neither parked nor able to move the gate, so the parked count can never
    reach the configured total.
    """
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)

    class Exploding(ModelBackend):
        async def complete(self, messages, tools=None):
            raise RuntimeError("backend down")

    agents = [
        AgentLoop(agent_id="a1", backend=CountingBackend([Completion(text=None)]),
                  server=server),
        AgentLoop(agent_id="a2", backend=Exploding(), server=server),
    ]
    session = Session(server=server, agents=agents, max_steps=0)
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol.start = lambda: None  # type: ignore[method-assign]
    session.protocol = protocol

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    # a2 dies on its first step, a1 parks at a gate that needs 2/2 forever.
    await asyncio.wait_for(session.run(initial_prompt="go"), timeout=3.0)
    await session.close()

    assert session._gate_deadlock is True
    assert derive_turn_done_reason(session, 1, None) == "gate_deadlock"
