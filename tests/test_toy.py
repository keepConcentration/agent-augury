"""Local (non-server) tools + L2 vs L3 contrast verification (DESIGN.md §6)."""

import json

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.server import MessageServer
from tests.test_agent_loop import make_agent


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


def test_l2_vs_l3_contrast_metrics():
    """The §6 acceptance script, imported and asserted here.

    Same discovery workload, same correction timing — ONLY the listening
    mode differs (§3.5.5). L3 must absorb the correction passively while
    never skipping a search; L2 must sacrifice a search slot to listen.
    """
    from examples.l2_vs_l3_toy import run_scenario

    l3 = run_scenario("L3")
    l2 = run_scenario("L2")

    assert l3["search_calls"] >= 3
    assert l3["final_answer"].endswith("43"), f"got {l3['final_answer']!r}"
    assert l3["radio_turn_seen"], "L3 must inject the correction as a [radio] turn"
    assert not l3["wait_used"], "L3 must not expose/use wait_for_mention"

    assert l2["wait_used"], "L2 must block on wait_for_mention explicitly"
    assert l2["final_answer"].endswith("43"), f"got {l2['final_answer']!r}"
    assert l3["search_calls"] > l2["search_calls"], (
        "passive awareness must preserve more work slots than foreground waiting"
    )
