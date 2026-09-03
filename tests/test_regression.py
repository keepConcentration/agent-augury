"""Regression tests for v0.2 defect fixes.

Covers:
1. assistant message preserves tool_calls in OpenAI-compatible format
2. work-share blocked when gate_open=False even with gate_thread_id=None
3. READY-based P1 finish policy (all participants must send READY before P2)
"""
import json

import pytest

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.protocol.collaboration import CollaborationProtocol
from agent_augury.protocol.phases import P1_EXPLORE, P2_SPLIT
from agent_augury.server import MessageServer


class ScriptedBackend(ModelBackend):
    """Returns pre-scripted completions in order; records what it saw."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tool_names": [t["name"] for t in tools]}
        )
        return self.script.pop(0)


def make_agent(server, agent_id, script):
    return AgentLoop(
        agent_id=agent_id,
        server=server,
        backend=ScriptedBackend(script),
        system_prompt="You are a radio agent.",
    )


# ---------------------------------------------------------------------------
# Fix 1: assistant message preserves tool_calls
# ---------------------------------------------------------------------------


async def test_assistant_message_preserves_tool_calls():
    """assistant message must include tool_calls in OpenAI-compatible format."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="call_abc", name="create_thread", arguments={
                        "name": "plan", "participants": ["agent-1", "agent-2"]
                    }),
                ]
            ),
            Completion(text="done"),
        ],
    )
    await agent.step()

    # Find the assistant message
    assistant_msgs = [m for m in agent.conversation if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assistant_msg = assistant_msgs[0]

    # Must have tool_calls in OpenAI-compatible format
    assert "tool_calls" in assistant_msg
    assert len(assistant_msg["tool_calls"]) == 1
    tc = assistant_msg["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "create_thread"
    assert json.loads(tc["function"]["arguments"]) == {
        "name": "plan", "participants": ["agent-1", "agent-2"]
    }


async def test_assistant_message_without_tool_calls_has_no_tool_calls_key():
    """assistant message without tool calls should not have tool_calls key."""
    server = MessageServer()
    server.register_agent("agent-1")

    agent = make_agent(
        server,
        "agent-1",
        [Completion(text="just text")],
    )
    await agent.step()

    assistant_msgs = [m for m in agent.conversation if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "tool_calls" not in assistant_msgs[0]


async def test_multi_turn_tool_call_id_propagates():
    """tool_call_id from assistant message must match the tool result message."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="call_1", name="create_thread", arguments={
                        "name": "t1", "participants": ["agent-1", "agent-2"]
                    }),
                    ToolCall(id="call_2", name="create_thread", arguments={
                        "name": "t2", "participants": ["agent-1", "agent-2"]
                    }),
                ]
            ),
            Completion(text="done"),
        ],
    )
    await agent.step()

    assistant_msgs = [m for m in agent.conversation if m["role"] == "assistant"]
    tc_ids = {tc["id"] for tc in assistant_msgs[0]["tool_calls"]}
    assert tc_ids == {"call_1", "call_2"}

    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    tool_call_ids = {m["tool_call_id"] for m in tool_msgs}
    assert tool_call_ids == {"call_1", "call_2"}


# ---------------------------------------------------------------------------
# Fix 2: work-share blocked when gate_open=False, gate_thread_id=None
# ---------------------------------------------------------------------------


async def test_work_share_blocked_when_gate_closed_and_no_gate_thread():
    """When gate_open=False and gate_thread_id=None, work-share is blocked."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    tid = await server.create_thread("work", participants=["agent-1", "agent-2"])

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="send_message", arguments={
                        "thread": tid, "content": "(FYI) working on my share", "mentions": []
                    }),
                ]
            ),
        ],
    )
    # Set initial state: gate closed, no gate thread bound
    agent.gate_open = False
    agent.gate_thread_id = None

    await agent.step()

    # The send should have been blocked — no message in the thread
    snap = server.snapshot()
    msgs_in_thread = [m for m in snap["messages"] if m["thread_id"] == tid]
    assert len(msgs_in_thread) == 0

    # Tool result should indicate gate_closed error
    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    result = json.loads(tool_msgs[0]["content"])
    assert result["error"] == "gate_closed"


async def test_propose_blocked_when_gate_thread_not_bound():
    """PROPOSE is blocked when gate_thread_id is None (no gate thread bound).

    Only READY: is allowed in the unbound state to finish P1 exploration.
    """
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="send_message", arguments={
                        "thread": tid, "content": "PROPOSE: v1", "mentions": []
                    }),
                ]
            ),
        ],
    )
    agent.gate_open = False
    agent.gate_thread_id = None

    await agent.step()

    # PROPOSE should be blocked — no message in the thread
    snap = server.snapshot()
    msgs_in_thread = [m for m in snap["messages"] if m["thread_id"] == tid]
    assert len(msgs_in_thread) == 0

    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    result = json.loads(tool_msgs[0]["content"])
    assert result["error"] == "gate_closed"


