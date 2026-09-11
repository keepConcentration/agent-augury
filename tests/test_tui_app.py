"""SessionTUIApplication unit tests - no full TTY required.

Covers:
* quit/plain/blank input, ctrl-d, ask_user choice path
* initial_task_mode one-shot release
* v1.5 Static bottom dock (no ScrollablePane); batched terminal log streaming
* choice panel option scrolling + ask_user log backup
* Ctrl+C interrupt / double-tap quit
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
# v1.5: Static terminal logs + bottom chrome (choice | input | status)
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


def test_bottom_chrome_layout(tmp_path):
    """v1.5: layout = choice? + input + status; no ScrollablePane / log window."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        from prompt_toolkit.layout import to_container
        from prompt_toolkit.layout.controls import FormattedTextControl

        assert tui.app.full_screen is False
        mouse = tui.app.mouse_support
        assert mouse() is False if callable(mouse) else not mouse
        assert not hasattr(tui, "scrollable")
        assert hasattr(tui, "static_log")

        layout = tui._build_layout()
        assert len(layout.children) == 3
        choice_container = layout.children[0]
        assert choice_container.content is tui.choice_window
        assert tui.choice_window.content.text == tui.choice_panel.render
        assert layout.children[1] is to_container(tui.input_bar.widget)
        status_window = layout.children[2]
        assert isinstance(status_window.content, FormattedTextControl)
        assert status_window.content.text == tui.status_bar._line
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_emit_log_buffers_and_writes(tmp_path, monkeypatch):
    """v1.5: append_text stores in log_buffer and enqueues Static writer."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    writes: list[str] = []
    monkeypatch.setattr(tui.static_log, "_write_fn", lambda p: writes.append(p))
    try:
        tui.append_text("hello log")
        assert "hello log" in tui.log_buffer.export_tail()
        assert writes == ["hello log\n"]
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_static_log_batches_manual_flush():
    """enqueue buffers until flush_now; one write contains all lines."""
    from agent_augury.tui.static_log import StaticLogWriter

    class _App:
        is_running = True

        def create_background_task(self, coro):
            # Swallow the delayed-flush coroutine without running it.
            try:
                coro.close()
            except Exception:  # noqa: BLE001, S110
                pass
            return MagicMock()

    writes: list[str] = []
    writer = StaticLogWriter(
        _App(),
        flush_interval=10.0,
        write_fn=lambda p: writes.append(p),
    )
    writer.enqueue("line1")
    writer.enqueue("line2")
    assert writes == []
    # flush_now would schedule _write_in_terminal; set not running for sync write:
    writer._app.is_running = False  # type: ignore[attr-defined]
    writer.flush_now()
    assert writes == ["line1\nline2\n"]


def test_static_log_atomic_paint_when_running(monkeypatch):
    """While running, flush uses atomic paint (not stock erase() mid-flush)."""
    from agent_augury.tui import static_log as sl

    calls: list[str] = []
    pending: list[object] = []

    class _Cursor:
        x = 0
        y = 2

    class _Output:
        responds_to_cpr = False

        def cursor_backward(self, _n):
            calls.append("cursor_backward")

        def cursor_up(self, _n):
            calls.append("cursor_up")

        def erase_down(self):
            calls.append("erase_down")

        def reset_attributes(self):
            calls.append("reset_attributes")

        def hide_cursor(self):
            calls.append("hide_cursor")

        def show_cursor(self):
            calls.append("show_cursor")

        def flush(self):
            calls.append("flush")

    class _Renderer:
        _cursor_pos = _Cursor()

        def reset(self):
            calls.append("reset")

        def erase(self):
            calls.append("erase")  # should NOT be used on happy path

    class _App:
        is_running = True
        _is_running = True
        _running_in_terminal = False
        _running_in_terminal_f = None
        output = _Output()
        renderer = _Renderer()

        def create_background_task(self, coro):
            pending.append(coro)
            calls.append("bg")
            return MagicMock()

        def _request_absolute_cursor_position(self):
            calls.append("cpr")

        def _redraw(self):
            calls.append("redraw")

    monkeypatch.setattr(sl, "get_app_or_none", lambda: None)
    writes: list[str] = []
    writer = sl.StaticLogWriter(_App(), write_fn=lambda p: writes.append(p))
    writer._buf.append("x\n")
    writer._buf_chars = 2
    writer.flush_now()

    import asyncio

    async def _run_pending():
        for c in pending:
            if asyncio.iscoroutine(c):
                await c

    asyncio.run(_run_pending())
    assert "erase_down" in calls
    assert "redraw" in calls
    assert "erase" not in calls  # stock erase() avoided
    assert writes == ["x\n"]


def test_status_bar_follow_indicator(tmp_path):
    """Status bar still exposes FOLLOW/SCROLL for compatibility."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        line = tui.status_bar._line()
        assert "FOLLOW" in line

        tui._set_log_follow(False)
        line2 = tui.status_bar._line()
        assert "SCROLL" in line2
    finally:
        pipe_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_user_input_still_logged(tui):
    """User input content is still logged (✓ human → ...)."""
    await tui.handle_input("hello world")
    exported = tui.log_buffer.export_tail()
    assert "✓ human → thread-1" in exported
    assert "hello world" in exported


def _render_text(tui: SessionTUIApplication) -> str:
    """Flatten FormattedText to a plain string for assertions."""
    parts = []
    for style, text in tui.choice_panel.render():
        parts.append(text)
    return "".join(parts)


