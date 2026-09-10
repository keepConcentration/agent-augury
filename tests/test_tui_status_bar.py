"""StatusBar unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_augury.tui.status_bar import StatusBar


def test_status_bar_snapshot_line():
    session = MagicMock()
    session.server.snapshot.return_value = {
        "threads": [1, 2],
        "messages": [1, 2, 3],
        "agents": ["a", "b"],
    }
    session.gate = None
    session.protocol = None

    bar = StatusBar(session)
    line = bar._line()
    assert "threads=2" in line
    assert "msgs=3" in line
    assert "agents=2" in line
    assert "gate=n/a" in line
    assert "phase=n/a" in line


def test_status_bar_running_hint():
    session = MagicMock()
    session.server.snapshot.return_value = {
        "threads": [],
        "messages": [],
        "agents": [],
    }
    session.gate = None
    session.protocol = None
    bar = StatusBar(session)
    bar.set_running(True)
    assert "running" in bar._line()
    bar.set_running(False)
    assert "waiting" in bar._line() or "\U0001f464" in bar._line()
