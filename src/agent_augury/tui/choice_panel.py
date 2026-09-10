"""ask_user choice panel - FIFO queue + FormattedTextControl.

v1.0 (INITIAL_TASK_TUI_INTEGRATION_RESULT.md, 결정 ③ 3-A):
- 옵션을 세로 목록(옵션당 1줄)으로 렌더 — 가로 join 제거 (3줄 잘림 해결).
- `line_count(max_lines=...)` 제공 — 패널 동적 높이(Dimension callable) 계산용
  (app.py `_choice_height()`가 `line_count(self._CHOICE_MAX_LINES)`로 호출).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout.controls import FormattedTextControl

# 패널 최대 논리 줄 수 기본값 (결정 ③ 3-A / D-4 확정).
# app.py가 `_CHOICE_MAX_LINES`(8)를 인자로 넘겨 재정의 가능 — 단일 소스 유지.
MAX_PANEL_LINES = 8


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

    def line_count(self, max_lines: int = MAX_PANEL_LINES) -> int:
        """논리 줄 수 — 질문 1 + 옵션 수 + (queued 1), 최대 *max_lines*.

        app.py의 `_choice_height()`가 이 값을 `Dimension(preferred=...)`로 사용
        (호출 시 `self._CHOICE_MAX_LINES`를 인자로 전달).
        (wrap_lines=True인 Window가 실제 줄바꿈 높이를 계산하므로, 이 값은
        옵션 wrap 전의 논리 줄 수.)
        """
        pq = self.active
        if pq is None:
            return 0
        return min(max_lines, 1 + len(pq.options) + (1 if len(self.queue) > 1 else 0))

    def render(self) -> FormattedText:
        if self._formatted is not None:
            return self._formatted
        pq = self.active
        if pq is None:
            self._formatted = FormattedText([("", "")])
            return self._formatted
        lines = [f"❓ {pq.agent_id}: {pq.question}"]
        # ★ 세로 목록 (결정 ③ 3-A) — 옵션당 1줄, 가로 join 제거
        for i, opt in enumerate(pq.options, 1):
            lines.append(f"   [{i}] {opt}")
        if len(self.queue) > 1:
            lines.append(f"   (queued {len(self.queue) - 1})")
        self._formatted = FormattedText([("bold", "\n".join(lines))])
        return self._formatted

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)
