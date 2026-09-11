"""SessionTUIApplication unit tests - no full TTY required.

Covers:
* (existing) quit/plain/blank input, ctrl-d, ask_user choice path
* (v1.0) initial_task_mode one-shot release + no _send on first input
* (v1.0) S2 log scroll: PgUp/PgDn/Alt+Up/Down adjust scroll position
* (v1.0) _log_follow tail-follow behaviour
* (v1.0) choice panel dynamic height (_choice_height)
* (v1.1) mouse wheel scroll-up/down toggles _log_follow (TUI_UX_FIX_DESIGN.md ②)
* (v1.1) F key toggles follow; status bar FOLLOW/SCROLL indicator (②)
* (v1.1) choice panel option-area scrolling with indicator (③)
* (v1.1) on_ask_user log backup of full question+options (③)
* (v1.2) ScrollablePane layout: log + input scroll together (TUI_SCROLLABLE_INPUT_DESIGN.md)
* (v1.2) keep_cursor_visible=False / keep_focused_window_visible=False (P8 — 입력창 강제 고정 제거)
* (v1.2) _follow_log_tail sets scrollable.vertical_scroll to a huge value (clamped on render)
* (v1.2) typing (on_text_changed) restores follow; submit restores follow
* (v1.2) user input still logged (✓ human → ...)
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
# v1.2: ScrollablePane layout (TUI_SCROLLABLE_INPUT_DESIGN.md)
# ---------------------------------------------------------------------------


def test_scrollable_pane_layout(tmp_path):
    """v1.2: layout = ScrollablePane(log+input) + fixed choice/status.

    P8: keep_cursor_visible=False / keep_focused_window_visible=False —
    ScrollablePane의 입력창 강제 가시화를 꺼서 "입력창 고정/3줄만 보임"을 방지.
    """
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        from prompt_toolkit.layout import HSplit, ScrollablePane, to_container
        from prompt_toolkit.layout.controls import FormattedTextControl

        layout = tui._build_layout()
        # HSplit children: [scrollable, ConditionalContainer(choice), Window(status)]
        assert len(layout.children) == 3
        scrollable = layout.children[0]
        assert isinstance(scrollable, ScrollablePane)
        # P8: 입력창 강제 가시화 OFF
        assert scrollable.keep_cursor_visible() is False
        assert scrollable.keep_focused_window_visible() is False
        # ScrollablePane content = HSplit(log_window, input_bar window)
        content = scrollable.content
        assert isinstance(content, HSplit)
        assert len(content.children) == 2
        assert content.children[0] is tui.log_window
        # TextArea wraps its internal Window; layout holds the Window.
        assert content.children[1] is to_container(tui.input_bar.widget)
        # choice panel + status bar are OUTSIDE (fixed) — control wired
        choice_container = layout.children[1]
        choice_window = choice_container.content
        assert isinstance(choice_window.content, FormattedTextControl)
        # control의 text 콜백이 panel.render와 같은지 (동작 검증, is 비교 대체)
        assert choice_window.content.text == tui.choice_panel.render
        status_window = layout.children[2]
        assert isinstance(status_window.content, FormattedTextControl)
        assert status_window.content.text == tui.status_bar._line
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_follow_tail_uses_scrollable_huge_value(tmp_path):
    """v1.2: _follow_log_tail sets scrollable.vertical_scroll to a huge value."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(20):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 5
        tui._set_log_follow(True)
        tui._follow_log_tail()
        # huge value set → clamped to max on next render (agent-2)
        assert tui.scrollable.vertical_scroll == tui._FOLLOW_MAX_SCROLL
    finally:
        pipe_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# v1.0: S2 log scroll (V4 / V4b) — now via ScrollablePane
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
    """V4: PgUp/PgDn adjust scrollable.vertical_scroll and disable follow."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(30):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 20

        pgup = _kb_handler(tui, Keys.PageUp) or _kb_handler(tui, "pageup")
        pgdn = _kb_handler(tui, Keys.PageDown) or _kb_handler(tui, "pagedown")
        assert pgup is not None
        assert pgdn is not None

        pgup(MagicMock())
        assert tui.scrollable.vertical_scroll == 10  # 20 - 10
        assert tui._log_follow is False

        pgdn(MagicMock())
        assert tui.scrollable.vertical_scroll == 20  # 10 + 10
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_alt_up_down_scroll_log_line(tmp_path):
    """V4b: Alt+Up/Down (escape+up/down) scroll by 1 line (1-A)."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(10):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 5

        alt_up = _kb_handler(tui, Keys.Escape, Keys.Up) or _kb_handler(tui, "escape", "up")
        alt_down = _kb_handler(tui, Keys.Escape, Keys.Down) or _kb_handler(tui, "escape", "down")
        assert alt_up is not None
        assert alt_down is not None

        alt_up(MagicMock())
        assert tui.scrollable.vertical_scroll == 4
        assert tui._log_follow is False

        alt_down(MagicMock())
        assert tui.scrollable.vertical_scroll == 5
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_follow_log_tail(tmp_path):
    """V8: _follow_log_tail pins to bottom when following; no-op when not.

    v1.2: follow=True → scrollable.vertical_scroll = huge (clamped on render).
    follow=False → scroll position untouched.
    """
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(20):
            tui.log_buffer.append(f"line {i}")

        # follow=True → _follow_log_tail sets huge value
        tui.scrollable.vertical_scroll = 3
        tui._set_log_follow(True)
        tui.append_text("tail line")
        assert tui.scrollable.vertical_scroll == tui._FOLLOW_MAX_SCROLL

        # follow=False → append_text does not move scroll
        tui.scrollable.vertical_scroll = 3
        tui._set_log_follow(False)
        tui.append_text("off-follow line")
        assert tui.scrollable.vertical_scroll == 3
    finally:
        pipe_ctx.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# v1.1 ②: mouse wheel follow toggle + F key + status indicator (v1.2 scrollable)
