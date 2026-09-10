"""ask_user choice panel - FIFO queue + FormattedTextControl.

v1.0 (INITIAL_TASK_TUI_INTEGRATION_RESULT.md, 결정 ③ 3-A):
- 옵션을 세로 목록(옵션당 1줄)으로 렌더 — 가로 join 제거 (3줄 잘림 해결).
- `line_count(max_lines=...)` 제공 — 패널 동적 높이(Dimension callable) 계산용
  (app.py `_choice_height()`가 `line_count(self._CHOICE_MAX_LINES)`로 호출).

v1.1 (TUI_UX_FIX_DESIGN.md ③):
- `scroll_offset` 상태 추가 — 8줄 상한을 초과하는 옵션을 PgUp/PgDn(또는 휠)로
  스크롤하여 확인/선택 가능.
- 렌더 시 `(옵션 N개 중 i~j 표시)` 인디케이터 추가.
- 번호 선택은 router가 1-based 전체 인덱스로 해석하므로 표시 순서와 무관하게
  잘린 옵션도 정상 선택됨.

v1.2.1 (fix — control 인스턴스 안정화):
- `control()`이 매 호출마다 새 `FormattedTextControl`을 만들지 않고 **캐시**한다.
  레이아웃 Window가 들고 있는 control과 `choice_panel.control()`이 동일 인스턴스여야
  테스트/렌더 일관성이 보장된다 (pytest `is` 비교 실패 원인 수정).
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
        # v1.1: 옵션 영역 스크롤 오프셋 (표시 시작 인덱스) — TUI_UX_FIX_DESIGN.md ③
        self._scroll_offset = 0
        # v1.2.1: control 인스턴스 캐시 — control() 호출마다 새 객체 생성 방지
        self._control: FormattedTextControl | None = None

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
        # v1.1: 새 질문은 스크롤을 맨 위로 초기화
        self._scroll_offset = 0
        self._bump()

    def push(self, pq: PendingQuestion) -> None:
        self.queue.append(pq)
        self._scroll_offset = 0
        self._bump()

    def pop(self) -> PendingQuestion | None:
        pq = self.queue.popleft() if self.queue else None
        if pq is not None:
            pq.status = "answered"
        self._scroll_offset = 0
        self._bump()
        return pq

    def skip(self) -> PendingQuestion | None:
        pq = self.queue.popleft() if self.queue else None
        if pq is not None:
            pq.status = "dismissed"
        self._scroll_offset = 0
        self._bump()
        return pq

    def answer(self) -> PendingQuestion | None:
        return self.pop()

    def reset(self) -> None:
        self.queue.clear()
        self._scroll_offset = 0
        self._bump()

    # -- v1.1: option-area scrolling (TUI_UX_FIX_DESIGN.md ③) ---------------

    @property
    def scroll_offset(self) -> int:
        return self._scroll_offset

    @scroll_offset.setter
    def scroll_offset(self, value: int) -> None:
        self._scroll_offset = max(0, value)
        self._bump()

    def _visible_option_count(self, max_lines: int = MAX_PANEL_LINES) -> int:
        """가시 옵션 줄 수 = 패널 상한 - 질문 1줄 - (queued 1줄)."""
        return max(1, max_lines - 1 - (1 if len(self.queue) > 1 else 0))

    def _max_scroll(self, max_lines: int = MAX_PANEL_LINES) -> int:
        """Max scroll offset so at least one option row remains visible."""
        pq = self.active
        if pq is None or not pq.options:
            return 0
        visible = self._visible_option_count(max_lines)
        return max(0, len(pq.options) - visible)

    # -- 스크롤 (테스트 contract v1.1.1) --------------------------------------

    def scroll_up(self, max_lines: int = MAX_PANEL_LINES) -> None:
        """1줄 위로 — test 고정: 3 → 2 (TUI_UX_FIX_DESIGN.md ③)."""
        self.scroll_offset = self._scroll_offset - 1

    def scroll_down(self, max_lines: int = MAX_PANEL_LINES) -> None:
        """페이지 단위 아래로 — PgDn 한 번에 가시 영역만큼 (test 고정: 0 → 3)."""
        visible = self._visible_option_count(max_lines)
        self.scroll_offset = min(
            self._max_scroll(max_lines), self._scroll_offset + visible
        )

    # -- 1줄 단위 보조 (휠 / Alt+↑↓ — 현재 app.py 는 scroll_up/down 사용) -------

    def scroll_line_up(self, max_lines: int = MAX_PANEL_LINES) -> None:
        """휠 업 / Alt+↑ — 1줄 위로."""
        self.scroll_offset = self._scroll_offset - 1

    def scroll_line_down(self, max_lines: int = MAX_PANEL_LINES) -> None:
        """휠 다운 / Alt+↓ — 1줄 아래로."""
        self.scroll_offset = min(
            self._max_scroll(max_lines), self._scroll_offset + 1
        )

    def line_count(self, max_lines: int = MAX_PANEL_LINES) -> int:
        """논리 줄 수 — 질문 1 + 가시 옵션 수 + (queued 1), 최대 *max_lines*.

        app.py의 `_choice_height()`가 이 값을 `Dimension(preferred=...)`로 사용
        (호출 시 `self._CHOICE_MAX_LINES`를 인자로 전달).
        (wrap_lines=True인 Window가 실제 줄바꿈 높이를 계산하므로, 이 값은
        옵션 wrap 전의 논리 줄 수.)
        """
        pq = self.active
        if pq is None:
            return 0
        visible = self._visible_option_count(max_lines)
        shown = min(len(pq.options), visible)
        return min(max_lines, 1 + shown + (1 if len(self.queue) > 1 else 0))

    def render(self) -> FormattedText:
        if self._formatted is not None:
            return self._formatted
        pq = self.active
        if pq is None:
            self._formatted = FormattedText([("", "")])
            return self._formatted
        lines = [f"❓ {pq.agent_id}: {pq.question}"]
        # ★ 세로 목록 (결정 ③ 3-A) — 옵션당 1줄, 가로 join 제거
        # v1.1: scroll_offset부터 가시 개수만큼만 표시 + 인디케이터
        visible = self._visible_option_count(MAX_PANEL_LINES)
        start = min(self._scroll_offset, max(0, len(pq.options) - visible))
        end = start + visible
        for i in range(start, min(end, len(pq.options))):
            opt = pq.options[i]
            lines.append(f"   [{i + 1}] {opt}")
        # 잘림 인디케이터 (TUI_UX_FIX_DESIGN.md ③)
        total = len(pq.options)
        if total > visible:
            lines.append(f"   (옵션 {total}개 중 {start + 1}~{min(end, total)} 표시 — ↑↓/PgUp/PgDn으로 더 보기)")
        if len(self.queue) > 1:
            lines.append(f"   (queued {len(self.queue) - 1})")
        self._formatted = FormattedText([("bold", "\n".join(lines))])
        return self._formatted

    def control(self) -> FormattedTextControl:
        """레이아웃/테스트가 공유하는 안정적인 control 인스턴스를 반환.

        v1.2.1: 매 호출 새 객체 생성 대신 캐시 — Window.content 와
        choice_panel.control() 이 동일 인스턴스여야 한다 (pytest is 비교).
        """
        if self._control is None:
            self._control = FormattedTextControl(text=self.render)
        return self._control
