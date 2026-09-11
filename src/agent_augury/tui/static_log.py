"""Static terminal log writer (Ink ``<Static>`` / log-update style).

Design: docs/tui/TUI_STATIC_BOTTOM_DOCK_DESIGN.md (v1.5)

Cursor/Ink avoids prompt flicker by painting ``erase + static + frame`` in
**one** stdout flush. Stock prompt_toolkit ``in_terminal`` calls
``renderer.erase()`` which **flushes** before the log write, so ``👤 >``
briefly disappears.

This writer uses a custom atomic paint:
  soft-erase (no mid flush when VT-buffered) → write logs → redraw chrome
  → single flush at the end of ``_redraw``.
"""

from __future__ import annotations

import asyncio
import sys
from asyncio import Future
from collections.abc import Callable
from typing import Any

from prompt_toolkit.application.current import get_app_or_none

__all__ = ["StaticLogWriter"]


class StaticLogWriter:
    """Enqueue ANSI/text payloads; flush above the live prompt_toolkit chrome."""

    def __init__(
        self,
        app: Any,
        *,
        flush_interval: float = 0.05,
        max_batch_chars: int = 16_384,
        write_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._app = app
        self._flush_interval = flush_interval
        self._max_batch_chars = max_batch_chars
        self._write_fn = write_fn
        self._buf: list[str] = []
        self._buf_chars = 0
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    def _emit(self, payload: str, *, flush: bool = True) -> None:
        if self._write_fn is not None:
            self._write_fn(payload)
            return
        app = self._app
        output = getattr(app, "output", None) if app is not None else None
        if output is not None:
            try:
                write = getattr(output, "write_raw", None) or getattr(output, "write", None)
                if write is not None:
                    write(payload)
                    if flush:
                        output.flush()
                    return
            except Exception:  # noqa: BLE001, S110
                pass
        out = getattr(sys, "stdout", None)
        if out is None:
            return
        out.write(payload)
        if flush:
            out.flush()

    def enqueue(self, text: str) -> None:
        if self._closed or not text:
            return
        payload = text if text.endswith("\n") else text + "\n"
        self._buf.append(payload)
        self._buf_chars += len(payload)
        if self._buf_chars >= self._max_batch_chars:
            self.flush_now()
            return
        self._schedule_flush()

    def _schedule_flush(self) -> None:
        app = self._app
        if app is not None and getattr(app, "is_running", False):
            if self._flush_task is not None and not self._flush_task.done():
                return
            try:
                if hasattr(app, "create_background_task"):
                    self._flush_task = app.create_background_task(self._delayed_flush())
                    return
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                loop = asyncio.get_running_loop()
                self._flush_task = loop.create_task(self._delayed_flush())
                return
            except RuntimeError:
                self.flush_now()
                return
        self.flush_now()

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._flush_interval)
        except asyncio.CancelledError:
            return
        await self._flush_async()

    def flush_now(self) -> None:
        """Flush buffer; schedule async erase/write when the app is running."""
        if not self._buf:
            return
        batch = "".join(self._buf)
        self._buf.clear()
        self._buf_chars = 0
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()

        app = self._app
        try:
            if (
                app is not None
                and getattr(app, "is_running", False)
                and hasattr(app, "create_background_task")
            ):
                app.create_background_task(self._write_in_terminal(batch))
                return
        except Exception:  # noqa: BLE001, S110
            pass
        self._emit(batch)

    async def _flush_async(self) -> None:
        if not self._buf:
            return
        batch = "".join(self._buf)
        self._buf.clear()
        self._buf_chars = 0
        self._flush_task = None
        await self._write_in_terminal(batch)

    async def _write_in_terminal(self, batch: str) -> None:
        """Ink-style atomic paint: soft-erase + logs + chrome redraw, one flush."""
        app = get_app_or_none() or self._app
        if app is None or not getattr(app, "_is_running", False):
            self._emit(batch)
            return

        # Serialize paints (same chaining as prompt_toolkit.in_terminal).
        previous = getattr(app, "_running_in_terminal_f", None)
        done: Future[None] = Future()
        app._running_in_terminal_f = done
        if previous is not None:
            await previous

        if getattr(app.output, "responds_to_cpr", False):
            await app.renderer.wait_for_cpr_responses()

        try:
            self._atomic_paint(app, batch)
        finally:
            if not done.done():
                done.set_result(None)

    def _atomic_paint(self, app: Any, batch: str) -> None:
        """Erase chrome + write Static batch + redraw without a blank gap flush.

        Unlike ``renderer.erase()`` (which flushes immediately), we queue the
        soft-erase and log bytes, then let ``_redraw`` flush once with the
        chrome already restored — same idea as Ink log-update.
        """
        renderer = app.renderer
        output = app.output
        cursor = getattr(renderer, "_cursor_pos", None)

        app._running_in_terminal = True
        try:
            hide = getattr(output, "hide_cursor", None)
            if callable(hide):
                try:
                    hide()
                except Exception:  # noqa: BLE001, S110
                    pass

            if cursor is not None:
                try:
                    if cursor.x:
                        output.cursor_backward(cursor.x)
                    if cursor.y:
                        output.cursor_up(cursor.y)
                    output.erase_down()
                    output.reset_attributes()
                except Exception:  # noqa: BLE001
                    # Fall back to stock erase (may flicker once).
                    renderer.erase()

            # Do not flush here — keep erase + logs in the same paint when VT-buffered.
            self._emit(batch, flush=False)

            renderer.reset()
            app._running_in_terminal = False
            app._request_absolute_cursor_position()
            app._redraw()

            show = getattr(output, "show_cursor", None)
            if callable(show):
                try:
                    show()
                    output.flush()
                except Exception:  # noqa: BLE001, S110
                    pass
        finally:
            app._running_in_terminal = False

    def close(self) -> None:
        """Flush remaining bytes; stop scheduling."""
        self._closed = True
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
        self.flush_now()