# ---------------------------------------------------------------------------


def test_wheel_up_disables_follow(tmp_path):
    """v1.1: scroll-up handler must disable _log_follow (no jump on next output)."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(30):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 20

        wheel_up = _kb_handler(tui, Keys.ScrollUp) or _kb_handler(tui, "<scroll-up>")
        assert wheel_up is not None

        wheel_up(MagicMock())
        # scrolled up by 3 → 17, follow disabled
        assert tui.scrollable.vertical_scroll == 17
        assert tui._log_follow is False
        # status bar mirrored
        assert tui.status_bar._log_follow is False
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_wheel_down_increases_scroll_keeps_scroll_state(tmp_path):
    """v1.1/v1.2: scroll-down increases scrollable position; follow stays off.

    v1.2 policy (TUI_SCROLLABLE_INPUT_DESIGN.md §5.3): follow restoration is
    explicit (typing hook / submit); wheel scroll just moves the position.
    """
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(30):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 5
        tui._set_log_follow(False)

        wheel_down = _kb_handler(tui, Keys.ScrollDown) or _kb_handler(tui, "<scroll-down>")
        assert wheel_down is not None

        wheel_down(MagicMock())
        assert tui.scrollable.vertical_scroll == 8  # 5 + 3
        assert tui._log_follow is False  # SCROLL state kept
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_f_key_toggles_follow(tmp_path):
    """v1.1: F key toggles _log_follow and mirrors to status bar."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        toggle = _kb_handler(tui, "f")
        assert toggle is not None

        assert tui._log_follow is True
        toggle(MagicMock())
        assert tui._log_follow is False
        assert tui.status_bar._log_follow is False

        toggle(MagicMock())
        assert tui._log_follow is True
        assert tui.status_bar._log_follow is True
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_status_bar_follow_indicator(tmp_path):
    """v1.1: status bar line shows FOLLOW/SCROLL based on follow state."""
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


# ---------------------------------------------------------------------------
# v1.2: typing / submit restore follow + user input still logged
# ---------------------------------------------------------------------------


