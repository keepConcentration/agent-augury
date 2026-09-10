"""Input routing - pure function. No TTY / prompt_toolkit dependency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_QUESTION_REPLY_NOTICE = (
    "(\uc9c8\ubb38\uc5d0 \uc751\ub2f5\ud558\ub294 \ub300\uc2e0 \uc77c\ubc18 \uba54\uc2dc\uc9c0\ub85c \ubcf4\ub0c4)"
)


class ActiveQuestion(Protocol):
    thread_id: str
    agent_id: str
    options: list[str]


@dataclass
class RouteResult:
    """Routing decision - caller executes (human_send / slash / quit)."""

    kind: str  # command | choice | question_reply | plain | ignored | quit
    content: str = ""
    thread_id: str | None = None
    mentions: list[str] | None = None
    notice: str | None = None
    command: str | None = None
    args: str = ""


@dataclass
class RouterContext:
    """Minimal context for a single route() call."""

    active_question: Any | None = None
    recent_thread: str | None = None
    response_format: str = "text"


def route(text: str, ctx: RouterContext) -> RouteResult:
    """Classify one input line. Does not perform I/O."""
    text = text.strip()
    if not text:
        return RouteResult(kind="ignored")

    if text.startswith("/"):
        name, _, args = text[1:].partition(" ")
        if name in ("quit", "exit"):
            return RouteResult(kind="quit", command=name)
        return RouteResult(kind="command", command=name, args=args.strip())

    pq = ctx.active_question
    if pq is not None:
        option_text = _resolve_option(pq, text, response_format=ctx.response_format)
        if option_text is not None:
            return RouteResult(
                kind="choice",
                content=option_text,
                thread_id=pq.thread_id,
                mentions=[pq.agent_id],
            )
        return RouteResult(
            kind="question_reply",
            content=text,
            thread_id=pq.thread_id,
            mentions=[pq.agent_id],
            notice=_QUESTION_REPLY_NOTICE,
        )

    return RouteResult(
        kind="plain",
        content=text,
        thread_id=ctx.recent_thread,
        mentions=None,
    )


def _resolve_option(pq: Any, text: str, *, response_format: str) -> str | None:
    """Return option text (or number) when *text* is a valid 1-based index."""
    options = getattr(pq, "options", None) or []
    if not options:
        return None
    try:
        idx = int(text.strip())
    except ValueError:
        return None
    if 1 <= idx <= len(options):
        return options[idx - 1] if response_format == "text" else text.strip()
    return None
