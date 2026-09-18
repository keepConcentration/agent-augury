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
from agent_augury.gateway import SurfaceSubscription


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
    session.gateway.attach(
        SurfaceSubscription(name="test-ui", mode="interact")
    )

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


def _nudge_for(agent_id, approvals, *, require_proposal=True, has_proposal=False):
    """Render the nudge an agent would get, without running a session."""
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    session = Session(server=server, agents=[])
    protocol = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
    gate = protocol.bind_gate(P2_SPLIT, "plan", require_proposal=require_proposal)
    gate.thread_id = "thread-1"
    gate.participants = ["a1", "a2", "a3"]
    gate.approvals = set(approvals)
    gate._proposal_received = has_proposal
    protocol.phase_manager._phase = P2_SPLIT
    session.protocol = protocol
    agent = AgentLoop(agent_id=agent_id, backend=ScriptBackend([]), server=server)
    fired = session._maybe_nudge_gate_thread(agent)
    texts = [m["content"] for m in agent.conversation if m["role"] == "user"]
    return fired, (texts[0] if texts else "")


def test_nudge_names_the_holdout():
    """Live x5: a human kept supplying "you are the only one left".

    The runtime already had it in gate.approvals and never rendered it.
    """
    fired, text = _nudge_for("a3", {"a1", "a2"}, has_proposal=True)
    assert fired
    assert "ONLY one left" in text
    assert "APPROVE:" in text
    assert "prose does not count" in text


def test_nudge_lists_who_is_still_missing():
    fired, text = _nudge_for("a2", {"a1"}, has_proposal=True)
    assert fired
    assert "Still missing: a2, a3" in text
    assert "ONLY one left" not in text


def test_nudge_asks_for_the_entry_signal_when_no_draft_exists():
    fired, text = _nudge_for("a1", set(), has_proposal=False)
    assert fired
    assert "Send PROPOSE:" in text


def test_no_nudge_for_an_agent_already_counted():
    """Saying "you have not voted" to an agent that voted would be false."""
    fired, text = _nudge_for("a1", {"a1"}, has_proposal=True)
    assert not fired
    assert text == ""


def test_nudge_tells_non_submitters_to_wait_not_to_draft():
    """Live `e3237b35`: the nudge told two agents to post FINAL: while the
    team had picked agent-4 — the exact action M4a then blocks."""
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    session = Session(server=server, agents=[])
    protocol = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
    gate = protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    gate.thread_id = "thread-1"
    gate.participants = ["a1", "a2", "a3"]
    protocol.submitter_id = "a3"
    protocol.phase_manager._phase = P2_SPLIT
    session.protocol = protocol

    a1 = AgentLoop(agent_id="a1", backend=ScriptBackend([]), server=server)
    assert session._maybe_nudge_gate_thread(a1)
    text = [m["content"] for m in a1.conversation if m["role"] == "user"][-1]
    assert "the team chose a3" in text
    assert "Do NOT write your own" in text

    # ...but the chosen agent is still asked for the draft.
    a3 = AgentLoop(agent_id="a3", backend=ScriptBackend([]), server=server)
    assert session._maybe_nudge_gate_thread(a3)
    a3_text = [m["content"] for m in a3.conversation if m["role"] == "user"][-1]
    assert "Send PROPOSE:" in a3_text
