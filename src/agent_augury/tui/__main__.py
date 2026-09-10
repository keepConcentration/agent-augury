"""Headless / demo entry for the session TUI package.

Usage::

    python -m agent_augury.tui

Exercises router + ChoicePanel + LogBuffer without a full Application.
"""

from __future__ import annotations

from agent_augury.tui.choice_panel import ChoicePanel
from agent_augury.tui.log_buffer import LogBuffer
from agent_augury.tui.router import RouterContext, route


def main() -> None:
    buf = LogBuffer()
    panel = ChoicePanel()
    panel.on_ask_user(
        "agent-1",
        "ask_user",
        {"thread": "thread-1", "question": "pick one?", "options": ["a", "b"]},
    )
    buf.append("demo: pending question loaded")
    print("active:", panel.active)
    print("route '1':", route("1", RouterContext(active_question=panel.active)))
    print("route '/help':", route("/help", RouterContext()))
    print("route '':", route("", RouterContext()))
    print("log export:\n", buf.export_tail())


if __name__ == "__main__":
    main()
