"""Agent-facing tools backed by the internal message server (§3.5.1).

L3 exposes: create_thread, send_message, read_resource.
L2 additionally exposes: wait_for_mention (foreground blocking receive).
"""

from __future__ import annotations

import json
from typing import Any

from ..server import MessageServer


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


class ToolBox:
    """Binds server operations into model-callable tools."""

    def __init__(self, server: MessageServer) -> None:
        self.server = server

    # -- tool specs ----------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        """JSON-schema tool specs; wait_for_mention only in L2 mode."""
        specs: list[dict[str, Any]] = [
            {
                "name": "create_thread",
                "description": "Open a named conversation thread with the given participants and return its id.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "participants": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["name", "participants"],
                },
            },
            {
                "name": "send_message",
                "description": (
                    "Post a message to a thread. Fire-and-forget: returns immediately. "
                    "Empty mentions broadcasts to the thread's participants (except you). "
                    'Prefix content with "(FYI)" or "(URGENT)" when appropriate.'
                ),
                "schema": {
                    "type": "object",
                    "properties": {
                        "thread": {"type": "string", "description": "thread id"},
                        "content": {"type": "string"},
                        "mentions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent ids to address; empty = broadcast",
                        },
                    },
                    "required": ["thread", "content"],
                },
            },
            {
                "name": "read_resource",
                "description": "Explicit full state dump of threads/messages for recovery or aggregation. Never injected automatically.",
                "schema": {"type": "object", "properties": {}},
            },
        ]
        if self.server.mode == "L2":
            specs.append(
                {
                    "name": "wait_for_mention",
                    "description": "BLOCKING foreground receive: wait until an unread mention arrives and return it. Calling this pauses your work.",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "timeout": {
                                "type": "number",
                                "description": "seconds to wait; omit to wait indefinitely",
                            }
                        },
                    },
                }
            )
        return specs

    # -- execution -----------------------------------------------------------

    async def execute(self, agent_id: str, name: str, args: dict[str, Any]) -> str:
        if name == "create_thread":
            tid = await self.server.create_thread(
                args["name"], participants=list(args["participants"])
            )
            return _json({"thread_id": tid})
        if name == "send_message":
            mid = await self.server.send_message(
                args["thread"],
                author=agent_id,
                content=args["content"],
                mentions=list(args.get("mentions") or []),
            )
            return _json({"message_id": mid, "status": "sent"})
        if name == "read_resource":
            snap = self.server.snapshot()
            self.server._emit_event({
                "type": "read_resource",
                "agent_id": agent_id,
                "threads": len(snap["threads"]),
                "messages": len(snap["messages"]),
                "timestamp": int(__import__("time").time()),
            })
            return _json(snap)
        if name == "wait_for_mention":
            batch = await self.server.wait_for_mention(
                agent_id, timeout=args.get("timeout")
            )
            return _json({"messages": batch})
        raise ValueError(f"unknown tool: {name}")