def test_typing_restores_follow(tmp_path):
    """v1.2: typing (on_text_changed → _on_typing) restores follow (P8 대체).

    bound method의 `is` 비교는 접근마다 새 객체가 생성되어 실패하므로,
    동작 검증으로 확인한다 (agent-4 제안).
    """
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(20):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 5
        tui._set_log_follow(False)

        # on_text_changed 훅이 실제로 follow를 복귀시키는지 동작 검증
        assert tui._log_follow is False
        tui.input_bar._handle_text_changed("hello")
        assert tui._log_follow is True
        assert tui.scrollable.vertical_scroll == tui._FOLLOW_MAX_SCROLL
    finally:
        pipe_ctx.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_submit_restores_follow(tui):
    """v1.2: Enter submit restores follow (huge scroll) before routing."""
    tui.scrollable.vertical_scroll = 5
    tui._set_log_follow(False)

    await tui.handle_input("hello")

    assert tui._log_follow is True
    assert tui.scrollable.vertical_scroll == tui._FOLLOW_MAX_SCROLL
    # input still sent
    assert len(tui._fake_session.sent) == 1


@pytest.mark.asyncio
async def test_user_input_still_logged(tui):
    """v1.2: user input content is still logged (✓ human → ...) — P2, agent-4."""
    await tui.handle_input("hello world")
    exported = tui.log_buffer.export_tail()
    assert "✓ human → thread-1" in exported
    assert "hello world" in exported


# ---------------------------------------------------------------------------
# v1.1 ③: choice panel option-area scrolling + indicator + log backup
# ---------------------------------------------------------------------------


def _render_text(tui: SessionTUIApplication) -> str:
    """Flatten FormattedText to a plain string for assertions."""
    parts = []
    for style, text in tui.choice_panel.render():
        parts.append(text)
    return "".join(parts)


def test_choice_panel_scroll_offset_for_many_options(tmp_path):
    """v1.1: 10 options → panel caps at 8 lines, scroll reveals the rest."""
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
        # 1 question + 7 visible options + 1 indicator = 8 (capped)
        assert tui.choice_panel.line_count(8) == 8
        assert tui.choice_panel.scroll_offset == 0

        rendered = _render_text(tui)
        assert "옵션 10개 중 1~7 표시" in rendered

        # scroll down → reveals later options
        tui.choice_panel.scroll_down(8)
        assert tui.choice_panel.scroll_offset == 3  # 10 - 7 = 3 max
        rendered2 = _render_text(tui)
        assert "[4]" in rendered2
        assert "옵션 10개 중 4~10 표시" in rendered2

        # scroll up
        tui.choice_panel.scroll_up(8)
        assert tui.choice_panel.scroll_offset == 2
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_choice_panel_pgdn_scrolls_options_not_log(tmp_path):
    """v1.1/v1.2: with a pending question, PgDn scrolls the panel, not the log."""
    session = FakeSession()
    tui, pipe_ctx = _make_tui(session, tmp_path=tmp_path)
    try:
        for i in range(20):
            tui.log_buffer.append(f"line {i}")
        tui.scrollable.vertical_scroll = 0

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

        before_scroll = tui.scrollable.vertical_scroll
        pgdn(MagicMock())
        # scrollable position unchanged; panel offset advanced
        assert tui.scrollable.vertical_scroll == before_scroll
        assert tui.choice_panel.scroll_offset == 3
    finally:
        pipe_ctx.__exit__(None, None, None)


def test_ask_user_log_backup(tmp_path):
    """v1.1: on_ask_user backs up full question+options to the log buffer."""
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
        # HSplit children: [scrollable, ConditionalContainer(choice), Window(status)]
        container = layout.children[1]
        assert hasattr(container, "content")
        choice_window = container.content
        # Window.wrap_lines is a Filter (to_filter(True)) — call it to check.
        assert choice_window.wrap_lines() is True
        # height callable is the dynamic height fn
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
