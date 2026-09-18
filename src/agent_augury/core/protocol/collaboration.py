"""P1~P5 full collaboration protocol state machine (DESIGN.md §2.3, §6 v0.2).

Extends the v0.1b consensus gate with the complete five-phase protocol:

1. **P1 EXPLORE** — agents independently explore the task. No messages sent
   (the orchestrator injects exploration tasks).
2. **P2 SPLIT** — agents pool discoveries, negotiate a split, and vote to
   approve. Unanimous APPROVE advances to P3.
3. **P3 EXECUTE** — each agent executes their assigned share. Work logs
   posted to a shared thread.
4. **P4 REVIEW** — agents broadcast results with evidence. Reviewers flag
   conflicts, insufficient evidence, or omissions.
5. **P5 SUBMIT** — the team elects who drafts the final answer (no fixed
   drafter), broadcasts it for final approval, and submits.

The ``CollaborationProtocol`` class drives the phase transitions using a
``PhaseManager`` and one ``ConsensusGate`` per approval point. It exposes
``advance()`` for phase transitions and tracks which agents have approved
at each gate.

Conventions (DESIGN.md §2.4):
- ``PROPOSE:`` — opens/updates a proposal
- ``APPROVE:`` — one vote per participant
- ``REJECT:`` — clears collected approvals
- ``RESULT:`` — P4 review result (with evidence)
- ``FINAL:`` — P5 final answer
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Set as AbstractSet
from typing import Any

from ..server import MessageServer
from .approval import ConsensusGate
from .assignments import parse_assignments, parse_split, parse_submitter
from .phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    REJECTED,
    Phase,
    PhaseManager,
)
from .signals import has_signal, is_ready_message

# Callback fired on any phase change
PhaseCallback = Callable[[Phase, Phase], None]


class CollaborationProtocol:
    """Drives the full P1~P5 collaboration protocol.

    Usage:
        protocol = CollaborationProtocol(server, participants=["a1","a2","a3"])
        protocol.start()  # → P1_EXPLORE

        # The orchestrator drives phase transitions:
        protocol.advance(P2_SPLIT)   # after P1 exploration is done
        # ... agents negotiate and vote ...
        protocol.advance(P3_EXECUTE) # after unanimous approval
        # ... agents execute ...
        protocol.advance(P4_REVIEW)
        # ... agents review ...
        protocol.advance(P5_SUBMIT)
        # ... team posts FINAL: and approves ...
        protocol.advance(COMPLETED)
    """

    def __init__(
        self,
        server: MessageServer,
        participants: list[str],
        *,
        mode: str = "full",
    ) -> None:
        self._server = server
        # "light" skips P2-P4: explore, then one final consensus gate.
        self.mode = mode
        self.participants = list(participants)

        self.phase_manager = PhaseManager(initial=P1_EXPLORE)
        self._gates: dict[Phase, ConsensusGate | None] = {
            P2_SPLIT: None,  # gate for P2 split approval
            P3_EXECUTE: None,
            P4_REVIEW: None,
            P5_SUBMIT: None,
        }
        self._current_gate: ConsensusGate | None = None
        self._current_gate_phase: Phase | None = None
        self._on_phase_change: PhaseCallback | None = None
        self._on_gate_open: Callable[[Phase], None] | None = None
        self._gate_open_fired: set[Phase] = set()
        # v0.2: READY-based P1 finish policy
        self._ready_states: set[str] = set()
        # What P2 agreed each agent would do, and who posts the P5 draft.
        # Committed once when the P2 gate opens (server re-read) — never from
        # every PROPOSE: mid-phase (PHASE_MACHINE_ROUTING §2.3).
        self._assignments: dict[str, str] = {}
        self.submitter_id: str | None = None
        # True when the winning P2 draft declared SPLIT: none (R1).
        self.split_none: bool = False
        # Subscribe once at construction (not only on checkpoint restore).
        self._server.subscribe(self._on_message)

    # -- configuration --------------------------------------------------------

    def on_phase_change(self, callback: PhaseCallback) -> None:
        """Register a callback for any phase transition."""
        self._on_phase_change = callback
        self.phase_manager.on_transition(callback)

    def on_gate_open(self, callback: Callable[[Phase], None]) -> None:
        """Register a callback fired when any gate opens (receives phase)."""
        self._on_gate_open = callback

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Begin the protocol at P1_EXPLORE."""
        self.phase_manager.advance(P1_EXPLORE)

    def begin_round(self, *, mode: str | None = None) -> None:
        """Open a new round on a finished protocol.

        FOLLOWUP_TURN_PROTOCOL_DESIGN: a follow-up question already produces a
        P1 (every agent answers independently); this gives it the P5 that
        merges and reviews those answers.

        Gates are reset **in place**. ``MessageServer`` has no ``unsubscribe``,
        so rebuilding them would leave the old gates subscribed and counting
        every vote twice.
        """
        if self.phase not in (COMPLETED, REJECTED):
            raise ValueError(
                f"begin_round() needs a finished protocol, not {self.phase}"
            )
        if mode is not None:
            self.mode = mode
        for gate in self._gates.values():
            if gate is not None:
                gate.reset_for_round()
        # Without this, _handle_gate_open returns early for a phase that fired
        # last round and the gate opens without the phase ever advancing.
        self._gate_open_fired.clear()
        self._ready_states.clear()
        # light has no P2, so nothing would ever overwrite last round's split.
        self._assignments.clear()
        self.submitter_id = None
        self.split_none = False
        self._current_gate = None
        self._current_gate_phase = None
        # advance() rejects COMPLETED -> P1 (terminal). Go through the phase
        # manager so surfaces still see the transition on the wire.
        self.phase_manager.advance(P1_EXPLORE)

    def snapshot(self) -> dict[str, Any]:
        """Checkpoint payload for protocol + gates."""
        gates: dict[str, Any] = {}
        for phase, gate in self._gates.items():
            if gate is not None:
                gates[phase] = gate.snapshot()
        return {
            "phase": self.phase,
            "participants": list(self.participants),
            "current_gate_phase": self._current_gate_phase,
            "gate_open_fired": sorted(self._gate_open_fired),
            "ready_states": sorted(self._ready_states),
            "assignments": dict(self._assignments),
            "submitter_id": self.submitter_id,
            "split_none": self.split_none,
            "gates": gates,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Hydrate phase and gate state from a checkpoint (no P1 reset)."""
        phase = data.get("phase") or P1_EXPLORE
        self.phase_manager.restore(str(phase))
        self._gate_open_fired = {str(x) for x in (data.get("gate_open_fired") or [])}
        # In-place: agents hold a reference to this set (see Session inject).
        self._ready_states.clear()
        self._ready_states.update(str(a) for a in (data.get("ready_states") or []))
        assigned = data.get("assignments")
        if isinstance(assigned, dict):
            self._assignments = {str(k): str(v) for k, v in assigned.items()}
        sub = data.get("submitter_id")
        self.submitter_id = str(sub) if sub else None
        self.split_none = bool(data.get("split_none", False))
        gates_data = data.get("gates") or {}
        for phase_name, snap in gates_data.items():
            if not isinstance(snap, dict):
                continue
            gate = self._gates.get(str(phase_name))
            if gate is None:
                continue
            try:
                gate.restore_state(snap)
            except KeyError:
                # Thread missing from MessageServer — leave unbound
                continue
        cgp = data.get("current_gate_phase")
        if cgp:
            self._current_gate_phase = str(cgp)
            self._setup_gate_for_phase(str(cgp))
        else:
            self._setup_gate_for_phase(self.phase)

    def _on_message(self, message: dict[str, Any]) -> None:
        """Track READY messages from participants for P1 finish policy.

        ``READY:`` with optional trailing text is recognized (case/whitespace
        tolerant). ``READYFOO`` / bare ``READY`` are ignored.
        When all participants have sent READY, automatically finish P1.

        P2 ``PROPOSE:`` ASSIGN/SUBMITTER/SPLIT lines are **not** parsed here —
        they are committed once from the server when the P2 gate opens
        (:meth:`_commit_p2_draft`). Mid-phase parsing would take a losing
        rival draft (PHASE_MACHINE_ROUTING §2.3).
        """
        if self.phase != P1_EXPLORE:
            return
        content = message.get("content", "")
        author = message.get("author", "")
        if author in self.participants and is_ready_message(content):
            self._ready_states.add(author)
            if self.all_ready:
                self.finish_p1()

    def _commit_p2_draft(self) -> None:
        """Read the winning P2 draft from MessageServer and commit split fields.

        Called once when the P2 gate opens — before ``next_phase_after_gate``
        so R1 routing sees the committed ``split_none`` / assignments.
        """
        gate = self._gates.get(P2_SPLIT)
        if gate is None or gate.draft_author is None or gate.thread_id is None:
            return
        content = ""
        for m in self._server.snapshot()["messages"]:
            if (
                m["thread_id"] == gate.thread_id
                and m["author"] == gate.draft_author
                and has_signal(m["content"], gate.entry_prefix)
            ):
                content = m["content"]  # last match = latest draft
        self._assignments = parse_assignments(content, self.participants) or {}
        self.submitter_id = parse_submitter(content, self.participants)
        self.split_none = parse_split(content)

    @property
    def all_ready(self) -> bool:
        """True if all participants have sent READY."""
        return set(self.participants) <= self._ready_states

    @property
    def ready_states(self) -> AbstractSet[str]:
        """Live read-only view of participants that have sent READY."""
        return self._ready_states

    def assignment_for(self, agent_id: str) -> str | None:
        """The share P2 agreed this agent would take, if it named one."""
        return self._assignments.get(agent_id)

    def has_ready(self, agent_id: str) -> bool:
        """True if this participant has already sent a READY signal."""
        return agent_id in self._ready_states

    def is_agent_done(self, agent_id: str) -> bool:
        """True when this agent has nothing left to do in the current phase.

        Reads the existing sources of truth only (READY set / gate approvals) —
        no parallel bookkeeping to keep in sync across checkpoints.
        """
        if self.phase == P1_EXPLORE:
            return agent_id in self._ready_states
        gate = self._gates.get(self.phase)
        if gate is None or gate.is_open:
            return False
        if gate.require_proposal and not gate.has_proposal:
            # Voting is not enough: the gate cannot open until someone PROPOSEs,
            # so nobody may park yet (a fully-approved, proposal-less gate would
            # otherwise park every agent and wait forever).
            return False
        return agent_id in gate.approvals

    def finish_p1(self) -> None:
        """Explicitly finish P1 exploration and advance to P2.

        This replaces fragile thread-counting heuristics in orchestrators —
        the orchestrator calls this when it determines P1 is done, rather
        than inferring it from server state.

        Policy: P2 entry is only allowed once ALL participants have sent
        a READY message. Before that, finish_p1() raises RuntimeError.
        """
        if self.phase != P1_EXPLORE:
            raise ValueError(
                f"finish_p1() can only be called from P1_EXPLORE, current phase: {self.phase}"
            )
        if not self.all_ready:
            missing = set(self.participants) - self._ready_states
            raise RuntimeError(
                f"Cannot finish P1: missing READY from {sorted(missing)}. "
                f"All participants must send READY before P2 entry."
            )
        self._ready_states.clear()
        self.advance(self.next_phase_after_p1)

    def advance(self, to: Phase) -> None:
        """Advance to the next phase, setting up gates as needed."""
        # Idempotent: no-op if same phase
        if to == self.phase:
            return

        frm = self.phase

        # Validate transition
        valid = self._valid_transitions(frm, to)
        if to not in valid:
            raise ValueError(
                f"invalid phase transition: {frm} → {to}. "
                f"valid targets from {frm}: {valid}"
            )

        self.phase_manager.advance(to)

        # Wire up the gate for the new phase if needed
        self._setup_gate_for_phase(to)

        # Fire gate-open callback for terminal phases
        if to in (COMPLETED, REJECTED) and self._on_gate_open:
            self._on_gate_open(to)

    @property
    def next_phase_after_p1(self) -> Phase:
        """Where P1 hands off — light jumps straight to the final gate."""
        return P5_SUBMIT if self.mode == "light" else P2_SPLIT

    def next_phase_after_gate(self, phase: Phase) -> Phase | None:
        """The phase a freshly opened gate advances to (None = stay put)."""
        if self.mode == "light":
            return COMPLETED if phase == P5_SUBMIT else None
        # R1b: team declared no split and named no ASSIGN → skip empty P3/P4.
        # E2: ASSIGN wins over SPLIT: none when both appear.
        if (
            phase == P2_SPLIT
            and self.split_none
            and not self._assignments
        ):
            return P5_SUBMIT
        return {
            P2_SPLIT: P3_EXECUTE,
            P3_EXECUTE: P4_REVIEW,
            P4_REVIEW: P5_SUBMIT,
            P5_SUBMIT: COMPLETED,
        }.get(phase)

    def _valid_transitions(self, frm: Phase, to: Phase) -> set[Phase]:
        """Return the set of valid target phases from ``frm``."""
        if self.mode == "light":
            transitions: dict[Phase, set[Phase]] = {
                P1_EXPLORE: {P5_SUBMIT, REJECTED},
                P5_SUBMIT: {COMPLETED, REJECTED},
                REJECTED: set(),
                COMPLETED: set(),
            }
            return transitions.get(frm, set())
        transitions = {
            P1_EXPLORE: {P2_SPLIT, REJECTED},
            P2_SPLIT: {P3_EXECUTE, P5_SUBMIT, REJECTED},
            P3_EXECUTE: {P4_REVIEW, REJECTED},
            P4_REVIEW: {P5_SUBMIT, REJECTED},
            P5_SUBMIT: {COMPLETED, REJECTED},
            REJECTED: set(),  # terminal
            COMPLETED: set(),  # terminal
        }
        return transitions.get(frm, set())

    def _setup_gate_for_phase(self, phase: Phase) -> None:
        """Wire up the ConsensusGate for the given phase."""
        gate = self._gates.get(phase)
        if gate is not None:
            self._current_gate = gate
            self._current_gate_phase = phase
            # Register gate callback to auto-advance on unanimous approval
            gate.on_open(lambda: self._handle_gate_open(phase))

    def _handle_gate_open(self, phase: Phase) -> None:
        """Called when a gate opens (unanimous approval reached)."""
        if phase in self._gate_open_fired:
            return
        self._gate_open_fired.add(phase)
        # Commit winning P2 draft *before* the session callback reads
        # next_phase_after_gate (which needs split_none / assignments).
        if phase == P2_SPLIT:
            self._commit_p2_draft()
        if self._on_gate_open:
            self._on_gate_open(phase)

    # -- gate binding (called by orchestrator before phase starts) ------------

    def bind_gate(
        self,
        phase: Phase,
        thread_name: str,
        *,
        require_proposal: bool = True,
        entry_prefix: str = "PROPOSE:",
        await_human_after_agents: bool = False,
    ) -> ConsensusGate:
        """Bind a gate for the given phase to a thread with the given name."""
        if phase not in self._gates:
            raise ValueError(f"no gate slot for phase {phase}")

        gate = ConsensusGate(
            self._server,
            thread_name=thread_name,
            require_proposal=require_proposal,
            entry_prefix=entry_prefix,
            await_human_after_agents=await_human_after_agents,
        )
        self._server.subscribe(gate.on_message)
        self._gates[phase] = gate
        return gate

    # -- views ----------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        """Current protocol phase."""
        return self.phase_manager.phase

    @property
    def current_gate(self) -> ConsensusGate | None:
        """The currently active gate (or None)."""
        return self._current_gate

    @property
    def current_gate_phase(self) -> Phase | None:
        """The phase the current gate belongs to."""
        return self._current_gate_phase

    def gate_for(self, phase: Phase) -> ConsensusGate | None:
        """Return the gate for a given phase."""
        return self._gates.get(phase)

    @property
    def is_complete(self) -> bool:
        """True if the protocol has reached COMPLETED."""
        return self.phase == COMPLETED

    @property
    def is_rejected(self) -> bool:
        """True if the protocol was rejected."""
        return self.phase == REJECTED

    def reject(self) -> None:
        """Reject the protocol (terminal state)."""
        self.advance(REJECTED)

    # -- status snapshot ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return a status snapshot for observability."""
        return {
            "phase": self.phase,
            "participants": self.participants,
            "current_gate_phase": self._current_gate_phase,
            "current_gate_open": (
                self._current_gate.is_open if self._current_gate else None
            ),
        }

    def __repr__(self) -> str:
        return (
            f"CollaborationProtocol(phase={self.phase!r}, "
            f"participants={self.participants!r})"
        )
