"""SessionTUIApplication - Ink-style Static logs + bottom chrome.

v1.5 (TUI_STATIC_BOTTOM_DOCK_DESIGN.md):
- Logs commit to host scrollback via ``run_in_terminal`` (erase → write →
  redraw), batched by ``StaticLogWriter`` — same contract as Ink ``<Static>``.
- Layout is bottom chrome only: choice? + input + status.
- ``full_screen=False``, ``mouse_support=False`` so the wheel scrolls the
  terminal (single scroll surface).
- Choice sits above input; removed on answer/skip.

v1.4: terminal streaming without batching / wrong ``app.run_in_terminal``.
v1.3–v1.2: ScrollablePane log UI — superseded.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.styles import Style

from .choice_panel import ChoicePanel
from .commands import dispatch
from .input_bar import InputBar
from .key_aliases import install_tui_key_aliases
from .log_buffer import LogBuffer
from .renderer import mask_sensitive, render_event
from .router import RouterContext, route
from .scrollable_pane import WheelScrollWindow
from .static_log import StaticLogWriter
from .status_bar import StatusBar


class SessionTUIApplication:
    """Bottom input/choice chrome; agent logs stream as Static terminal output."""

    _CHOICE_MAX_LINES = 8

    def __init__(
        self,
        session: Any,
        *,
        history_file: str | Path = "~/.agent-augury/human_history.txt",
        response_format: str = "text",
        key_aliases: bool = True,
        on_quit: Callable[[], None] | None = None,
        on_next_turn: Callable[[str], None] | None = None,
        on_interrupt: Callable[[], None] | None = None,
        initial_task_mode: bool = False,
        preserve_log_on_exit: bool = False,
        pt_input: Any | None = None,
        pt_output: Any | None = None,
    ) -> None:
        if key_aliases:
            install_tui_key_aliases()

        self._session = session
        self._response_format = response_format
        self._on_quit = on_quit
        self._on_next_turn = on_next_turn
        self._on_interrupt = on_interrupt
        self._initial_task_mode = initial_task_mode
        self._preserve_log_on_exit = preserve_log_on_exit
        self._recent_thread: str | None = None
        self._pending_messages: list[dict[str, Any]] = []
        self._last_ctrl_c: float = 0.0
        self._running = False
        self._shutting_down = False
        # Kept for status-bar API compatibility; terminal scroll owns history.
        self._log_follow = True

        # Buffer for /commands + export; display is terminal streaming (no invalidate).
        self.log_buffer = LogBuffer(invalidate=None)
        self.choice_panel = ChoicePanel(self._invalidate)
        self.status_bar = StatusBar(session)
        self.input_bar = InputBar(
            self.handle_input,
            app_ref=self,
            on_quit=on_quit,
            on_text_changed=None,
            history=history_file,
        )

        self.choice_window = WheelScrollWindow(
            self.choice_panel.control(),
            height=self._choice_height,
            wrap_lines=True,
            style="class:choice",
            wheel_handler=None,
        )

        app_kwargs: dict[str, Any] = {
            "layout": Layout(self._build_layout()),
            "full_screen": False,
            "paste_mode": True,
            # Let the host terminal own the mouse wheel (single scroll).
            "mouse_support": False,
            "key_bindings": self._build_app_kb(),
            "style": Style.from_dict({
                "status": "bg:#333333 fg:#cccccc",
                "choice": "bg:#333333",
            }),
            "refresh_interval": 0.1,
        }
        if pt_input is not None:
            app_kwargs["input"] = pt_input
        if pt_output is not None:
            app_kwargs["output"] = pt_output
        self.app = Application(**app_kwargs)
        self.log_buffer.set_app(self.app, bind_invalidate=False)
        self.choice_panel.set_app(self.app)
        self.input_bar.set_app(self.app)
        self.static_log = StaticLogWriter(self.app)

        self._status_task: asyncio.Task[None] | None = None

    def _invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def _build_layout(self) -> HSplit:
        # Bottom chrome only — logs are printed above via the terminal.
        return HSplit([
            ConditionalContainer(
                self.choice_window,
                filter=self.choice_panel.has_pending,
            ),
            self.input_bar.widget,
            Window(
                self.status_bar.control(),
                height=1,
                style="class:status",
            ),
        ])

    def _choice_height(self) -> Dimension:
        return Dimension(
            min=1,
            max=self._CHOICE_MAX_LINES,
            preferred=self.choice_panel.line_count(self._CHOICE_MAX_LINES),
        )

    def _set_log_follow(self, follow: bool) -> None:
        self._log_follow = follow
        try:
            self.status_bar.set_log_follow(follow)
        except Exception:  # noqa: BLE001, S110
            pass

    def _build_app_kb(self) -> KeyBindings:
        """Choice-option keys only; log history uses the terminal scrollbar."""
        kb = KeyBindings()

        @kb.add("pageup")
        def _pgup(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_up(self._CHOICE_MAX_LINES)
                self.app.invalidate()

        @kb.add("pagedown")
        def _pgdn(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_down(self._CHOICE_MAX_LINES)
                self.app.invalidate()

        @kb.add("escape", "up")
        def _alt_up(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_up(self._CHOICE_MAX_LINES)
                self.app.invalidate()

        @kb.add("escape", "down")
        def _alt_down(event: Any) -> None:
            if self.choice_panel.has_pending_bool():
                self.choice_panel.scroll_line_down(self._CHOICE_MAX_LINES)
                self.app.invalidate()

        return kb

    def _emit_log(self, text: str) -> None:
        if not text:
            return
        self.log_buffer.append(text)
        self.static_log.enqueue(text)

    def _handle_ctrl_c(self) -> None:
        """Ctrl+C: interrupt run / double-tap quit."""
        now = time.monotonic()
        double = self._last_ctrl_c > 0.0 and (now - self._last_ctrl_c) < 1.0

        if self._running:
            if double:
                self.append_text("⚠️ Ctrl+C pressed twice — exiting...")
                if self._on_interrupt is not None:
                    self._on_interrupt()
                if self._on_quit is not None:
                    self._on_quit()
                try:
                    if self.app.is_running:
                        self.app.exit()
                except Exception:  # noqa: BLE001, S110
                    pass
                return
            if self._on_interrupt is not None:
                self._on_interrupt()
            self._last_ctrl_c = now
            try:
                self.input_bar.widget.buffer.reset()
            except Exception:  # noqa: BLE001, S110
                pass
            self.append_text(
                "⏹ Agents stopped. Send a message to resume, "
                "or press Ctrl+C again to exit."
            )
            return

        if double:
            self.append_text("⚠️ Ctrl+C pressed twice — exiting...")
            if self._on_quit is not None:
                self._on_quit()
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception:  # noqa: BLE001, S110
                pass
            return
        self._last_ctrl_c = now
        self.append_text("⚠️ Press Ctrl+C again to exit, or type /quit")

    def set_running(self, running: bool) -> None:
        self._running = running
        self.status_bar.set_running(running)
        self._invalidate()

    def append_event(self, event: dict[str, Any]) -> None:
        if event.get("protocol_violation"):
            try:
                self.status_bar.increment_violation()
            except Exception:  # noqa: BLE001, S110
                pass
        if event.get("type") == "create_thread" and self._pending_messages:
            tid = event.get("thread_id")
            if tid:
                self.create_background_task(self._flush_pending_messages(tid))
        ansi = render_event(event)
        if ansi:
            self._emit_log(ansi)

    def append_text(self, text: str) -> None:
        if text:
            self._emit_log(text)

    def on_ask_user(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        result: Any = None,
    ) -> None:
        self.choice_panel.on_ask_user(agent_id, tool, args, result)
        thread_id = args.get("thread")
        if thread_id:
            self._recent_thread = thread_id
        try:
            question = args.get("question", "")
            options = list(args.get("options") or [])
            lines = [f"❓ [{agent_id}] {question}"]
            for i, opt in enumerate(options, 1):
                lines.append(f"   [{i}] {opt}")
            self._emit_log(mask_sensitive("\n".join(lines)))
            self._emit_log("-" * 40)
        except Exception:  # noqa: BLE001, S110
            pass
        self._invalidate()

    def create_background_task(self, coro: Any) -> Any:
        return self.app.create_background_task(coro)

    def create_task(self, coro: Any) -> Any:
        return self.create_background_task(coro)

    async def handle_input(self, text: str) -> None:
        ctx = RouterContext(
            active_question=self.choice_panel.active,
            recent_thread=self._resolve_recent_thread(),
            response_format=self._response_format,
        )
        result = route(text, ctx)

        if result.kind == "ignored":
            return

        if result.kind == "quit":
            if self._running and self._on_interrupt is not None:
                self._on_interrupt()
            if self._on_quit is not None:
                self._on_quit()
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception:  # noqa: BLE001, S110
                pass
            return

        if result.kind == "command":
            out = dispatch(
                result.command or "",
                result.args,
                {
                    "snapshot": self._session.server.snapshot(),
                    "gate": getattr(self._session, "gate", None),
                    "phase": (
                        self._session.protocol.phase
                        if getattr(self._session, "protocol", None) is not None
                        else "n/a"
                    ),
                    "recent_thread": self._recent_thread,
                    "log_buffer": self.log_buffer,
                    "choice_panel": self.choice_panel,
                },
            )
            if out:
                self.append_text(out)
            return

        if result.kind in ("choice", "question_reply"):
            if result.notice:
                self.append_text(result.notice)
            await self._send(
                result.thread_id,
                result.content,
                mentions=result.mentions,
            )
            self.choice_panel.answer()
            self._invalidate()
            return

        if result.kind == "plain":
            if self._initial_task_mode:
                if self._on_next_turn is not None:
                    self._on_next_turn(result.content)
                self._initial_task_mode = False
                return
            await self._send(result.thread_id, result.content, mentions=None)
            if not self._running and self._on_next_turn is not None:
                self._on_next_turn(result.content)
            return

    async def _send(
        self,
        thread_id: str | None,
        content: str,
        *,
        mentions: list[str] | None,
    ) -> None:
        if not thread_id:
            self._pending_messages.append({
                "content": content,
                "mentions": mentions,
            })
            preview = mask_sensitive(content)
            if len(preview) > 60:
                preview = preview[:57] + "..."
            self.append_text(f"⏳ 대기 중 (thread 생성 후 자동 전송): {preview}")
            return
        try:
            await self._session.human_send(
                thread_id,
                content=content,
                mentions=mentions,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_text("✗ send failed: " + f"{exc}")
            return
        preview = mask_sensitive(content)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        who = f"mentions={mentions}" if mentions else "broadcast"
        self.append_text(f"✓ human → {thread_id} ({who}): {preview}")
        self._recent_thread = thread_id

    async def _flush_pending_messages(self, thread_id: str) -> None:
        if not self._pending_messages:
            return
        msgs = self._pending_messages[:]
        self._pending_messages.clear()
        self.append_text(f"📤 대기 메시지 {len(msgs)}건 전송 중...")
        for msg in msgs:
            await self._send(thread_id, msg["content"], mentions=msg["mentions"])

    def _resolve_recent_thread(self) -> str | None:
        if self._recent_thread:
            return self._recent_thread
        try:
            snap = self._session.server.snapshot()
            threads = snap.get("threads") or []
            if threads:
                return threads[0].get("thread_id")
        except Exception:  # noqa: BLE001, S110
            pass
        return None

    async def run(self) -> None:
        self._status_task = asyncio.create_task(self.status_bar.run(self.app))
        try:
            await self.app.run_async()
        finally:
            if self._status_task is not None:
                self._status_task.cancel()
                try:
                    await self._status_task
                except asyncio.CancelledError:
                    pass
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self.static_log.close()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            if self.app.is_running:
                self.app.exit()
        except Exception:  # noqa: BLE001, S110
            pass
        self.choice_panel.reset()
        try:
            self.input_bar.widget.buffer.reset()
        except Exception:  # noqa: BLE001, S110
            pass
        # Logs already streamed to the terminal; optional dump only if asked.
        if self._preserve_log_on_exit:
            tail = self.log_buffer.export_tail()
            if tail:
                print(tail, flush=True)
