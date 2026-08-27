"""v0.1b — consensus gate over the internal message server (DESIGN.md §6 v0.1b).

Conventions decided by the orchestrator (2026-08-26):
- Message prefixes: ``PROPOSE:`` / ``APPROVE:`` / ``REJECT:``.
- The gate binds to the first thread matching ``thread_name``; its
  participants define unanimity.
- Approvals only count once at least one PROPOSE exists.
- REJECT from anyone clears collected approvals (consensus must re-form).
"""

from agent_augury.protocol.approval import ConsensusGate
from agent_augury.server import MessageServer


def make_server(*agents):
    server = MessageServer()
    for a in agents:
        server.register_agent(a)
    return server


def make_gate(server, thread_name="plan"):
    gate = ConsensusGate(server, thread_name=thread_name)
    server.subscribe(gate.on_message)
    return gate


async def test_gate_binds_on_matching_thread_and_tracks_proposal():
    server = make_server("a", "b")
    gate = make_gate(server)

    plan = await server.create_thread("plan", participants=["a", "b"])
    hunt = await server.create_thread("hunt", participants=["a", "b"])

    await server.send_message(plan, author="a", content="PROPOSE: a→검색, b→정리", mentions=[])
    await server.send_message(hunt, author="a", content="PROPOSE: 여긴 다른 스레드", mentions=[])

    assert gate.thread_id == plan
    assert gate.participants == ["a", "b"]
    assert gate.has_proposal
    assert not gate.is_open


async def test_gate_opens_only_after_unanimous_approve():
    server = make_server("a", "b", "c")
    gate = make_gate(server)
    plan = await server.create_thread("plan", participants=["a", "b", "c"])

    await server.send_message(plan, author="a", content="PROPOSE: v1", mentions=[])
    await server.send_message(plan, author="a", content="APPROVE: ok", mentions=[])
    assert not gate.is_open
    await server.send_message(plan, author="b", content="APPROVE: ok", mentions=[])
    assert not gate.is_open
    await server.send_message(plan, author="c", content="APPROVE: ok", mentions=[])
    assert gate.is_open
    assert gate.opened_at_seq is not None


async def test_approvals_before_any_proposal_do_not_count():
    server = make_server("a", "b")
    gate = make_gate(server)
    plan = await server.create_thread("plan", participants=["a", "b"])

    await server.send_message(plan, author="a", content="APPROVE: premature", mentions=[])
    await server.send_message(plan, author="b", content="APPROVE: premature", mentions=[])
    assert not gate.is_open

    await server.send_message(plan, author="a", content="PROPOSE: v1", mentions=[])
    await server.send_message(plan, author="b", content="APPROVE: ok", mentions=[])
    assert not gate.is_open  # premature approvals were void
    await server.send_message(plan, author="a", content="APPROVE: ok", mentions=[])
    assert gate.is_open


async def test_reject_clears_collected_approvals():
    server = make_server("a", "b")
    gate = make_gate(server)
    plan = await server.create_thread("plan", participants=["a", "b"])

    await server.send_message(plan, author="a", content="PROPOSE: v1", mentions=[])
    await server.send_message(plan, author="a", content="APPROVE: ok", mentions=[])
    await server.send_message(plan, author="b", content="REJECT: 분할이 이상함", mentions=[])
    assert not gate.is_open

    # consensus must re-form from zero
    await server.send_message(plan, author="b", content="APPROVE: v2 ok", mentions=[])
    assert not gate.is_open
    await server.send_message(plan, author="a", content="APPROVE: v2 ok", mentions=[])
    assert gate.is_open


async def test_non_plan_messages_do_not_open_gate():
    server = make_server("a", "b")
    gate = make_gate(server)
    await server.create_thread("plan", participants=["a", "b"])
    hunt = await server.create_thread("hunt", participants=["a", "b"])

    await server.send_message(hunt, author="a", content="APPROVE: wrong room", mentions=[])
    await server.send_message(hunt, author="b", content="APPROVE: wrong room", mentions=[])
    assert not gate.is_open


