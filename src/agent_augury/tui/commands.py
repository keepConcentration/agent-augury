"""Slash command registry (hermes slash_exec reduced)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    handler: Callable[[dict[str, Any], str], str]


COMMANDS: dict[str, SlashCommand] = {}


def register(cmd: SlashCommand) -> None:
    COMMANDS[cmd.name] = cmd


def dispatch(name: str, args: str, ctx: dict[str, Any]) -> str:
    """Run a slash command; unknown names return a help hint."""
    cmd = COMMANDS.get(name)
    if cmd is None:
        known = ", ".join(f"/{n}" for n in sorted(COMMANDS))
        return f"unknown command: /{name}  (/help: {known})"
    try:
        return cmd.handler(ctx, args)
    except Exception as exc:  # noqa: BLE001
        return f"/{name} failed: {exc}"


def _help_text(_ctx: dict[str, Any] | None = None, _args: str = "") -> str:
    lines = [
        "Commands:",
        "  /help      - this help",
        "  /status    - threads / gate / phase snapshot",
        "  /threads   - thread list",
        "  /clear     - clear log buffer",
        "  /skip      - dismiss current ask_user question",
        "  /quit /exit - exit REPL (close session)",
        "",
        "Keys:",
        "  Enter               - submit",
        "  Shift+Enter / Esc+Enter - newline",
        "  Ctrl+Enter          - newline (Windows)",
        "  Ctrl+C              - stop agents (again to quit)",
        "  Ctrl+D              - quit",
        "  Up / Down           - input history",
        "  PgUp / PgDn         - scroll log",
        "  Alt+Up / Alt+Down   - scroll log (line)",
        "  Mouse wheel         - scroll log",
        "",
        "Blank Enter is ignored. Exit via /quit or Ctrl+D.",
        "Plain quit/exit is sent to agents (not exit).",
    ]
    return "\n".join(lines)


def _status_text(ctx: dict[str, Any], _args: str = "") -> str:
    snap = ctx.get("snapshot") or {}
    gate = ctx.get("gate")
    phase = ctx.get("phase", "n/a")
    gate_s = (
        "OPEN"
        if gate and getattr(gate, "is_open", False)
        else ("CLOSED" if gate else "n/a")
    )
    return (
        f"threads={len(snap.get('threads', []))}  |  "
        f"msgs={len(snap.get('messages', []))}  |  "
        f"gate={gate_s}  |  phase={phase}  |  "
        f"agents={len(snap.get('agents', []))}"
    )


def _threads_text(ctx: dict[str, Any], _args: str = "") -> str:
    snap = ctx.get("snapshot") or {}
    recent = ctx.get("recent_thread")
    threads = snap.get("threads") or []
    if not threads:
        return "(no threads)"
    lines = []
    for t in threads:
        tid = t.get("thread_id", "?")
        name = t.get("name", "")
        mark = " <- recent" if tid == recent else ""
        lines.append(f"  {tid}  {name}{mark}")
    return "Threads:\n" + "\n".join(lines)


def _clear_buffer(ctx: dict[str, Any], _args: str = "") -> str:
    buf = ctx.get("log_buffer")
    if buf is not None:
        buf.clear()
    return "(log cleared)"


def _skip_question(ctx: dict[str, Any], _args: str = "") -> str:
    panel = ctx.get("choice_panel")
    if panel is None or not panel.has_pending_bool():
        return "(no question to skip)"
    pq = panel.skip()
    if pq is None:
        return "(no question to skip)"
    return f"(skipped question from {pq.agent_id})"


register(SlashCommand("help", "command + key help", _help_text))
register(SlashCommand("status", "threads/msgs/gate/phase", _status_text))
register(SlashCommand("threads", "thread list + recent", _threads_text))
register(SlashCommand("clear", "clear log buffer", _clear_buffer))
register(SlashCommand("skip", "dismiss current question", _skip_question))

# v1.4: /verbose toggle (P2-3 run_command 로그 필터링)
_verbose_mode = False


def verbose_mode() -> bool:
    return _verbose_mode


def _toggle_verbose(_ctx, _args):
    global _verbose_mode
    _verbose_mode = not _verbose_mode
    return f"(verbose mode: {'ON' if _verbose_mode else 'OFF'})"


def _copy_log(ctx, args):
    """Copy last N lines of log to clipboard (or export to file)."""
    buf = ctx.get("log_buffer")
    if buf is None:
        return "(no log buffer)"
    n = 50
    if args.strip():
        try:
            n = int(args.strip())
        except ValueError:
            pass
    tail = buf.export_tail(n)
    if not tail:
        return "(log is empty)"
    try:
        import pyperclip
        pyperclip.copy(tail)
        return f"(copied last {len(tail.splitlines())} lines to clipboard)"
    except ImportError:
        import tempfile
        from pathlib import Path
        path = Path(tempfile.gettempdir()) / "agent-augury-log.txt"
        path.write_text(tail, encoding="utf-8")
        return f"(pyperclip not installed. Saved to {path})"


def _export_log(ctx, _args):
    """Export full log to a file."""
    buf = ctx.get("log_buffer")
    if buf is None:
        return "(no log buffer)"
    tail = buf.export_tail(99999)
    if not tail:
        return "(log is empty)"
    from datetime import UTC, datetime
    from pathlib import Path
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = Path.home() / ".agent-augury" / f"log-{ts}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tail, encoding="utf-8")
    return f"(log exported to {path})"


register(SlashCommand("verbose", "toggle verbose tool logging", _toggle_verbose))
register(SlashCommand("copy", "copy last N log lines to clipboard", _copy_log))
register(SlashCommand("export", "export full log to file", _export_log))
