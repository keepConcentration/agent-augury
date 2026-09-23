"""Dynamic roster — R0/R1 (DYNAMIC_ROSTER_DESIGN v1.4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.config import load_config
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.protocol.approval import ConsensusGate
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P5_SUBMIT,
)
from agent_augury.core.server import MessageServer
from agent_augury.core.session import HUMAN_CHAT_THREAD_NAME, Session
from agent_augury.gateway.turn_done import derive_turn_done_reason

POOL = ["a1", "a2", "a3", "a4"]


class CountingBackend(ModelBackend):
    def __init__(self, script: list[Completion] | None = None) -> None:
        self.script = list(script or [])
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


async def _bind_all_gates(protocol: CollaborationProtocol, server: MessageServer) -> dict[str, str]:
    tids: dict[str, str] = {}
    for phase, name in (
        (P2_SPLIT, "plan"),
        (P3_EXECUTE, "execution"),
        (P5_SUBMIT, "submission"),
    ):
        if protocol.gate_for(phase) is None:
            protocol.bind_gate(
                phase,
                name,
                require_proposal=(phase != P3_EXECUTE),
                entry_prefix="PROPOSE:" if phase == P2_SPLIT else (
                    "FINAL:" if phase == P5_SUBMIT else "PROPOSE:"
                ),
            )
        tid = await server.create_thread(name, participants=list(protocol.participants))
        protocol.gate_for(phase).bind_to_thread(tid)
        tids[phase] = tid
    return tids


# -- gate / server primitives -------------------------------------------------


def test_shrink_opens_gate_when_remaining_all_approved():
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    gate = ConsensusGate(server, "plan", require_proposal=False)
    gate.participants = ["a1", "a2", "a3"]
    gate.approvals = {"a1", "a2"}
    gate._proposal_received = True
    gate.set_participants(["a1", "a2"], seq=7)
    assert gate.is_open
    assert gate.opened_at_seq == 7
    assert gate.approvals == {"a1", "a2"}


@pytest.mark.asyncio
async def test_drop_inbox_keeps_human_messages():
    server = MessageServer()
    server.register_agent("a1")
    server.register_human()
    plan = await server.create_thread("plan", participants=["a1"])
    human = await server.create_thread(
        HUMAN_CHAT_THREAD_NAME, participants=["a1"]
    )
    await server.send_message(plan, author="a1", content="phase noise")
    # Self-send does not land in own inbox; poke via a second agent on plan.
    server.register_agent("a2")
    server.set_thread_participants(plan, ["a1", "a2"])
    await server.send_message(plan, author="a2", content="for a1 on plan")
    await server.human_send(human, author="human", content="keep me", mentions=["a1"])
    assert server.inbox_size("a1") == 2
    server.drop_inbox_from("a1", {plan})
    assert server.inbox_size("a1") == 1
    drained = await server.drain_inbox("a1")
    assert drained[0]["content"] == "keep me"
    assert drained[0]["thread_id"] == human


def test_set_participants_without_running_loop():
    """No event loop: memory update only (no create_task crash)."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    # create_thread is async — drive a tiny loop just for setup, then leave it.
    async def _setup():
        return await server.create_thread("plan", participants=["a1", "a2"])

    tid = asyncio.run(_setup())
    server.set_thread_participants(tid, ["a1"])
    assert server.get_thread(tid)["participants"] == ["a1"]


def test_set_roster_shrinks_ready_quorum():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=-1
    )
    protocol._ready_states.update(POOL)
    protocol.set_roster(["a1", "a2"])
    assert protocol.participants == ["a1", "a2"]
    assert protocol._ready_states == {"a1", "a2"}
    assert protocol.all_ready


# -- R0 / R1 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_can_name_bench_agent():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2, roster_max=8
    )
    assert protocol.participants == ["a1", "a2"]
    await _bind_all_gates(protocol, server)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    gate = protocol.gate_for(P2_SPLIT)
    tid = gate.thread_id
    await server.send_message(
        tid,
        author="a1",
        content=(
            "PROPOSE:\nASSIGN a1: x\nASSIGN a4: bench work\n"
            "SUBMITTER: a1\n"
        ),
    )
    await server.send_message(tid, author="a2", content="APPROVE:")
    assert gate.is_open
    assert "a4" in protocol.participants
    assert protocol.assignment_for("a4") == "bench work"


