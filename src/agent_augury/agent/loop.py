"""Agent loop: step() with inbox drain as single consumer (§3.5.2, §3.6).

Also resolves ``$thread:N`` placeholders against this agent's own
create_thread results, so scripted/real tool sequences can reference
threads created earlier in the same run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..backend.base import Completion, ModelBackend
from ..server import MessageServer
from .system_prompt import render_system_prompt
from .tools import ToolBox

Message = dict[str, Any]

_THREAD_REF = re.compile(r"^\$thread:(\d+)$")
_THREAD_BY_NAME = re.compile(r"^\$thread_by_name:(.+)$")


@dataclass
class StepResult:
    """Outcome of one agent step."""

    text: str | None = None
    tool_calls: list[Any] = field(default_factory=list)
    drained_count: int = 0


def format_radio_block(messages: list[Message]) -> str:
    """§3.6 — drained messages merge into ONE user turn wrapped in [radio].

    Message content passes through unchanged; prefixes like URGENT:/FYI:
    travel inside the content itself.
    """
    lines = ["[radio]"]
    lines.extend(f"from {m['author']}: {m['content']}".rstrip() for m in messages)
    return "\n".join(lines)


@dataclass(frozen=True)
class LocalTool:
    """A non-server tool bound directly to the agent (e.g. search)."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]  # sync or async → JSON-serializable


class AgentLoop:
    """One agent's turn loop. The ONLY inbox consumer is step()."""

    def __init__(
        self,
        agent_id: str,
        server: MessageServer,
        backend: ModelBackend,
        system_prompt: str | None = None,
        local_tools: list[LocalTool] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.server = server
        self.backend = backend
        self.tools = ToolBox(server)
        self.local_tools: dict[str, LocalTool] = {t.name: t for t in local_tools or []}
        self.conversation: list[Message] = [
            {
                "role": "system",
                "content": system_prompt or render_system_prompt(agent_id),
            }
        ]
        self._custom_system_prompt = system_prompt is not None
        # thread ids this agent created, in creation order ($thread:N source)
        self.created_threads: list[str] = []
        # gate-aware execution state (injected by Session each step)
        self.gate_open: bool = True
        self.gate_thread_id: str | None = None
        # v0.2: current protocol phase (injected by Session each step)
        self.current_phase: str = ""

    # -- tool spec passthrough (mode-aware) ---------------------------------

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        specs = self.tools.specs()
        for tool in self.local_tools.values():
            specs.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": tool.schema,
                }
            )
        return specs

    def _update_phase_in_prompt(self) -> None:
        """Update the system prompt to reflect the current phase."""
        if self._custom_system_prompt:
            return  # user-supplied prompt — don't overwrite
        if self.conversation and self.conversation[0]["role"] == "system":
            self.conversation[0]["content"] = render_system_prompt(
                self.agent_id, self.current_phase
            )

    async def step(self) -> StepResult:
        """One model turn. Drains the inbox first; injects a [radio] user turn."""
        # Update system prompt with current phase
        self._update_phase_in_prompt()
        drained = await self.server.drain_inbox(self.agent_id)

        if drained:
            self.conversation.append(
                {"role": "user", "content": format_radio_block(drained)}
            )

        completion: Completion = await self.backend.complete(
            self.conversation, self.tool_specs
        )
        self.conversation.append(
            {"role": "assistant", "content": completion.text or ""}
        )

        tool_results: list[Message] = []
        for call in completion.tool_calls:
            args = self._resolve_refs(call.arguments)
            try:
                result = await self._execute_tool(call.name, args)
                if call.name == "create_thread":
                    self.created_threads.append(json.loads(result)["thread_id"])
            except Exception as exc:  # noqa: BLE001 — surfaced to the model verbatim
                result = json.dumps({"error": repr(exc)}, ensure_ascii=False)
            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })
        if tool_results:
            self.conversation.extend(tool_results)

        return StepResult(
            text=completion.text,
            tool_calls=completion.tool_calls,
            drained_count=len(drained),
        )

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name in self.local_tools:
            tool = self.local_tools[name]
            value = tool.handler(args)
            if hasattr(value, "__await__"):
                value = await value
            return json.dumps(value, ensure_ascii=False, default=str)
        # gate-aware execution: block work-share on non-gate threads while gate is closed
        if name == "send_message" and not self.gate_open:
            thread_id = args.get("thread")
            if (
                thread_id
                and self.gate_thread_id is not None
                and thread_id != self.gate_thread_id
            ):
                return json.dumps(
                    {
                        "error": "gate_closed",
                        "message": (
                            f"Gate is CLOSED. Work-share on thread '{thread_id}' is blocked. "
                            f"Post APPROVE on the gate thread '{self.gate_thread_id}' to open the gate."
                        ),
                    },
                    ensure_ascii=False,
                )
        return await self.tools.execute(self.agent_id, name, args)

    def _resolve_refs(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str):
                m = _THREAD_BY_NAME.match(value)
                if m:
                    resolved[key] = self._find_thread_by_name(m.group(1))
                    continue
                m = _THREAD_REF.match(value)
                if m:
                    idx = int(m.group(1))
                    try:
                        resolved[key] = self.created_threads[idx]
                        continue
                    except IndexError:
                        raise ValueError(
                            f"$thread:{idx} has no matching create_thread result "
                            f"(agent {self.agent_id} created {len(self.created_threads)})"
                        ) from None
            resolved[key] = value
        return resolved

    def _find_thread_by_name(self, name: str) -> str:
        """Resolve a thread id from the SSOT by name (what read_resource offers)."""
        for thread in self.server.snapshot()["threads"]:
            if thread["name"] == name:
                return thread["thread_id"]
        raise ValueError(f"no thread named {name!r} exists yet")
