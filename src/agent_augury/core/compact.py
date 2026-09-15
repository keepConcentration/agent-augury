"""Conversation compact / tombstone (SESSION_RESUME_M4_DESIGN §4).

M4b: rule-based summary + tool tombstones.
M4e: optional ``llm_summary`` via a model backend (fallback to rules on failure).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)

_PATHISH = re.compile(
    r"(?:[A-Za-z]:[\\/]|/|\./|\.\./)[^\s\"']{2,}",
)

_LLM_SYSTEM = (
    "You summarize prior multi-agent work for a checkpoint compact. "
    "Reply with a concise plain-text summary only (no tools, no markdown fences). "
    "Keep: task goal, files/paths touched, decisions, and remaining work. "
    "Max ~400 words."
)


class _CompleteBackend(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any: ...


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


def _split_head_tail(
    conversation: list[dict[str, Any]],
    *,
    keep_tail_chars: int,
    keep_tail_messages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (system, head, tail)."""
    msgs = list(conversation)
    system: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for m in msgs:
        if m.get("role") == "system":
            system = [dict(m)]  # keep latest system only
        else:
            rest.append(dict(m))

    tail_n = max(1, keep_tail_messages)
    if len(rest) <= tail_n:
        head, tail = [], rest
    else:
        head, tail = rest[:-tail_n], rest[-tail_n:]

    while approx_chars(tail) > keep_tail_chars and len(tail) > 1:
        head.append(tail.pop(0))
    return system, head, tail


def rule_based_summary(
    head: list[dict[str, Any]],
    *,
    agent_id: str = "",
    phase: str = "",
) -> str:
    """Deterministic compact summary (M4b)."""
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
    return "; ".join(summary_bits)


def _head_as_plain_text(head: list[dict[str, Any]], *, max_chars: int = 24_000) -> str:
    lines: list[str] = []
    total = 0
    for m in head:
        role = str(m.get("role") or "?")
        content = m.get("content")
        if role == "tool":
            name = m.get("name") or "tool"
            body = str(content)[:800]
            chunk = f"tool:{name}: {body}"
        elif role == "assistant" and m.get("tool_calls"):
            chunk = f"assistant(tool_calls): {str(content)[:400]}"
        else:
            chunk = f"{role}: {str(content)[:1200]}"
        if total + len(chunk) > max_chars:
            lines.append("...(truncated)")
            break
        lines.append(chunk)
        total += len(chunk)
    return "\n".join(lines)


async def llm_based_summary(
    head: list[dict[str, Any]],
    *,
    backend: _CompleteBackend,
    agent_id: str = "",
    phase: str = "",
) -> str | None:
    """Ask *backend* to summarize *head*; None on failure (caller falls back)."""
    if not head:
        return None
    plain = _head_as_plain_text(head)
    user = (
        f"Agent id: {agent_id or '?'}\n"
        f"Protocol phase: {phase or 'n/a'}\n\n"
        f"Conversation excerpt to summarize:\n{plain}"
    )
    try:
        completion = await backend.complete(
            [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": user},
            ],
            [],  # no tools
        )
    except Exception as exc:  # noqa: BLE001 — compact must not break flush
        _LOG.warning("llm compact failed: %s", exc)
        return None
    text = (completion.text or "").strip() if completion is not None else ""
    if not text:
        return None
    # strip accidental fences
    if text.startswith("```"):
        text = text.strip("`").strip()
    return text[:4000]


def _assemble(
    system: list[dict[str, Any]],
    tail: list[dict[str, Any]],
    summary_body: str,
    *,
    before: int,
    agent_id: str,
    head_len: int,
    llm: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    compact_user = {
        "role": "user",
        "content": "[checkpoint compact] Earlier work summary: " + summary_body,
    }
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
        "omitted_messages": head_len,
        "llm_summary": llm,
    }
    return new_conv, meta


def compact_conversation(
    conversation: list[dict[str, Any]],
    *,
    soft_limit_chars: int = 200_000,
    keep_tail_chars: int = 80_000,
    keep_tail_messages: int = 40,
    agent_id: str = "",
    phase: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Sync rule-based compact (M4b). Use ``compact_conversation_async`` for M4e."""
    if soft_limit_chars <= 0:
        return list(conversation), None
    before = approx_chars(conversation)
    if before <= soft_limit_chars:
        return list(conversation), None

    system, head, tail = _split_head_tail(
        conversation,
        keep_tail_chars=keep_tail_chars,
        keep_tail_messages=keep_tail_messages,
    )
    if not head:
        return list(conversation), None

    body = rule_based_summary(head, agent_id=agent_id, phase=phase)
    return _assemble(
        system, tail, body, before=before, agent_id=agent_id, head_len=len(head), llm=False
    )


async def compact_conversation_async(
    conversation: list[dict[str, Any]],
    *,
    soft_limit_chars: int = 200_000,
    keep_tail_chars: int = 80_000,
    keep_tail_messages: int = 40,
    agent_id: str = "",
    phase: str = "",
    llm_summary: bool = False,
    backend: _CompleteBackend | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Compact with optional LLM summary (M4e); falls back to rules on failure."""
    if soft_limit_chars <= 0:
        return list(conversation), None
    before = approx_chars(conversation)
    if before <= soft_limit_chars:
        return list(conversation), None

    system, head, tail = _split_head_tail(
        conversation,
        keep_tail_chars=keep_tail_chars,
        keep_tail_messages=keep_tail_messages,
    )
    if not head:
        return list(conversation), None

    used_llm = False
    body: str | None = None
    if llm_summary and backend is not None:
        body = await llm_based_summary(
            head, backend=backend, agent_id=agent_id, phase=phase
        )
        used_llm = body is not None
    if body is None:
        body = rule_based_summary(head, agent_id=agent_id, phase=phase)

    return _assemble(
        system,
        tail,
        body,
        before=before,
        agent_id=agent_id,
        head_len=len(head),
        llm=used_llm,
    )
