"""Protocol: consensus gate + P1~P5 collaboration protocol (DESIGN.md §2.3)."""

from .approval import ConsensusGate
from .collaboration import CollaborationProtocol
from .phases import (
    APPROVED,
    COMPLETED,
    OPEN,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    PROPOSED,
    REJECTED,
    Phase,
    PhaseManager,
)

__all__ = [
    "APPROVED",
    "COMPLETED",
    "OPEN",
    "P1_EXPLORE",
    "P2_SPLIT",
    "P3_EXECUTE",
    "P4_REVIEW",
    "P5_SUBMIT",
    "PROPOSED",
    "REJECTED",
    "CollaborationProtocol",
    "ConsensusGate",
    "Phase",
    "PhaseManager",
]
