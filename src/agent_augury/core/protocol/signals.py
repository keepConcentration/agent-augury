"""Protocol signal matching helpers (READY / APPROVE prefixes)."""

from __future__ import annotations


def is_ready_message(content: str) -> bool:
    """True if ``content`` is a P1 READY signal.

    Accepts ``READY:`` with optional trailing text after the colon, and
    ignores surrounding whitespace / letter case.

    Rejects lookalikes without the colon (``READYFOO``, ``READY``).
    """
    text = (content or "").strip()
    return text.upper().startswith("READY:")
