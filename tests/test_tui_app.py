"""SessionTUIApplication unit tests - no full TTY required."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from agent_augury.tui.app import SessionTUIApplication
from agent_augury.tui.input_bar import InputBar


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.gate = None
        self.protocol = None
        self._server = MagicMock()
        self._server.snapshot.return_value = {
            "threads": [{"thread_id": "thread-1", "name": "work"}],
            "messages": [],
            "agents": ["agent-1"],
        }

    @property
    def server(self) -> Any:
        return self._server

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
    ) -> str:
        self.sent.append(
            {"thread_id": thread_id, "content": content, "mentions": mentions}
        )
        return "msg-1"


@pytest.fixture
def tui(tmp_path):
    session = FakeSession()
    quit_calls: list[bool] = []
    next_turns: list[str] = []

    with create_pipe_input() as pipe:
        app = SessionTUIApplication(
            session,
            history_file=tmp_path / "hist.txt",
            key_aliases=False,
            on_quit=lambda: quit_calls.append(True),
            on_next_turn=lambda t: next_turns.append(t),
            preserve_log_on_exit=False,
            pt_input=pipe,
            pt_output=DummyOutput(),
        )
        app._quit_calls = quit_calls  # type: ignore[attr-defined]
        app._next_turns = next_turns  # type: ignore[attr-defined]
        app._fake_session = session  # type: ignore[attr-defined]
        yield app


@pytest.mark.asyncio
async def test_handle_input_quit_calls_on_quit(tui):
    await tui.handle_input("/quit")
    assert tui._quit_calls == [True]


@pytest.mark.asyncio
async def test_handle_input_plain_sends_and_next_turn(tui):
    await tui.handle_input("hello")
    assert len(tui._fake_session.sent) == 1
    assert tui._fake_session.sent[0]["content"] == "hello"
    assert tui._next_turns == ["hello"]
    exported = tui.log_buffer.export_tail()
    assert "human" in exported
    assert "thread-1" in exported


@pytest.mark.asyncio
async def test_handle_input_blank_ignored(tui):
    await tui.handle_input("   ")
    assert tui._fake_session.sent == []
    assert tui._next_turns == []
    assert tui._quit_calls == []


def test_ctrl_d_binding_invokes_on_quit(tmp_path):
    """P0 regression: InputBar c-d must call on_quit before app.exit()."""
    called: list[bool] = []

    class _App:
        def __init__(self) -> None:
            self.exited = False

        def exit(self) -> None:
            self.exited = True

    bar = InputBar(
        lambda _t: None,
        on_quit=lambda: called.append(True),
        history=tmp_path / "h.txt",
    )
    fake_app = _App()
    event = MagicMock()
    event.app = fake_app

    handlers = [
        b.handler
        for b in bar._kb.bindings
        if Keys.ControlD in b.keys
        or "c-d" in {getattr(k, "value", str(k)) for k in b.keys}
    ]
    assert handlers, "c-d binding missing"
    handlers[0](event)

    assert called == [True]
    assert fake_app.exited is True


@pytest.mark.asyncio
async def test_ask_user_choice_path(tui):
    tui.on_ask_user(
        "agent-1",
        "ask_user",
        {"thread": "thread-1", "question": "DB?", "options": ["pg", "mysql"]},
    )
    assert tui.choice_panel.has_pending_bool()
    await tui.handle_input("1")
    assert tui._fake_session.sent[0]["content"] == "pg"
    assert tui._fake_session.sent[0]["mentions"] == ["agent-1"]
    assert not tui.choice_panel.has_pending_bool()
    assert tui._next_turns == []
