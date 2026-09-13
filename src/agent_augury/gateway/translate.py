"""Translate Core/MessageServer observer events into Augury Wire events."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .types import EVENT_TYPES, WireEvent, make_event


def translate_core_event(event: dict[str, Any]) -> WireEvent | None:
    """Map a Core observer dict to a Wire event, or None if not publishable."""
    if not isinstance(event, dict):
        return None
    etype = event.get("type")
    if etype == "tool" and event.get("tool") == "ask_user":
        return _ask_user_to_question(event)
    if etype == "tool":
        return make_event(
            "tool",
            agent_id=event.get("agent_id"),
            tool=event.get("tool"),
            args=event.get("args") or {},
            result=event.get("result"),
        )
    if etype == "step":
        result = event.get("result")
        payload: dict[str, Any] = {}
        if result is not None:
            text = getattr(result, "text", None)
            tools = getattr(result, "tool_calls", None)
            if text is not None:
                payload["text"] = text
            if tools is not None:
                payload["tool_calls"] = list(tools) if tools else []
            if not payload and isinstance(result, dict):
                payload = dict(result)
        return make_event(
            "agent.step",
            agent_id=str(event.get("agent_id", "")),
            result=payload,
        )
    if etype == "create_thread":
        return make_event(
            "thread.created",
            thread_id=event.get("thread_id"),
            agent_id=event.get("agent_id"),
            name=event.get("name"),
            participants=list(event.get("participants") or []),
        )
    if etype in ("send_message", "message"):
        author = event.get("author") or event.get("agent_id")
        return make_event(
            "message",
            thread_id=event.get("thread_id"),
            agent_id=author,
            author=author,
            content=event.get("content"),
            message_id=event.get("message_id"),
            mentions=list(event.get("mentions") or []),
        )
    if etype == "read_resource":
        return make_event(
            "read_resource",
            agent_id=event.get("agent_id"),
            threads=event.get("threads", 0),
            messages=event.get("messages", 0),
        )
    if etype in EVENT_TYPES:
        fields = {k: v for k, v in event.items() if k != "type"}
        return make_event(str(etype), **fields)
    text = event.get("text") or event.get("content")
    if text:
        return make_event("log", text=str(text), core_type=etype)
    return None


def _ask_user_to_question(event: dict[str, Any]) -> WireEvent:
    args = event.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    question_id = str(event.get("question_id") or args.get("question_id") or uuid4())
    return make_event(
        "human.question",
        question_id=question_id,
        agent_id=str(event.get("agent_id", "")),
        thread_id=str(args.get("thread") or event.get("thread_id") or ""),
        question=str(args.get("question") or event.get("question") or ""),
        options=list(args.get("options") or event.get("options") or []),
    )
