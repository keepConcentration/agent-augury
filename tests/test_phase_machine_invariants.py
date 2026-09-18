"""BFS liveness checks for the P1–P5 phase machine (PHASE_MACHINE_ROUTING §3.4).

L1/L2 only, headless assumption. S1–S3 stay in gate/assignment unit tests.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any

import pytest

from agent_augury.core.protocol.collaboration import CollaborationProtocol
from agent_augury.core.protocol.phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    REJECTED,
)
from agent_augury.core.server import MessageServer

TERMINAL = frozenset({COMPLETED, REJECTED})
AGENTS = ("a1", "a2", "a3")
THREADS = {
    P2_SPLIT: ("plan", True, "PROPOSE:"),
    P3_EXECUTE: ("work", False, ""),
    P4_REVIEW: ("results", False, ""),
    P5_SUBMIT: ("submission", True, "FINAL:"),
}


async def _boot() -> tuple[MessageServer, CollaborationProtocol, dict[str, str]]:
    server = MessageServer()
    for a in AGENTS:
        server.register_agent(a)
    protocol = CollaborationProtocol(server, participants=list(AGENTS))
    tids: dict[str, str] = {}
    for phase, (name, require, entry) in THREADS.items():
        protocol.bind_gate(
            phase,
            name,
            require_proposal=require,
            entry_prefix=entry or "PROPOSE:",
        )
        tid = await server.create_thread(name, participants=list(AGENTS))
        protocol.gate_for(phase).bind_to_thread(tid)
        tids[phase] = tid

    def _advance(ph: str) -> None:
        nxt = protocol.next_phase_after_gate(ph)
        if nxt:
            protocol.advance(nxt)

    protocol.on_gate_open(_advance)
    protocol.start()
    return server, protocol, tids


def _key(protocol: CollaborationProtocol) -> tuple:
    phase = protocol.phase
    gate = protocol.gate_for(phase)
    return (
        phase,
        frozenset(protocol._ready_states),
        frozenset(gate.approvals) if gate else frozenset(),
        bool(gate and gate.has_proposal),
        gate.draft_author if gate else None,
        bool(gate and gate.is_open),
        bool(gate and getattr(gate, "human_pending", False)),
        protocol.split_none,
        frozenset(protocol._assignments.items()),
        frozenset(protocol._gate_open_fired),
    )


def _capture(
    server: MessageServer, protocol: CollaborationProtocol
) -> dict[str, Any]:
    return {
        "proto": protocol.snapshot(),
        "messages": deepcopy(server._messages),
    }


async def _restore(
    snap: dict[str, Any],
) -> tuple[MessageServer, CollaborationProtocol, dict[str, str]]:
    server, protocol, tids = await _boot()
    # Replace messages before restore so draft_author commit finds them.
    server._messages = deepcopy(snap["messages"])
    for m in server._messages:
        server._message_index[m["message_id"]] = m
    protocol.restore(snap["proto"])
    # restore() may leave gates on old thread ids — re-point to this server.
    for phase, tid in tids.items():
        gate = protocol.gate_for(phase)
        if gate is None:
            continue
        gate.thread_id = tid
        gate.participants = list(AGENTS)
    return server, protocol, tids


def _all_parked(protocol: CollaborationProtocol) -> bool:
    if protocol.phase in TERMINAL:
        return False
    return all(protocol.is_agent_done(a) for a in AGENTS)


def _actions(
    protocol: CollaborationProtocol, tids: dict[str, str]
) -> list[tuple[str, str, str]]:
    phase = protocol.phase
    if phase in TERMINAL:
        return []
    out: list[tuple[str, str, str]] = []
    if phase == P1_EXPLORE:
        tid = tids[P2_SPLIT]
        for a in AGENTS:
            if a not in protocol._ready_states:
                out.append((a, tid, "READY: done"))
        return out

    gate = protocol.gate_for(phase)
    if gate is None or gate.is_open:
        return out
    tid = tids[phase]

    if phase == P2_SPLIT:
        if not gate.has_proposal:
            out.append((
                "a1", tid,
                (
                    "PROPOSE: split\nASSIGN a1: x\nASSIGN a2: y\n"
                    "ASSIGN a3: z\nSUBMITTER: a1\n"
                ),
            ))
            out.append(("a1", tid, "PROPOSE:\nSPLIT: none\nSUBMITTER: a1\n"))
        elif gate.has_proposal:
            for a in AGENTS:
                if a not in gate.approvals:
                    out.append((a, tid, "APPROVE: ok"))
            out.append(("a1", tid, "REJECT: redo"))
        return out

    if phase in (P3_EXECUTE, P4_REVIEW):
        for a in AGENTS:
            if a not in gate.approvals:
                out.append((a, tid, "APPROVE: done"))
        return out

    if phase == P5_SUBMIT:
        if not gate.has_proposal:
            out.append(("a1", tid, "FINAL: 42"))
        else:
            for a in AGENTS:
                if a not in gate.approvals:
                    out.append((a, tid, "APPROVE: ok"))
            out.append(("a1", tid, "REJECT: redo"))
        return out
    return out


@pytest.mark.asyncio
async def test_bfs_l1_l2_no_deadlock_headless():
    """Every reachable non-terminal state can move, or is D12-exitable (parked)."""
    server, protocol, tids = await _boot()
    root = _key(protocol)
    snaps: dict[tuple, dict[str, Any]] = {root: _capture(server, protocol)}
    frontier: deque[tuple] = deque([root])
    seen: set[tuple] = set()
    stuck: list[tuple] = []

    while frontier:
        key = frontier.popleft()
        if key in seen:
            continue
        seen.add(key)
        server, protocol, tids = await _restore(snaps[key])

        if protocol.phase in TERMINAL:
            continue
        # L2 + D12: headless all-parked ends the turn — not a protocol stuck.
        if _all_parked(protocol):
            continue

        acts = _actions(protocol, tids)
        if not acts:
            stuck.append(key)
            continue

        for author, tid, content in acts:
            s2, p2, t2 = await _restore(snaps[key])
            # Remap tid to this restore's thread for the current phase.
            phase = p2.phase
            use_tid = t2[P2_SPLIT] if phase == P1_EXPLORE else t2[phase]
            await s2.send_message(use_tid, author=author, content=content)
            nk = _key(p2)
            if nk not in snaps:
                snaps[nk] = _capture(s2, p2)
                frontier.append(nk)

    assert not stuck, f"L1 dead ends (sample): {stuck[:3]}; explored={len(seen)}"
    assert len(seen) >= 8, f"BFS too shallow: explored {len(seen)}"
