"""Terminal key-sequence aliases (hermes pt_input_extras reduced).

Windows notes
-------------
``create_input()`` always returns ``Win32Input``. On modern Windows Terminal it
often picks ``Vt100ConsoleInputReader``, which yields only ``UnicodeChar`` and
**drops** ``ControlKeyState`` — so Shift+Enter becomes plain Enter (submit).

Fixes:
1. Prefer ``ConsoleInputReader`` (keeps Shift/Ctrl modifiers).
2. Map Shift+Enter / Shift+CtrlJ → Escape+Enter / Escape+CtrlJ (newline).
3. Keep ANSI CSI aliases for VT-capable hosts that emit modifyOtherKeys.
"""

from __future__ import annotations

import sys


def install_shift_enter_alias() -> int:
    """Map common Shift+Enter byte sequences to Esc+Enter (newline)."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    target = (Keys.Escape, Keys.ControlM)
    sequences = (
        "\x1b[13;2u",
        "\x1b[27;2;13~",
        "\x1b[13;2~",
    )
    n = 0
    for seq in sequences:
        if seq not in ANSI_SEQUENCES:
            ANSI_SEQUENCES[seq] = target
            n += 1
    return n


def install_ctrl_enter_alias() -> int:
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    target = (Keys.Escape, Keys.ControlM)
    sequences = (
        "\x1b[13;5u",
        "\x1b[27;5;13~",
    )
    n = 0
    for seq in sequences:
        if seq not in ANSI_SEQUENCES:
            ANSI_SEQUENCES[seq] = target
            n += 1
    return n


def install_modify_other_keys_aliases() -> int:
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    mapping = {
        "\x1b[99;5u": Keys.ControlC,
    }
    n = 0
    for seq, key in mapping.items():
        if seq not in ANSI_SEQUENCES:
            ANSI_SEQUENCES.setdefault(seq, key)
            n += 1
    return n


def install_ignored_terminal_sequences() -> int:
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    n = 0
    for seq in ("\x1b[I", "\x1b[O"):
        if seq not in ANSI_SEQUENCES:
            ANSI_SEQUENCES[seq] = Keys.Ignore
            n += 1
    return n


def install_win32_prefer_console_reader() -> int:
    """Force ``ConsoleInputReader`` so Enter modifiers are not dropped.

    ``Vt100ConsoleInputReader`` only forwards UnicodeChar into the VT parser,
    discarding Shift/Ctrl on VK_RETURN. Prefer the classic reader instead.
    """
    if sys.platform != "win32":
        return 0
    try:
        from prompt_toolkit.input import win32 as win32_input
    except Exception:  # noqa: BLE001
        return 0

    if getattr(win32_input.Win32Input, "_augury_force_console_reader", False):
        return 0

    original_init = win32_input.Win32Input.__init__

    def __init__(self, stdin=None):
        original_init(self, stdin)
        reader = self.console_input_reader
        if isinstance(reader, win32_input.Vt100ConsoleInputReader):
            try:
                reader.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self.console_input_reader = win32_input.ConsoleInputReader()
            # raw_mode() keys off this flag — keep classic input events.
            self._use_virtual_terminal_input = False

    win32_input.Win32Input.__init__ = __init__  # type: ignore[method-assign]
    win32_input.Win32Input._augury_force_console_reader = True  # type: ignore[attr-defined]
    return 1


def install_win32_shift_enter_alias() -> int:
    """Win32: Shift+Enter → (Escape, ControlM); Shift+\\n → (Escape, ControlJ).

    prompt_toolkit only special-cases Ctrl+Enter as Escape+ControlJ. Shift+Enter
    otherwise stays ControlM/ControlJ and hits Enter/C-j = submit.
    """
    if sys.platform != "win32":
        return 0
    try:
        from prompt_toolkit.input.win32 import ConsoleInputReader
        from prompt_toolkit.key_binding.key_processor import KeyPress
        from prompt_toolkit.keys import Keys
    except Exception:  # noqa: BLE001
        return 0

    n = 0
    if not getattr(ConsoleInputReader, "_augury_shift_enter_patched", False):
        original = ConsoleInputReader._event_to_key_presses

        def _event_to_key_presses(self, ev):
            keys = original(self, ev)
            ctrl = bool(
                ev.ControlKeyState & self.LEFT_CTRL_PRESSED
                or ev.ControlKeyState & self.RIGHT_CTRL_PRESSED
            )
            shift = bool(ev.ControlKeyState & self.SHIFT_PRESSED)
            if (
                shift
                and not ctrl
                and len(keys) == 1
                and keys[0].key in (Keys.ControlM, Keys.ControlJ)
            ):
                return [KeyPress(Keys.Escape, ""), keys[0]]
            return keys

        ConsoleInputReader._event_to_key_presses = _event_to_key_presses  # type: ignore[method-assign]
        ConsoleInputReader._augury_shift_enter_patched = True  # type: ignore[attr-defined]
        n += 1

    # Belt-and-suspenders: if something still constructs a VT reader, preserve Shift.
    try:
        from prompt_toolkit.input.win32 import EventTypes, Vt100ConsoleInputReader
        from prompt_toolkit.win32_types import KEY_EVENT_RECORD
    except Exception:  # noqa: BLE001
        return n

    if getattr(Vt100ConsoleInputReader, "_augury_shift_enter_patched", False):
        return n

    SHIFT = 0x0010
    LEFT_CTRL = 0x0008
    RIGHT_CTRL = 0x0004

    def _get_keys(self, read, input_records):
        for i in range(read.value):
            ir = input_records[i]
            if ir.EventType not in EventTypes:
                continue
            ev = getattr(ir.Event, EventTypes[ir.EventType])
            if not (isinstance(ev, KEY_EVENT_RECORD) and ev.KeyDown):
                continue
            u_char = ev.uChar.UnicodeChar
            if u_char == "\x00":
                continue
            shift = bool(ev.ControlKeyState & SHIFT)
            ctrl = bool(ev.ControlKeyState & (LEFT_CTRL | RIGHT_CTRL))
            if shift and not ctrl and u_char in ("\r", "\n"):
                yield "\x1b"
            yield u_char

    Vt100ConsoleInputReader._get_keys = _get_keys  # type: ignore[method-assign]
    Vt100ConsoleInputReader._augury_shift_enter_patched = True  # type: ignore[attr-defined]
    return n + 1


def install_tui_key_aliases() -> int:
    """Install all aliases once. Failures are ignored. Returns count added."""
    total = 0
    for installer in (
        install_shift_enter_alias,
        install_ctrl_enter_alias,
        install_modify_other_keys_aliases,
        install_ignored_terminal_sequences,
        install_win32_prefer_console_reader,
        install_win32_shift_enter_alias,
    ):
        try:
            total += installer()
        except Exception:  # noqa: BLE001, S110  # 키 별칭 설치는 무해 실패 허용
            pass
    return total
