"""Full-screen session TUI (prompt_toolkit Application).

Design: docs/tui/SESSION_TUI_REDESIGN.md (v2.5).
"""

from __future__ import annotations

from .choice_panel import ChoicePanel, PendingQuestion
from .router import RouterContext, RouteResult, route

__all__ = [
    "ChoicePanel",
    "PendingQuestion",
    "RouteResult",
    "RouterContext",
    "SessionTUIApplication",
    "route",
]


def __getattr__(name: str):
    if name == "SessionTUIApplication":
        from .app import SessionTUIApplication

        return SessionTUIApplication
    raise AttributeError(name)