async def test_subscribers_receive_messages_in_order():
    server = make_server("a", "b")
    seen = []
    server.subscribe(lambda m: seen.append((m["seq"], m["author"])))
    plan = await server.create_thread("plan", participants=["a", "b"])
    m1 = await server.send_message(plan, author="a", content="one", mentions=["b"])
    m2 = await server.send_message(plan, author="a", content="two", mentions=["b"])
    snap = server.snapshot()
    seq = {m["message_id"]: m["seq"] for m in snap["messages"]}
    assert seen == [(seq[m1], "a"), (seq[m2], "a")]


# ---------------------------------------------------------------------------
# P3+ gates (require_proposal=False): first message binds AND is evaluated
# ---------------------------------------------------------------------------


async def test_gate_without_proposal_approve_counts_on_first_message():
    """For P3+ gates (require_proposal=False), the first message binds the
    gate AND is evaluated for APPROVE/REJECT — so a single agent's
    APPROVE on a single-participant thread opens the gate immediately."""
    server = make_server("a", "b")
    gate = ConsensusGate(server, thread_name="work", require_proposal=False)
    server.subscribe(gate.on_message)

    work = await server.create_thread("work", participants=["a", "b"])
    # First message is APPROVE → binds AND counts as approval
    await server.send_message(work, author="a", content="APPROVE: ok", mentions=[])

    assert gate.thread_id == work
    assert gate.has_proposal  # bound = has_proposal for require_proposal=False
    assert "a" in gate.approvals
    assert not gate.is_open  # not unanimous yet (needs b)


async def test_gate_without_proposal_reject_on_first_message_clears():
    """REJECT on the first message of a P3+ gate clears approvals."""
    server = make_server("a", "b")
    gate = ConsensusGate(server, thread_name="work", require_proposal=False)
    server.subscribe(gate.on_message)

    work = await server.create_thread("work", participants=["a", "b"])
    await server.send_message(work, author="a", content="APPROVE: ok", mentions=[])
    assert "a" in gate.approvals

    # REJECT clears
    await server.send_message(work, author="b", content="REJECT: nope", mentions=[])
    assert len(gate.approvals) == 0
    assert not gate.is_open


async def test_gate_without_proposal_unanimous_approve_opens():
    """P3+ gate opens when all participants approve (no PROPOSE needed)."""
    server = make_server("a", "b", "c")
    gate = ConsensusGate(server, thread_name="work", require_proposal=False)
    server.subscribe(gate.on_message)

    work = await server.create_thread("work", participants=["a", "b", "c"])
    await server.send_message(work, author="a", content="APPROVE: ok", mentions=[])
    assert not gate.is_open
    await server.send_message(work, author="b", content="APPROVE: ok", mentions=[])
    assert not gate.is_open
    await server.send_message(work, author="c", content="APPROVE: ok", mentions=[])
    assert gate.is_open


async def test_execution_only_counted_after_gate_opens():
    """Pass-criteria primitive: work-share messages carry seq > opened_at_seq."""
    server = make_server("a", "b")
    gate = make_gate(server)
    plan = await server.create_thread("plan", participants=["a", "b"])
    await server.send_message(plan, author="a", content="PROPOSE: v1", mentions=[])
    await server.send_message(plan, author="a", content="APPROVE: ok", mentions=[])
    await server.send_message(plan, author="b", content="APPROVE: ok", mentions=[])
    assert gate.is_open

    hunt = await server.create_thread("hunt", participants=["a", "b"])
    work = await server.send_message(hunt, author="a", content="(FYI) 내 몫 수행 중", mentions=[])
    snap = server.snapshot()
    seq = {m["message_id"]: m["seq"] for m in snap["messages"]}
    assert seq[work] > gate.opened_at_seq
