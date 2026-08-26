"""Minimal collaboration protocol: the consensus gate (DESIGN.md §6 v0.1b).

Conventions:
- ``PROPOSE:`` — opens/updates a proposal; approvals count only afterwards.
- ``APPROVE:`` — one vote per participant.
- ``REJECT:``  — clears collected approvals; consensus must re-form.

The gate binds to the FIRST thread whose name matches ``thread_name``
(threads carry their name in the server, so the gate keeps a server
reference to resolve it at bind time).
"""

from __future__ import annotations

from typing import Any

from ..server import MessageServer


class ConsensusGate:
    """Watches messages via server subscription; flips open on unanimity."""

    def __init__(self, server: MessageServer, thread_name: str) -> None:
        self._server = server
        self.thread_name = thread_name
        self.thread_id: str | None = None
        self.participants: list[str] = []
        self.approvals: set[str] = set()
        self.has_proposal = False
        self.opened_at_seq: int | None = None

    # -- subscription entrypoint ---------------------------------------------

    def on_message(self, message: dict[str, Any]) -> None:
        if self.opened_at_seq is not None:
            return  # gate already opened — protocol phase is over

        thread_id = message["thread_id"]

        # not yet bound: only a PROPOSE on a matching (first) thread binds us
        if self.thread_id is None:
            if not message["content"].startswith("PROPOSE:"):
                return
            try:
                thread = self._server.get_thread(thread_id)
            except KeyError:
                return
            if thread["name"] != self.thread_name:
                return
            self.thread_id = thread_id
            self.participants = list(thread["participants"])
            self.has_proposal = True
            return

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

    # -- views -----------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.opened_at_seq is not None
