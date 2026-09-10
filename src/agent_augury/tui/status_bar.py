"""Status bar - snapshot line + 1s invalidate timer."""

from __future__ import annotations

import asyncio
from typing import Any

from prompt_toolkit.layout.controls import FormattedTextControl


class StatusBar:
    """threads / msgs / gate / phase / agents - hybrid refresh."""

    def __init__(self, session: Any, *, refresh_interval: float = 1.0) -> None:
        self._session = session
        self._refresh_interval = refresh_interval
        self._idle_hint = '👤 waiting'
        self._running = False

    def set_running(self, running: bool) -> None:
        self._running = running

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
        return (
            f"threads={len(snap.get('threads', []))} · "
            f"msgs={len(snap.get('messages', []))} · "
            f"gate={gate_s} · phase={phase} · "
            f"agents={len(snap.get('agents', []))} · {hint}"
        )

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self._line)

    async def run(self, app: Any) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval)
            app.invalidate()
