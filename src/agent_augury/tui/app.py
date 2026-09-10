"""SessionTUIApplication - full-screen layout + input routing lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.styles import Style

from .choice_panel import ChoicePanel
from .commands import dispatch
from .input_bar import InputBar
from .key_aliases import install_tui_key_aliases
from .log_buffer import LogBuffer
from .renderer import mask_sensitive, render_event
from .router import RouterContext, route
from .status_bar import StatusBar


class SessionTUIApplication:
    """Single Application owning log / choice / status / input panes."""

    def __init__(
        self,
        session: Any,
        *,
        history_file: str | Path = "~/.agent-augury/human_history.txt",
        response_format: str = "text",
        key_aliases: bool = True,
        on_quit: Callable[[], None] | None = None,
        on_next_turn: Callable[[str], None] | None = None,
        preserve_log_on_exit: bool = True,
        pt_input: Any | None = None,
        pt_output: Any | None = None,
    ) -> None:
        if key_aliases:
            install_tui_key_aliases()

        self._session = session
        self._response_format = response_format
        self._on_quit = on_quit
        self._on_next_turn = on_next_turn
        self._preserve_log_on_exit = preserve_log_on_exit
        self._recent_thread: str | None = None
        self._running = False
        self._shutting_down = False

        self.log_buffer = LogBuffer(self._invalidate)
        self.choice_panel = ChoicePanel(self._invalidate)
        self.status_bar = StatusBar(session)
        self.input_bar = InputBar(
            self.handle_input,
            app_ref=self,
            on_quit=on_quit,
            history=history_file,
        )

        app_kwargs: dict[str, Any] = {
            "layout": Layout(self._build_layout()),
            "full_screen": True,
            "paste_mode": True,
            "style": Style.from_dict({
                "status": "bg:#222222",
                "choice": "bg:#333333",
            }),
            "refresh_interval": 0.1,
        }
        if pt_input is not None:
            app_kwargs["input"] = pt_input
        if pt_output is not None:
            app_kwargs["output"] = pt_output
        self.app = Application(**app_kwargs)
        self.log_buffer.set_app(self.app)
        self.choice_panel.set_app(self.app)
        self.input_bar.set_app(self.app)

        self._status_task: asyncio.Task[None] | None = None

    def _invalidate(self) -> None:
        if hasattr(self, "app"):
            self.app.invalidate()

    def _build_layout(self) -> HSplit:
        return HSplit([
            Window(self.log_buffer.control(), wrap_lines=True),
            ConditionalContainer(
                Window(
                    self.choice_panel.control(),
                    height=3,
                    style="class:choice",
                ),
                filter=self.choice_panel.has_pending,
            ),
            Window(
                self.status_bar.control(),
                height=1,
                style="class:status",
            ),
            self.input_bar.widget,
        ])

    def set_running(self, running: bool) -> None:
        self._running = running
        self.status_bar.set_running(running)
        self._invalidate()

    def append_event(self, event: dict[str, Any]) -> None:
        ansi = render_event(event)
        if ansi:
            self.log_buffer.append(ansi)

    def append_text(self, text: str) -> None:
        if text:
            self.log_buffer.append(text)

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
            if self._on_quit is not None:
                self._on_quit()
            try:
                if self.app.is_running:
                    self.app.exit()
            except Exception:  # noqa: BLE001
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
            return

        if result.kind == "plain":
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
            self.append_text("\u2717 send failed: no active thread")
            return
        try:
            await self._session.human_send(
                thread_id,
                content=content,
                mentions=mentions,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_text(f"\u2717 send failed: {exc}")
            return
        preview = mask_sensitive(content)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        who = f"mentions={mentions}" if mentions else "broadcast"
        # Design R10: "\u2713 human \u2192 {thread} ..."
        self.append_text(f"\u2713 human \u2192 {thread_id} ({who}): {preview}")
        self._recent_thread = thread_id

    def _resolve_recent_thread(self) -> str | None:
        if self._recent_thread:
            return self._recent_thread
        try:
            snap = self._session.server.snapshot()
            threads = snap.get("threads") or []
            if threads:
                return threads[0].get("thread_id")
        except Exception:  # noqa: BLE001
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
            if self.app.is_running:
                self.app.exit()
        except Exception:  # noqa: BLE001
            pass
        self.choice_panel.reset()
        try:
            self.input_bar.widget.buffer.reset()
        except Exception:  # noqa: BLE001
            pass
        if self._preserve_log_on_exit:
            tail = self.log_buffer.export_tail()
            if tail:
                print(tail, flush=True)
