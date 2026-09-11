"""InputBar - TextArea with accept_handler (Enter=submit).

v1.2 (TUI_SCROLLABLE_INPUT_DESIGN.md):
- ``on_text_changed`` 훅 추가 — 사용자가 타이핑을 시작/변경하면 호출되어
  앱이 follow로 복귀한다 (keep_cursor_visible 자동 조정을 대체).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory, History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import TextArea

SubmitHandler = Callable[[str], Awaitable[None] | None]
QuitHandler = Callable[[], None]
TextChangedHandler = Callable[[str], None]


class InputBar:
    """Bottom input line: Enter submits; Shift/Esc+Enter and Ctrl+Enter insert newline."""

    def __init__(
        self,
        on_submit: SubmitHandler,
        *,
        app_ref: Any | None = None,
        on_quit: QuitHandler | None = None,
        on_text_changed: TextChangedHandler | None = None,
        prompt: str = '👤 > ',
        history: History | str | Path | None = None,
        height: int = 3,
    ) -> None:
        self._on_submit = on_submit
        self._on_quit = on_quit
        self._on_text_changed = on_text_changed
        self._app = app_ref
        self._kb = self._build_bindings()

        hist: History | None
        if isinstance(history, (str, Path)):
            path = Path(history).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            hist = FileHistory(str(path))
        else:
            hist = history

        def _accept(buff: Buffer) -> bool:
            # Do NOT reset here - validate_and_handle appends history then resets.
            text = buff.text
            self._dispatch(text)
            return False

        self.widget = TextArea(
            multiline=True,
            prompt=prompt,
            height=height,
            accept_handler=_accept,
            history=hist,
        )
        # prompt_toolkit 3.0 TextArea has no key_bindings= kwarg - attach on control.
        self.widget.control.key_bindings = self._kb

        # v1.2: 타이핑 시 follow 복귀 훅 (TUI_SCROLLABLE_INPUT_DESIGN.md §5.3)
        try:
            self.widget.buffer.on_text_changed += self._handle_text_changed
        except Exception:  # noqa: BLE001, S110 — 훅 실패는 치명적이지 않음
            pass

    def set_app(self, app: Any) -> None:
        self._app = app

    def set_on_quit(self, on_quit: QuitHandler | None) -> None:
        self._on_quit = on_quit

    def _handle_text_changed(self, _event: Any) -> None:
        """타이핑/변경 시 follow 복귀 (keep_cursor_visible 대체)."""
        if self._on_text_changed is not None:
            try:
                self._on_text_changed(self.widget.buffer.text)
            except Exception:  # noqa: BLE001, S110
                pass

    def _dispatch(self, text: str) -> None:
        result = self._on_submit(text)
        if asyncio.iscoroutine(result):
            app = self._app
            if app is not None and hasattr(app, "create_background_task"):
                app.create_background_task(result)
            elif app is not None and hasattr(app, "create_task"):
                app.create_task(result)
            else:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    pass

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        # Enter / C-j = submit (override multiline default newline).
        @kb.add("enter", eager=True)
        @kb.add("c-j", eager=True)
        def _submit(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        # Newline: Esc+Enter (Shift+Enter via win32 patch → Escape,ControlM).
        # eager so a pending Escape+Enter wins over Enter=submit when both match.
        @kb.add("escape", "enter", eager=True)
        def _nl1(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        # Newline: Esc+C-j (win32 Ctrl+Enter, or Shift+Enter as \\n).
        @kb.add("escape", "c-j", eager=True)
        def _nl2(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("c-d")
        def _quit(event: Any) -> None:
            if self._on_quit is not None:
                self._on_quit()
            event.app.exit()

        @kb.add("c-c")
        def _clear(event: Any) -> None:
            # Ctrl+C → SessionTUIApplication._handle_ctrl_c when wired;
            # standalone InputBar (no app) still clears the draft.
            if self._app is not None and hasattr(self._app, '_handle_ctrl_c'):
                self._app._handle_ctrl_c()
            else:
                event.current_buffer.reset()

        return kb
