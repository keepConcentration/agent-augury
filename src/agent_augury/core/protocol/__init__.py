"""Protocol: consensus gate + P1~P5 collaboration protocol (DESIGN.md §2.3)."""

from .approval import ConsensusGate
from .collaboration import CollaborationProtocol
from .defaults import DEFAULT_PROTOCOL
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
from .signals import is_ready_message

__all__ = [
    "APPROVED",
    "COMPLETED",
    "DEFAULT_PROTOCOL",
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
    "is_ready_message",
]
