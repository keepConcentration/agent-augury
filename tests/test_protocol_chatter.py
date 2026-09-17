"""Protocol chatter reduction (PROTOCOL_CHATTER_REDUCTION_DESIGN.md).

Covers: D10 late PROPOSE, ready_states checkpoint, duplicate vote no-op,
idle_not_allowed, session.gate event, done-set park + D5 drain-only skip.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P1_EXPLORE, P2_SPLIT
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session


class CountingBackend(ModelBackend):
    def __init__(self, script: list[Completion] | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, tools=None):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


async def _bound_gate(server: MessageServer, participants, **kw):
    protocol = CollaborationProtocol(server, participants=list(participants))
    protocol.bind_gate(P2_SPLIT, "plan", **kw)
    tid = await server.create_thread("plan", participants=list(participants))
    gate = protocol.gate_for(P2_SPLIT)
    assert gate is not None
    gate.bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    return protocol, gate, tid


# ---------------------------------------------------------------------------
# D10 — late PROPOSE must re-check unanimity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_proposal_opens_gate_after_everyone_approved():
    """APPROVE x N before any PROPOSE: the PROPOSE must open the gate.

    Without the re-check the gate stays closed forever, and duplicate-vote
    soft-blocking (C1) removes the accidental re-APPROVE that used to rescue it.
    """
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    _, gate, tid = await _bound_gate(
        server, ["a1", "a2", "a3"], require_proposal=True
    )
    server.subscribe(gate.on_message)

    for a in ("a1", "a2", "a3"):
        await server.send_message(tid, author=a, content="APPROVE: ok")
    # M4a: a vote needs a draft to be about, so these do not stick
    assert gate.approvals == set()
    assert not gate.is_open  # no proposal yet

    await server.send_message(tid, author="a1", content="PROPOSE: the plan")
    assert not gate.is_open           # earlier votes were never counted
    for a in ("a1", "a2", "a3"):
        await server.send_message(tid, author=a, content="APPROVE: ok")
    assert gate.is_open


@pytest.mark.asyncio
async def test_late_proposal_parks_for_human_when_after_agents():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    server.register_human()
    _, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=True, await_human_after_agents=True
    )
    server.subscribe(gate.on_message)
    fired: list[bool] = []
    gate.on_human_pending(lambda: fired.append(True))

    await server.send_message(tid, author="a1", content="PROPOSE: plan")
    for a in ("a1", "a2"):
        await server.send_message(tid, author=a, content="APPROVE: ok")

    assert gate.human_pending is True
    assert not gate.is_open
    assert fired == [True]


@pytest.mark.asyncio
async def test_proposal_without_unanimity_does_not_open():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    _, gate, tid = await _bound_gate(server, ["a1", "a2"], require_proposal=True)
    server.subscribe(gate.on_message)

    await server.send_message(tid, author="a1", content="APPROVE: ok")
    await server.send_message(tid, author="a1", content="PROPOSE: plan")
    assert not gate.is_open


# ---------------------------------------------------------------------------
# ready_states checkpoint (C2 prereq)
# ---------------------------------------------------------------------------


def test_ready_states_survive_snapshot_restore():
    server = MessageServer()
    p = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
    p._ready_states.update({"a1", "a2"})
    snap = p.snapshot()
    assert snap["ready_states"] == ["a1", "a2"]

    p2 = CollaborationProtocol(server, participants=["a1", "a2", "a3"])
    ref = p2.ready_states  # agents hold this reference
    p2.restore(snap)
    assert p2.has_ready("a1") and p2.has_ready("a2")
    assert not p2.has_ready("a3")
    # restore is in-place: the previously handed out reference is still live
    assert ref is p2.ready_states
    assert "a1" in ref


def test_ready_states_restore_allows_all_ready():
    server = MessageServer()
    p = CollaborationProtocol(server, participants=["a1", "a2"])
    p._ready_states.update({"a1", "a2"})
    snap = p.snapshot()

    p2 = CollaborationProtocol(server, participants=["a1", "a2"])
    p2.restore(snap)
    assert p2.all_ready is True


# ---------------------------------------------------------------------------
# C1 — duplicate APPROVE / READY soft-block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_approve_is_not_sent():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    server.subscribe(gate.on_message)

    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.current_phase = P2_SPLIT
    agent.gate_open = False
    agent.gate_thread_id = tid
    agent.gate_approvals = gate.approvals
    agent.ready_states = protocol.ready_states

    first = await agent._execute_tool(
        "send_message", {"thread": tid, "content": "APPROVE: yes"}
    )
    assert "error" not in json.loads(first)
    before = len(server.snapshot()["messages"])

    second = await agent._execute_tool(
        "send_message", {"thread": tid, "content": "APPROVE: yes again"}
    )
    payload = json.loads(second)
    assert payload["error"] == "already_approved"
    assert payload["approvals"] == ["a1"]
    assert len(server.snapshot()["messages"]) == before  # nothing appended


@pytest.mark.asyncio
async def test_reject_reopens_voting_after_duplicate_block():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    server.subscribe(gate.on_message)
    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.current_phase = P2_SPLIT
    agent.gate_open = False
    agent.gate_thread_id = tid
    agent.gate_approvals = gate.approvals
    agent.ready_states = protocol.ready_states

    await agent._execute_tool(
        "send_message", {"thread": tid, "content": "APPROVE: yes"}
    )
    await server.send_message(tid, author="a2", content="REJECT: no")
    assert gate.approvals == set()
    # live reference — the agent sees the cleared set without re-injection
    retry = await agent._execute_tool(
        "send_message", {"thread": tid, "content": "APPROVE: again"}
    )
    assert "error" not in json.loads(retry)


@pytest.mark.asyncio
async def test_duplicate_ready_is_not_sent():
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    tid = await server.create_thread("human", participants=["a1", "a2"])

    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.current_phase = P1_EXPLORE
    agent.gate_open = False
    agent.gate_thread_id = None
    agent.ready_states = protocol.ready_states

    first = await agent._execute_tool(
        "send_message", {"thread": tid, "content": "READY: done"}
    )
    assert "error" not in json.loads(first)
    before = len(server.snapshot()["messages"])

    second = await agent._execute_tool(
        "send_message", {"thread": tid, "content": "READY: done again"}
    )
    assert json.loads(second)["error"] == "already_ready"
    assert len(server.snapshot()["messages"]) == before


@pytest.mark.asyncio
async def test_no_gate_phase_resets_stale_approvals():
    """A phase without a gate must not inherit the previous phase's approvals."""
    from agent_augury.core.session import _inject_protocol_gate_state

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, gate, _tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    gate.approvals.add("a1")
    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)

    _inject_protocol_gate_state(agent, protocol)
    assert "a1" in agent.gate_approvals  # P2 has a gate

    protocol.phase_manager._phase = P1_EXPLORE
    _inject_protocol_gate_state(agent, protocol)
    assert agent.gate_approvals == frozenset()


