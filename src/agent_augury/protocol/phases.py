"""Phase transition hooks — explicit extension points for v0.2 P1~P5.

DESIGN.md §6 v0.1b scope: the consensus gate flips OPEN on unanimous approval.
This module makes that transition an explicit, observable hook so that v0.2 can
chain further phases (P1~P5) without rewriting the gate itself.

Design:
- ``Phase`` is a string enum-like marker (e.g. "PROPOSED", "APPROVED", "OPEN").
- ``PhaseManager`` owns the current phase and fires ``on_transition`` callbacks
  whenever the gate (or any future driver) advances it.
- v0.2 adds P1~P5 phases on top of the v0.1b gate lifecycle.

This is intentionally minimal — a single hook list and an explicit ``advance()``
method. v0.2 adds phases; it does not change this API.
"""

from __future__ import annotations

from typing import Callable

Phase = str

# Canonical v0.1b phases (gate lifecycle)
PROPOSED = "PROPOSED"
APPROVED = "APPROVED"  # unanimous — gate OPEN
OPEN = "OPEN"  # alias kept for readability; APPROVED == OPEN in v0.1b

# v0.2 P1~P5 phases (full collaboration protocol)
P1_EXPLORE = "P1_EXPLORE"
P2_SPLIT = "P2_SPLIT"
P3_EXECUTE = "P3_EXECUTE"
P4_REVIEW = "P4_REVIEW"
P5_SUBMIT = "P5_SUBMIT"

# Terminal / failure states
REJECTED = "REJECTED"
COMPLETED = "COMPLETED"

PhaseCallback = Callable[[Phase, Phase], None]  # (from_phase, to_phase)


class PhaseManager:
    """Owns the current protocol phase and fires transition callbacks.

    Not thread-safe by design — single asyncio loop (§3.5.4).
    """

    def __init__(self, initial: Phase = PROPOSED) -> None:
        self._phase: Phase = initial
        self._on_transition: list[PhaseCallback] = []

    @property
    def phase(self) -> Phase:
        return self._phase

    def on_transition(self, callback: PhaseCallback) -> None:
        """Register a callback invoked on every phase change."""
        self._on_transition.append(callback)

    def advance(self, to: Phase) -> None:
        """Advance to a new phase, firing all registered callbacks.

        No-op if ``to`` equals the current phase (idempotent).
        """
        if to == self._phase:
            return
        frm = self._phase
        self._phase = to
        for cb in self._on_transition:
            cb(frm, to)

    def __repr__(self) -> str:
        return f"PhaseManager(phase={self._phase!r})"