@pytest.mark.asyncio
async def test_split_none_roster_is_submitter_only():
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=["a1", "a2", "a3"], roster_start=-1
    )
    await _bind_all_gates(protocol, server)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    tid = protocol.gate_for(P2_SPLIT).thread_id
    await server.send_message(
        tid,
        author="a2",
        content="PROPOSE:\nSPLIT: none\nSUBMITTER: a2\n",
    )
    for a in ("a1", "a3"):
        await server.send_message(tid, author=a, content="APPROVE:")
    assert protocol.participants == ["a2"]
    assert protocol.next_phase_after_gate(P2_SPLIT) == P5_SUBMIT


@pytest.mark.asyncio
async def test_no_submitter_is_noop():
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=["a1", "a2"], roster_start=-1
    )
    await _bind_all_gates(protocol, server)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    tid = protocol.gate_for(P2_SPLIT).thread_id
    before = list(protocol.participants)
    await server.send_message(
        tid, author="a1", content="PROPOSE:\nSPLIT: none\n"
    )
    await server.send_message(tid, author="a2", content="APPROVE:")
    assert protocol.participants == before


@pytest.mark.asyncio
async def test_max_truncates_keeping_submitter():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=-1, roster_max=2
    )
    await _bind_all_gates(protocol, server)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    tid = protocol.gate_for(P2_SPLIT).thread_id
    await server.send_message(
        tid,
        author="a3",
        content=(
            "PROPOSE:\nASSIGN a1: A\nASSIGN a2: B\nASSIGN a3: C\n"
            "ASSIGN a4: D\nSUBMITTER: a3\n"
        ),
    )
    for a in ("a1", "a2", "a4"):
        await server.send_message(tid, author=a, content="APPROVE:")
    assert protocol.participants[0] == "a3"
    assert len(protocol.participants) == 2


def test_p1_quorum_is_start_not_pool():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2
    )
    protocol._ready_states.update(["a1", "a2"])
    assert protocol.all_ready
    assert not ({"a1", "a2", "a3"} <= protocol._ready_states and
                set(protocol.participants) <= {"a1", "a2", "a3"} and
                len(protocol.participants) > 2)


def test_begin_round_resets_roster_to_start():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2
    )
    protocol.participants = ["a4"]
    protocol.phase_manager._phase = COMPLETED
    protocol.begin_round()
    assert protocol.participants == ["a1", "a2"]
    assert protocol.phase == P1_EXPLORE


# -- demotion / spawn / D12 --------------------------------------------------


@pytest.mark.asyncio
async def test_demoted_agent_parks_without_model_call():
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    backends = {
        aid: CountingBackend(
            [Completion(text="hi"), Completion(text=None), Completion(text=None)]
        )
        for aid in ("a1", "a2")
    }
    # a3 would step forever if demotion did not park it — with a script that
    # runs out, "calls stopped" proves nothing (it just finished).
    backends["a3"] = CountingBackend([Completion(text="working")] * 5000)
    agents = [
        AgentLoop(agent_id=aid, backend=backends[aid], server=server)
        for aid in ("a1", "a2", "a3")
    ]
    session = Session(server=server, agents=agents, max_steps=5000)
    protocol = CollaborationProtocol(
        server, pool=["a1", "a2", "a3"], roster_start=-1
    )
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2", "a3"])
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    session.protocol = protocol

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def _run() -> None:
        task = asyncio.create_task(session.run(initial_prompt="go"))
        await asyncio.sleep(0.05)
        await server.send_message(tid, author="a1", content="noise for a3")
        assert backends["a3"].calls > 0  # it really was looping

        protocol.set_roster(["a1", "a2"])
        # The two preconditions of the no-model park (session.py C2):
        assert protocol.is_agent_done("a3")
        assert server.inbox_size("a3") == 0  # phase-thread unread was dropped

        await asyncio.sleep(0.05)  # let the in-flight step finish
        settled = backends["a3"].calls
        await asyncio.sleep(0.2)  # a live loop would climb over this window
        assert backends["a3"].calls == settled
        await asyncio.wait_for(task, timeout=3.0)

    await _run()
    await session.close()


@pytest.mark.asyncio
async def test_followup_turn_does_not_double_spawn():
    """run() must clear the roster callback on exit.

    begin_round() (a follow-up turn) calls set_roster() BEFORE run()
    re-registers, so a stale callback spawns duplicate agent loops that no
    run tracks, awaits or cancels.
    """
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    backends = {
        aid: CountingBackend([Completion(text="hi"), Completion(text=None)] * 20)
        for aid in ("a1", "a2")
    }
    agents = [
        AgentLoop(agent_id=aid, backend=backends[aid], server=server)
        for aid in ("a1", "a2")
    ]
    session = Session(server=server, agents=agents, max_steps=100)
    session.protocol = CollaborationProtocol(
        server, pool=["a1", "a2"], roster_start=-1
    )

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    await session.run(initial_prompt="go")
    assert session.protocol._on_roster_change is None

    before = {aid: backends[aid].calls for aid in backends}
    session.protocol.set_roster(session.protocol.initial_roster())
    await asyncio.sleep(0.1)
    assert {aid: backends[aid].calls for aid in backends} == before
    await session.close()


