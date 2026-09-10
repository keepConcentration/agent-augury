"""ScrollablePane / Window subclasses for wheel-scroll routing.

Design: docs/tui/TUI_SCROLL_INITIAL_TASK_LOG_FIX_DESIGN.md

prompt_toolkit sends wheel events through coordinate-based mouse handlers
(not Keys.ScrollUp/ScrollDown key bindings). These subclasses intercept
the wheel events and delegate them to app-level callbacks so that
ScrollablePane.vertical_scroll (and choice panel scroll) are updated correctly.
"""

from __future__ import annotations

from prompt_toolkit.layout import ScrollablePane, Window
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

__all__ = ["FollowScrollablePane", "WheelScrollWindow"]


class FollowScrollablePane(ScrollablePane):
    """ScrollablePane that delegates wheel events to an app callback.

    Prompt_toolkit's default mouse handler chain passes SCROLL_UP/SCROLL_DOWN
    to the innermost Window._mouse_handler(), which tries to scroll that
    Window.vertical_scroll (always 0 inside a ScrollablePane whose virtual
    screen already shows all content).  We intercept after the coordinate
    translation (_copy_over_mouse_handlers) and re-route wheel events to a
    callback that updates ScrollablePane.vertical_scroll.
    """

    def __init__(self, content, wheel_handler=None, **kwargs):
        super().__init__(content, **kwargs)
        self._wheel_handler = wheel_handler

    def _copy_over_mouse_handlers(
        self, mouse_handlers, temp_mouse_handlers, write_position, virtual_width
    ):
        super()._copy_over_mouse_handlers(
            mouse_handlers, temp_mouse_handlers, write_position, virtual_width
        )
        if self._wheel_handler is None:
            return

        y0 = write_position.ypos
        x0 = write_position.xpos
        for y in range(write_position.height):
            row = mouse_handlers.mouse_handlers.get(y + y0)
            if not row:
                continue
            for x in range(virtual_width):
                handler = row.get(x + x0)
                if handler is not None:
                    row[x + x0] = self._wrap(handler)

    def _wrap(self, handler):
        wheel = self._wheel_handler

        def wrapped(event: MouseEvent):
            if event.event_type in (
                MouseEventType.SCROLL_UP,
                MouseEventType.SCROLL_DOWN,
            ):
                return wheel(event)
            return handler(event)

        return wrapped


class WheelScrollWindow(Window):
    """Window that delegates wheel events to an app callback.

    Used for fixed (non-ScrollablePane) windows such as the choice panel,
    so that wheel scrolls the panel options rather than doing nothing.
    """

    def __init__(self, *args, wheel_handler=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_handler = wheel_handler

    def _mouse_handler(self, mouse_event: MouseEvent):
        if self._wheel_handler is not None and mouse_event.event_type in (
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        ):
            return self._wheel_handler(mouse_event)
        return super()._mouse_handler(mouse_event)
