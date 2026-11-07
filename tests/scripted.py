"""Test-only scripted model backend.

The product has no offline backend (``--demo`` and ``type: fake`` were
removed). Tests still need deterministic model turns, so they get them here:

- :class:`ScriptedBackend` — construct directly with a list of completions.
- ``type: scripted`` in a config — accepted only while :func:`install` is in
  effect (``tests/conftest.py`` does it for every test; ``scripted_child`` does
  it in a subprocess). ``script`` entries: a string is the final text, a mapping
  may hold ``text`` and ``tool_calls`` ({name, arguments}).
"""

from __future__ import annotations

from typing import Any

from agent_augury.backend.base import Completion, Message, ModelBackend, ToolCall, ToolSpec

SCRIPTED = "scripted"


class ScriptedBackend(ModelBackend):
    """Replays a pre-scripted list of completions in order.

    Records every (messages, tools) pair it was shown so tests can assert on
    exact model-visible state.
    """

    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> Completion:
        completion = self.script.pop(0)  # IndexError when the script runs dry
        self.calls.append({"messages": messages, "tools": tools})
        return completion

    @property
    def call_count(self) -> int:
        return len(self.calls)


def completion_from_spec(entry: Any) -> Completion:
    """Config ``script`` entry → Completion."""
    if isinstance(entry, str):
        return Completion(text=entry)
    if isinstance(entry, dict):
        calls = [
            ToolCall(id=f"cfg-{i}", name=c["name"], arguments=dict(c.get("arguments") or {}))
            for i, c in enumerate(entry.get("tool_calls") or [])
        ]
        return Completion(text=entry.get("text"), tool_calls=calls)
    raise ValueError(f"cannot interpret script entry: {entry!r}")


def install(setattr) -> None:
    """Make ``type: scripted`` a valid backend through *setattr* (e.g. ``monkeypatch.setattr``)."""
    import agent_augury.backends_factory as factory
    from agent_augury import config
    from agent_augury.core import session

    real = factory.build_backend

    def build_backend(spec: dict[str, Any], *args: Any, **kwargs: Any) -> ModelBackend:
        if spec.get("type") == SCRIPTED:
            return ScriptedBackend([completion_from_spec(e) for e in spec.get("script", [])])
        return real(spec, *args, **kwargs)

    setattr(factory, "build_backend", build_backend)
    setattr(session, "build_backend", build_backend)
    setattr(config, "_VALID_BACKEND_TYPES", {*config._VALID_BACKEND_TYPES, SCRIPTED})
