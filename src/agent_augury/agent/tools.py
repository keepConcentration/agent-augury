"""Agent-facing tools backed by the internal message server (§3.5.1).

Exposes: create_thread, send_message, read_resource, read_file,
list_directory, write_file.
"""

from __future__ import annotations

import json
from typing import Any

from ..server import MessageServer


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


class ToolBox:
    """Binds server operations into model-callable tools."""

    def __init__(self, server: MessageServer, allowed_roots: list[str] | None = None) -> None:
        self.server = server
        self.allowed_roots = allowed_roots

    # -- tool specs ----------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        """JSON-schema tool specs."""
        return [
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
            {
                "name": "read_file",
                "description": "Read a file's content from the filesystem. Returns the file content as text.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path to read"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_directory",
                "description": "List contents of a directory. Returns entries with name, is_dir, and size.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "directory path to list", "default": "."},
                    },
                },
            },
            {
                "name": "write_file",
                "description": "Write content to a file on the filesystem. Creates parent directories as needed.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path to write"},
                        "content": {"type": "string", "description": "content to write"},
                    },
                    "required": ["path", "content"],
                },
            },
        ]

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
        if name == "read_file":
            return await self._read_file(args)
        if name == "list_directory":
            return await self._list_directory(args)
        if name == "write_file":
            return await self._write_file(args)
        raise ValueError(f"unknown tool: {name}")

    # -- filesystem tools -----------------------------------------------------

    async def _read_file(self, args: dict[str, Any]) -> str:
        """Read a file's content."""
        import os
        path = args.get("path", "")
        if not path:
            return _json({"error": "path is required"})
        # Security: resolve to absolute path and check it's within allowed roots
        abs_path = os.path.abspath(path)
        allowed_roots = self.allowed_roots
        if allowed_roots:
            if not any(abs_path.startswith(os.path.abspath(root)) for root in allowed_roots):
                return _json({"error": f"path outside allowed roots: {path}"})
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return _json({"path": abs_path, "content": content, "size": len(content)})
        except Exception as exc:
            return _json({"error": f"failed to read {path}: {exc}"})

    async def _list_directory(self, args: dict[str, Any]) -> str:
        """List directory contents."""
        import os
        path = args.get("path", ".")
        abs_path = os.path.abspath(path)
        allowed_roots = self.allowed_roots
        if allowed_roots:
            if not any(abs_path.startswith(os.path.abspath(root)) for root in allowed_roots):
                return _json({"error": f"path outside allowed roots: {path}"})
        try:
            entries = []
            for entry in os.listdir(abs_path):
                full = os.path.join(abs_path, entry)
                stat = os.stat(full)
                entries.append({
                    "name": entry,
                    "is_dir": os.path.isdir(full),
                    "size": stat.st_size,
                })
            return _json({"path": abs_path, "entries": entries})
        except Exception as exc:
            return _json({"error": f"failed to list {path}: {exc}"})

    async def _write_file(self, args: dict[str, Any]) -> str:
        """Write content to a file."""
        import os
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return _json({"error": "path is required"})
        abs_path = os.path.abspath(path)
        allowed_roots = self.allowed_roots
        if allowed_roots:
            if not any(abs_path.startswith(os.path.abspath(root)) for root in allowed_roots):
                return _json({"error": f"path outside allowed roots: {path}"})
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return _json({"path": abs_path, "size": len(content), "status": "written"})
        except Exception as exc:
            return _json({"error": f"failed to write {path}: {exc}"})