@pytest.mark.asyncio
async def test_off_thread_send_gets_clear_message():
    """A demoted agent sending mid-step gets an explanation, not a ValueError."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    tid = await server.create_thread("plan", participants=["a1"])
    agent = AgentLoop(agent_id="a2", backend=CountingBackend(), server=server)

    denied = agent._not_on_thread_denied({"thread": tid, "content": "x"})
    assert denied is not None
    assert "not_a_participant" in denied

    # On the thread, or an unknown id: not this guard's business.
    on_thread = AgentLoop(agent_id="a1", backend=CountingBackend(), server=server)
    assert on_thread._not_on_thread_denied({"thread": tid, "content": "x"}) is None
    assert agent._not_on_thread_denied({"thread": "nope", "content": "x"}) is None


@pytest.mark.asyncio
async def test_assigned_bench_agent_is_spawned():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    backends = {aid: CountingBackend([Completion(text=None)]) for aid in POOL}
    agents = [
        AgentLoop(agent_id=aid, backend=backends[aid], server=server)
        for aid in POOL
    ]
    session = Session(server=server, agents=agents, max_steps=5)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2, roster_max=8
    )
    session.protocol = protocol
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    spawned_ids: list[str] = []

    async def _setup() -> None:
        tid = await server.create_thread(
            "plan", participants=list(protocol.participants), bootstrap=True
        )
        protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
        protocol.set_roster(protocol.initial_roster())
        protocol.phase_manager._phase = P2_SPLIT
        protocol._setup_gate_for_phase(P2_SPLIT)
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def _run_and_recruit() -> int:
        task = asyncio.create_task(session.run(initial_prompt="go"))
        await asyncio.sleep(0.05)
        before = backends["a4"].calls
        protocol.set_roster(["a1", "a4"])
        spawned_ids.extend(protocol.participants)
        result = await asyncio.wait_for(task, timeout=3.0)
        assert backends["a4"].calls > before
        return result

    await _run_and_recruit()
    await session.close()
    assert "a4" in spawned_ids


@pytest.mark.asyncio
async def test_deadlock_still_detected_with_bench_and_demoted():
    server = MessageServer()
    for a in ("a1", "a2", "a3"):
        server.register_agent(a)
    agents = [
        AgentLoop(
            agent_id=aid,
            backend=CountingBackend([Completion(text=None)]),
            server=server,
        )
        for aid in ("a1", "a2")
    ]
    # a3 never spawned (bench).
    session = Session(
        server=server,
        agents=agents + [
            AgentLoop(
                agent_id="a3",
                backend=CountingBackend(),
                server=server,
            )
        ],
        max_steps=0,
    )
    protocol = CollaborationProtocol(
        server, pool=["a1", "a2", "a3"], roster_start=2
    )
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=False)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
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
async def test_human_can_poke_demoted_agent():
    server = MessageServer()
    server.register_human()
    for a in ("a1", "a2"):
        server.register_agent(a)
    plan = await server.create_thread("plan", participants=["a1", "a2"])
    human = await server.create_thread(
        HUMAN_CHAT_THREAD_NAME, participants=["a1", "a2"]
    )
    await server.send_message(plan, author="a1", content="phase")
    await server.human_send(human, author="human", content="hey a2", mentions=["a2"])
    server.set_thread_participants(plan, ["a1"])
    # Phase unread dropped; human poke remains.
    assert server.inbox_size("a2") == 1
    msgs = await server.drain_inbox("a2")
    assert msgs[0]["content"] == "hey a2"


@pytest.mark.asyncio
async def test_roster_survives_resume(tmp_path: Path):
    server = MessageServer(db_path=str(tmp_path / "msg.db"))
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2
    )
    await _bind_all_gates(protocol, server)
    protocol.set_roster(["a1", "a4"])
    snap = protocol.snapshot()
    assert snap["participants"] == ["a1", "a4"]
    # A resume follows a shutdown. Without this the test raced the background
    # participant write and lost on slow runners (CI macos-latest/py3.11).
    await server.close()

    server2 = MessageServer(db_path=str(tmp_path / "msg.db"))
    await server2.load()
    for a in POOL:
        server2.register_agent(a)
    protocol2 = CollaborationProtocol(
        server2, pool=POOL, roster_start=2
    )
    for phase, name, req, prefix in (
        (P2_SPLIT, "plan", True, "PROPOSE:"),
        (P3_EXECUTE, "execution", False, ""),
        (P5_SUBMIT, "submission", True, "FINAL:"),
    ):
        protocol2.bind_gate(
            phase, name, require_proposal=req, entry_prefix=prefix or "PROPOSE:"
        )
    protocol2.restore(snap)
    assert protocol2.participants == ["a1", "a4"]
    gate = protocol2.gate_for(P2_SPLIT)
    assert gate is not None
    assert list(gate.participants) == ["a1", "a4"]


@pytest.mark.asyncio
async def test_roster_change_survives_close(tmp_path: Path):
    """``close()`` must flush the background participant write.

    ``set_thread_participants`` persists via an un-awaited task. Closing
    without draining let the task wake to a shut connection and die unobserved,
    so a roster change made just before shutdown silently reverted to the
    bootstrap roster on the next resume.
    """
    db = str(tmp_path / "msg.db")
    server = MessageServer(db_path=db)
    for a in POOL:
        server.register_agent(a)
    tid = await server.create_thread("plan", participants=["a1", "a2"])
    server.set_thread_participants(tid, ["a1", "a4"])
    await server.close()

    resumed = MessageServer(db_path=db)
    await resumed.load()
    assert resumed.get_thread(tid)["participants"] == ["a1", "a4"]


@pytest.mark.asyncio
async def test_large_pool_split_none_model_calls_bounded():
    """Regression: 100-agent pool + SPLIT: none → only the roster ever runs.

    Bound is per-agent steps x roster size, not "start + 1": a roster agent
    steps again for each message that lands before it parks.
    """
    n = 100
    pool = [f"a{i}" for i in range(n)]
    server = MessageServer()
    for a in pool:
        server.register_agent(a)
    backends = {
        aid: CountingBackend([Completion(text=None)]) for aid in pool
    }
    agents = [
        AgentLoop(agent_id=aid, backend=backends[aid], server=server)
        for aid in pool
    ]
    session = Session(server=server, agents=agents, max_steps=10)
    protocol = CollaborationProtocol(
        server, pool=pool, roster_start=2, roster_max=8
    )
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    tid = await server.create_thread("plan", participants=list(protocol.participants))
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    protocol._setup_gate_for_phase(P2_SPLIT)
    session.protocol = protocol

    async def _setup() -> None:
        session._setup_done = True
        session._output_task = asyncio.create_task(session._output_consumer())

    session._setup = _setup  # type: ignore[method-assign]

    async def _run() -> None:
        task = asyncio.create_task(session.run(initial_prompt="go"))
        await asyncio.sleep(0.05)
        # Open P2 with SPLIT: none → roster shrinks to submitter only.
        await server.send_message(
            tid,
            author="a0",
            content="PROPOSE:\nSPLIT: none\nSUBMITTER: a0\n",
        )
        await server.send_message(tid, author="a1", content="APPROVE:")
        await asyncio.wait_for(task, timeout=5.0)

    await _run()
    await session.close()
    total_calls = sum(b.calls for b in backends.values())
    # The point of R0: agents a2..a99 never get a task.
    assert all(backends[f"a{i}"].calls == 0 for i in range(2, n))
    assert total_calls > 0
    # roster is start=2, each steps at most twice here; the 98 benched add 0.
    assert total_calls <= 4


def test_config_roster_defaults(tmp_path: Path):
    data = {
        "max_steps": 5,
        "protocol": {
            "participants": ["a1", "a2", "a3"],
            "gates": {"P5_SUBMIT": "submission"},
            "mode": "light",
        },
        "agents": [
            {"id": "a1", "backend": {"type": "fake", "script": ["x"]}},
            {"id": "a2", "backend": {"type": "fake", "script": ["x"]}},
            {"id": "a3", "backend": {"type": "fake", "script": ["x"]}},
        ],
    }
    path = tmp_path / "r.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(path, allow_fake=True)
    assert cfg["protocol"]["roster"]["start"] == 2
    assert cfg["protocol"]["roster"]["max"] == 3


def test_start_k_limits_initial_roster():
    server = MessageServer()
    for a in POOL:
        server.register_agent(a)
    protocol = CollaborationProtocol(
        server, pool=POOL, roster_start=2
    )
    assert protocol.initial_roster() == ["a1", "a2"]
    assert protocol.participants == ["a1", "a2"]
