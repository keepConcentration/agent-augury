"""Minimal collaboration protocol: the consensus gate (DESIGN.md §6 v0.1b).

Conventions:
- ``PROPOSE:`` — opens/updates a proposal; approvals count only afterwards.
- ``APPROVE:`` — one vote per participant.
- ``REJECT:`` — clears collected approvals; consensus must re-form.

The gate binds to the FIRST thread whose name matches ``thread_name``
(threads carry their name in the server, so the gate keeps a server
reference to resolve it at bind time).

v0.2: ``require_proposal=False`` allows gates to bind without a PROPOSE
message (used for P3+ gates where work logs start immediately).

v0.8 (PHASE_ENTRY_SIGNAL_DESIGN): the required signal is per-gate
(``entry_prefix``) — P2 opens on ``PROPOSE:``, P5 on ``FINAL:``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..server import MessageServer

GateCallback = Callable[[], None]


class ConsensusGate:
    """Watches messages via server subscription; flips open on unanimity."""

    def __init__(
        self,
        server: MessageServer,
        thread_name: str,
        *,
        require_proposal: bool = True,
        entry_prefix: str = "PROPOSE:",
        await_human_after_agents: bool = False,
    ) -> None:
        self._server = server
        self.thread_name = thread_name
        self.thread_id: str | None = None
        self.participants: list[str] = []
        self.approvals: set[str] = set()
        self.require_proposal = require_proposal
        # The signal that must appear before votes can open this gate
        # (P2 negotiates a plan with PROPOSE:, P5 drafts an answer with FINAL:).
        self.entry_prefix = entry_prefix
        self.opened_at_seq: int | None = None
        self._bound: bool = False
        self._on_open: GateCallback | None = None
        self._proposal_received: bool = False
        # Who posted the draft this gate is voting on. Votes belong to a
        # specific draft, so a replacement resets them (see on_message).
        self.draft_author: str | None = None
        # HUMAN_APPROVAL_GATE_DESIGN: after_agents 2nd stage
        self.await_human_after_agents = bool(await_human_after_agents)
        self.human_pending: bool = False
        self._on_human_pending: GateCallback | None = None

    # -- explicit binding (for pre-created threads) -------------------------

    def bind_to_thread(self, thread_id: str) -> None:
        """Explicitly bind this gate to a pre-created thread.

        Used by Session to bind gates to threads before the protocol starts,
        so the gate is ready when the phase begins.

        Note: does NOT mark _proposal_received. has_proposal reflects whether
        an actual PROPOSE message arrived, not whether the gate is bound.
        """
        thread = self._server.get_thread(thread_id)
        if thread["name"] != self.thread_name:
            raise ValueError(
                f"thread name mismatch: expected {self.thread_name!r}, "
                f"got {thread['name']!r}"
            )
        self.thread_id = thread_id
        self.participants = list(thread["participants"])

    # -- subscription entrypoint ---------------------------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        if self.opened_at_seq is not None:
            return  # gate already opened — protocol phase is over

        thread_id = message["thread_id"]

        # not yet bound: look up the thread and match by name
        if self.thread_id is None:
            try:
                thread = self._server.get_thread(thread_id)
            except KeyError:
                return
            if thread["name"] != self.thread_name:
                return
            # Name-based binding (no explicit bind_to_thread):
            # - require_proposal=False: any message on the matching thread binds
            # - require_proposal=True: only the entry signal binds, and it counts
            if self.require_proposal:
                if not message["content"].startswith(self.entry_prefix):
                    return  # waiting for the entry signal
                self._proposal_received = True
            self.thread_id = thread_id
            self.participants = list(thread["participants"])
            # Fall through to evaluate this message for APPROVE/REJECT
            # (don't return — the first message may itself be a vote)

        # bound: evaluate votes on the plan thread only
        if thread_id != self.thread_id:
            return
        content = message["content"]
        author = message["author"]

        # Stage 2: waiting for human after agent unanimity
        if self.await_human_after_agents and self.human_pending:
            if content.startswith("REJECT:"):
                self._reset_for_redo()
                return
            if author == "human" and content.startswith("APPROVE:"):
                self._open_gate(message)
            return

        if content.startswith(self.entry_prefix):
            if self.draft_author is None:
                self.draft_author = author
            elif self.approvals:
                # The draft changed under the voters' feet: whatever they
                # approved is not what would be submitted. Re-collect.
                self.approvals.clear()
            self._proposal_received = True
            # A PROPOSE may arrive AFTER everyone already voted (agents on a
            # pre-bound thread can APPROVE before any proposal exists). Without
            # this re-check the gate would never open, and duplicate-vote
            # soft-blocking removes the accidental re-APPROVE that used to
            # rescue it.
            self._maybe_open(message)
        elif content.startswith("REJECT:"):
            self._reset_for_redo()
        elif content.startswith("APPROVE:"):
            if author == "human":
                # Ignore human votes before agent unanimity (after_agents T5)
                return
            if not self.require_proposal:
                # P3+ (require_proposal=False): first APPROVE acts as the proposal
                self._proposal_received = True
            if self.require_proposal and not self.has_proposal:
                # Nothing to vote on yet. Counting these would let a draft
                # be submitted on approvals nobody gave it (M4a/D10).
                # No deadlock: the tool layer blocks this send, and
                # is_agent_done() keeps everyone awake until a draft lands.
                return
            if author in self.participants:
                self.approvals.add(author)
                self._maybe_open(message)

    def _reset_for_redo(self) -> None:
        """``REJECT:`` — votes AND the draft go; someone must propose again."""
        self.approvals.clear()
        self.human_pending = False
        self._proposal_received = False
        self.draft_author = None

    def _maybe_open(self, message: dict[str, Any]) -> None:
        """Open (or park for human) once every participant has approved."""
        if not self.participants:
            return
        if not (set(self.participants) <= self.approvals):
            return
        if self.require_proposal and not self.has_proposal:
            return
        if self.await_human_after_agents:
            if not self.human_pending:
                self.human_pending = True
                if self._on_human_pending:
                    self._on_human_pending()
        else:
            self._open_gate(message)

    def _open_gate(self, message: dict[str, Any]) -> None:
        self.human_pending = False
        self.opened_at_seq = message["seq"]
        if self._on_open:
            self._on_open()

    # -- views -----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Serializable gate state for checkpoints."""
        return {
            "thread_name": self.thread_name,
            "thread_id": self.thread_id,
            "approvals": sorted(self.approvals),
            "opened_at_seq": self.opened_at_seq,
            "proposal_received": self._proposal_received,
            "draft_author": self.draft_author,
            "require_proposal": self.require_proposal,
            "await_human_after_agents": self.await_human_after_agents,
            "human_pending": self.human_pending,
        }

    def restore_state(self, snap: dict[str, Any]) -> None:
        """Restore approvals / open flag after ``bind_to_thread`` (or with thread_id)."""
        tid = snap.get("thread_id")
        if tid and self.thread_id is None:
            self.bind_to_thread(str(tid))
        elif tid:
            self.thread_id = str(tid)
            thread = self._server.get_thread(str(tid))
            self.participants = list(thread["participants"])
        self.approvals = {str(a) for a in (snap.get("approvals") or [])}
        opened = snap.get("opened_at_seq")
        self.opened_at_seq = int(opened) if opened is not None else None
        self._proposal_received = bool(snap.get("proposal_received", False))
        da = snap.get("draft_author")
        self.draft_author = str(da) if da else None
        if "await_human_after_agents" in snap:
            self.await_human_after_agents = bool(snap["await_human_after_agents"])
        self.human_pending = bool(snap.get("human_pending", False))

    @property
    def is_open(self) -> bool:
        return self.opened_at_seq is not None

    def on_open(self, callback: GateCallback) -> None:
        """Register a callback invoked when the gate flips open."""
        self._on_open = callback

    def on_human_pending(self, callback: GateCallback) -> None:
        """Register a callback when agent unanimity parks for human approval."""
        self._on_human_pending = callback

    @property
    def has_proposal(self) -> bool:
        """True once a binding proposal message has been recorded.

        v0.2: uses _proposal_received so that bind_to_thread() alone
        (without a PROPOSE message) does not make this True.
        """
        return self._proposal_received