def test_choice_panel_scroll_offset_for_many_options(tmp_path):
    """10 options → panel caps at 8 lines, scroll reveals the rest."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {
                "thread": "thread-1",
                "question": "Q?",
                "options": [f"option {i}" for i in range(1, 11)],
            },
        )
        assert tui.choice_panel.line_count(8) == 8
        assert tui.choice_panel.scroll_offset == 0

        rendered = _render_text(tui)
        assert "옵션 10개 중 1~7 표시" in rendered

        tui.choice_panel.scroll_down(8)
        assert tui.choice_panel.scroll_offset == 3
        rendered2 = _render_text(tui)
        assert "[4]" in rendered2
        assert "옵션 10개 중 4~10 표시" in rendered2

        tui.choice_panel.scroll_up(8)
        assert tui.choice_panel.scroll_offset == 2
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_choice_panel_pgdn_scrolls_options(tmp_path):
    """With a pending question, PgDn scrolls choice options."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {
                "thread": "thread-1",
                "question": "Q?",
                "options": [f"option {i}" for i in range(1, 11)],
            },
        )
        pgdn = _kb_handler(tui, Keys.PageDown) or _kb_handler(tui, "pagedown")
        assert pgdn is not None
        pgdn(MagicMock())
        assert tui.choice_panel.scroll_offset == 3
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_ask_user_log_backup(tmp_path):
    """on_ask_user backs up full question+options to the log buffer."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {
                "thread": "thread-1",
                "question": "DB?",
                "options": ["pg", "mysql", "sqlite", "mongo"],
            },
        )
        exported = tui.log_buffer.export_tail()
        assert "❓ [agent-1] DB?" in exported
        assert "[1] pg" in exported
        assert "[2] mysql" in exported
        assert "[4] mongo" in exported
    finally:
        pipe_ctx.__exit__(None, None, None)


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
        assert dim.preferred == 3

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
    """Choice window uses dynamic height + wrap_lines=True."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.on_ask_user(
            "agent-1",
            "ask_user",
            {"thread": "thread-1", "question": "Q?", "options": ["a", "b"]},
        )
        layout = tui._build_layout()
        choice_container = layout.children[0]
        choice_window = choice_container.content
        assert choice_window.wrap_lines() is True
        assert callable(choice_window.height)
    finally:
        pipe_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Ctrl+C: interrupt run vs double-tap quit
# ---------------------------------------------------------------------------


def test_ctrl_c_while_running_calls_interrupt_not_quit(tmp_path):
    """1st Ctrl+C during run → on_interrupt only; TUI stays open for resume."""
    session = FakeSession()
    interrupts: list[bool] = []
    quits: list[bool] = []
    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    try:
        tui = SessionTUIApplication(
            session,
            history_file=tmp_path / "hist.txt",
            key_aliases=False,
            on_quit=lambda: quits.append(True),
            on_next_turn=lambda _t: None,
            on_interrupt=lambda: interrupts.append(True),
            preserve_log_on_exit=False,
            pt_input=pipe,
            pt_output=DummyOutput(),
        )
        tui.set_running(True)
        tui._handle_ctrl_c()
        assert interrupts == [True]
        assert quits == []
        exported = tui.log_buffer.export_tail()
        assert "Agents stopped" in exported
        assert "Ctrl+C again" in exported
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_ctrl_c_twice_while_running_quits(tmp_path):
    """2nd Ctrl+C within 1s while running → interrupt + quit."""
    session = FakeSession()
    interrupts: list[bool] = []
    quits: list[bool] = []
    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    try:
        tui = SessionTUIApplication(
            session,
            history_file=tmp_path / "hist.txt",
            key_aliases=False,
            on_quit=lambda: quits.append(True),
            on_next_turn=lambda _t: None,
            on_interrupt=lambda: interrupts.append(True),
            preserve_log_on_exit=False,
            pt_input=pipe,
            pt_output=DummyOutput(),
        )
        tui.set_running(True)
        tui._handle_ctrl_c()
        tui._handle_ctrl_c()
        assert interrupts == [True, True]
        assert quits == [True]
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_ctrl_c_idle_still_double_tap_quit(tmp_path):
    """Idle: 1st Ctrl+C warns; 2nd quits — no on_interrupt."""
    session = FakeSession()
    interrupts: list[bool] = []
    quits: list[bool] = []
    pipe_ctx = create_pipe_input()
    pipe = pipe_ctx.__enter__()
    try:
        tui = SessionTUIApplication(
            session,
            history_file=tmp_path / "hist.txt",
            key_aliases=False,
            on_quit=lambda: quits.append(True),
            on_next_turn=lambda _t: None,
            on_interrupt=lambda: interrupts.append(True),
            preserve_log_on_exit=False,
            pt_input=pipe,
            pt_output=DummyOutput(),
        )
        tui.set_running(False)
        tui._handle_ctrl_c()
        assert interrupts == []
        assert quits == []
        tui._handle_ctrl_c()
        assert quits == [True]
        assert interrupts == []
    finally:
        pipe_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_message_while_interrupted_triggers_next_turn(tmp_path):
    """After interrupt (not running), a plain message resumes via on_next_turn."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        tui.set_running(False)
        await tui.handle_input("resume please")
        assert tui._next_turns == ["resume please"]
        assert len(session.sent) == 1
    finally:
        pipe_ctx.__exit__(None, None, None)
