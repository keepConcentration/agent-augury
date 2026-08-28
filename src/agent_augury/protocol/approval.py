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
"""

from __future__ import annotations

from typing import Any, Callable

from ..server import MessageServer

GateCallback = Callable[[], None]


class ConsensusGate:
    """Watches messages via server subscription; flips open on unanimity."""

    def __init__(self, server: MessageServer, thread_name: str, *, require_proposal: bool = True, bind_prefixes: list[str] | None = None) -> None:
        self._server = server
        self.thread_name = thread_name
        self.thread_id: str | None = None
        self.participants: list[str] = []
        self.approvals: set[str] = set()
        self.require_proposal = require_proposal
        self.bind_prefixes = bind_prefixes or (["PROPOSE:"] if require_proposal else None)
        self.opened_at_seq: int | None = None
        self._bound: bool = False
        self._on_open: GateCallback | None = None
        # v0.2: separate flag tracking whether a PROPOSE message was actually received.
        # This prevents has_proposal from being True when bind_to_thread() is called
        # explicitly (e.g. during Session setup) before any PROPOSE arrives.
        self._proposal_received: bool = False

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
            # Bind based on bind_prefixes:
            # - None: any message to the matching thread binds it (require_proposal=False)
            # - list: only messages starting with one of the prefixes bind it
            if self.bind_prefixes is not None:
                if not any(message["content"].startswith(p) for p in self.bind_prefixes):
                    return  # waiting for a binding message
                # PROPOSE message binds AND marks proposal received
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
        if content.startswith("PROPOSE:"):
            self._proposal_received = True
        elif content.startswith("REJECT:"):
            self.approvals.clear()
        elif content.startswith("APPROVE:"):
            if not self.require_proposal:
                # P3+ (require_proposal=False): first APPROVE acts as the proposal
                # P2 (require_proposal=True): proposal must come from a real PROPOSE
                self._proposal_received = True
            if author in self.participants:
                self.approvals.add(author)
                if set(self.participants) <= self.approvals and (
                    not self.require_proposal or self.has_proposal
                ):
                    self.opened_at_seq = message["seq"]
                    if self._on_open:
                        self._on_open()

    # -- views -----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.opened_at_seq is not None

    def on_open(self, callback: GateCallback) -> None:
        """Register a callback invoked when the gate flips open."""
        self._on_open = callback

    @property
    def has_proposal(self) -> bool:
        """True once a binding proposal message has been recorded.

        v0.2: uses _proposal_received so that bind_to_thread() alone
        (without a PROPOSE message) does not make this True.
        """
        return self._proposal_received