# ---------------------------------------------------------------------------
# C3 — idle_not_allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["sleep 5", "true", ":", "/bin/sleep 1"])
async def test_sleep_blocked_during_gate_wait(command):
    server = MessageServer()
    server.register_agent("a1")
    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.current_phase = P2_SPLIT
    agent.gate_open = False

    payload = json.loads(
        await agent._execute_tool("run_command", {"command": command})
    )
    assert payload["error"] == "idle_not_allowed"


@pytest.mark.asyncio
async def test_real_command_and_open_gate_are_not_blocked():
    server = MessageServer()
    server.register_agent("a1")
    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.current_phase = P2_SPLIT

    # real work during a gate wait → not an idle command
    agent.gate_open = False
    out = json.loads(await agent._execute_tool("run_command", {"command": "git --version"}))
    assert out.get("error") != "idle_not_allowed"

    # gate open → sleep is the agent's own business
    agent.gate_open = True
    out = json.loads(await agent._execute_tool("run_command", {"command": "true"}))
    assert out.get("error") != "idle_not_allowed"


# ---------------------------------------------------------------------------
# C2 + D5 — done-set park and drain-only skip
# ---------------------------------------------------------------------------


async def _run_session(server, agents, protocol, *, inject, timeout=5.0):
    session = Session(server=server, agents=agents, max_steps=0)
    session.protocol = protocol
    protocol.start = lambda: None  # type: ignore[method-assign]

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]
    injector = asyncio.create_task(inject(session))
    try:
        await asyncio.wait_for(session.run(initial_prompt="go"), timeout=timeout)
        await injector
    finally:
        await session.close()
    return session


@pytest.mark.asyncio
async def test_done_agent_stops_calling_the_model_across_iterations():
    """A peer vote wakes a done agent, but must not cost a completion.

    This is the invariant a single-step test cannot catch: D5 returns
    ``skipped``, the session continues, and the TOP-of-loop park has to absorb
    the next iteration instead of letting it reach ``backend.complete``.
    """
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    server.subscribe(gate.on_message)

    b1 = CountingBackend([Completion(text="APPROVE: from a1")])
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    b2 = CountingBackend([Completion(text=None)])
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    # a1 is already counted at the gate before the session starts.
    gate.approvals.add("a1")

    async def inject(session):
        for _ in range(60):
            if b1.calls >= 1:
                break
            await asyncio.sleep(0.05)
        calls_after_park = b1.calls
        # peer traffic: wakes a1's park, drains, must NOT add a completion
        for i in range(3):
            await server.send_message(tid, author="a2", content=f"working {i}")
        await asyncio.sleep(0.4)
        assert b1.calls == calls_after_park, (
            f"done agent called the model on peer traffic: "
            f"{calls_after_park} -> {b1.calls}"
        )
        session.request_interrupt()

    await _run_session(server, [a1, a2], protocol, inject=inject)


