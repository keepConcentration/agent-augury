"""InputBar - TextArea with accept_handler (Enter=submit)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory, History
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import TextArea

SubmitHandler = Callable[[str], Awaitable[None] | None]
QuitHandler = Callable[[], None]


class InputBar:
    """Bottom input line: Enter submits, Shift/Esc+Enter inserts newline."""

    def __init__(
        self,
        on_submit: SubmitHandler,
        *,
        app_ref: Any | None = None,
        on_quit: QuitHandler | None = None,
        prompt: str = "\U0001f464 > ",
        history: History | str | Path | None = None,
        height: int = 3,
    ) -> None:
        self._on_submit = on_submit
        self._on_quit = on_quit
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
            text = buff.text
            buff.reset()
            self._dispatch(text)
            return True

        self.widget = TextArea(
            multiline=True,
            prompt=prompt,
            height=height,
            accept_handler=_accept,
            history=hist,
        )
        # prompt_toolkit 3.0 TextArea has no key_bindings= kwarg — attach on control.
        self.widget.control.key_bindings = self._kb

    def set_app(self, app: Any) -> None:
        self._app = app

    def set_on_quit(self, on_quit: QuitHandler | None) -> None:
        self._on_quit = on_quit

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

        @kb.add("escape", "enter")
        def _nl1(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        # Shift+Enter is normalized to Esc+Enter via key_aliases (ANSI sequences).
        # Do not bind "s-enter" — invalid in prompt_toolkit 3.x key names.

        @kb.add("c-d")
        def _quit(event: Any) -> None:
            # Must notify CLI session_loop (quit_flag + next_turn) before exit,
            # otherwise await next_turn.get() deadlocks (P0 review).
            if self._on_quit is not None:
                self._on_quit()
            event.app.exit()

        @kb.add("c-c")
        def _clear(event: Any) -> None:
            event.current_buffer.reset()

        return kb
