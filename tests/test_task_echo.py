"""The request itself reaches the two phases that can lose it.

Live gap (session 2026-09-23): the human asked for a *review* — gaps, things to
build, improvements, features worth adding. P2 proposed "ASSIGN agent-1: Explore
and document ..." for both agents, and what shipped was an architecture
walkthrough with none of the four items.

Nothing in P1-P5 compared the output to the request. P4 reviews teammates'
submissions against each other, and P5 only requires that a FINAL: exists, so
once P2 dropped the task the remaining phases faithfully checked the wrong one.
P2 fixes what gets built and P5 declares it done, so the reminder lands there.

Advisory, like assignments: this is prompt text, it gates nothing.
"""

from __future__ import annotations

from agent_augury.core.agent.system_prompt import render_system_prompt
from agent_augury.core.protocol.phases import (
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
)

TASK = "프로젝트 리뷰해줘. 미비한 점, 만들어야 할 점, 개선할 점을 MD로."


def _prompt(phase: str, task: str | None = TASK) -> str:
    return render_system_prompt("agent-1", phase, task=task)


def test_p2_is_told_to_cover_the_whole_request():
    out = _prompt(P2_SPLIT)
    assert TASK in out
    assert "ASSIGN" in out
    assert "WHOLE request" in out


def test_p5_is_told_to_check_the_draft_against_the_request():
    out = _prompt(P5_SUBMIT)
    assert TASK in out
    assert "REJECT:" in out


def test_quiet_in_phases_that_only_inherit_the_drift():
    """P1/P3/P4 already carry the request in the conversation; no echo there."""
    for phase in (P1_EXPLORE, P3_EXECUTE, P4_REVIEW):
        assert TASK not in _prompt(phase)


def test_no_task_means_no_block():
    for phase in (P2_SPLIT, P5_SUBMIT):
        out = _prompt(phase, task=None)
        assert "What the human asked for" not in out


def test_long_request_is_clipped():
    long_task = "A" * 2000
    out = _prompt(P2_SPLIT, task=long_task)
    assert "A" * 1200 in out
    assert "A" * 1300 not in out
    assert "…" in out


def test_checkpoint_round_trips_the_request(tmp_path):
    """A resumed round must keep the request, or both checks quietly disarm."""
    from agent_augury.core.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path, "sess-1")
    store.save(
        fingerprint="fp",
        conversations={"agent-1": []},
        protocol={},
        inbox={},
        task=TASK,
    )
    assert store.load()[0]["task"] == TASK

    # A later save that does not pass the task must not erase it.
    store.save(
        fingerprint="fp",
        conversations={"agent-1": []},
        protocol={},
        inbox={},
    )
    assert store.load()[0]["task"] == TASK
