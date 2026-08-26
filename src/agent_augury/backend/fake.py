"""Fake ModelBackend — deterministic completions for tests & L2/L3 verification."""

from __future__ import annotations

from .base import Completion, Message, ModelBackend, ToolSpec


class FakeModelBackend(ModelBackend):
    """Replays a pre-scripted list of completions in order.

    Records every (messages, tools) pair it was shown so verification scripts
    can assert on exact model-visible state.
    """

    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Completion:
        completion = self.script.pop(0)  # may raise IndexError when dry
        self.calls.append({"messages": messages, "tools": tools})
        return completion

    @property
    def call_count(self) -> int:
        return len(self.calls)
