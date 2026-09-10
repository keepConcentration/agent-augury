"""Terminal key-sequence aliases (hermes pt_input_extras reduced)."""

from __future__ import annotations


def install_shift_enter_alias() -> int:
    """Map common Shift+Enter byte sequences to Esc+Enter (newline)."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    target = (Keys.Escape, Keys.ControlM)
    sequences = (
        "\x1b[13;2u",
        "\x1b[27;2;13~",
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


def install_tui_key_aliases() -> int:
    """Install all aliases once. Failures are ignored. Returns count added."""
    total = 0
    for installer in (
        install_shift_enter_alias,
        install_ctrl_enter_alias,
        install_modify_other_keys_aliases,
        install_ignored_terminal_sequences,
    ):
        try:
            total += installer()
        except Exception:  # noqa: BLE001
            pass
    return total
