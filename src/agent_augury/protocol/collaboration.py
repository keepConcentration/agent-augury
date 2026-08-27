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
5. **P5 SUBMIT** — the assembler composes the final answer, broadcasts it for
   final approval, and submits.

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

from typing import Any, Callable

from ..server import MessageServer
from .approval import ConsensusGate
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

# Callback fired on any phase change
PhaseCallback = Callable[[Phase, Phase], None]


class CollaborationProtocol:
    """Drives the full P1~P5 collaboration protocol.

    Usage:
        protocol = CollaborationProtocol(server, participants=["a1","a2","a3"])
        protocol.bind_assembler("a1")
        protocol.start()  # → P1_EXPLORE

        # The orchestrator drives phase transitions:
        protocol.advance(P2_SPLIT)   # after P1 exploration is done
        # ... agents negotiate and vote ...
        protocol.advance(P3_EXECUTE) # after unanimous approval
        # ... agents execute ...
        protocol.advance(P4_REVIEW)
        # ... agents review ...
        protocol.advance(P5_SUBMIT)
        # ... assembler submits ...
        protocol.advance(COMPLETED)
    """

    def __init__(
        self,
        server: MessageServer,
        participants: list[str],
        *,
        assembler_id: str | None = None,
    ) -> None:
        self._server = server
        self.participants = list(participants)
        self.assembler_id = assembler_id or participants[0]

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

    # -- configuration --------------------------------------------------------

    def bind_assembler(self, agent_id: str) -> None:
        """Set the assembler agent (defaults to first participant)."""
        self.assembler_id = agent_id

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

    def _valid_transitions(self, frm: Phase, to: Phase) -> set[Phase]:
        """Return the set of valid target phases from ``frm``."""
        transitions: dict[Phase, set[Phase]] = {
            P1_EXPLORE: {P2_SPLIT, REJECTED},
            P2_SPLIT: {P3_EXECUTE, REJECTED},
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
        if self._on_gate_open:
            self._on_gate_open(phase)

    # -- gate binding (called by orchestrator before phase starts) ------------

    def bind_gate(
        self, phase: Phase, thread_name: str, *, require_proposal: bool = True
    ) -> ConsensusGate:
        """Bind a gate for the given phase to a thread with the given name."""
        if phase not in self._gates:
            raise ValueError(f"no gate slot for phase {phase}")

        gate = ConsensusGate(self._server, thread_name=thread_name, require_proposal=require_proposal)
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
            "assembler_id": self.assembler_id,
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
