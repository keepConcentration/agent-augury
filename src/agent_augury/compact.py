"""Conversation compact / tombstone (SESSION_RESUME_M4_DESIGN §4)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

_PATHISH = re.compile(
    r"(?:[A-Za-z]:[\\/]|/|\./|\.\./)[^\s\"']{2,}",
)


def approx_chars(conversation: list[dict[str, Any]]) -> int:
    try:
        return len(json.dumps(conversation, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in conversation)


def _extract_paths(text: str, *, limit: int = 12) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATHISH.finditer(text or ""):
        p = m.group(0)
        if p not in seen:
            seen.add(p)
            found.append(p)
        if len(found) >= limit:
            break
    return found


def _tombstone_tool_content(msg: dict[str, Any]) -> dict[str, Any]:
    content = msg.get("content")
    n = len(str(content)) if content is not None else 0
    name = msg.get("name") or "tool"
    tid = msg.get("tool_call_id") or msg.get("id") or "?"
    out = dict(msg)
    out["content"] = f"[omitted tool result: {name}|{tid}, {n} chars]"
    return out


def compact_conversation(
    conversation: list[dict[str, Any]],
    *,
    soft_limit_chars: int = 200_000,
    keep_tail_chars: int = 80_000,
    keep_tail_messages: int = 40,
    agent_id: str = "",
    phase: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Shrink *conversation* in-place policy; return (new_list, compaction_meta|None)."""
    if soft_limit_chars <= 0:
        return list(conversation), None
    before = approx_chars(conversation)
    if before <= soft_limit_chars:
        return list(conversation), None

    msgs = list(conversation)
    system: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for m in msgs:
        if m.get("role") == "system" and not system:
            system.append(dict(m))
        elif m.get("role") == "system":
            system = [dict(m)]  # keep latest system only
        else:
            rest.append(dict(m))

    # Tail by message count then trim by chars
    tail_n = max(1, keep_tail_messages)
    if len(rest) <= tail_n:
        head, tail = [], rest
    else:
        head, tail = rest[:-tail_n], rest[-tail_n:]

    while approx_chars(tail) > keep_tail_chars and len(tail) > 1:
        # drop oldest of tail into head (will be compacted)
        head.append(tail.pop(0))

    paths: list[str] = []
    last_user = ""
    for m in head:
        role = m.get("role")
        content = m.get("content")
        if role == "user" and isinstance(content, str) and content.strip():
            if not content.startswith("[radio]") and not content.startswith(
                "[checkpoint compact]"
            ):
                last_user = content.strip()[:200]
            paths.extend(_extract_paths(content))
        elif role == "assistant" and isinstance(content, str):
            paths.extend(_extract_paths(content))
        elif role == "tool":
            paths.extend(_extract_paths(str(content)))

    # unique paths preserve order
    uniq_paths: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq_paths.append(p)
    uniq_paths = uniq_paths[:12]

    summary_bits = [
        f"agent={agent_id or '?'}",
        f"phase={phase or 'n/a'}",
        f"omitted_messages={len(head)}",
    ]
    if last_user:
        summary_bits.append(f"last_user={last_user!r}")
    if uniq_paths:
        summary_bits.append("paths=" + ", ".join(uniq_paths))

    compact_user = {
        "role": "user",
        "content": "[checkpoint compact] Earlier work summary: " + "; ".join(summary_bits),
    }

    # Tombstone tool payloads still inside tail if oversized single messages
    new_tail: list[dict[str, Any]] = []
    for m in tail:
        if m.get("role") == "tool" and approx_chars([m]) > 8_000:
            new_tail.append(_tombstone_tool_content(m))
        else:
            new_tail.append(m)

    new_conv = system + [compact_user] + new_tail
    after = approx_chars(new_conv)
    meta = {
        "at": time.time(),
        "agent_id": agent_id,
        "chars_before": before,
        "chars_after": after,
        "omitted_messages": len(head),
    }
    return new_conv, meta
