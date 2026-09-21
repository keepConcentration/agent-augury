"""Wire event → plain text for chat platforms (Discord bot, Slack webhook, …).

Ink and other Interactive Surfaces render structured Wire events in the UI.
Chat adapters must **not** reuse Ink/TUI log lines (``💭 agent: …``).

``recipient_agent_id`` is set when one bot/webhook line is tied to a single
agent (Discord ``bots[]`` per-agent route). The bot display name replaces
redundant ``agent_id`` prefixes; prose is sent as the body only.
"""

from __future__ import annotations

import json
from typing import Any

from agent_augury.gateway.types import WireEvent

# Discord message limit 2000; stay under with headroom (chunking is transport-side).
_CHAT_MAX_CONTENT = 1800


def _truncate(text: str) -> str:
    """Legacy one-line clip for TUI/log helpers — not used for chat outbound."""
    if len(text) <= _CHAT_MAX_CONTENT:
        return text
    return text[:_CHAT_MAX_CONTENT] + "…"


def _label(name: str, *, recipient_agent_id: str | None) -> bool:
    """True when the human-readable author/agent label should be shown."""
    if not name or name == "?":
        return False
    if recipient_agent_id is None:
        return True
    return name != recipient_agent_id


# Per-value clip inside an approval card. Every key is still listed.
_APPROVAL_VALUE_MAX = 300


def format_approval_args(args: dict[str, Any]) -> str:
    """Render every arg the approval digest binds, one ``key: value`` per line.

    Loopjacking (arXiv:2609.21081) representation variant: a card showing only
    ``command``/``path`` hides the rest of what ``args_digest()`` binds — e.g.
    ``write_file``'s ``content``. The human must see every field, so long values
    are clipped with an explicit "+N chars" marker rather than dropped.
    """
    lines: list[str] = []
    for key in sorted(args):
        value = args[key]
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > _APPROVAL_VALUE_MAX:
            hidden = len(text) - _APPROVAL_VALUE_MAX
            text = f"{text[:_APPROVAL_VALUE_MAX]}… (+{hidden} chars)"
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def format_wire_for_chat_surface(
    event: WireEvent,
    *,
    recipient_agent_id: str | None = None,
) -> str | None:
    """Format one Wire event for outbound chat (Discord/Slack)."""
    etype = event.get("type")

    if etype == "thread.created":
        name = str(event.get("name") or "?")
        parts = ", ".join(str(p) for p in (event.get("participants") or []))
        if event.get("bootstrap"):
            label = f"opened **{name}**"
        else:
            label = f"Thread **{name}**"
        if parts:
            return f"{label} ({parts})"
        return f"{label} started"

    if etype == "message":
        content = str(event.get("content") or "")
        if content.startswith("[ask-user]"):
            return None
        author = str(event.get("author") or event.get("agent_id") or "?")
        if _label(author, recipient_agent_id=recipient_agent_id):
            return f"**{author}**: {content}"
        return content

    if etype == "tool":
        agent_id = str(event.get("agent_id") or "?")
        tool = str(event.get("tool") or "?")
        icons = {
            "read_file": "📖",
            "write_file": "📝",
            "list_directory": "📁",
            "search": "🔍",
            "send_message": "💬",
            "create_thread": "🧵",
            "read_resource": "📊",
        }
        icon = icons.get(tool, "🔧")
        line = f"{icon} {tool}(…)"
        if _label(agent_id, recipient_agent_id=recipient_agent_id):
            return f"{icon} {agent_id}: {tool}(…)"
        return line

    if etype == "agent.step":
        result = event.get("result") or {}
        text = result.get("text") if isinstance(result, dict) else None
        if not text:
            return None
        agent_id = str(event.get("agent_id") or "?")
        body = str(text)
        if _label(agent_id, recipient_agent_id=recipient_agent_id):
            return f"{agent_id}: {body}"
        return body

    if etype == "read_resource":
        agent_id = str(event.get("agent_id") or "?")
        threads = event.get("threads", 0)
        messages = event.get("messages", 0)
        line = f"read_resource (threads={threads}, messages={messages})"
        if _label(agent_id, recipient_agent_id=recipient_agent_id):
            return f"{agent_id}: {line}"
        return line

    if etype == "human.question":
        q = str(event.get("question") or "")
        agent = str(event.get("agent_id") or "?")
        if _label(agent, recipient_agent_id=recipient_agent_id):
            return f"❓ {agent}: {q}"
        return q

    if etype == "approval.request":
        agent = str(event.get("agent_id") or "?")
        tool = str(event.get("tool") or "tool")
        aid = str(event.get("approval_id") or "?")
        preview = event.get("args_preview") or {}
        detail = ""
        if isinstance(preview, dict) and preview:
            detail = "\n```\n" + format_approval_args(preview) + "\n```"
        who = f"[{agent}] " if _label(agent, recipient_agent_id=recipient_agent_id) else ""
        return (
            f"🔐 Approval needed {who}{tool} ({aid}){detail}\n"
            "Reply: 1/approve or 2/deny"
        )

    if etype in ("approval.resolved", "approval.granted", "approval.expired"):
        decision = event.get("decision") or str(etype).split(".")[-1]
        aid = event.get("approval_id") or "?"
        tool = event.get("tool") or ""
        reason = event.get("reason")
        extra = f" reason={reason}" if reason else ""
        return f"🔐 Approval {decision} [{aid}] {tool}{extra}".strip()

    if etype == "tool.denied":
        return (
            f"🚫 Tool denied: {event.get('tool') or '?'} "
            f"({event.get('reason') or ''})"
        )

    if etype == "log" and event.get("text"):
        return str(event["text"])

    return None


def format_core_event_log_line(event: dict[str, Any]) -> str | None:
    """Legacy log/TUI one-liner (headless stderr parity tests only — not chat)."""
    event_type = event.get("type")

    if event_type == "create_thread":
        name = event.get("name", "?")
        participants = ", ".join(event.get("participants", []))
        return f"🧵 create_thread **{name}** ({participants})"

    if event_type == "send_message":
        author = event.get("author", "?")
        content = _truncate(str(event.get("content", "")))
        return f"💬 {author}: {content}"

    if event_type == "tool":
        agent_id = event.get("agent_id", "?")
        tool = event.get("tool", "?")
        icons = {
            "read_file": "📖",
            "write_file": "📝",
            "list_directory": "📁",
            "search": "🔍",
            "send_message": "💬",
            "create_thread": "🧵",
            "read_resource": "📊",
        }
        icon = icons.get(tool, "🔧")
        return f"{icon} {agent_id}: {tool}(...)"

    if event_type == "read_resource":
        agent_id = event.get("agent_id", "?")
        threads = event.get("threads", 0)
        messages = event.get("messages", 0)
        return f"📊 {agent_id}: read_resource (threads={threads}, messages={messages})"

    if event_type == "step":
        agent_id = event.get("agent_id", "?")
        result = event.get("result")
        text = getattr(result, "text", None) if result else None
        if text:
            return f"💭 {agent_id}: {_truncate(str(text))}"
        return None

    return None
