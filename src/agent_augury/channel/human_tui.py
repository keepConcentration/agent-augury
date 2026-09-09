"""HumanTUIAdapter — prompt_toolkit-based always-on input channel (TUI v1.0).

Implements the TUI-level input loop for human-in-the-loop sessions:

* Persistent bottom-anchored input line via ``PromptSession.prompt_async()``
* Pinned ``ask_user`` choice panel rendered in ``bottom_toolbar``
* Number-key quick response (substitutes option text before ``human_send``)
* ``FileHistory`` for input recall across sessions (R6)
* asyncio-native — no ``run_in_executor`` / threading (D1, R8)

Design reference: ``docs/tui/TUI_ALWAYS_ON_INPUT_DESIGN.md`` (v0.6).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory


@dataclass
class PendingQuestion:
    """An active ask_user question waiting for human response (D3)."""

    thread_id: str
    agent_id: str
    question: str
    options: list[str]
    created_at: float
    status: Literal["pending", "answered", "dismissed"] = "pending"


class HumanTUIAdapter:
    """prompt_toolkit-based always-on input adapter.

    Lifecycle:

    1. Instantiate with a Session and TUI config.
    2. Call :meth:`run_input_loop` — runs until the session closes or user quits.
    3. Tool events (``ask_user``) arrive via :meth:`on_ask_user` — updates the
       pinned panel.
    4. User input is routed through :meth:`_deliver` — either choice
       substitution or plain ``human_send``.
    """

    def __init__(
        self,
        session: Any,  # Session (avoid circular import)
        *,
        response_format: Literal["text", "number"] = "text",
        history_file: str | Path = "~/.agent-augury/human_history.txt",
        input_prompt: str = "👤 > ",
        multiline: bool = True,
        pin_options: bool = True,
        choice_queue: bool = False,
    ) -> None:
        self._session = session
        self._response_format = response_format
        self._input_prompt = input_prompt
        self._multiline = multiline
        self._pin_options = pin_options
        self._choice_queue = choice_queue

        # Pending question state (v1.0: latest 1 + optional queue)
        self._pending_question: PendingQuestion | None = None
        self._pending_queue: list[PendingQuestion] = []

        # Track the most recent active thread for non-question input
        self._recent_thread: str | None = None

        # History file path (PromptSession created lazily — D5: non-TTY safe)
        self._history_path = Path(history_file).expanduser()
        self._multiline_setting = multiline

        # PromptSession is created lazily on first run_input_loop() call
        # so that unit tests (non-TTY) can exercise _deliver/_resolve_option
        # without triggering Windows console detection.
        self._ps: PromptSession | None = None

        # Stop flag for clean shutdown
        self._stop: bool = False

    def _ensure_session(self) -> PromptSession:
        """Lazily create the PromptSession (first call only)."""
        if self._ps is None:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._ps = PromptSession(
                history=FileHistory(str(self._history_path)),
                multiline=self._multiline_setting,
                enable_open_in_editor=True,
            )
        return self._ps

    # -- ask_user event handling (D8) ----------------------------------------

    def on_ask_user(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        """Handle an ``ask_user`` tool event (D8: existing tool event path).

        Called by the CLI's ``on_tool_event`` callback when
        ``tool == "ask_user"``.  Updates the pending question so the next
        ``prompt_async()`` renders it in ``bottom_toolbar``.
        """
        thread_id = args.get("thread", "")
        question = args.get("question", "")
        options = args.get("options", [])

        pq = PendingQuestion(
            thread_id=thread_id,
            agent_id=agent_id,
            question=question,
            options=list(options) if options else [],
            created_at=time.time(),
        )

        if self._choice_queue:
            self._pending_queue.append(pq)

        self._pending_question = pq
        if thread_id:
            self._recent_thread = thread_id

    # -- option resolution (D7) ----------------------------------------------

    def _resolve_option(self, text: str) -> str | None:
        """If *text* is a valid option number, return the option text.

        Returns ``None`` when there is no pending question, the question has
        no options, or the input is not a valid 1-based index.
        """
        pq = self._pending_question
        if not pq or pq.status != "pending" or not pq.options:
            return None
        try:
            idx = int(text.strip())
        except ValueError:
            return None
        if 1 <= idx <= len(pq.options):
            return pq.options[idx - 1]
        return None

    # -- toolbar rendering ---------------------------------------------------

    def _render_toolbar(self) -> HTML:
        """Render the ``bottom_toolbar`` HTML from the current pending question."""
        pq = self._pending_question
        if not pq or pq.status != "pending":
            return HTML("")

        lines = [f"<b>❓ {pq.agent_id}:</b> {pq.question}"]
        if pq.options:
            choices = "   ".join(
                f"[{i + 1}] {opt}" for i, opt in enumerate(pq.options)
            )
            lines.append(f"   {choices}")

        if self._choice_queue and len(self._pending_queue) > 1:
            lines.append(f"   <i>(대기 {len(self._pending_queue)}개)</i>")

        return HTML("\n".join(lines))

    # -- input routing (D2, D7, D10) -----------------------------------------

    async def _deliver(self, text: str) -> None:
        """Route user input: choice substitution or plain ``human_send``.

        * Number input while a question is pending → substitute option text
          and send to the asking agent only (D7, D10).
        * Plain text → ``human_send`` to the most recent active thread.
        """
        text = text.strip()
        if not text:
            return

        # Quick-response to a pending question?
        pq = self._pending_question
        if pq and pq.status == "pending":
            option_text = self._resolve_option(text)
            if option_text is not None:
                content = option_text if self._response_format == "text" else text
                try:
                    await self._session.human_send(
                        pq.thread_id,
                        content=content,
                        mentions=[pq.agent_id],
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  [tui] failed to deliver reply: {exc}", flush=True)
                pq.status = "answered"
                self._pending_question = None
                # Pop next from queue if choice_queue mode
                if self._choice_queue and self._pending_queue:
                    self._pending_queue.pop(0)
                    if self._pending_queue:
                        self._pending_question = self._pending_queue[0]
                return

        # Plain text → human_send to recent thread
        thread_id = self._recent_thread
        if thread_id is None:
            snap = self._session.server.snapshot()
            if snap["threads"]:
                thread_id = snap["threads"][0]["thread_id"]

        if thread_id:
            try:
                await self._session.human_send(thread_id, content=text)
            except Exception as exc:  # noqa: BLE001
                print(f"  [tui] failed to deliver: {exc}", flush=True)

    # -- main loop (D1, R8) --------------------------------------------------

    async def run_input_loop(self) -> None:
        """Main input loop — runs until Ctrl+D, ``/quit``, or :meth:`stop`.

        Uses :meth:`PromptSession.prompt_async` so input is fully
        asyncio-native (no ``run_in_executor``).
        """
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

    # -- lifecycle (D13) -----------------------------------------------------

    def stop(self) -> None:
        """Signal the input loop to exit (idempotent)."""
        self._stop = True

    def cleanup(self) -> None:
        """Reset state on session close (D13)."""
        self._pending_question = None
        self._pending_queue.clear()
        self._stop = True
