"""Default P1–P5 collaboration protocol for wizard / interactive sessions.

Participants default to all agents at Session assembly time.
No ``assembler_id`` / roles — phase gates only; collaboration inside
each phase is free-form.
"""

from __future__ import annotations

from typing import Any

# Gates only — Session fills participants from agent ids when omitted.
DEFAULT_PROTOCOL: dict[str, Any] = {
    "gates": {
        "P2_SPLIT": "plan",
        "P3_EXECUTE": "execution",
        "P4_REVIEW": "review",
        "P5_SUBMIT": "submission",
    },
}
