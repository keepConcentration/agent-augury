"""SessionTUIApplication - full-screen layout + input routing lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, Layout, Window
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
            history=history_file,
        )

        # ★ 로그 Window — S2 단일 소스 (v1.0 §2.3).
        #   _build_layout()이 self.log_window를 참조하므로 반드시 그 전에 정의.
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
        return HSplit([
            self.log_window,                        # ★ S2 로그 Window
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
            self.input_bar.widget,
        ])

    def _choice_height(self) -> Dimension:
        return Dimension(
            min=1,
            max=self._CHOICE_MAX_LINES,
            preferred=self.choice_panel.line_count(self._CHOICE_MAX_LINES),
        )

    # -- global key bindings (v1.0 §2.3 — self closure, event.app 금지) ------

    def _build_app_kb(self) -> KeyBindings:
        """전역 kb: PgUp/PgDn·Alt+↑/↓ = 로그 스크롤 (결정 ① 1-A).

        ⚠️ self 클로저로 구성 — `event.app`은 prompt_toolkit Application이지
        SessionTUIApplication이 아니므로 `event.app.log_window` 금지 (v0.13 함정).
        """
        kb = KeyBindings()

        @kb.add("pageup")
        def _pgup(event: Any) -> None:
            self.log_window.vertical_scroll = max(
                0, self.log_window.vertical_scroll - self._LOG_SCROLL_PAGE
            )
            self._log_follow = False
            self.app.invalidate()

        @kb.add("pagedown")
        def _pgdn(event: Any) -> None:
            max_scroll = max(0, self.log_buffer.line_count() - 1)
            self.log_window.vertical_scroll = min(
                max_scroll, self.log_window.vertical_scroll + self._LOG_SCROLL_PAGE
            )
            if self.log_window.vertical_scroll >= max_scroll:
                self._log_follow = True     # 끝 도달 → follow 복귀
            self.app.invalidate()

        @kb.add("escape", "up")     # Alt+↑ (결정 ① 1-A)
        def _alt_up(event: Any) -> None:
            self.log_window.vertical_scroll = max(
                0, self.log_window.vertical_scroll - 1
            )
            self._log_follow = False
            self.app.invalidate()

        @kb.add("escape", "down")   # Alt+↓ (결정 ① 1-A)
        def _alt_down(event: Any) -> None:
            max_scroll = max(0, self.log_buffer.line_count() - 1)
            self.log_window.vertical_scroll = min(
                max_scroll, self.log_window.vertical_scroll + 1
            )
            if self.log_window.vertical_scroll >= max_scroll:
                self._log_follow = True
            self.app.invalidate()

        return kb

    def _follow_log_tail(self) -> None:
        """append 후 tail follow — follow=True면 최신 하단 유지 (v1.0 §2.3)."""
        if not self._log_follow:
            return
        try:
            self.log_window.vertical_scroll = max(
                0, self.log_buffer.line_count() - 1
            )
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

    def create_background_task(self, coro: Any) -> Any:
        return self.app.create_background_task(coro)

    def create_task(self, coro: Any) -> Any:
        return self.create_background_task(coro)

    async def handle_input(self, text: str) -> None:
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
        # Design R10 feedback line
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
