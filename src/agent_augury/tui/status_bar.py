"""Status bar - snapshot line + 1s invalidate timer.

v1.1 (TUI_UX_FIX_DESIGN.md ②): 로그 follow 상태(FOLLOW/SCROLL) 인디케이터 추가.
`set_log_follow(bool)`로 SessionTUIApplication이 상태를 반영한다.

v1.2.1 (fix): `control()`이 매 호출 새 객체를 만들지 않도록 **캐시** — 레이아웃
Window가 들고 있는 control 과 status_bar.control() 이 동일 인스턴스여야
테스트/렌더 일관성이 보장된다 (pytest `is` 비교 실패 원인 수정).
"""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit.layout.controls import FormattedTextControl


class StatusBar:
    """threads / msgs / gate / phase / agents / follow - hybrid refresh."""

    def __init__(self, session: Any, *, refresh_interval: float = 1.0) -> None:
        self._session = session
        self._refresh_interval = refresh_interval
        self._idle_hint = '👤 waiting'
        self._running = False
        # v1.4: protocol violation counter (P2-8)
        self._violation_count = 0
        # v1.1: 로그 follow 상태 (app.py가 갱신) — TUI_UX_FIX_DESIGN.md ②
        self._log_follow = True
        # v1.2.1: control 인스턴스 캐시
        self._control: FormattedTextControl | None = None

    def set_running(self, running: bool) -> None:
        self._running = running

    def set_log_follow(self, follow: bool) -> None:
        """앱 레벨 follow 상태를 반영 (SessionTUIApplication이 호출)."""
        self._log_follow = follow

    def increment_violation(self) -> None:
        """v1.4: protocol violation counter increment (P2-8)."""
        self._violation_count += 1

    def _line(self) -> str:
        try:
            snap = self._session.server.snapshot()
        except Exception:  # noqa: BLE001
            snap = {"threads": [], "messages": [], "agents": []}
        gate = getattr(self._session, "gate", None)
        protocol = getattr(self._session, "protocol", None)
        phase = protocol.phase if protocol is not None else "n/a"
        gate_s = (
            "OPEN"
            if gate and getattr(gate, "is_open", False)
            else ("CLOSED" if gate else "n/a")
        )
        hint = "running" if self._running else self._idle_hint
        # v1.1: follow 상태 인디케이터 (TUI_UX_FIX_DESIGN.md ②)
        follow_s = "FOLLOW" if self._log_follow else "SCROLL"
        viol_s = f" viol={self._violation_count}" if self._violation_count else ""
        return (
            f"threads={len(snap.get('threads', []))} · "
            f"msgs={len(snap.get('messages', []))} · "
            f"gate={gate_s} · phase={phase} · "
            f"agents={len(snap.get('agents', []))} · "
            f"{follow_s}{viol_s} · {hint}"
        )

    def control(self) -> FormattedTextControl:
        """레이아웃/테스트가 공유하는 안정적인 control 인스턴스를 반환.

        v1.2.1: 매 호출 새 객체 생성 대신 캐시 — Window.content 와
        status_bar.control() 이 동일 인스턴스여야 한다 (pytest is 비교).
        """
        if self._control is None:
            self._control = FormattedTextControl(text=self._line)
        return self._control

    async def run(self, app: Any) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            app.invalidate()
