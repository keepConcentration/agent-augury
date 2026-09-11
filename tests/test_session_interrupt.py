"""Session.request_interrupt() stops agent loops without closing the session."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.server import MessageServer
from agent_augury.session import Session


class HangingBackend(ModelBackend):
    """Blocks until cancelled or released — models a long LLM/tool call."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._gate = asyncio.Event()
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        self.entered.set()
        await self._gate.wait()
        return Completion(text="should-not-reach")


@pytest.mark.asyncio
async def test_request_interrupt_stops_hanging_step():
    server = MessageServer()
    server.register_agent("a1")
    backend = HangingBackend()
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=10)

    run_task = asyncio.create_task(session.run(initial_prompt="go"))
    await asyncio.wait_for(backend.entered.wait(), timeout=2.0)
    assert backend.calls == 1

    session.request_interrupt()
    steps = await asyncio.wait_for(run_task, timeout=2.0)

    assert session.interrupted() is True
    assert steps == 0  # cancelled before a successful step completed

    # Next run clears the interrupt flag and can proceed.
    class DoneBackend(ModelBackend):
        async def complete(self, messages, tools=None):
            return Completion(text=None)

    agent.backend = DoneBackend()
    steps2 = await session.run(initial_prompt="again")
    assert session.interrupted() is False
    assert steps2 >= 1

    await session.close()


@pytest.mark.asyncio
async def test_request_interrupt_between_steps():
    """Cooperative flag stops before the next step without mid-await cancel."""

    class CountingBackend(ModelBackend):
        def __init__(self) -> None:
            self.n = 0
            self.after_first = asyncio.Event()

        async def complete(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                self.after_first.set()
                await asyncio.sleep(0)
                # Keep producing text so the agent would continue without interrupt.
                return Completion(text="step1", tool_calls=[])
            return Completion(text="step2")

    # Fake tool-less forever loop: non-empty text with no tools still continues
    # only if there are pending inbox msgs; empty tool_calls + text keeps going
    # actually: `if not result.tool_calls and result.text is None` — text set means continue!
    # Wait - if text is not None and no tool_calls and no pending, it does NOT break:
    #   if not result.tool_calls and result.text is None and not has_pending: break
    # So text="step1" with no tools → continues. Good.

    server = MessageServer()
    server.register_agent("a1")
    backend = CountingBackend()
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=50)

    run_task = asyncio.create_task(session.run(initial_prompt="go"))
    await asyncio.wait_for(backend.after_first.wait(), timeout=2.0)
    session.request_interrupt()
    steps = await asyncio.wait_for(run_task, timeout=2.0)

    assert session.interrupted() is True
    assert backend.n <= 2  # may have entered step 2 before the flag was seen
    assert steps <= 2
    await session.close()
