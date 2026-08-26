"""L2 vs L3 contrast verification — the v0.1a acceptance script (DESIGN.md §6).

Scenario: agent-a is searching for "the answer to everything". Mid-search,
agent-b sends an URGENT correction: the answer is 43, not 42.

The ONLY difference between the two runs is the listening mode (§3.5.5):
- L3 (passive): correction is pushed to the inbox; step() absorbs it as a
  [radio] turn. The search workload never pauses.
- L2 (foreground): there is no push; agent-a must spend a step calling
  wait_for_mention (a blocking foreground tool call) to hear the same news.

Both runs use the SAME scripted discovery behavior, so any difference in
search_calls / steps comes from the communication mode alone.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_augury.agent.loop import AgentLoop, LocalTool  # noqa: E402
from agent_augury.backend.base import Completion, ToolCall  # noqa: E402
from agent_augury.backend.fake import FakeModelBackend  # noqa: E402
from agent_augury.server import MessageServer  # noqa: E402

SEARCH_RESULTS = ["42?", "41?", "40?", "39?", "38?"]


def _search_tool(calls: list[str]) -> LocalTool:
    async def handler(arguments: dict) -> str:
        calls.append(arguments.get("q", ""))
        return json.dumps({"query": arguments.get("q"), "candidates": SEARCH_RESULTS})

    return LocalTool(
        name="search",
        description="Search the answer space.",
        schema={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        handler=handler,
    )


def run_scenario(mode: str) -> dict:
    """Run one contrast leg; returns numeric metrics only."""

    async def scenario() -> dict:
        server = MessageServer()
        server.set_mode(mode)
        server.register_agent("agent-a")
        server.register_agent("agent-b")

        search_calls: list[str] = []
        metrics = {"wait_used": False, "radio_turn_seen": False}

        # ---- agent-b: one-shot corrector -------------------------------
        b_script = [
            Completion(
                tool_calls=[
                    ToolCall(
                        id="b1",
                        name="create_thread",
                        arguments={"name": "hunt", "participants": ["agent-a", "agent-b"]},
                    ),
                    ToolCall(
                        id="b2",
                        name="send_message",
                        arguments={
                            "thread": "$thread:0",
                            "content": "(URGENT) 정답은 42가 아니라 43.",
                            "mentions": ["agent-a"],
                        },
                    ),
                ]
            ),
            Completion(text="correction sent"),
        ]
        b_loop = AgentLoop(
            agent_id="agent-b", server=server, backend=FakeModelBackend(b_script)
        )

        # ---- agent-a: scripted discovery with mid-flight correction ----
        if mode == "L3":
            # keeps searching straight through the correction boundary; the
            # [radio] turn arrives automatically between searches.
            a_script = [
                Completion(tool_calls=[ToolCall(id="a1", name="search", arguments={"q": "answer"})]),
                Completion(tool_calls=[ToolCall(id="a2", name="search", arguments={"q": "answer"})]),
                Completion(tool_calls=[ToolCall(id="a3", name="search", arguments={"q": "answer"})]),
                Completion(text="정답은 43"),
            ]
        else:
            # L2: no push exists; the agent must BLOCK on wait_for_mention
            # between searches (that's the whole point of the contrast).
            a_script = [
                Completion(tool_calls=[ToolCall(id="a1", name="search", arguments={"q": "answer"})]),
                Completion(
                    tool_calls=[
                        ToolCall(id="a2", name="wait_for_mention", arguments={"timeout": 5})
                    ]
                ),
                Completion(tool_calls=[ToolCall(id="a3", name="search", arguments={"q": "answer"})]),
                Completion(text="정답은 43"),
            ]

        metrics["wait_used"] = any(
            c.name == "wait_for_mention" for s in a_script for c in s.tool_calls
        )

        a_loop = AgentLoop(
            agent_id="agent-a",
            server=server,
            backend=FakeModelBackend(a_script),
            local_tools=[_search_tool(search_calls)],
        )

        if mode == "L3":
            # 1. agent-a starts searching BEFORE any thread/correction exists
            await a_loop.step()  # search — inbox empty at drain time

        # 2. agent-b delivers its correction mid-flight
        await b_loop.step()
        tid = b_loop.created_threads[0]

        if mode == "L3":
            # correction sits in the inbox, unabsorbed until the next boundary
            assert server.inbox_size("agent-a") == 1

        # 3. agent-a keeps stepping until its final text
        while True:
            result = await a_loop.step()
            if mode == "L3" and result.drained_count:
                metrics["radio_turn_seen"] = True
            if not result.tool_calls and result.text is not None:
                break

        metrics.update(
            {
                "mode": mode,
                "search_calls": len(search_calls),
                "steps": a_loop.backend.call_count,
                "final_answer": a_loop.conversation[-1]["content"],
            }
        )
        return metrics

    return asyncio.run(scenario())


def main() -> int:
    l3 = run_scenario("L3")
    l2 = run_scenario("L2")

    print("=== L3 (passive awareness: push + inbox) ===")
    print(f"  search_calls={l3['search_calls']} steps={l3['steps']} answer={l3['final_answer']}")
    print("=== L2 (foreground wait_for_mention) ===")
    print(f"  search_calls={l2['search_calls']} steps={l2['steps']} answer={l2['final_answer']}")

    assert l3["search_calls"] >= 3, f"L3 must keep searching (got {l3['search_calls']})"
    assert l3["final_answer"] == "정답은 43", "L3 must absorb the correction"
    assert l3["radio_turn_seen"], "L3 must inject the [radio] turn"
    assert not l3["wait_used"], "L3 must never call wait_for_mention"
    assert l2["wait_used"], "L2 must block on wait_for_mention"
    assert l2["final_answer"] == "정답은 43"
    assert l3["search_calls"] > l2["search_calls"], (
        f"L3 {l3['search_calls']} searches vs L2 {l2['search_calls']}"
    )
    print("\nPASS: passive awareness preserved more work slots than foreground waiting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
