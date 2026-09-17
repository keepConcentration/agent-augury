"""P2 split becomes something later phases can remind agents of.

Live gap (session d24695b5): P2 agreed "agent-3 argues the O position", nothing
checked in P3, and agent-3 never argued it. The team ended 4:0 instead of the
debate the user asked for, and noted the missing side itself.

Advisory by design: assignments never gate anything, so a bad or missing ASSIGN
line cannot stall a session.
"""

from __future__ import annotations

import pytest

from agent_augury.core.agent.system_prompt import render_system_prompt
from agent_augury.core.protocol.assignments import parse_assignments, parse_submitter
from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import P2_SPLIT, P3_EXECUTE, P5_SUBMIT
from agent_augury.core.server import MessageServer

AGENTS = ["agent-1", "agent-2", "agent-3", "agent-4"]

PROPOSAL = """PROPOSE: 분할안
- ASSIGN agent-1: 공리주의 관점
**ASSIGN agent-2: 의무론 관점**
ASSIGN agent-3 = O 입장 옹호
  assign AGENT-4: 기술적 현실
ASSIGN ghost: 참가자가 아님
SUBMITTER: agent-2
"""


def test_parses_decorated_and_drops_unknown_ids():
    got = parse_assignments(PROPOSAL, AGENTS)
    assert got["agent-1"] == "공리주의 관점"
    assert got["agent-2"] == "의무론 관점"       # ** stripped
    assert got["agent-3"] == "O 입장 옹호"       # '=' separator
    assert "ghost" not in got                    # not a participant
    assert "AGENT-4" not in got                  # ids are case-sensitive
    assert parse_submitter(PROPOSAL, AGENTS) == "agent-2"


def test_no_assignment_lines_is_not_an_error():
    assert parse_assignments("PROPOSE: just prose, no lines", AGENTS) == {}
    assert parse_submitter("PROPOSE: nobody named", AGENTS) is None


def test_submitter_must_be_a_participant():
    assert parse_submitter("SUBMITTER: outsider", AGENTS) is None


@pytest.mark.asyncio
async def test_protocol_captures_the_split_from_p2():
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=AGENTS)
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    tid = await server.create_thread("plan", participants=AGENTS)
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT

    await server.send_message(tid, author="agent-1", content=PROPOSAL)

    assert protocol.assignment_for("agent-3") == "O 입장 옹호"
    assert protocol.submitter_id == "agent-2"
    assert protocol.assignment_for("nobody") is None


@pytest.mark.asyncio
async def test_split_survives_checkpoint_and_a_redo():
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=AGENTS)
    protocol.bind_gate(P2_SPLIT, "plan", require_proposal=True)
    tid = await server.create_thread("plan", participants=AGENTS)
    protocol.gate_for(P2_SPLIT).bind_to_thread(tid)
    protocol.phase_manager._phase = P2_SPLIT
    await server.send_message(tid, author="agent-1", content=PROPOSAL)

    # a redone plan replaces the old split
    await server.send_message(
        tid, author="agent-1",
        content="PROPOSE: v2\nASSIGN agent-3: X 입장 옹호\nSUBMITTER: agent-4\n",
    )
    assert protocol.assignment_for("agent-3") == "X 입장 옹호"
    assert protocol.submitter_id == "agent-4"

    snap = protocol.snapshot()
    fresh = CollaborationProtocol(server, participants=AGENTS)
    fresh.restore(snap)
    assert fresh.assignment_for("agent-3") == "X 입장 옹호"
    assert fresh.submitter_id == "agent-4"


def test_agent_sees_its_own_share_in_p3():
    prompt = render_system_prompt(
        "agent-3", phase=P3_EXECUTE, assignment="O 입장 옹호",
    )
    assert "O 입장 옹호" in prompt


def test_p5_names_the_elected_submitter():
    mine = render_system_prompt(
        "agent-2", phase=P5_SUBMIT, submitter_id="agent-2",
    )
    assert "chose YOU to submit" in mine

    theirs = render_system_prompt(
        "agent-3", phase=P5_SUBMIT, submitter_id="agent-2",
    )
    assert "chose agent-2 to submit" in theirs
    assert "do not write your own" in theirs

    nobody = render_system_prompt("agent-3", phase=P5_SUBMIT)
    assert "no agent is designated" in nobody


# ---------------------------------------------------------------------------
# Live repro: the first real SUBMITTER line in the wild was not parsed
# ---------------------------------------------------------------------------

REAL_PROPOSAL = """PROPOSE: 수학 계산 문제의 분담

ASSIGN agent-1: 계산 결과 도출 (풀이 과정 포함)
ASSIGN agent-2: 계산 결과 도출 (풀이 과정 포함)
ASSIGN agent-3: 계산 결과 도출 (풀이 과정 포함)
ASSIGN agent-4: 계산 결과 도출 (풀이 과정 포함) + 전체 결과 검증 및 제출

모든 에이전트가 같은 결과(-8)를 도출하면 검증 완료입니다.
SUBMITTER: agent-4 (검증 후 최종 제출)
"""


def test_real_proposal_from_a_live_session():
    """Anchoring SUBMITTER to end-of-line dropped the trailing parenthetical,
    so the election silently did nothing and a different agent drafted."""
    got = parse_assignments(REAL_PROPOSAL, AGENTS)
    assert len(got) == 4
    assert parse_submitter(REAL_PROPOSAL, AGENTS) == "agent-4"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("SUBMITTER: agent-4 (검증 후 최종 제출)", "agent-4"),
        ("**SUBMITTER: agent-3**", "agent-3"),
        ("SUBMITTER = agent-1  # 제출 담당", "agent-1"),
        ("SUBMITTER: 나는 agent-2 를 제안합니다", "agent-2"),
        ("SUBMITTER: agent-2 가 아니라 agent-3", "agent-2"),   # earliest wins
        ("SUBMITTER: outsider", None),
        ("SUBMITTER: 미정", None),
    ],
)
def test_submitter_line_shapes(line, expected):
    assert parse_submitter(line, AGENTS) == expected
