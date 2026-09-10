"""SessionTUIApplication unit tests - no full TTY required.

Covers:
* (existing) quit/plain/blank input, ctrl-d, ask_user choice path
* (v1.0) initial_task_mode one-shot release + no _send on first input
* (v1.0) S2 log scroll: PgUp/PgDn/Alt+Up/Down adjust log_window.vertical_scroll
* (v1.0) _log_follow tail-follow behaviour
* (v1.0) choice panel dynamic height (_choice_height)
"""

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


def _make_tui(
    session: FakeSession | None = None,
    *,
    initial_task_mode: bool = False,
    tmp_path: Any = None,
) -> tuple[SessionTUIApplication, object]:
    """Build a TUI with PipeInput; returns (app, pipe_ctx)."""
    from pathlib import Path

    if session is None:
        session = FakeSession()
    if tmp_path is None:
        import tempfile

        tmp_path = Path(tempfile.mkdtemp())

    quit_calls: list[bool] = []
    next_turns: list[str] = []
    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    tui = SessionTUIApplication(
        session,
        history_file=tmp_path / "hist.txt",
        key_aliases=False,
        on_quit=lambda: quit_calls.append(True),
        on_next_turn=lambda t: next_turns.append(t),
        initial_task_mode=initial_task_mode,
        preserve_log_on_exit=False,
        pt_input=pipe,
        pt_output=DummyOutput(),
    )
    tui._quit_calls = quit_calls  # type: ignore[attr-defined]
    tui._next_turns = next_turns  # type: ignore[attr-defined]
    tui._fake_session = session  # type: ignore[attr-defined]
    return tui, pipe_ctx


@pytest.fixture
def tui(tmp_path):
    with create_pipe_input() as pipe:
        session = FakeSession()
        quit_calls: list[bool] = []
        next_turns: list[str] = []

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


# ---------------------------------------------------------------------------
# Existing behaviour (regression)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# v1.0: initial_task_mode — one-shot release (V1 / V9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_task_mode_first_input_triggers_next_turn_only(tmp_path):
    """V1: first plain input in initial_task_mode -> on_next_turn only, no _send."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, initial_task_mode=True, tmp_path=tmp_path)
    try:
        await tui.handle_input("hello")
        # _send must NOT be called (no thread yet) — human_send not invoked
        assert session.sent == []
        # on_next_turn triggered with the task
        assert tui._next_turns == ["hello"]
    finally:
        pipe_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_initial_task_mode_released_after_first_input(tmp_path):
    """V9 (v1.0.1): after first task, subsequent inputs use the normal _send path.

    The second input simulates a message typed *while the first turn is running*
    (set_running(True)): it must go through human_send (injected into the current
    turn) and must NOT trigger a new next_turn.
    """
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, initial_task_mode=True, tmp_path=tmp_path)
    try:
        await tui.handle_input("hello")
        assert tui._initial_task_mode is False  # released

        # Second input while RUNNING must go through human_send (injected)
        tui.set_running(True)
        await tui.handle_input("방향 바꿔줘")
        assert len(session.sent) == 1
        assert session.sent[0]["content"] == "방향 바꿔줘"
        assert tui._next_turns == ["hello"]  # only first input triggered next_turn
    finally:
        pipe_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_initial_task_mode_blank_keeps_waiting(tmp_path):
    """V2: blank input in initial_task_mode is ignored; mode stays active."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, initial_task_mode=True, tmp_path=tmp_path)
    try:
        await tui.handle_input("   ")
        assert session.sent == []
        assert tui._next_turns == []
        assert tui._initial_task_mode is True  # still waiting
    finally:
        pipe_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# v1.0: S2 log scroll (V4 / V4b)
# ---------------------------------------------------------------------------


def _kb_handler(tui: SessionTUIApplication, *want: object):
    """Find a global kb handler matching *want* keys."""
    kb = tui._build_app_kb()
    for b in kb.bindings:
        vals = {getattr(k, "value", str(k)) for k in b.keys}
        keys = set(b.keys) | vals
        if all(w in keys or getattr(w, "value", w) in vals for w in want) and len(b.keys) == len(want):
            return b.handler
    return None


