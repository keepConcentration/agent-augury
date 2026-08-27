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

    # -- explicit binding (for pre-created threads) -------------------------

    def bind_to_thread(self, thread_id: str) -> None:
        """Explicitly bind this gate to a pre-created thread.

        Used by Session to bind gates to threads before the protocol starts,
        so the gate is ready when the phase begins.
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
            self.thread_id = thread_id
            self.participants = list(thread["participants"])
            # Fall through to evaluate this message for APPROVE/REJECT
            # (don't return — the first message may itself be a vote)

        # bound: evaluate votes on the plan thread only
        if thread_id != self.thread_id:
            return
        content = message["content"]
        author = message["author"]
        if content.startswith("REJECT:"):
            self.approvals.clear()
        elif content.startswith("APPROVE:") and self.has_proposal:
            if author in self.participants:
                self.approvals.add(author)
                if set(self.participants) <= self.approvals:
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
        """True once a binding proposal message has been recorded."""
        if self.bind_prefixes is None:
            # require_proposal=False → bound by any message; proposal state
            # is tracked by whether we've seen any message on the thread
            return self.thread_id is not None
        # require_proposal=True → check if any message started with a prefix
        # The gate binds on the first PROPOSE: message, so thread_id != None
        # means we've seen a proposal.
        return self.thread_id is not None
