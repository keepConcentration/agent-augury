"""HumanTUIAdapter — thin shim over ``tui/`` (compat for existing tests).

Design reference: ``docs/tui/SESSION_TUI_REDESIGN.md`` (v2.5).
Legacy PromptSession path remains for non-Application callers / unit tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory

from agent_augury.tui.choice_panel import ChoicePanel, PendingQuestion
from agent_augury.tui.router import RouterContext, route

__all__ = ["HumanTUIAdapter", "PendingQuestion"]


class HumanTUIAdapter:
    """Compat adapter: same public API as TUI v1.0, routing via ``tui.router``."""

    def __init__(
        self,
        session: Any,
        *,
        response_format: Literal["text", "number"] = "text",
        history_file: str | Path = "~/.agent-augury/human_history.txt",
        input_prompt: str = "👤 > ",
        multiline: bool = True,
        pin_options: bool = True,
        choice_queue: bool = True,
    ) -> None:
        self._session = session
        self._response_format = response_format
        self._input_prompt = input_prompt
        self._multiline = multiline
        self._pin_options = pin_options
        self._choice_queue = choice_queue

        self._panel = ChoicePanel()
        self._recent_thread: str | None = None
        self._history_path = Path(history_file).expanduser()
        self._multiline_setting = multiline
        self._ps: PromptSession | None = None
        self._stop: bool = False

    # -- compat properties ---------------------------------------------------

    @property
    def _pending_question(self) -> PendingQuestion | None:
        return self._panel.active

    @_pending_question.setter
    def _pending_question(self, value: PendingQuestion | None) -> None:
        self._panel.reset()
        if value is not None:
            self._panel.push(value)

    @property
    def _pending_queue(self) -> list[PendingQuestion]:
        return list(self._panel.queue)

    def _ensure_session(self) -> PromptSession:
        if self._ps is None:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._ps = PromptSession(
                history=FileHistory(str(self._history_path)),
                multiline=self._multiline_setting,
                enable_open_in_editor=True,
            )
        return self._ps

    def on_ask_user(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        if not self._choice_queue and self._panel.has_pending_bool():
            self._panel.reset()
        self._panel.on_ask_user(agent_id, tool, args, result)
        thread_id = args.get("thread", "")
        if thread_id:
            self._recent_thread = thread_id

    def _resolve_option(self, text: str) -> str | None:
        from agent_augury.tui.router import _resolve_option

        pq = self._panel.active
        if pq is None or pq.status != "pending":
            return None
        return _resolve_option(pq, text, response_format=self._response_format)

    def _render_toolbar(self) -> HTML:
        pq = self._panel.active
        if not pq or pq.status != "pending":
            return HTML("")
        lines = [f"<b>❓ {pq.agent_id}:</b> {pq.question}"]
        if pq.options:
            choices = "   ".join(
                f"[{i + 1}] {opt}" for i, opt in enumerate(pq.options)
            )
            lines.append(f"   {choices}")
        if self._choice_queue and len(self._panel.queue) > 1:
            lines.append(f"   <i>(대기 {len(self._panel.queue) - 1}개)</i>")
        return HTML("\n".join(lines))

    async def _deliver(self, text: str) -> None:
        ctx = RouterContext(
            active_question=self._panel.active,
            recent_thread=self._recent_thread or self._fallback_thread(),
            response_format=self._response_format,
        )
        result = route(text, ctx)
        if result.kind in ("ignored", "quit", "command"):
            return
        thread_id = result.thread_id
        if not thread_id:
            return
        try:
            await self._session.human_send(
                thread_id,
                content=result.content,
                mentions=result.mentions,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [tui] failed to deliver: {exc}", flush=True)
            return
        self._recent_thread = thread_id
        if result.kind in ("choice", "question_reply"):
            self._panel.answer()

    def _fallback_thread(self) -> str | None:
        snap = self._session.server.snapshot()
        threads = snap.get("threads") or []
        if threads:
            return threads[0].get("thread_id")
        return None

    async def run_input_loop(self) -> None:
        ps = self._ensure_session()
        while not self._stop:
            try:
                text = await ps.prompt_async(
                    self._input_prompt,
                    bottom_toolbar=(
                        self._render_toolbar if self._pin_options else None
                    ),
                )
            except (EOFError, KeyboardInterrupt):
                break
            text = text.strip()
            if not text:
                continue
            if text.lower() in ("/quit", "/exit"):
                break
            await self._deliver(text)

    def stop(self) -> None:
        self._stop = True

    def cleanup(self) -> None:
        self._panel.reset()
        self._stop = True