def test_log_window_s2_single_source(tmp_path):
    """log_window uses get_cursor_position reading vertical_scroll directly (S2)."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        ctrl = tui.log_window.content
        # FormattedTextControl created with get_cursor_position callable
        gcp = getattr(ctrl, "get_cursor_position", None)
        assert gcp is not None
        # Point(x=0, y=self.log_window.vertical_scroll)
        tui.log_window.vertical_scroll = 7

        pt = gcp()
        assert pt.x == 0
        assert pt.y == 7
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_pgup_pgdn_scroll_log(tmp_path):
    """V4: PgUp/PgDn adjust vertical_scroll and toggle _log_follow."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(30):
            tui.log_buffer.append(f"line {i}")
        tui.log_window.vertical_scroll = 20

        pgup = _kb_handler(tui, Keys.PageUp) or _kb_handler(tui, "pageup")
        pgdn = _kb_handler(tui, Keys.PageDown) or _kb_handler(tui, "pagedown")
        assert pgup is not None
        assert pgdn is not None

        pgup(MagicMock())
        assert tui.log_window.vertical_scroll == 10  # 20 - 10
        assert tui._log_follow is False

        pgdn(MagicMock())
        assert tui.log_window.vertical_scroll == 20  # 10 + 10
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_alt_up_down_scroll_log_line(tmp_path):
    """V4b: Alt+Up/Down (escape+up/down) scroll by 1 line (1-A)."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(10):
            tui.log_buffer.append(f"line {i}")
        tui.log_window.vertical_scroll = 5

        alt_up = _kb_handler(tui, Keys.Escape, Keys.Up) or _kb_handler(tui, "escape", "up")
        alt_down = _kb_handler(tui, Keys.Escape, Keys.Down) or _kb_handler(tui, "escape", "down")
        assert alt_up is not None
        assert alt_down is not None

        alt_up(MagicMock())
        assert tui.log_window.vertical_scroll == 4
        assert tui._log_follow is False

        alt_down(MagicMock())
        assert tui.log_window.vertical_scroll == 5
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_follow_log_tail(tmp_path):
    """V8: _follow_log_tail pins to bottom when following; no-op when not."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(20):
            tui.log_buffer.append(f"line {i}")
        # follow=True → append_text keeps tail at bottom
        tui.append_text("tail line")
        assert tui.log_window.vertical_scroll >= tui.log_buffer.line_count() - 2

        # follow=False → append_text does not move scroll
        tui.log_window.vertical_scroll = 3
        tui._log_follow = False
        tui.append_text("off-follow line")
        assert tui.log_window.vertical_scroll == 3
    finally:
        pipe_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# v1.0: choice panel dynamic height (V3)
# ---------------------------------------------------------------------------


def test_choice_height_dynamic(tmp_path):
    """_choice_height uses panel.line_count with min=1, max=8."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {"thread": "thread-1", "question": "DB?", "options": ["pg", "mysql"]},
        )
        dim = tui._choice_height()
        assert dim.min == 1
        assert dim.max == 8
        assert dim.preferred == 3  # question 1 + options 2

        # Many options → capped at 8
        tui.choice_panel.reset()
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {
                "thread": "thread-1",
                "question": "DB?",
                "options": [f"o{i}" for i in range(1, 20)],
            },
        )
        dim2 = tui._choice_height()
        assert dim2.preferred == 8
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_layout_uses_dynamic_height_and_wrap(tmp_path):
    """Layout wires the choice panel with dynamic height + wrap_lines=True."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {"thread": "thread-1", "question": "Q?", "options": ["a", "b"]},
        )
        layout = tui._build_layout()
        # HSplit children: [log_window, ConditionalContainer(choice), status, input]
        container = layout.children[1]
        assert hasattr(container, "content")
        choice_window = container.content
        # Window.wrap_lines is a Filter (to_filter(True)) — call it to check.
        assert choice_window.wrap_lines() is True
        # height callable is the dynamic height fn
        assert callable(choice_window.height)
    finally:
        pipe_ctx.__exit__(None, None, None)
