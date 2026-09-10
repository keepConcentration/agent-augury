"""Slash command + key alias smoke tests."""

from __future__ import annotations

from agent_augury.tui.commands import dispatch
from agent_augury.tui.key_aliases import install_tui_key_aliases


def test_dispatch_help():
    out = dispatch("help", "", {})
    assert "/quit" in out
    assert "Enter" in out
    assert "Ctrl+Enter" in out
    assert "Blank Enter is ignored" in out


def test_dispatch_unknown():
    out = dispatch("nope", "", {})
    assert "unknown command" in out


def test_dispatch_status():
    out = dispatch(
        "status",
        "",
        {
            "snapshot": {"threads": [1], "messages": [1, 2], "agents": ["a"]},
            "gate": None,
            "phase": "n/a",
        },
    )
    assert "threads=1" in out
    assert "msgs=2" in out


def test_install_key_aliases_idempotent():
    n1 = install_tui_key_aliases()
    n2 = install_tui_key_aliases()
    assert n1 >= 0
    assert n2 == 0
