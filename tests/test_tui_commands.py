"""Slash command + key alias smoke tests."""

from __future__ import annotations

from agent_augury.tui.commands import dispatch
from agent_augury.tui.key_aliases import install_tui_key_aliases


def test_dispatch_help():
    out = dispatch("help", "", {})
    assert "/quit" in out
    assert "Enter" in out
    assert "Ctrl+Enter" in out
    assert "Blank Enter is ignored" in out


def test_dispatch_unknown():
    out = dispatch("nope", "", {})
    assert "unknown command" in out


def test_dispatch_status():
    out = dispatch(
        "status",
        "",
        {
            "snapshot": {"threads": [1], "messages": [1, 2], "agents": ["a"]},
            "gate": None,
            "phase": "n/a",
        },
    )
    assert "threads=1" in out
    assert "msgs=2" in out


def test_install_key_aliases_idempotent():
    n1 = install_tui_key_aliases()
    n2 = install_tui_key_aliases()
    assert n1 >= 0
    assert n2 == 0


def test_win32_shift_enter_maps_to_escape_enter():
    """Win32 Shift+Enter becomes Escape+ControlM (newline), not bare Enter."""
    import sys

    if sys.platform != "win32":
        return

    from prompt_toolkit.input.win32 import ConsoleInputReader
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.win32_types import KEY_EVENT_RECORD

    from agent_augury.tui.key_aliases import (
        install_tui_key_aliases,
        install_win32_shift_enter_alias,
    )

    install_tui_key_aliases()
    assert install_win32_shift_enter_alias() in (0, 1)  # 0 if already patched
    assert getattr(ConsoleInputReader, "_augury_shift_enter_patched", False)

    reader = ConsoleInputReader()
    ev = KEY_EVENT_RECORD()
    ev.KeyDown = True
    ev.RepeatCount = 1
    ev.VirtualKeyCode = 0x0D  # VK_RETURN
    ev.uChar.UnicodeChar = "\r"
    ev.ControlKeyState = ConsoleInputReader.SHIFT_PRESSED

    keys = reader._event_to_key_presses(ev)
    assert len(keys) == 2
    assert keys[0].key == Keys.Escape
    assert keys[1].key == Keys.ControlM


def test_win32_prefer_console_reader_over_vt():
    """Win32Input should use ConsoleInputReader so Shift on Enter is kept."""
    import sys

    if sys.platform != "win32":
        return

    from prompt_toolkit.input.win32 import ConsoleInputReader, Win32Input

    from agent_augury.tui.key_aliases import install_tui_key_aliases

    install_tui_key_aliases()
    assert getattr(Win32Input, "_augury_force_console_reader", False)
    inp = Win32Input()
    try:
        assert isinstance(inp.console_input_reader, ConsoleInputReader)
        assert inp._use_virtual_terminal_input is False
    finally:
        inp.close()


def test_win32_shift_newline_char_maps_to_escape_cj():
    """Shift+Enter reported as \\n becomes Escape+ControlJ (still newline)."""
    import sys

    if sys.platform != "win32":
        return

    from prompt_toolkit.input.win32 import ConsoleInputReader
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.win32_types import KEY_EVENT_RECORD

    from agent_augury.tui.key_aliases import install_tui_key_aliases

    install_tui_key_aliases()
    reader = ConsoleInputReader()
    ev = KEY_EVENT_RECORD()
    ev.KeyDown = True
    ev.RepeatCount = 1
    ev.VirtualKeyCode = 0x0D
    ev.uChar.UnicodeChar = "\n"
    ev.ControlKeyState = ConsoleInputReader.SHIFT_PRESSED

    keys = reader._event_to_key_presses(ev)
    assert len(keys) == 2
    assert keys[0].key == Keys.Escape
    assert keys[1].key == Keys.ControlJ
