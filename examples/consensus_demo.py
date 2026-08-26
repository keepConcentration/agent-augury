"""v0.1b acceptance — minimal split-consensus protocol end-to-end (DESIGN.md §6).

Flow: agent-1 opens the ``plan`` thread and proposes a split → everyone
approves → the gate flips OPEN → each agent posts its work share.

Assertions are ORDER-based on server sequence numbers:
  1. every APPROVE comes after the PROPOSE
  2. every work-share message carries seq > gate.opened_at_seq
     (nobody executes before unanimous approval)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_augury.session import Session  # noqa: E402

AGENTS = ["agent-1", "agent-2", "agent-3"]

CFG = {
    "mode": "L3",
    "max_steps": 24,
    "gate": {"thread_name": "plan"},
    "agents": [
        {
            "id": "agent-1",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "create_thread", "arguments": {"name": "plan", "participants": AGENTS}},
                    {"name": "send_message", "arguments": {
                        "thread": "$thread:0", "content": "PROPOSE: agent-1→탐색, 2·3→검증/정리", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread:0", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread_by_name:plan", "content": "(FYI) 탐색 완료: 정답 후보 43", "mentions": []}},
                ]},
            ]},
        },
        {
            "id": "agent-2",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread_by_name:plan", "content": "(FYI) 검증 완료: 근거 일치", "mentions": []}},
                ]},
            ]},
        },
        {
            "id": "agent-3",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {
                        "thread": "$thread_by_name:plan", "content": "(FYI) 정리 완료", "mentions": []}},
                ]},
            ]},
        },
    ],
}


def main() -> int:
    session = Session.from_config(dict(CFG))
    steps = asyncio.run(session.run())

    gate = session.gate
    assert gate is not None and gate.is_open, f"gate never opened: approvals={gate and gate.approvals}"

    snap = session.server.snapshot()
    by_seq = sorted(snap["messages"], key=lambda m: m["seq"])

    propose = next(m for m in by_seq if m["content"].startswith("PROPOSE:"))
    approves = [m for m in by_seq if m["content"].startswith("APPROVE:")]
    work = [m for m in by_seq if m["content"].startswith("(FYI)")]

    assert len(approves) == 3, f"expected 3 approvals, got {len(approves)}"
    assert all(m["seq"] > propose["seq"] for m in approves), "vote before proposal"
    assert len(work) == 3
    assert all(m["seq"] > gate.opened_at_seq for m in work), "work executed before unanimity"

    print(f"steps={steps}")
    for m in by_seq:
        marker = ""
        if gate.opened_at_seq == m["seq"]:
            marker = "  ← GATE OPEN"
        print(f"  [{m['seq']:>2}] {m['author']}: {m['content'][:60]}{marker}")
    print("PASS: propose → unanimous approve → gate OPEN → work shares, in strict order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
