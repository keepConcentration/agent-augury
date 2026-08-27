"""v0.2 E2E — P1~P5 full collaboration protocol (DESIGN.md §2.3, §6).

Scenario with 3 agents using FakeModelBackend:
- agent-1: assembler (creates threads, proposes split, composes final answer)
- agent-2: executor A
- agent-3: executor B

Flow:
  P1: all agents explore silently
  P2: agent-1 creates all threads → proposes split on plan → unanimous APPROVE
  P3: each agent posts work logs → unanimous APPROVE on execution
  P4: agents broadcast results → unanimous APPROVE on review
  P5: agent-1 posts FINAL → unanimous APPROVE on submission → COMPLETED

The demo drives phase transitions via session.on_step callback: after the
assembler creates all threads (end of P1), the orchestrator advances to P2.
Thereafter, gates auto-advance on unanimous approval.

Assertions:
  - All phases advance in order
  - Each gate opens only after unanimous approval
  - Final answer is approved by all agents
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_augury.session import Session  # noqa: E402
from agent_augury.protocol.phases import (
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    COMPLETED,
)

AGENTS = ["agent-1", "agent-2", "agent-3"]

CFG = {
    "mode": "L3",
    "max_steps": 30,
    "protocol": {
        "participants": AGENTS,
        "assembler_id": "agent-1",
        "gates": {
            "P2_SPLIT": "plan",
            "P3_EXECUTE": "execution",
            "P4_REVIEW": "review",
            "P5_SUBMIT": "submission",
        },
    },
    "agents": [
        {
            "id": "agent-1",
            "backend": {
                "type": "fake",
                "script": [
                    # P1: explore silently
                    {"tool_calls": [
                        {"name": "search", "arguments": {"q": "main topic"}},
                    ]},
                    # P2: create ALL threads + propose split
                    {"tool_calls": [
                        {"name": "create_thread", "arguments": {
                            "name": "plan", "participants": AGENTS}},
                        {"name": "create_thread", "arguments": {
                            "name": "execution", "participants": AGENTS}},
                        {"name": "create_thread", "arguments": {
                            "name": "review", "participants": AGENTS}},
                        {"name": "create_thread", "arguments": {
                            "name": "submission", "participants": AGENTS}},
                        {"name": "send_message", "arguments": {
                            "thread": "$thread:0",
                            "content": "PROPOSE: agent-1→검색·종합, agent-2→검증, agent-3→정리",
                            "mentions": []}},
                    ]},
                    # P2: approve plan → P2 gate opens
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread:0",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P3: post work log
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "(FYI) agent-1 검색 완료: 정답 후보 43",
                            "mentions": []}},
                    ]},
                    # P3: approve execution → P3 gate opens
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P4: broadcast result
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "RESULT: 정답은 43 (근거: 다중 소수 곱)",
                            "mentions": []}},
                    ]},
                    # P4: approve review → P4 gate opens
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P5: compose final answer
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:submission",
                            "content": "FINAL: 정답은 43",
                            "mentions": []}},
                    ]},
                    # P5: approve submission → P5 gate opens → COMPLETED
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:submission",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    {"text": "done"},
                ],
            },
        },
        {
            "id": "agent-2",
            "backend": {
                "type": "fake",
                "script": [
                    # P1: explore silently
                    {"text": "exploring..."},
                    # P2: approve split
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P3: post work log
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "(FYI) agent-2 검증 완료: 근거 일치",
                            "mentions": []}},
                    ]},
                    # P3: approve execution
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P4: broadcast result
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "RESULT: 검증 완료 (이의 없음)",
                            "mentions": []}},
                    ]},
                    # P4: approve review
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P5: wait for assembler to post FINAL first
                    {"text": "waiting for final answer"},
                    # P5: approve submission
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:submission",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    {"text": "done"},
                ],
            },
        },
        {
            "id": "agent-3",
            "backend": {
                "type": "fake",
                "script": [
                    # P1: explore silently
                    {"text": "exploring..."},
                    # P2: approve split
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P3: post work log
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "(FYI) agent-3 정리 완료",
                            "mentions": []}},
                    ]},
                    # P3: approve execution
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P4: broadcast result
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "RESULT: 정리 완료 (이의 없음)",
                            "mentions": []}},
                    ]},
                    # P4: approve review
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:review",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    # P5: wait for assembler to post FINAL first
                    {"text": "waiting for final answer"},
                    # P5: approve submission
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:submission",
                            "content": "APPROVE: ok",
                            "mentions": []}},
                    ]},
                    {"text": "done"},
                ],
            },
        },
    ],
}


def main() -> int:
    session = Session.from_config(dict(CFG))
    protocol = session.protocol
    assert protocol is not None

    # Track thread creation to detect P1→P2 transition
    p1_done = False

    def on_step(agent_id, result):
        nonlocal p1_done
        # After assembler creates all 4 threads, advance from P1 to P2
        if not p1_done and agent_id == "agent-1" and len(protocol._server.snapshot()["threads"]) >= 4:
            if protocol.phase == P1_EXPLORE:
                protocol.advance(P2_SPLIT)
                p1_done = True

    session.on_step = on_step

    steps = asyncio.run(session.run())

    snap = session.server.snapshot()
    by_seq = sorted(snap["messages"], key=lambda m: m["seq"])

    # Phase assertions
    assert protocol.phase == "COMPLETED", f"protocol not completed: {protocol.phase}"
    assert protocol.is_complete

    # Gate assertions
    p2_gate = protocol.gate_for("P2_SPLIT")
    p3_gate = protocol.gate_for("P3_EXECUTE")
    p4_gate = protocol.gate_for("P4_REVIEW")
    p5_gate = protocol.gate_for("P5_SUBMIT")
    assert p2_gate is not None and p2_gate.is_open, "P2 gate never opened"
    assert p3_gate is not None and p3_gate.is_open, "P3 gate never opened"
    assert p4_gate is not None and p4_gate.is_open, "P4 gate never opened"
    assert p5_gate is not None and p5_gate.is_open, "P5 gate never opened"

    # Message ordering assertions
    propose = next(m for m in by_seq if m["content"].startswith("PROPOSE:"))
    approvals = [m for m in by_seq if m["content"].startswith("APPROVE:")]
    work = [m for m in by_seq if m["content"].startswith("(FYI)")]
    results = [m for m in by_seq if m["content"].startswith("RESULT:")]
    finals = [m for m in by_seq if m["content"].startswith("FINAL:")]

    assert len(approvals) >= 12, f"expected at least 12 approvals, got {len(approvals)}"
    assert len(work) == 3, f"expected 3 work shares, got {len(work)}"
    assert len(results) == 3, f"expected 3 results, got {len(results)}"
    assert len(finals) == 1, f"expected 1 final, got {len(finals)}"

    # All approvals come after proposal
    assert all(m["seq"] > propose["seq"] for m in approvals), "vote before proposal"

    # Work shares come after P2 gate opened
    assert all(m["seq"] > p2_gate.opened_at_seq for m in work), "work before P2 gate open"

    # Final answer approved
    final_approvals = [m for m in by_seq if m["content"].startswith("APPROVE:") and m["seq"] > finals[0]["seq"]]
    assert len(final_approvals) == 3, f"expected 3 final approvals, got {len(final_approvals)}"

    print(f"steps={steps}")
    print(f"threads={len(snap['threads'])} messages={len(snap['messages'])}")
    print(f"phase={protocol.phase}")
    print(f"P2 gate opened at seq={p2_gate.opened_at_seq}")
    print(f"P3 gate opened at seq={p3_gate.opened_at_seq}")
    print(f"P4 gate opened at seq={p4_gate.opened_at_seq}")
    print(f"P5 gate opened at seq={p5_gate.opened_at_seq}")
    print("--- message sequence ---")
    for m in by_seq:
        marker = ""
        if p2_gate.opened_at_seq == m["seq"]:
            marker = "  ← P2 GATE OPEN"
        elif p3_gate.opened_at_seq == m["seq"]:
            marker = "  ← P3 GATE OPEN"
        elif p4_gate.opened_at_seq == m["seq"]:
            marker = "  ← P4 GATE OPEN"
        elif p5_gate.opened_at_seq == m["seq"]:
            marker = "  ← P5 GATE OPEN"
        print(f"  [{m['seq']:>2}] {m['author']}: {m['content'][:60]}{marker}")
    print("PASS: P1~P5 full collaboration protocol completed in order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
