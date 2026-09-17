"""Signal matching tolerates markdown / bracket decoration (signals.py).

Live repro (session d24695b5, thread-4): three agents posted a P5 draft but the
gate saw only one, because two wrapped it.

    seq 29  agent-2  '[FINAL: ...'     -> not recognised
    seq 30  agent-1  'FINAL: ...'      -> recognised
    seq 31  agent-4  '**FINAL: [X] ...' -> not recognised
"""

from __future__ import annotations

import pytest

from agent_augury.core.protocol.approval import ConsensusGate
from agent_augury.core.protocol.signals import (
    has_signal,
    is_ready_message,
    misplaced_signal,
)
from agent_augury.core.server import MessageServer


@pytest.mark.parametrize(
    "content",
    [
        "FINAL: 98",
        "**FINAL: [X] 98**",          # markdown bold (observed)
        "[FINAL: 98]",                # bracketed (observed)
        "  FINAL: 98",
        "# FINAL: 98",
        "- FINAL: 98",
        "`FINAL: 98`",
        "final: 98",                  # case
    ],
)
def test_decorated_signals_are_recognised(content):
    assert has_signal(content, "FINAL:")


@pytest.mark.parametrize(
    "content",
    [
        "FINALLY we agree",           # no colon
        "FINAL 98",                   # no colon
        "> FINAL: someone else wrote this",   # blockquote = citing, not claiming
        '"FINAL: quoted"',
        "I will post FINAL: later",
        "",
    ],
)
def test_lookalikes_and_quotes_are_not_signals(content):
    assert not has_signal(content, "FINAL:")


def test_ready_keeps_its_old_contract():
    assert is_ready_message("READY:")
    assert is_ready_message("  ready: done")
    assert not is_ready_message("READYFOO")
    assert not is_ready_message("READY")


@pytest.mark.asyncio
async def test_bold_draft_claims_the_gate():
    """The exact seq-31 message must now count as the draft."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=True,
        entry_prefix="FINAL:",
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate.bind_to_thread(tid)

    await server.send_message(tid, author="a1", content="**FINAL: [X] 답은 X**")
    assert gate.draft_author == "a1"
    assert gate.has_proposal

    for a in ("a1", "a2"):
        await server.send_message(tid, author=a, content="*APPROVE:* 동의")
    assert gate.is_open


@pytest.mark.asyncio
async def test_gate_and_soft_block_agree_on_decoration():
    """If the two matchers disagree, an agent is blocked for a message the gate
    then ignores (or vice versa). Same helper, same verdict."""
    import json

    from agent_augury.backend.base import Completion, ModelBackend
    from agent_augury.core.agent.loop import AgentLoop

    class Quiet(ModelBackend):
        async def complete(self, messages, tools=None):
            return Completion(text=None)

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=True,
        entry_prefix="FINAL:",
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate.bind_to_thread(tid)
    await server.send_message(tid, author="a1", content="FINAL: 98")

    rival = AgentLoop(agent_id="a2", backend=Quiet(), server=server)
    rival.gate_open = False
    rival.gate_thread_id = tid
    rival.gate_entry_prefix = "FINAL:"
    rival.gate_draft_author_fn = lambda: gate.draft_author

    out = json.loads(
        await rival._execute_tool(
            "send_message", {"thread": tid, "content": "**FINAL: mine**"}
        )
    )
    assert out["error"] == "draft_already_posted"


NL = chr(10)


# --- misplaced signal (live `dc79a366` seq 19: P4 stalled at 3/4) -----------


REPORT_WITH_TRAILING_VOTE = (
    "## P4 REVIEW - 답안 형식 정리" + NL + NL + "검토 완료." + NL + NL
    + "APPROVE: 검토 완료"
)


def test_signal_opening_a_later_line_is_flagged():
    """agent-4 appended its vote to a report; the gate reads line one only."""
    assert misplaced_signal(REPORT_WITH_TRAILING_VOTE) == "APPROVE:"
    # ...and it is still NOT a signal -- the gate must not change its mind.
    assert not has_signal(REPORT_WITH_TRAILING_VOTE, "APPROVE:")


def test_a_proper_signal_is_not_flagged():
    assert misplaced_signal("APPROVE: ok" + NL + "FINAL: a quote here") is None
    assert misplaced_signal("**FINAL:** 98" + NL + "APPROVE: mine") is None


def test_citations_stay_mid_line_and_are_not_flagged():
    """Every false-positive candidate from the 470-message scan sits mid-line."""
    for quote in (
        "P5 제출 단계 진행 중." + NL + "agent-3의 FINAL: 드래프트를 기다립니다.",
        "검토 의견" + NL + "위 5가지를 수정하여 다시 FINAL:을 게시해 주세요.",
        "@agent-3 제출 게이트를 열기 위해 FINAL: 초안을 보내주세요.",
    ):
        assert misplaced_signal(quote) is None, quote


def test_empty_and_single_line_content():
    assert misplaced_signal("") is None
    assert misplaced_signal("just a work log") is None
