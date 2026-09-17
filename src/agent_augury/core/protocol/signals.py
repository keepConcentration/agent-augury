"""Protocol signal matching (READY / PROPOSE / APPROVE / REJECT / FINAL).

Models wrap the signal in markdown or brackets — ``**FINAL: ...**``,
``[FINAL: ...]``, ``# APPROVE:`` — so a literal ``startswith`` silently drops
the message. Measured on real sessions: ``FINAL:`` missed 2 of 12 that way,
while the short one-line signals matched every time.

Everything that decides "is this a signal?" MUST go through :func:`has_signal`,
gate and soft-block alike: if the two disagree, an agent gets blocked for a
message the gate then ignores.
"""

from __future__ import annotations

# Leading noise that still means "this is my signal".
# NOT stripped: ``>`` (blockquote) and quote marks — those usually mean the
# agent is citing someone else's message, not making its own claim.
_DECORATION = " \t\r\n*_[(#-`"


def strip_decoration(content: str) -> str:
    """Drop leading markdown / bracket noise before matching a prefix."""
    return (content or "").lstrip(_DECORATION)


def has_signal(content: str, prefix: str) -> bool:
    """True when ``content`` opens with ``prefix`` (decoration and case ignored).

    ``prefix`` carries its own colon (``"APPROVE:"``), so lookalikes without one
    (``APPROVED``, bare ``READY``) do not match.
    """
    if not prefix:
        return False
    return strip_decoration(content).upper().startswith(prefix.upper())


def is_ready_message(content: str) -> bool:
    """True if ``content`` is a P1 READY signal.

    Accepts ``READY:`` with optional trailing text, ignoring surrounding
    whitespace, decoration and letter case. Rejects lookalikes without the
    colon (``READYFOO``, bare ``READY``).
    """
    return has_signal(content, "READY:")


# Every prefix the protocol reads. Order is irrelevant; each carries its colon.
SIGNAL_PREFIXES = (
    "READY:",
    "PROPOSE:",
    "APPROVE:",
    "REJECT:",
    "FINAL:",
    "RESULT:",
)


def misplaced_signal(content: str) -> str | None:
    """The signal an agent meant to send but buried below the first line.

    Only the opening of a message is a signal -- a prefix further down is
    usually a citation ("waiting for agent-3's FINAL:"), so the gate must not
    count it. But agents also append their vote to the end of a report, and
    then believe they voted: measured over 470 real messages, a signal
    *opening a later line* was an intended signal **4 times out of 4**, while
    every citation sat mid-line. One of those four stalled P4 at 3/4 until a
    human intervened (`dc79a366` seq 19).

    Returns the prefix so the caller can tell the agent to move it up. This
    does NOT make the message a signal -- the gate still reads line one.
    """
    lines = (content or "").splitlines() or [""]
    if any(has_signal(lines[0], p) for p in SIGNAL_PREFIXES):
        return None                      # already a proper signal
    for line in lines[1:]:
        for prefix in SIGNAL_PREFIXES:
            if has_signal(line, prefix):
                return prefix
    return None
