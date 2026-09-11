# TUI Static bottom dock (Ink `<Static>` pattern) v1.5 — ARCHIVED

> **Superseded by** [`MULTI_FRONT_DESIGN.md`](../../architecture/MULTI_FRONT_DESIGN.md).
> Historical: prompt_toolkit approximation of Ink Static (atomic paint, Win32
> Shift+Enter). Runtime code in `tui/` remains until Ink surface lands (M2+).

## Historical summary

- Logs → terminal scrollback via soft-erase + write + redraw (one paint).
- Bottom chrome: choice? + input + status; `full_screen=False`.
- Primary UX target later moved to **Ink Surface** under multi-front architecture.