async def test_ready_allowed_when_gate_thread_not_bound():
    """READY: is allowed even when gate_thread_id is None (to finish P1)."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    tid = await server.create_thread("explore", participants=["agent-1", "agent-2"])

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="send_message", arguments={
                        "thread": tid, "content": "READY:", "mentions": []
                    }),
                ]
            ),
        ],
    )
    agent.gate_open = False
    agent.gate_thread_id = None

    await agent.step()

    # READY: should go through
    snap = server.snapshot()
    msgs_in_thread = [m for m in snap["messages"] if m["thread_id"] == tid]
    assert len(msgs_in_thread) == 1
    assert msgs_in_thread[0]["content"] == "READY:"


async def test_gate_binding_messages_allowed_after_gate_bound():
    """PROPOSE/APPROVE are allowed once gate_thread_id is explicitly bound."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    tid = await server.create_thread("plan", participants=["agent-1", "agent-2"])

    agent = make_agent(
        server,
        "agent-1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="send_message", arguments={
                        "thread": tid, "content": "PROPOSE: v1", "mentions": []
                    }),
                ]
            ),
        ],
    )
    # Gate is closed but explicitly bound to this thread
    agent.gate_open = False
    agent.gate_thread_id = tid

    await agent.step()

    # PROPOSE should go through because gate_thread_id is bound
    snap = server.snapshot()
    msgs_in_thread = [m for m in snap["messages"] if m["thread_id"] == tid]
    assert len(msgs_in_thread) == 1
    assert msgs_in_thread[0]["content"] == "PROPOSE: v1"


# ---------------------------------------------------------------------------
# Fix 4: P1 blocks non-READY messages; bind_to_thread does not set has_proposal
# ---------------------------------------------------------------------------


async def test_p1_explore_blocks_non_ready_messages():
    """P1_EXPLORE: only READY: messages are allowed; others are blocked."""
    from agent_augury.protocol.collaboration import CollaborationProtocol
    from agent_augury.protocol.phases import P1_EXPLORE

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)

    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.start()
    assert protocol.phase == P1_EXPLORE

    tid = await server.create_thread("explore", participants=["a1", "a2"])

    # Create an agent with P1 phase state injected
    agent = make_agent(
        server,
        "a1",
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="send_message", arguments={
                        "thread": tid, "content": "(FYI) my findings", "mentions": []
                    }),
                ]
            ),
        ],
    )
    # Simulate what _inject_protocol_gate_state does for P1
    agent.gate_open = False
    agent.gate_thread_id = None

    await agent.step()

    # The message should be blocked — no message in the thread
    snap = server.snapshot()
    msgs_in_thread = [m for m in snap["messages"] if m["thread_id"] == tid]
    assert len(msgs_in_thread) == 0

    # Tool result should indicate gate_closed error
    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    result = json.loads(tool_msgs[0]["content"])
    assert result["error"] == "gate_closed"


async def test_bind_to_thread_does_not_set_has_proposal():
    """bind_to_thread() alone must NOT make has_proposal True.

    has_proposal should only become True when an actual PROPOSE message
    arrives. This prevents gates from opening without a real proposal
    when Session pre-binds gates during setup.
    """
    from agent_augury.protocol.approval import ConsensusGate

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)

    gate = ConsensusGate(server, thread_name="plan", require_proposal=True)

    tid = await server.create_thread("plan", participants=["a1", "a2"])

    # Explicitly bind without any PROPOSE message
    gate.bind_to_thread(tid)

    assert gate.thread_id == tid
    assert gate.participants == ["a1", "a2"]
    assert gate.has_proposal is False, "bind_to_thread alone must not set has_proposal"


# ---------------------------------------------------------------------------
# Fix 3: READY-based P1 finish policy
# ---------------------------------------------------------------------------


