"""Protocol sessions: reuse open threads; soft-block inventing new ones."""

from __future__ import annotations

import json

import pytest

from agent_augury.backend.base import Completion, ModelBackend, ToolCall
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.agent.system_prompt import (
    format_session_threads_block,
    render_system_prompt,
)
from agent_augury.core.server import MessageServer
from agent_augury.gateway.translate import translate_core_event


def test_format_session_threads_block_lists_ids():
    block = format_session_threads_block(
        [{"thread_id": "thread-1", "name": "plan"}, {"thread_id": "thread-5", "name": "human"}],
        ready_thread_id="thread-5",
    )
    assert "thread-1" in block
    assert "name='plan'" in block
    assert "thread-5" in block
    assert "READY:" in block


def test_render_system_prompt_prefers_existing_threads():
    prompt = render_system_prompt(
        "agent-1",
        phase="P1_EXPLORE",
        session_threads=[
            {"thread_id": "thread-1", "name": "plan"},
            {"thread_id": "thread-5", "name": "human"},
        ],
        ready_thread_id="thread-5",
    )
    assert "Open session threads" in prompt
    assert "`thread-5`" in prompt
    assert "Do not invent new threads" in prompt or "do **not** invent" in prompt
    assert "Do NOT open new threads" in prompt
    assert "human" in prompt


def test_translate_preserves_bootstrap_flag():
    wire = translate_core_event(
        {
            "type": "create_thread",
            "thread_id": "thread-1",
            "name": "plan",
            "participants": ["a1"],
            "bootstrap": True,
        }
    )
    assert wire is not None
    assert wire["type"] == "thread.created"
    assert wire.get("bootstrap") is True


class _ScriptBackend(ModelBackend):
    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)

    async def complete(self, messages, tools=None):
        if not self.script:
            return Completion(text=None)
        return self.script.pop(0)


@pytest.mark.asyncio
async def test_protocol_soft_blocks_new_create_thread():
    server = MessageServer()
    server.register_agent("a1")
    await server.create_thread("plan", participants=["a1"], bootstrap=True)
    backend = _ScriptBackend(
        [
            Completion(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="create_thread",
                        arguments={
                            "name": "p1-explore-extra",
                            "participants": ["a1"],
                        },
                    )
                ],
            )
        ]
    )
    agent = AgentLoop(agent_id="a1", server=server, backend=backend)
    agent.current_phase = "P1_EXPLORE"
    agent.gate_open = False
    result = await agent.step()
    assert result.tool_calls
    # tool result is in conversation
    tool_msgs = [m for m in agent.conversation if m.get("role") == "tool"]
    assert tool_msgs
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["error"] == "protocol_threads_fixed"
    assert any(t.get("name") == "plan" for t in payload["threads"])
    # No new thread created
    names = {t["name"] for t in server.snapshot()["threads"]}
    assert "p1-explore-extra" not in names


@pytest.mark.asyncio
async def test_protocol_allows_create_thread_reuse_by_name():
    server = MessageServer()
    server.register_agent("a1")
    tid = await server.create_thread("plan", participants=["a1"], bootstrap=True)
    backend = _ScriptBackend(
        [
            Completion(
                text=None,
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="create_thread",
                        arguments={"name": "plan", "participants": ["a1"]},
                    )
                ],
            )
        ]
    )
    agent = AgentLoop(agent_id="a1", server=server, backend=backend)
    agent.current_phase = "P2_SPLIT"
    agent.gate_open = False
    agent.gate_thread_id = tid
    await agent.step()
    tool_msgs = [m for m in agent.conversation if m.get("role") == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["thread_id"] == tid
    assert payload.get("reused") is True