@pytest.mark.asyncio
async def test_done_agent_keeps_peer_messages_in_conversation():
    """D5 skips the model but must not throw the radio away."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    _protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    server.subscribe(gate.on_message)
    gate.approvals.add("a1")

    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.protocol_done = True
    await server.send_message(tid, author="a2", content="APPROVE: peer vote")

    result = await agent.step()
    assert result.skipped is True
    assert agent.backend.calls == 0
    radio = [m for m in agent.conversation if "APPROVE: peer vote" in str(m.get("content"))]
    assert radio, "peer vote must survive in the conversation"
    assert server.inbox_size("a1") == 0


@pytest.mark.asyncio
async def test_done_agent_answers_when_mentioned():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    _protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    gate.approvals.add("a1")

    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.protocol_done = True
    await server.send_message(
        tid, author="a2", content="can you check this?", mentions=["a1"]
    )

    result = await agent.step()
    assert result.skipped is False
    assert agent.backend.calls == 1


@pytest.mark.asyncio
async def test_done_agent_ignores_human_broadcast_but_answers_urgent():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    server.register_human()
    _protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    gate.approvals.add("a1")

    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    agent.protocol_done = True

    await server.human_send(tid, author="human", content="how's it going")
    assert (await agent.step()).skipped is True
    assert agent.backend.calls == 0

    await server.human_send(tid, author="human", content="URGENT: stop now")
    assert (await agent.step()).skipped is False
    assert agent.backend.calls == 1


@pytest.mark.asyncio
async def test_done_agent_does_not_get_the_gate_thread_nudge():
    """The nudge tells an agent to APPROVE — pointless once it already did."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol, gate, _tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=False
    )
    server.subscribe(gate.on_message)
    gate.approvals.add("a1")

    b1 = CountingBackend([Completion(text=None)])
    a1 = AgentLoop(agent_id="a1", backend=b1, server=server)
    b2 = CountingBackend([Completion(text=None)])
    a2 = AgentLoop(agent_id="a2", backend=b2, server=server)

    async def inject(session):
        await asyncio.sleep(0.4)
        assert ("a1", P2_SPLIT) not in session._gate_thread_nudged
        session.request_interrupt()

    await _run_session(server, [a1, a2], protocol, inject=inject)


@pytest.mark.asyncio
async def test_done_park_respects_max_steps():
    """Without steps_done the park would outlive the budget."""
    server = MessageServer()
    server.register_agent("a1")
    protocol, gate, _ = await _bound_gate(server, ["a1"], require_proposal=False)
    gate.approvals.add("a1")

    agent = AgentLoop(
        agent_id="a1", backend=CountingBackend([Completion(text=None)]), server=server
    )
    session = Session(server=server, agents=[agent], max_steps=1)
    session.protocol = protocol
    protocol.start = lambda: None  # type: ignore[method-assign]
    # budget already spent → park must give up immediately, not spin
    agent.protocol_done = True
    woke = await asyncio.wait_for(
        session._wait_for_gate_wakeup(agent, steps_done=lambda: 1), timeout=2.0
    )
    assert woke is False
    await session.close()


@pytest.mark.asyncio
async def test_protocol_done_defaults_false_without_protocol():
    server = MessageServer()
    server.register_agent("a1")
    agent = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    assert agent.protocol_done is False
    assert agent.gate_approvals == frozenset()
    assert agent.ready_states == frozenset()


# ---------------------------------------------------------------------------
# C5 — protocol.mode light / off
# ---------------------------------------------------------------------------


def _normalize(yaml_text: str):
    import yaml as _yaml

    from agent_augury.config import ConfigError, normalize_protocol_mode

    data = _yaml.safe_load(yaml_text) or {}
    return normalize_protocol_mode(data, config_error=ConfigError), data


@pytest.mark.parametrize(
    "text",
    [
        "protocol: false",
        "protocol:",
        "protocol:\n  mode: off",      # YAML 1.1 parses bare off as False
        'protocol:\n  mode: "off"',
        "protocol:\n  mode: no",
    ],
)
def test_protocol_off_variants_normalize_to_none(text):
    spec, data = _normalize(text)
    assert spec is None
    assert "protocol" not in data


