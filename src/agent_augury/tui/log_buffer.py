"""LogBuffer - deque + dirty ANSI cache for FormattedTextControl."""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Callable

from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.layout.controls import FormattedTextControl

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class LogBuffer:
    """Line buffer with dirty-cache FormattedText for the log pane."""

    def __init__(
        self,
        invalidate: Callable[[], None] | None = None,
        maxlen: int = 1000,
    ) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._cache: FormattedText | None = None
        self._invalidate = invalidate
        self._app: Any = None

    def set_app(self, app: Any) -> None:
        self._app = app
        self._invalidate = app.invalidate

    def append(self, ansi_block: str) -> None:
        if not ansi_block:
            return
        self._lines.extend(ansi_block.split("\n"))
        self._cache = None
        if self._invalidate is not None:
            self._invalidate()

    def append_separator(self) -> None:
        self.append("-" * 40)

    def clear(self) -> None:
        self._lines.clear()
        self._cache = None
        if self._invalidate is not None:
            self._invalidate()

    def render(self) -> FormattedText:
        if self._cache is None:
            self._cache = ANSI("\n".join(self._lines))
        return self._cache

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)

    def export_tail(self, n: int = 200) -> str:
        """Plain-text dump for preserve-on-exit (ANSI stripped)."""
        lines = list(self._lines)[-n:]
        return _ANSI_RE.sub("", "\n".join(lines))
