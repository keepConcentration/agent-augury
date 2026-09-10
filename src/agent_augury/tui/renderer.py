"""Event -> ANSI string renderer (rich record=True). Also non-TTY fallback."""

from __future__ import annotations

import io
import os
import re
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

_SENSITIVE_PATTERNS = [
    (re.compile(r"(Authorization:\s+Bearer\s+)[^\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Bearer\s+)[^\s]+", re.IGNORECASE), r"\1***"),
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s"]+', re.IGNORECASE), r"\1***"),
    (re.compile(r'(token["\s:=]+)[^\s"]+', re.IGNORECASE), r"\1***"),
]

_TOOL_ICONS = {
    "read_file": '📖',
    "write_file": '📝',
    "list_directory": '📁',
    "send_message": '💬',
    "create_thread": '🧵',
    "read_resource": '📊',
    # v0.7 — AGENT_TOOLS_EXPANSION_DESIGN.md 신규 도구 아이콘
    "run_command": '⚙️',
    "fetch_url": '🌐',
    "web_search": '🔎',
    "edit_file": '✏️',
    "append_file": '➕',
}

_style_console = Console(
    record=True,
    file=io.StringIO(),
    force_terminal=True,
    highlight=False,
)


def mask_sensitive(text: str) -> str:
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def render_event(event: dict[str, Any]) -> str | None:
    """Convert one session/cli event into a single ANSI block, or None to skip."""
    event_type = event.get("type")

    if event_type == "step":
        result = event.get("result")
        text = getattr(result, "text", None) if result is not None else event.get("text")
        if not text:
            return None
    elif event_type == "send_message":
        if event.get("content", "").startswith("[ask-user]"):
            return None
    elif event_type == "tool":
        tool = event.get("tool", "")
        # v1.4: protocol_violation -> always render (dont skip)
        if event.get("protocol_violation"):
            pass
        elif tool in ("send_message", "create_thread", "read_resource"):
            return None
        # v1.4: verbose OFF + run_command -> skip (log noise reduction, #3)
        elif tool == "run_command":
            from .commands import _verbose_mode
            if not _verbose_mode:
                return None
    elif event_type not in (
        "create_thread",
        "read_resource",
        "feedback",
        "summary",
        "step",
        "send_message",
        "tool",
    ):
        return None

    try:
        if event_type == "step":
            result = event.get("result")
            text = getattr(result, "text", None) if result is not None else event.get("text")
            _style_console.print(f"💭 {event.get('agent_id', '')}:")
            _style_console.print(Markdown(text))

        elif event_type == "create_thread":
            tid = event["thread_id"]
            name = event["name"]
            participants = ", ".join(event["participants"])
            _style_console.print(
                f"🧵 [{tid}] create_thread {name} ({participants})"
            )

        elif event_type == "send_message":
            author = event["author"]
            tid = event["thread_id"]
            delivered = event.get("delivered_to", [])
            targets = ", ".join(delivered) if delivered else "broadcast"
            _style_console.print(f"💬 [{author} -> {targets}][{tid}]")
            _style_console.print(Markdown(mask_sensitive(event.get("content", ""))))

        elif event_type == "read_resource":
            agent_id = event["agent_id"]
            threads = event.get("threads", 0)
            messages = event.get("messages", 0)
            _style_console.print(
                f"📊 {agent_id}: read_resource "
                f"(threads={threads}, messages={messages})"
            )

        elif event_type == "tool":
            tool = event.get("tool", "")
            agent_id = event.get("agent_id", "")
            args = event.get("args", {})
            if tool == "ask_user":
                question = args.get("question", "")
                options = args.get("options")
                _style_console.print(
                    f"👤 {agent_id} asks: {question}", style="bold"
                )
                if options:
                    _style_console.print("   (options: " + " / ".join(options) + ")")
            else:
                icon = _TOOL_ICONS.get(tool, '🔧')
                # v1.4: protocol violation -> bold red (P2-8)
                if event.get("protocol_violation"):
                    msg = event.get("violation_message", "")
                    phase = event.get("phase", "?")
                    _style_console.print(
                        f"⚠️ [{agent_id}] PROTOCOL VIOLATION (phase={phase}): {msg}",
                        style="bold red"
                    )
                # v1.4: run_command -> show command (#2)
                elif tool == "run_command":
                    cmd = args.get("command", "")
                    short = cmd[:60] + "..." if len(cmd) > 60 else cmd
                    _style_console.print(f"{icon} {agent_id}: {tool} `{short}`")
                # v1.4: fetch_url -> show URL (#2)
                elif tool == "fetch_url":
                    url = args.get("url", "")
                    short = url[:60] + "..." if len(url) > 60 else url
                    _style_console.print(f"{icon} {agent_id}: {tool} {short}")
                # v1.4: file tools -> show basename (#2)
                elif tool in ("read_file", "write_file", "edit_file", "append_file", "list_directory"):
                    path = args.get("path", "")
                    if path:
                        short_path = os.path.basename(path.replace("\\", "/"))
                        _style_console.print(f"{icon} {agent_id}: {tool} {short_path}")
                    else:
                        _style_console.print(f"{icon} {agent_id}: {tool}")
                else:
                    path = args.get("path", "")
                    if path:
                        short_path = os.path.basename(path.replace("\\", "/"))
                        _style_console.print(f"{icon} {agent_id}: {tool} {short_path}")
                    else:
                        _style_console.print(f"{icon} {agent_id}: {tool}")

        elif event_type == "feedback" or event_type == "summary":
            _style_console.print(event.get("text", ""))

        else:
            return None

        return _style_console.export_text(styles=True, clear=True).rstrip("\n")
    except Exception:
        _style_console.export_text(clear=True)
        raise
