"""Local (non-server) tools — unit tests."""

import json

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.server import MessageServer


async def test_local_tool_executes_without_server_roundtrip():
    async def handler(arguments):
        return {"query": arguments.get("q"), "hits": ["r1"]}

    from agent_augury.agent.loop import LocalTool

    server = MessageServer()
    server.register_agent("agent-a")
    agent = AgentLoop(
        agent_id="agent-a",
        server=server,
        backend=FakeModelBackend(
            [
                Completion(tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "answer"})]),
                Completion(text="done"),
            ]
        ),
        system_prompt="test",
        local_tools=[
            LocalTool(
                name="search",
                description="Search.",
                schema={"type": "object", "properties": {"q": {"type": "string"}}},
                handler=handler,
            )
        ],
    )
    await agent.step()

    tool_msgs = [m for m in agent.conversation if m["role"] == "tool"]
    payload = json.loads(tool_msgs[0]["content"])
    assert payload == {"query": "answer", "hits": ["r1"]}
    # local tool spec was exposed to the model alongside server tools
    names = {t["name"] for t in agent.tool_specs}
    assert "search" in names
