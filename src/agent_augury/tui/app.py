"""SessionTUIApplication - full-screen layout + input routing lifecycle.

v1.1 (TUI_UX_FIX_DESIGN.md):
- ② 마우스 휠 스크롤 시 `_log_follow` 해제 (SCROLL_UP/SCROLL_DOWN 바인딩),
  `F` 키 follow 토글, 상태 표시줄 FOLLOW/SCROLL.
- ① `on_ask_user` 시 질문+전체 옵션을 로그 버퍼에 백업 (긴 선택지 확인).
- ③ 선택지 패널 활성 시 PgUp/PgDn이 로그 대신 패널 옵션을 스크롤.

v1.2 (TUI_SCROLLABLE_INPUT_DESIGN.md — 옵션 A):
- `ScrollablePane` 도입: **로그 + 입력창 위젯이 함께 스크롤** (일반 CLI처럼
  위로 스크롤하면 입력창도 함께 올라감).
- 선택지 패널/상태바는 ScrollablePane **밖 고정** — ask_user 질문/세션 상태 항상 가시.
- **keep_cursor_visible=False / keep_focused_window_visible=False** — ScrollablePane의
  입력창 강제 가시화를 끄고, follow(최하단) 상태를 우리가 명시적으로 관리한다.
  (기본 True면 렌더마다 입력창이 화면 하단에 붙도록 scroll이 되돌려져
  "입력창 고정"이 재현되고 로그가 위로 밀려 "3줄만 보임"이 된다 — 회귀 수정.)
- 타이핑 시 follow 복귀: `InputBar.on_text_changed` 훅 → `_on_typing()`이
  `_set_log_follow(True)` + `_follow_log_tail()` (keep_cursor_visible 대체).
- Enter 제출 시 명시적 최하단 복귀 (`vertical_scroll` 큰 값 → 내부 클램프).
- 사용자 입력 내용 로깅(`✓ human → ...`)은 기존 경로 그대로 유지.

v1.2.1 (회귀 수정):
- HSplit의 `(container, weight)` 튜플 문법은 prompt_toolkit에서 지원하지 않아
  `to_container()`가 ValueError를 던졌다 (pytest 25건 실패 원인).
  → `self.scrollable`을 튜플 없이 children에 직접 넣는다. ScrollablePane 자체가
  preferred_height로 남는 공간을 차지하므로 weight 불필요.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    HSplit,
    Layout,
    ScrollablePane,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from .choice_panel import ChoicePanel
from .commands import dispatch
from .input_bar import InputBar
from .key_aliases import install_tui_key_aliases
from .log_buffer import LogBuffer
from .renderer import mask_sensitive, render_event
from .router import RouterContext, route
from .status_bar import StatusBar


class SessionTUIApplication:
    """Single Application owning log / choice / status / input panes."""

    _CHOICE_MAX_LINES = 8       # D-4 확정 (결정 ③ 3-A)
    _LOG_SCROLL_PAGE = 10       # D-5 확정 (PgUp/PgDn)
    # ScrollablePane 최하단 정렬용 충분히 큰 값 — 내부 _make_window_visible()이
    # max(virtual_height - visible_height)로 클램프한다 (agent-2 검증, §5.2).
    _FOLLOW_MAX_SCROLL = 10**9

    def __init__(
        self,
        session: Any,
        *,
        history_file: str | Path = "~/.agent-augury/human_history.txt",
        response_format: str = "text",
        key_aliases: bool = True,
        on_quit: Callable[[], None] | None = None,
        on_next_turn: Callable[[str], None] | None = None,
        initial_task_mode: bool = False,   # ★ v1.0: Initial Task 대기 모드
        preserve_log_on_exit: bool = True,
        pt_input: Any | None = None,
        pt_output: Any | None = None,
    ) -> None:
        if key_aliases:
            install_tui_key_aliases()

        self._session = session
        self._response_format = response_format
        self._on_quit = on_quit
        self._on_next_turn = on_next_turn
        self._initial_task_mode = initial_task_mode
        self._preserve_log_on_exit = preserve_log_on_exit
        self._recent_thread: str | None = None
        self._running = False
        self._shutting_down = False
        self._log_follow = True     # ★ tail follow (앱 레벨 상태, v1.0 §2.3)

        self.log_buffer = LogBuffer(self._invalidate)
        self.choice_panel = ChoicePanel(self._invalidate)
        self.status_bar = StatusBar(session)
        self.input_bar = InputBar(
            self.handle_input,
            app_ref=self,
            on_quit=on_quit,
            on_text_changed=self._on_typing,
            history=history_file,
        )

        # ★ 로그 Window — S2 단일 소스 (v1.0 §2.3).
        #   get_cursor_position 람다는 렌더 시점에 평가 → 초기화 순환 참조 없음.
        self.log_window = Window(
            FormattedTextControl(
                text=lambda: self.log_buffer.render(),
                get_cursor_position=lambda: Point(
                    x=0, y=self.log_window.vertical_scroll
                ),
            ),
            wrap_lines=True,
            always_hide_cursor=True,        # 커서 숨김 (focusable=False 유지)
            allow_scroll_beyond_bottom=True,
        )

        # ★ v1.2: ScrollablePane — 로그 + 입력창 위젯이 함께 스크롤
        #   (TUI_SCROLLABLE_INPUT_DESIGN.md 옵션 A).
        #   keep_cursor_visible=False / keep_focused_window_visible=False:
        #   ScrollablePane의 입력창 강제 가시화를 끄고 follow를 우리가 관리.
        #   선택지 패널/상태바는 이 Pane 밖(아래)에 고정.
        self.scrollable = ScrollablePane(
            HSplit([
                self.log_window,
                self.input_bar.widget,
            ]),
            keep_cursor_visible=False,
            keep_focused_window_visible=False,
        )

        app_kwargs: dict[str, Any] = {
            "layout": Layout(self._build_layout()),
            "full_screen": True,
            "paste_mode": True,
            "mouse_support": True,                  # ★ 마우스 휠 (v1.0)
            "key_bindings": self._build_app_kb(),   # ★ 전역 kb (self 클로저)
            "style": Style.from_dict({
                "status": "bg:#222222",
                "choice": "bg:#333333",
            }),
            "refresh_interval": 0.1,
        }
        if pt_input is not None:
            app_kwargs["input"] = pt_input
        if pt_output is not None:
            app_kwargs["output"] = pt_output
        self.app = Application(**app_kwargs)
        self.log_buffer.set_app(self.app)
        self.choice_panel.set_app(self.app)
        self.input_bar.set_app(self.app)

        self._status_task: asyncio.Task[None] | None = None

    def _invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    # -- layout --------------------------------------------------------------

    def _build_layout(self) -> HSplit:
        # v1.2: ScrollablePane(로그+입력) + 고정 선택지 패널 + 고정 상태바.
        #   ⚠️ v1.2.1: HSplit은 (container, weight) 튜플을 지원하지 않는다
        #   (to_container()가 튜플 처리 못해 ValueError — pytest 25건 실패 원인).
        #   ScrollablePane 자체가 preferred_height로 남는 공간을 차지하므로
        #   weight 없이 children에 직접 넣는다.
        return HSplit([
            self.scrollable,                    # ★ 로그+입력 (함께 스크롤)
            ConditionalContainer(
                Window(
                    self.choice_panel.control(),
                    height=self._choice_height,     # ★ 동적 높이 (결정 ③ 3-A)
                    wrap_lines=True,                # ★ 긴 옵션 wrap (잘림 없음)
                    style="class:choice",
                ),
                filter=self.choice_panel.has_pending,
            ),
            Window(
                self.status_bar.control(),
                height=1,
                style="class:status",
            ),
        ])

    def _choice_height(self) -> Dimension:
        return Dimension(
            min=1,
            max=self._CHOICE_MAX_LINES,
            preferred=self.choice_panel.line_count(self._CHOICE_MAX_LINES),
        )

    # -- log follow state (v1.1 ② — status bar 동기화) -----------------------

    def _set_log_follow(self, follow: bool) -> None:
        """Set follow state and mirror it to the status bar."""
        self._log_follow = follow
        try:
            self.status_bar.set_log_follow(follow)
        except Exception:  # noqa: BLE001, S110 — status bar는 best-effort
            pass

    # -- v1.2: 타이핑 시 follow 복귀 (keep_cursor_visible 대체) ---------------

    def _on_typing(self, _text: str) -> None:
        """InputBar.on_text_changed → follow 복귀 (TUI_SCROLLABLE_INPUT_DESIGN.md §5.3).

        ScrollablePane의 keep_cursor_visible을 끈 대신, 사용자가 타이핑을
        시작/변경하면 최하단 follow로 복귀해 입력창이 화면 하단에 보이게 한다.
        """
        self._set_log_follow(True)
        self._follow_log_tail()

    # -- global key bindings (v1.0 §2.3 — self closure, event.app 금지) ------

    def _build_app_kb(self) -> KeyBindings:
        """전역 kb: PgUp/PgDn·Alt+↑/↓ = 스크롤 (결정 ① 1-A).

        v1.1 (TUI_UX_FIX_DESIGN.md ②③):
        - 마우스 휠(SCROLL_UP/DOWN) 추가 — 스크롤 시 `_log_follow` 해제.
        - `F` 키 — follow 토글.
        - 선택지 패널이 활성이면:
            PgUp/PgDn      → 패널 옵션 **페이지 단위** 스크롤
            휠 / Alt+↑/↓   → 패널 옵션 **1줄 단위** 스크롤

        v1.2 (TUI_SCROLLABLE_INPUT_DESIGN.md):
        - 패널 비활성일 때 스크롤 대상 = `ScrollablePane.vertical_scroll` (로그+입력 함께)
        - 패널 활성이면 기존 패널 옵션 스크롤 분기 유지

        ⚠️ self 클로저로 구성 — `event.app`은 prompt_toolkit Application이지
        SessionTUIApplication이 아니므로 `event.app.log_window` 금지 (v0.13 함정).
        """
        kb = KeyBindings()

        @kb.add("pageup")
        def _pgup(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_up(self._CHOICE_MAX_LINES)   # 페이지 단위
                return
            self.scrollable.vertical_scroll = max(
                0, self.scrollable.vertical_scroll - self._LOG_SCROLL_PAGE
            )
            self._set_log_follow(False)
            self.app.invalidate()

        @kb.add("pagedown")
        def _pgdn(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_down(self._CHOICE_MAX_LINES)  # 페이지 단위
                return
            self.scrollable.vertical_scroll = (
                self.scrollable.vertical_scroll + self._LOG_SCROLL_PAGE
            )
            self._set_log_follow(False)
            self.app.invalidate()

        @kb.add("escape", "up")     # Alt+↑ (결정 ① 1-A)
        def _alt_up(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_up(self._CHOICE_MAX_LINES)  # 1줄 단위
                return
            self.scrollable.vertical_scroll = max(
                0, self.scrollable.vertical_scroll - 1
            )
            self._set_log_follow(False)
            self.app.invalidate()

        @kb.add("escape", "down")   # Alt+↓ (결정 ① 1-A)
        def _alt_down(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_down(self._CHOICE_MAX_LINES)  # 1줄 단위
                return
            self.scrollable.vertical_scroll = self.scrollable.vertical_scroll + 1
            self._set_log_follow(False)
            self.app.invalidate()

        # ★ v1.1 ②: 마우스 휠 — 스크롤 시 follow 해제/복귀 (TUI_UX_FIX_DESIGN.md)
        #   Keys.ScrollUp/ScrollDown enum (v1.1.1 fix — 문자열 키 파싱 실패 방지)
        @kb.add(Keys.ScrollUp, eager=True)
        def _wheel_up(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_up(self._CHOICE_MAX_LINES)  # 1줄 단위
                return
            self.scrollable.vertical_scroll = max(
                0, self.scrollable.vertical_scroll - 3
            )
            self._set_log_follow(False)
            self.app.invalidate()

        @kb.add(Keys.ScrollDown, eager=True)
        def _wheel_down(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_down(self._CHOICE_MAX_LINES)  # 1줄 단위
                return
            self.scrollable.vertical_scroll = self.scrollable.vertical_scroll + 3
            self._set_log_follow(False)
            self.app.invalidate()

        # ★ v1.1 ②: F 키 — follow 토글 (TUI_UX_FIX_DESIGN.md)
        @kb.add("f", eager=True)
        def _toggle_follow(event: Any) -> None:
            self._set_log_follow(not self._log_follow)
            if self._log_follow:
                self._follow_log_tail()
            self.app.invalidate()

        return kb

    def _follow_log_tail(self) -> None:
        """append 후 tail follow — follow=True면 최하단 유지 (v1.0 §2.3).

        v1.2: ScrollablePane 기준 — 충분히 큰 값을 설정하면 내부
        `_make_window_visible()`이 `max(virtual_height - visible_height)`로
        클램프해 최하단(입력창 화면 하단)으로 정렬한다 (agent-2 검증).
        """
        if not self._log_follow:
            return
        try:
            self.scrollable.vertical_scroll = self._FOLLOW_MAX_SCROLL
        except Exception:  # noqa: BLE001, S110  # 렌더 전 초기화 중 무해 실패 무시
            pass

    # -- lifecycle -----------------------------------------------------------

    def set_running(self, running: bool) -> None:
        self._running = running
        self.status_bar.set_running(running)
        self._invalidate()

    def append_event(self, event: dict[str, Any]) -> None:
        ansi = render_event(event)
        if ansi:
            self.log_buffer.append(ansi)
            self._follow_log_tail()

    def append_text(self, text: str) -> None:
        if text:
            self.log_buffer.append(text)
            self._follow_log_tail()

    def on_ask_user(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
    ) -> None:
        self.choice_panel.on_ask_user(agent_id, tool, args, result)
        thread_id = args.get("thread")
        if thread_id:
            self._recent_thread = thread_id
        # ★ v1.1 ③: 로그 백업 — 패널이 잘려도 전체 옵션을 로그 스크롤로 확인
        #   (TUI_UX_FIX_DESIGN.md ③ — 패널=현재 질문, 로그=전체 이력 보완 관계)
        try:
            question = args.get("question", "")
            options = list(args.get("options") or [])
            lines = [f"❓ [{agent_id}] {question}"]
            for i, opt in enumerate(options, 1):
                lines.append(f"   [{i}] {opt}")
            self.log_buffer.append(mask_sensitive("\n".join(lines)))
            self.log_buffer.append("-" * 40)
            self._follow_log_tail()
        except Exception:  # noqa: BLE001, S110 — 로그 백업 실패는 치명적이지 않음
            pass

    def create_background_task(self, coro: Any) -> Any:
        return self.app.create_background_task(coro)

    def create_task(self, coro: Any) -> Any:
        return self.create_background_task(coro)

    async def handle_input(self, text: str) -> None:
        # ★ v1.2: Enter 제출 시 최하단 follow 복귀 (사용자 요구)
        #   (TUI_SCROLLABLE_INPUT_DESIGN.md §5.3 — 제출 시 명시적 복귀)
        self._set_log_follow(True)
        self._follow_log_tail()

        ctx = RouterContext(
            active_question=self.choice_panel.active,
            recent_thread=self._resolve_recent_thread(),
            response_format=self._response_format,
        )
        result = route(text, ctx)

        if result.kind == "ignored":
            return

        if result.kind == "quit":
            if self._on_quit is not None:
                self._on_quit()
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception:  # noqa: BLE001, S110  # 종료 시 무해한 정리 실패 무시
                pass
            return

        if result.kind == "command":
            out = dispatch(
                result.command or "",
                result.args,
                {
                    "snapshot": self._session.server.snapshot(),
                    "gate": getattr(self._session, "gate", None),
                    "phase": (
                        self._session.protocol.phase
                        if getattr(self._session, "protocol", None) is not None
                        else "n/a"
                    ),
                    "recent_thread": self._recent_thread,
                    "log_buffer": self.log_buffer,
                    "choice_panel": self.choice_panel,
                },
            )
            if out:
                self.append_text(out)
            return

        if result.kind in ("choice", "question_reply"):
            if result.notice:
                self.append_text(result.notice)
            await self._send(
                result.thread_id,
                result.content,
                mentions=result.mentions,
            )
            self.choice_panel.answer()
            return

        if result.kind == "plain":
            if self._initial_task_mode:     # Initial Task 대기 중 (v1.0 §2.1)
                if self._on_next_turn is not None:
                    self._on_next_turn(result.content)   # session.run 트리거만 (스레드 없음)
                # ★ 첫 입력 후 해제 — 이후 턴은 정상 _send 경로 (설계 보강)
                self._initial_task_mode = False
                return
            await self._send(result.thread_id, result.content, mentions=None)
            if not self._running and self._on_next_turn is not None:
                self._on_next_turn(result.content)
            return

    async def _send(
        self,
        thread_id: str | None,
        content: str,
        *,
        mentions: list[str] | None,
    ) -> None:
        if not thread_id:
            self.append_text('✗ send failed: no active thread')
            return
        try:
            await self._session.human_send(
                thread_id,
                content=content,
                mentions=mentions,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_text('✗ send failed: ' + f"{exc}")
            return
        preview = mask_sensitive(content)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        who = f"mentions={mentions}" if mentions else "broadcast"
        # Design R10 feedback line — 사용자 입력 내용 로깅 (기존 경로 유지, P2)
        self.append_text(f"✓ human → {thread_id} ({who}): {preview}")
        self._recent_thread = thread_id

    def _resolve_recent_thread(self) -> str | None:
        if self._recent_thread:
            return self._recent_thread
        try:
            snap = self._session.server.snapshot()
            threads = snap.get("threads") or []
            if threads:
                return threads[0].get("thread_id")
        except Exception:  # noqa: BLE001, S110  # 스냅샷 실패 시 recent_thread 폴백
            pass
        return None

    async def run(self) -> None:
        self._status_task = asyncio.create_task(self.status_bar.run(self.app))
        try:
            await self.app.run_async()
        finally:
            if self._status_task is not None:
                self._status_task.cancel()
                try:
                    await self._status_task
                except asyncio.CancelledError:
                    pass
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            if self.app.is_running:
                self.app.exit()
        except Exception:  # noqa: BLE001, S110  # 종료 시 무해한 정리 실패 무시
            pass
        self.choice_panel.reset()
        try:
            self.input_bar.widget.buffer.reset()
        except Exception:  # noqa: BLE001, S110  # 종료 시 무해한 정리 실패 무시
            pass
        if self._preserve_log_on_exit:
            tail = self.log_buffer.export_tail()
            if tail:
                print(tail, flush=True)
