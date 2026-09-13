"""Core Session <-> SessionGateway bridge (M3).

Surfaces speak Wire; this bridge:

- publishes translated Core events (including ``ask_user`` -> ``human.question``)
- routes Wire commands to ``human_send`` / ``request_interrupt`` / quit
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .bus import SessionGateway
from .translate import translate_core_event
from .types import WireCommand, WireEvent, make_event


class SessionLike(Protocol):
    """Minimal session surface used by the bridge."""

    def request_interrupt(self) -> None: ...

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
    ) -> str: ...


SendFn = Callable[..., Any]  # sync or async human_send substitute
InterruptFn = Callable[[], None]
QuitFn = Callable[[], None]


@dataclass
class PendingQuestion:
    question_id: str
    thread_id: str
    agent_id: str
    question: str
    options: list[str] = field(default_factory=list)


@dataclass
class SessionBridge:
    """Bind a Core session (or demo stand-in) to a :class:`SessionGateway`."""

    gateway: SessionGateway
    session: SessionLike | None = None
    on_interrupt: InterruptFn | None = None
    on_quit: QuitFn | None = None
    send_fn: SendFn | None = None
    loop: asyncio.AbstractEventLoop | None = None
    _pending: deque[PendingQuestion] = field(default_factory=deque)
    _recent_thread: str | None = None
    _running: bool = False

    def install(self) -> None:
        """Wire ``gateway.on_command`` to this bridge."""
        self.gateway.on_command = self.handle_command

    def attach_session_callbacks(self, session: Any) -> None:
        """Chain Core ``on_step`` / ``on_tool_event`` into Wire publish (non-destructive)."""
        prev_step = getattr(session, "on_step", None)
        prev_tool = getattr(session, "on_tool_event", None)

        def on_step(agent_id: str, result: Any) -> None:
            self.publish_core_event(
                {"type": "step", "agent_id": agent_id, "result": result}
            )
            if prev_step is not None:
                prev_step(agent_id, result)

        def on_tool_event(event: dict[str, Any]) -> None:
            self.publish_core_event(event)
            if prev_tool is not None:
                prev_tool(event)

        session.on_step = on_step
        session.on_tool_event = on_tool_event
        self.session = session

    @property
    def pending(self) -> PendingQuestion | None:
        return self._pending[0] if self._pending else None

    @property
    def recent_thread(self) -> str | None:
        return self._recent_thread

    def set_running(self, running: bool) -> None:
        self._running = running

    def publish_core_event(self, event: dict[str, Any]) -> WireEvent | None:
        """Translate and fan-out a Core observer event."""
        wire = translate_core_event(event)
        if wire is None:
            return None
        if wire["type"] == "human.question":
            self._track_question(wire)
        elif wire["type"] == "thread.created" and wire.get("thread_id") or wire.get("thread_id"):
            self._recent_thread = str(wire["thread_id"])
        self.gateway.publish(wire)
        return wire

    def publish_ask_user(
        self,
        *,
        agent_id: str,
        thread_id: str,
        question: str,
        options: list[str] | None = None,
        question_id: str | None = None,
    ) -> WireEvent:
        """Convenience for demos / tests — emit a structured HITL question."""
        event: dict[str, Any] = {
            "type": "tool",
            "tool": "ask_user",
            "agent_id": agent_id,
            "args": {
                "thread": thread_id,
                "question": question,
                "options": list(options or []),
            },
        }
        if question_id:
            event["question_id"] = question_id
        out = self.publish_core_event(event)
        assert out is not None
        return out

    def handle_command(self, cmd: WireCommand) -> dict[str, Any]:
        typ = cmd.get("type")
        if typ == "session.interrupt":
            self._do_interrupt()
            return {"interrupted": True}
        if typ == "session.quit":
            self._do_interrupt()
            if self.on_quit is not None:
                self.on_quit()
            self.gateway.publish(make_event("session.ended", reason="quit"))
            return {}
        if typ == "session.start":
            self.set_running(True)
            return {"started": True}
        if typ == "human.skip":
            skipped = self._pop_pending(cmd.get("question_id"))
            if skipped is None:
                return {"skipped": False, "reason": "no pending question"}
            self.gateway.publish(
                make_event("log", text=f"skip question from {skipped.agent_id}")
            )
            return {"skipped": True, "question_id": skipped.question_id}
        if typ in ("human.send", "human.answer"):
            return self._handle_human_message(cmd)
        return {}

    def _handle_human_message(self, cmd: WireCommand) -> dict[str, Any]:
        content = str(cmd.get("content", ""))
        typ = cmd.get("type")
        thread_id = cmd.get("thread_id")
        mentions = list(cmd.get("mentions") or [])

        if typ == "human.answer":
            pq = self._pop_pending(cmd.get("question_id"))
            if pq is None:
                return {"queued": False, "error": "no pending question"}
            thread_id = thread_id or pq.thread_id
            if not mentions:
                mentions = [pq.agent_id] if pq.agent_id else []
            # Map bare option index to option text (pt TUI parity).
            content = _resolve_option_content(pq, content)

        thread_id = thread_id or self._recent_thread
        if not thread_id:
            return {"queued": False, "error": "no thread_id"}

        self._recent_thread = str(thread_id)
        self._dispatch_send(str(thread_id), content, mentions or None)
        return {"queued": True, "thread_id": thread_id}

    def _dispatch_send(
        self,
        thread_id: str,
        content: str,
        mentions: list[str] | None,
    ) -> None:
        if self.send_fn is not None:
            result = self.send_fn(thread_id, content, mentions=mentions)
            self._maybe_schedule(result)
            return
        if self.session is None:
            raise RuntimeError("no session/send_fn installed on SessionBridge")
        self._maybe_schedule(
            self.session.human_send(thread_id, content, mentions=mentions)
        )

    def _maybe_schedule(self, result: Any) -> None:
        if not isinstance(result, Awaitable):
            return
        loop = self.loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)  # type: ignore[arg-type]
                return
        if loop.is_running():
            loop.create_task(result)  # type: ignore[arg-type]
        else:
            loop.run_until_complete(result)  # type: ignore[arg-type]

    def _do_interrupt(self) -> None:
        if self.on_interrupt is not None:
            self.on_interrupt()
        elif self.session is not None:
            self.session.request_interrupt()
        self.set_running(False)
        self.gateway.publish(make_event("log", text="run interrupted"))

    def _track_question(self, wire: WireEvent) -> None:
        pq = PendingQuestion(
            question_id=str(wire.get("question_id") or ""),
            thread_id=str(wire.get("thread_id") or ""),
            agent_id=str(wire.get("agent_id") or ""),
            question=str(wire.get("question") or ""),
            options=list(wire.get("options") or []),
        )
        if pq.thread_id:
            self._recent_thread = pq.thread_id
        self._pending.append(pq)

    def _pop_pending(self, question_id: Any | None) -> PendingQuestion | None:
        if not self._pending:
            return None
        if question_id:
            qid = str(question_id)
            for i, pq in enumerate(self._pending):
                if pq.question_id == qid:
                    del self._pending[i]
                    return pq
            return None
        return self._pending.popleft()


def _resolve_option_content(pq: PendingQuestion, content: str) -> str:
    text = content.strip()
    if not pq.options:
        return content
    try:
        idx = int(text)
    except ValueError:
        return content
    if 1 <= idx <= len(pq.options):
        return pq.options[idx - 1]
    return content
