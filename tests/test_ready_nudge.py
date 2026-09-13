"""READY signal matching + P1 park nudge."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.protocol.collaboration import CollaborationProtocol
from agent_augury.protocol.phases import P1_EXPLORE, P2_SPLIT
from agent_augury.protocol.signals import is_ready_message
from agent_augury.server import MessageServer
from agent_augury.session import Session


@pytest.mark.parametrize(
    "content,expected",
    [
        ("READY:", True),
        ("READY: done", True),
        ("  ready:  ", True),
        ("ready: exploring done", True),
        ("READY", False),
        ("READYFOO", False),
        ("FYI: READY:", False),
        ("", False),
    ],
)
def test_is_ready_message(content: str, expected: bool) -> None:
    assert is_ready_message(content) is expected


class ScriptBackend(ModelBackend):
    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.calls = 0
        self.last_messages: list | None = None

    async def complete(self, messages, tools=None):
        self.calls += 1
        self.last_messages = list(messages)
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


@pytest.mark.asyncio
async def test_p1_nudge_once_then_park_until_ready():
    """Idle P1 agent without READY gets one nudge step, then parks."""
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")

    # After nudge, agent still forgets READY → empty → park.
    b1 = ScriptBackend(
        [
            Completion(text="explored, done narrating"),
            Completion(text=None),  # after nudge
        ]
    )
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    b2 = ScriptBackend([Completion(text=None)])
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    session = Session(server=server, agents=[a1, a2], max_steps=0)
    session.protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    session.protocol.start = lambda: None  # type: ignore[method-assign]
    session.protocol.phase_manager._phase = P1_EXPLORE

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def wait_then_interrupt() -> None:
        for _ in range(200):
            if b1.calls >= 2 and "a1" in session._ready_nudged:
                await asyncio.sleep(0.1)
                if b1.calls == 2:
                    break
            await asyncio.sleep(0.05)
        assert b1.calls == 2, f"expected one nudge step, got {b1.calls}"
        assert "a1" in session._ready_nudged
        nudge_msgs = [
            m
            for m in a1.conversation
            if m.get("role") == "user" and "[protocol]" in str(m.get("content", ""))
        ]
        assert len(nudge_msgs) == 1
        session.request_interrupt()

    injector = asyncio.create_task(wait_then_interrupt())
    await asyncio.wait_for(session.run(initial_prompt="explore"), timeout=5.0)
    await injector
    await session.close()


@pytest.mark.asyncio
async def test_p1_no_nudge_after_ready():
    """Agent that already sent READY: does not get a nudge before park."""
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")

    tid = await server.create_thread("explore", participants=["a1", "a2"])

    b1 = ScriptBackend(
        [
            Completion(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="send_message",
                        arguments={
                            "thread": tid,
                            "content": "READY: done",
                            "mentions": [],
                        },
                    )
                ]
            ),
            Completion(text=None),
        ]
    )
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    b2 = ScriptBackend(
        [
            Completion(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="send_message",
                        arguments={
                            "thread": tid,
                            "content": "READY:",
                            "mentions": [],
                        },
                    )
                ]
            ),
            Completion(text=None),
        ]
    )
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    session = Session(server=server, agents=[a1, a2], max_steps=20)
    session.protocol = CollaborationProtocol(server, participants=["a1", "a2"])

    async def wait_for_p2_then_interrupt() -> None:
        for _ in range(200):
            if session.protocol is not None and session.protocol.phase == P2_SPLIT:
                break
            await asyncio.sleep(0.05)
        assert session.protocol is not None
        assert session.protocol.phase == P2_SPLIT
        session.request_interrupt()

    injector = asyncio.create_task(wait_for_p2_then_interrupt())
    await asyncio.wait_for(session.run(initial_prompt="go"), timeout=5.0)
    await injector
    await session.close()

    assert session._ready_nudged == set()
    nudge_msgs = [
        m
        for m in a1.conversation
        if m.get("role") == "user" and "[protocol]" in str(m.get("content", ""))
    ]
    assert nudge_msgs == []