@pytest.mark.parametrize("text", ["protocol: true", "protocol:\n  mode: on", "protocol:\n  mode: turbo"])
def test_bad_protocol_mode_raises(text):
    from agent_augury.config import ConfigError

    with pytest.raises(ConfigError):
        _normalize(text)


def test_full_is_the_default_and_keeps_gates():
    spec, _ = _normalize("protocol:\n  gates:\n    P2_SPLIT: plan\n    P5_SUBMIT: submission")
    assert spec["mode"] == "full"
    assert spec["gates"]["P2_SPLIT"] == "plan"


def test_light_keeps_only_the_final_gate():
    spec, _ = _normalize(
        "protocol:\n  mode: light\n  gates:\n    P2_SPLIT: plan\n    P5_SUBMIT: wrap"
    )
    assert spec["gates"] == {"P5_SUBMIT": "wrap"}


def test_light_defaults_the_final_gate_name():
    spec, _ = _normalize("protocol:\n  mode: light")
    assert spec["gates"] == {"P5_SUBMIT": "submission"}


@pytest.mark.asyncio
async def test_light_mode_goes_p1_to_p5_directly():
    from agent_augury.core.protocol.phases import COMPLETED, P5_SUBMIT

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, participants=["a1", "a2"], mode="light"
    )
    protocol.bind_gate(P5_SUBMIT, "submission", require_proposal=False)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate = protocol.gate_for(P5_SUBMIT)
    assert gate is not None
    gate.bind_to_thread(tid)
    protocol.start()

    for a in ("a1", "a2"):
        await server.send_message(tid, author=a, content="READY: explored")
    assert protocol.phase == P5_SUBMIT  # P2-P4 never visited

    protocol.advance(COMPLETED)
    assert protocol.is_complete


def test_light_mode_rejects_p2():
    from agent_augury.core.protocol.phases import P2_SPLIT as P2

    server = MessageServer()
    protocol = CollaborationProtocol(server, participants=["a1"], mode="light")
    with pytest.raises(ValueError):
        protocol.advance(P2)


def test_full_mode_transitions_are_unchanged():
    from agent_augury.core.protocol.phases import P3_EXECUTE

    server = MessageServer()
    protocol = CollaborationProtocol(server, participants=["a1"])
    assert protocol.mode == "full"
    assert protocol.next_phase_after_p1 == P2_SPLIT
    assert protocol.next_phase_after_gate(P2_SPLIT) == P3_EXECUTE


# ---------------------------------------------------------------------------
# Regression: proposal-less P2 must not park everyone (observed deadlock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_approved_without_proposal_nobody_is_done():
    """P2 gate at 4/4 but no PROPOSE: yet — parking everyone deadlocks.

    Observed live: agents skipped PROPOSE: and went straight to APPROVE:.
    The gate correctly stays shut (require_proposal), but if every agent counts
    as "done" they all park, so the PROPOSE: that would open it never arrives.
    """
    server = MessageServer()
    agents = ["a1", "a2", "a3", "a4"]
    for a in agents:
        server.register_agent(a)
    protocol, gate, tid = await _bound_gate(server, agents, require_proposal=True)
    server.subscribe(gate.on_message)

    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: done")

    assert gate.approvals == set()      # M4a: proposal-less votes uncounted
    assert not gate.has_proposal
    assert not gate.is_open
    # nobody may park — someone still has to PROPOSE
    assert [protocol.is_agent_done(a) for a in agents] == [False] * 4

    # and once a proposal lands the gate opens on the votes already cast
    await server.send_message(tid, author="a1", content="PROPOSE: the plan")
    assert not gate.is_open
    for a in agents:
        await server.send_message(tid, author=a, content="APPROVE: done")
    assert gate.is_open
    assert [protocol.is_agent_done(a) for a in agents] == [False] * 4  # gate open


@pytest.mark.asyncio
async def test_done_still_true_once_proposal_exists():
    """The guard must not disable done-set for a normal PROPOSE→APPROVE gate."""
    server = MessageServer()
    protocol, gate, tid = await _bound_gate(
        server, ["a1", "a2"], require_proposal=True
    )
    for a in ("a1", "a2"):
        server.register_agent(a)
    server.subscribe(gate.on_message)

    await server.send_message(tid, author="a1", content="PROPOSE: plan")
    await server.send_message(tid, author="a1", content="APPROVE: yes")

    assert gate.has_proposal
    assert not gate.is_open          # a2 still pending
    assert protocol.is_agent_done("a1") is True
    assert protocol.is_agent_done("a2") is False