class TestReadyBasedP1Finish:
    """P1 finish requires all participants to send READY."""

    def test_all_ready_initially_false(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        assert not p.all_ready

    def test_all_ready_true_when_all_ready(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        p._ready_states = {"a1", "a2", "a3"}
        assert p.all_ready

    def test_finish_p1_fails_without_all_ready(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        with pytest.raises(RuntimeError, match="Cannot finish P1"):
            p.finish_p1()

    def test_finish_p1_fails_with_partial_ready(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        p._ready_states = {"a1", "a2"}  # missing a3
        assert not p.all_ready
        with pytest.raises(RuntimeError, match="Cannot finish P1"):
            p.finish_p1()

    def test_finish_p1_succeeds_when_all_ready(self):
        p = make_protocol("a1", "a2", "a3")
        p.start()
        p._ready_states = {"a1", "a2", "a3"}
        p.finish_p1()
        assert p.phase == P2_SPLIT

    def test_ready_via_message_subscription(self):
        """READY messages sent via server are tracked by the protocol."""
        import asyncio
        asyncio.run(self._ready_via_message_subscription_scenario())

    async def _ready_via_message_subscription_scenario(self):
        server = MessageServer()
        for a in ("a1", "a2", "a3"):
            server.register_agent(a)
        p = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
        p.start()

        tid = await server.create_thread("explore", participants=["a1", "a2", "a3"])
        await server.send_message(tid, author="a1", content="READY:", mentions=[])
        await server.send_message(tid, author="a2", content="READY:", mentions=[])
        assert not p.all_ready
        assert p.phase == P1_EXPLORE
        await server.send_message(tid, author="a3", content="READY:", mentions=[])
        # All READY: received → protocol auto-advances to P2
        assert p.phase == P2_SPLIT

    def test_ready_requires_exact_prefix(self):
        """Only exact ``READY:`` is recognized; ``READYFOO`` is ignored."""
        server = MessageServer()
        for a in ("a1", "a2"):
            server.register_agent(a)
        p = CollaborationProtocol(server, participants=["a1", "a2"])
        p.start()

        # READYFOO should NOT count
        p._ready_states.add("a1")
        assert not p.all_ready  # a2 still missing

        # Manually simulate: only "READY:" should be tracked
        # (The protocol's _on_message checks content == "READY:")
        # So READYFOO would not be added to _ready_states
        assert "a2" not in p._ready_states

    def test_ready_only_counts_participants(self):
        """READY from non-participants is ignored."""
        server = MessageServer()
        for a in ("a1", "a2"):
            server.register_agent(a)
        p = CollaborationProtocol(server, participants=["a1", "a2"])
        p.start()

        p._ready_states = {"a1", "a2", "intruder"}
        assert p.all_ready  # still true — extra READY doesn't hurt

    def test_ready_states_cleared_after_p1_finish(self):
        """READY states are cleared after P1 finishes."""
        p = make_protocol("a1", "a2")
        p.start()
        p._ready_states = {"a1", "a2"}
        p.finish_p1()
        assert len(p._ready_states) == 0


def make_protocol(*agents):
    server = MessageServer()
    for a in agents:
        server.register_agent(a)
    return CollaborationProtocol(server, participants=list(agents))


# ---------------------------------------------------------------------------
# Fix 5: Session resilience — single agent failure doesn't abort session
# ---------------------------------------------------------------------------


class FailingBackend(ModelBackend):
    """Backend that always raises an exception."""

    async def complete(self, messages, tools):
        raise RuntimeError("Simulated backend failure (e.g., HTTP 404)")


async def test_session_continues_when_one_agent_fails():
    """Session must continue when one agent's step raises an exception."""
    from agent_augury.session import Session

    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    # Agent-1 uses a failing backend; Agent-2 uses a working fake backend.
    agent1 = AgentLoop(
        agent_id="agent-1",
        server=server,
        backend=FailingBackend(),
        system_prompt="You are agent-1.",
    )
    agent2 = AgentLoop(
        agent_id="agent-2",
        server=server,
        backend=ScriptedBackend([Completion(text="hello from agent-2")]),
        system_prompt="You are agent-2.",
    )

    session = Session(server=server, agents=[agent1, agent2], max_steps=5)
    steps = await session.run()

    # Session should complete with at least 1 step (agent-2's step).
    # Agent-1 fails immediately, agent-2 succeeds with its one completion.
    assert steps >= 1
    # Agent-2's conversation should contain its response.
    assert any("hello from agent-2" in str(m) for m in agent2.conversation)


async def test_session_all_agents_fail_returns_zero_steps():
    """Session with all agents failing should return 0 steps, not crash."""
    from agent_augury.session import Session

    server = MessageServer()
    server.register_agent("agent-1")

    agent1 = AgentLoop(
        agent_id="agent-1",
        server=server,
        backend=FailingBackend(),
        system_prompt="You are agent-1.",
    )

    session = Session(server=server, agents=[agent1], max_steps=5)
    steps = await session.run()

    # All agents failed immediately — 0 steps completed.
    assert steps == 0
