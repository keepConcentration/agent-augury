"""Core Session <-> SessionGateway bridge (M3).

Surfaces speak Wire; this bridge:

- publishes translated Core events (including ``ask_user`` -> ``human.question``)
- routes Wire commands to ``human_send`` / ``request_interrupt`` / quit
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .bus import SessionGateway
from .translate import translate_core_event
from .types import WireCommand, WireEvent, make_event

log = logging.getLogger(__name__)


class SessionLike(Protocol):
    """Minimal session surface used by the bridge."""

    def request_interrupt(self) -> None: ...

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
        source: dict[str, Any] | None = None,
    ) -> str: ...

    async def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]: ...


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
class PendingApproval:
    approval_id: str
    agent_id: str
    tool: str
    args_preview: dict[str, Any] = field(default_factory=dict)
    ttl_seconds: float | None = None


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
    _pending_approvals: deque[PendingApproval] = field(default_factory=deque)
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
    def pending_approval(self) -> PendingApproval | None:
        return self._pending_approvals[0] if self._pending_approvals else None

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
        elif wire["type"] == "thread.created" and wire.get("thread_id"):
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
        if typ == "approval.resolve":
            return self._handle_approval_resolve(cmd)
        if typ in ("human.send", "human.answer"):
            return self._handle_human_message(cmd)
        return {}

    def _handle_approval_resolve(self, cmd: WireCommand) -> dict[str, Any]:
        approval_id = str(cmd.get("approval_id") or "").strip()
        decision = str(cmd.get("decision") or "").strip().lower()
        reason = cmd.get("reason")
        if not approval_id:
            return {"ok": False, "error": "approval_id required"}
        if decision not in ("granted", "denied"):
            return {"ok": False, "error": "decision must be granted|denied"}
        if self.session is None or not hasattr(self.session, "resolve_approval"):
            return {"ok": False, "error": "no session.resolve_approval installed"}
        reason_s = None if reason is None else str(reason)
        self.clear_approval(approval_id)
        self._maybe_schedule(
            self.session.resolve_approval(
                approval_id, decision, reason=reason_s
            )
        )
        return {"queued": True, "approval_id": approval_id, "decision": decision}

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
        known = self._coerce_known_thread(thread_id) if thread_id else None
        if known:
            use_id = known
            self._recent_thread = known
        elif thread_id and self.session is not None:
            # Unknown id — Session.human_send falls back to durable ``human`` thread.
            use_id = str(thread_id)
        elif thread_id:
            use_id = str(thread_id)
            self._recent_thread = use_id
        else:
            return {"queued": False, "error": "no thread_id"}

        source = cmd.get("source")
        if source is not None and not isinstance(source, dict):
            source = None

        self._dispatch_send(
            use_id, content, mentions or None, source=source
        )
        return {"queued": True, "thread_id": use_id}

    def _coerce_known_thread(self, thread_id: Any) -> str | None:
        """Return a MessageServer-known thread id, or None if unknown/unset."""
        if not thread_id:
            return None
        ref = str(thread_id).strip()
        if not ref:
            return None
        session = self.session
        server = getattr(session, "server", None) if session is not None else None
        if server is None:
            return ref
        resolve = getattr(server, "resolve_thread_id", None)
        if not callable(resolve):
            return ref
        known = resolve(ref)
        if known:
            return known
        if self._recent_thread:
            recent = resolve(self._recent_thread)
            if recent:
                return recent
        return None

    def _dispatch_send(
        self,
        thread_id: str,
        content: str,
        mentions: list[str] | None,
        *,
        source: dict[str, Any] | None = None,
    ) -> None:
        if self.send_fn is not None:
            result = self.send_fn(
                thread_id, content, mentions=mentions, source=source
            )
            self._maybe_schedule(result)
            return
        if self.session is None:
            raise RuntimeError("no session/send_fn installed on SessionBridge")
        self._maybe_schedule(
            self.session.human_send(
                thread_id, content, mentions=mentions, source=source
            )
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
            task = loop.create_task(result)  # type: ignore[arg-type]

            def _log_task_error(done: asyncio.Task[Any]) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = done.exception()
                    if exc is not None:
                        log.exception("bridge async command failed: %s", exc)

            task.add_done_callback(_log_task_error)
        else:
            loop.run_until_complete(result)  # type: ignore[arg-type]

    def _do_interrupt(self) -> None:
        if self.on_interrupt is not None:
            self.on_interrupt()
        elif self.session is not None:
            self.session.request_interrupt()
        self.set_running(False)
        self.gateway.publish(make_event("log", text="run interrupted"))

    def track_approval_request(
        self,
        *,
        approval_id: str,
        agent_id: str,
        tool: str,
        args_preview: dict[str, Any] | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        """Record a pending tool approval for Discord/Ink routing."""
        aid = str(approval_id or "").strip()
        if not aid:
            return
        # Deduplicate joins (same approval_id republished).
        for pa in self._pending_approvals:
            if pa.approval_id == aid:
                return
        self._pending_approvals.append(
            PendingApproval(
                approval_id=aid,
                agent_id=str(agent_id or ""),
                tool=str(tool or ""),
                args_preview=dict(args_preview or {}),
                ttl_seconds=ttl_seconds,
            )
        )

    def clear_approval(self, approval_id: Any | None = None) -> PendingApproval | None:
        """Drop a pending approval (by id, or oldest if *approval_id* is None)."""
        if not self._pending_approvals:
            return None
        if approval_id:
            aid = str(approval_id)
            for i, pa in enumerate(self._pending_approvals):
                if pa.approval_id == aid:
                    del self._pending_approvals[i]
                    return pa
            return None
        return self._pending_approvals.popleft()

    def _track_question(self, wire: WireEvent) -> None:
        pq = PendingQuestion(
            question_id=str(wire.get("question_id") or ""),
            thread_id=str(wire.get("thread_id") or ""),
            agent_id=str(wire.get("agent_id") or ""),
            question=str(wire.get("question") or ""),
            options=list(wire.get("options") or []),
        )
        known = self._coerce_known_thread(pq.thread_id) if pq.thread_id else None
        if known:
            pq.thread_id = known
            self._recent_thread = known
        # Do not poison recent_thread with agent-hallucinated ids (e.g. "c-user-test").
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
