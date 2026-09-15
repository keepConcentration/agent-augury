"""Gate thread id surfaces in prompts / protocol nudges."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.agent.system_prompt import render_system_prompt
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P2_SPLIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session


def test_render_system_prompt_includes_gate_thread_id():
    prompt = render_system_prompt(
        "agent-1",
        phase="P2_SPLIT",
        gate_thread_id="thread-0",
        gate_thread_name="plan",
    )
    assert "Gate thread id: `thread-0`" in prompt
    assert "name: plan" in prompt
    assert "Do not create another thread named 'plan'" in prompt


def test_render_system_prompt_omits_gate_when_unbound():
    prompt = render_system_prompt("agent-1", phase="P2_SPLIT")
    assert "Gate thread id" not in prompt


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
async def test_create_thread_reused_notes_existing_plan():
    server = MessageServer()
    server.register_agent("a1")
    tid = await server.create_thread("plan", participants=["a1"])
    agent = AgentLoop(
        agent_id="a1",
        backend=ScriptBackend(
            [
                Completion(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="create_thread",
                            arguments={
                                "name": "plan",
                                "participants": ["a1"],
                            },
                        )
                    ]
                )
            ]
        ),
        server=server,
    )
    await agent.step()
    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["thread_id"] == tid
    assert payload["reused"] is True


@pytest.mark.asyncio
async def test_p2_gate_thread_nudge_once():
    """After P1→P2, idle agent gets one nudge naming the bound plan thread."""
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    plan = await server.create_thread("plan", participants=["a1", "a2"])

    b1 = ScriptBackend(
        [
            Completion(text=None),  # first empty → park path → nudge
            Completion(text=None),  # after nudge stay silent → park
        ]
    )
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    b2 = ScriptBackend([Completion(text=None)])
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    session = Session(server=server, agents=[a1, a2], max_steps=0)
    session.protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    gate = session.protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    gate.bind_to_thread(plan)
    session.protocol.start = lambda: None  # type: ignore[method-assign]
    session.protocol.phase_manager._phase = P2_SPLIT
    session.protocol._setup_gate_for_phase(P2_SPLIT)

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def wait_then_interrupt() -> None:
        for _ in range(200):
            if b1.calls >= 2 and ("a1", P2_SPLIT) in session._gate_thread_nudged:
                break
            await asyncio.sleep(0.01)
        session.request_interrupt()

    asyncio.create_task(wait_then_interrupt())
    await session.run()

    assert ("a1", P2_SPLIT) in session._gate_thread_nudged
    nudged = [
        m["content"]
        for m in a1.conversation
        if m["role"] == "user" and "[protocol]" in m.get("content", "")
    ]
    assert nudged
    assert plan in nudged[0]
    assert "plan" in nudged[0]

    # System prompt on the post-nudge step should also carry the id.
    assert b1.last_messages is not None
    system = b1.last_messages[0]["content"]
    assert f"Gate thread id: `{plan}`" in system
