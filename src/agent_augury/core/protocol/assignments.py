"""Parse the split agreed in P2 so later phases can remind each agent of it.

Live gap (session d24695b5): P2 agreed "agent-3 argues the O position", P3 never
checked, and agent-3 never argued it. Three agents wrote X, the team noted the
missing side itself, and the user's request ("persuade each other") never
happened. Nothing in the runtime knew about the assignment.

Free-text proposals cannot be read without NLP (a stated non-goal), so the
proposal carries explicit lines:

    PROPOSE: 분할안
    ASSIGN agent-1: 공리주의 관점
    ASSIGN agent-3: O 입장 옹호   <- the side nobody took
    SUBMITTER: agent-2

    # Or, when no split is needed (PHASE_MACHINE_ROUTING §2.2):
    PROPOSE:
    SPLIT: none
    SUBMITTER: agent-3

Parsing is deliberately strict: an agent id that is not a participant is
dropped rather than guessed at.
"""

from __future__ import annotations

import re

# Leading list/markdown noise is tolerated, same spirit as signals.has_signal.
_LEAD = r"^[ \t]*[-*#>•]*[ \t]*"
_ID = r"([A-Za-z0-9_.\-]+)"

_ASSIGN_RE = re.compile(_LEAD + r"ASSIGN[ \t]+" + _ID + r"[ \t]*[:=][ \t]*(.+?)[ \t]*$",
                        re.IGNORECASE | re.MULTILINE)
# The id is rarely alone on the line: "SUBMITTER: agent-4 (verify then submit)"
# was the first real one seen, and anchoring to end-of-line dropped it.
_SUBMITTER_RE = re.compile(_LEAD + r"SUBMITTER[ 	]*[:=][ 	]*(.+)$",
                           re.IGNORECASE | re.MULTILINE)
# `SPLIT: none` is short enough that models write it ON the PROPOSE: line
# ("PROPOSE: SPLIT: none"), unlike ASSIGN/SUBMITTER which they put on their
# own lines. Line-anchoring alone missed the winning draft in the first
# live run (session log 2026-09-18).
_SPLIT_RE = re.compile(
    _LEAD + r"(?:PROPOSE[ \t]*[:=][ \t]*)?SPLIT[ \t]*[:=][ \t]*(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Strip the decoration models wrap a share in (**bold**, `code`, trailing dots).
_TRIM = " \t*_`\"'.,;"


def parse_assignments(content: str, participants: list[str]) -> dict[str, str]:
    """Return ``{agent_id: share}`` for participants named by ``ASSIGN`` lines."""
    known = set(participants)
    out: dict[str, str] = {}
    for agent_id, share in _ASSIGN_RE.findall(content or ""):
        share = share.strip(_TRIM)
        if agent_id in known and share:
            out[agent_id] = share
    return out


def parse_submitter(content: str, participants: list[str]) -> str | None:
    """Return the agent elected to post the P5 draft, if named.

    Takes the participant mentioned earliest on the line, so trailing
    prose and light phrasing both resolve.
    """
    for rest in _SUBMITTER_RE.findall(content or ""):
        best = None
        for agent_id in participants:
            at = rest.find(agent_id)
            if at >= 0 and (best is None or at < best[0]):
                best = (at, agent_id)
        if best:
            return best[1]
    return None


def parse_split(content: str) -> bool:
    """True when the proposal declares ``SPLIT: none`` (any decoration/case).

    Only the literal token ``none`` counts — other values are ignored so a
    free-text ``SPLIT: into three parts`` cannot skip P3/P4.
    """
    for value in _SPLIT_RE.findall(content or ""):
        if value.strip(_TRIM).lower() == "none":
            return True
    return False
