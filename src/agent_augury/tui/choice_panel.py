"""ask_user choice panel - FIFO queue + FormattedTextControl."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout.controls import FormattedTextControl


@dataclass
class PendingQuestion:
    """An active ask_user question waiting for human response."""

    thread_id: str
    agent_id: str
    question: str
    options: list[str]
    created_at: float = field(default_factory=time.time)
    status: Literal["pending", "answered", "dismissed"] = "pending"


class ChoicePanel:
    """FIFO PendingQuestion queue with pinned-panel rendering."""

    def __init__(self, invalidate: Callable[[], None] | None = None) -> None:
        self.queue: deque[PendingQuestion] = deque()
        self._invalidate = invalidate
        self._app: Any = None
        self._formatted: FormattedText | None = None

    def set_app(self, app: Any) -> None:
        self._app = app
        self._invalidate = app.invalidate

    def _bump(self) -> None:
        self._formatted = None
        if self._invalidate is not None:
            self._invalidate()

    @property
    def active(self) -> PendingQuestion | None:
        return self.queue[0] if self.queue else None

    @property
    def has_pending(self) -> Condition:
        return Condition(lambda: bool(self.queue))

    def has_pending_bool(self) -> bool:
        return bool(self.queue)

    def on_ask_user(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
    ) -> None:
        """Handle ask_user tool event (D8 path)."""
        pq = PendingQuestion(
            thread_id=args.get("thread", ""),
            agent_id=agent_id,
            question=args.get("question", ""),
            options=list(args.get("options") or []),
        )
        self.queue.append(pq)
        self._bump()

    def push(self, pq: PendingQuestion) -> None:
        self.queue.append(pq)
        self._bump()

    def pop(self) -> PendingQuestion | None:
        pq = self.queue.popleft() if self.queue else None
        if pq is not None:
            pq.status = "answered"
        self._bump()
        return pq

    def skip(self) -> PendingQuestion | None:
        pq = self.queue.popleft() if self.queue else None
        if pq is not None:
            pq.status = "dismissed"
        self._bump()
        return pq

    def answer(self) -> PendingQuestion | None:
        return self.pop()

    def reset(self) -> None:
        self.queue.clear()
        self._bump()

    def render(self) -> FormattedText:
        if self._formatted is not None:
            return self._formatted
        pq = self.active
        if pq is None:
            self._formatted = FormattedText([("", "")])
            return self._formatted
        qmark = "\u2753"
        lines = [f"{qmark} {pq.agent_id}: {pq.question}"]
        if pq.options:
            lines.append(
                "   " + "   ".join(f"[{i + 1}] {opt}" for i, opt in enumerate(pq.options))
            )
        if len(self.queue) > 1:
            lines.append(f"   (queued {len(self.queue) - 1})")
        self._formatted = FormattedText([("bold", "\n".join(lines))])
        return self._formatted

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)
