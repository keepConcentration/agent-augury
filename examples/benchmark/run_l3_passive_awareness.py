"""L3 Passive Awareness benchmark — deterministic numeric verification.

Scenario (DESIGN.md §6, examples/benchmark/README.md):

    agent-a: 숫자 탐색 중 (search tool을 N회 반복)
    agent-b: "(URGENT) 정답은 42가 아니라 43." 정정 멘션을 agent-a로 push

결과:
    agent-a는 search를 N회 이상 수행 AND 최종 답 = 43
    정정은 [radio] 블록으로 step 경계에서 자동 흡수 (전경 wait 없음)

Offline · deterministic — FakeModelBackend로 tool 시퀀스를 고정하고,
수동 인터리빙으로 제어한다 (실제 LLM의 wall-clock 벤치마크는 Phase 2).

실행:
    python examples/benchmark/run_l3_passive_awareness.py
    pytest examples/benchmark/ -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# src 레이아웃 bootstrap — 설치 없이 repo 루트에서 실행 가능하게
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_augury.agent.loop import AgentLoop, LocalTool
from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.server import MessageServer

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
}

FINAL_ANSWER = "정답은 43"


async def run_l3_scenario(search_rounds: int = 3) -> dict:
    """Run the L3 scenario with scripted backends; return numeric metrics."""
    server = MessageServer()
    server.register_agent("agent-a")
    server.register_agent("agent-b")

    search_log: list[dict] = []

    async def search_handler(arguments: dict) -> dict:
        search_log.append(arguments)
        return {"query": arguments.get("q", ""), "hits": ["r1"]}

    # agent-a: search를 search_rounds회 수행한 뒤 최종 답을 낸다.
    agent_a_script = [
        Completion(
            tool_calls=[
                ToolCall(id=f"s{i}", name="search", arguments={"q": "answer"})
            ]
        )
        for i in range(1, search_rounds + 1)
    ]
    agent_a_script.append(Completion(text=FINAL_ANSWER))

    agent_a = AgentLoop(
        agent_id="agent-a",
        server=server,
        backend=FakeModelBackend(agent_a_script),
        system_prompt="You are agent-a, a radio agent searching for the answer.",
        local_tools=[
            LocalTool(
                name="search",
                description="Search the space of numbers.",
                schema=SEARCH_SCHEMA,
                handler=search_handler,
            )
        ],
    )

    # agent-b: 스레드를 열고 URGENT 정정을 agent-a로 push (fire-and-forget).
    agent_b = AgentLoop(
        agent_id="agent-b",
        server=server,
        backend=FakeModelBackend(
            [
                Completion(
                    tool_calls=[
                        ToolCall(
                            id="t1",
                            name="create_thread",
                            arguments={
                                "name": "hunt",
                                "participants": ["agent-a", "agent-b"],
                            },
                        ),
                        ToolCall(
                            id="m1",
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
        ),
        system_prompt="You are agent-b, a radio agent.",
    )

    # Deterministic interleaving:
    # 1) agent-a가 혼자 탐색 (search_rounds-1회) — 정정이 아직 안 옴
    # 2) agent-b가 URGENT 정정 push
    # 3) agent-a의 다음 step()이 inbox를 drain → [radio] 블록, 그 후 탐색 계속
    # 4) agent-a 최종 답 / agent-b 종료
    for _ in range(search_rounds - 1):
        await agent_a.step()
    await agent_b.step()  # create thread + send URGENT (agent-a inbox로 push)
    await agent_a.step()  # step 경계에서 [radio] 흡수 후 탐색
    await agent_a.step()  # 최종 답: "정답은 43"
    await agent_b.step()  # "correction sent"

    # -- metrics ------------------------------------------------------------
    user_turns = [m for m in agent_a.conversation if m["role"] == "user"]
    assistant_turns = [m for m in agent_a.conversation if m["role"] == "assistant"]

    radio_msgs = [m for m in user_turns if m.get("content", "").startswith("[radio]")]
    radio_seen = len(radio_msgs) >= 1
    radio_content = radio_msgs[0]["content"] if radio_msgs else ""

    final_texts = [m["content"] for m in assistant_turns if m.get("content")]
    final_answer = final_texts[-1] if final_texts else None

    # 정답이 [radio] 흡수 이후에 나와야 한다 (흡수 순서).
    radio_index = next(
        (
            i
            for i, m in enumerate(agent_a.conversation)
            if m.get("content", "").startswith("[radio]")
        ),
        None,
    )
    answer_index = next(
        (
            i
            for i, m in enumerate(agent_a.conversation)
            if m["role"] == "assistant" and m.get("content") == FINAL_ANSWER
        ),
        None,
    )
    final_answer_after_radio = (
        radio_index is not None
        and answer_index is not None
        and answer_index > radio_index
    )

    # 명시적 폴링 금지: agent-a가 read_resource를 한 번도 호출하지 않았어야 함.
    assistant_tool_names = [
        tc["function"]["name"]
        for m in assistant_turns
        if m.get("tool_calls")
        for tc in m["tool_calls"]
    ]
    polled = "read_resource" in assistant_tool_names

    return {
        "search_steps": len(search_log),
        "final_answer": final_answer,
        "radio_seen": radio_seen,
        "radio_content": radio_content,
        "final_answer_after_radio": final_answer_after_radio,
        "polled": polled,
    }


def assert_benchmark(metrics: dict, search_rounds: int = 3) -> None:
    """README.md '단언 (숫자)' 섹션의 어서션 그대로."""
    assert metrics["search_steps"] >= search_rounds, "작업이 방해받지 않음 (search N회 이상)"
    assert metrics["final_answer"] == FINAL_ANSWER, "정정이 흡수됨 (최종 답 = 43)"
    assert metrics["radio_seen"], "push → [radio] 삽입 확인"
    assert metrics["final_answer_after_radio"], "정정 이후에 답이 나옴 (흡수 순서)"
    assert metrics["polled"] is False, "명시적 폴링/대기 도구 사용 안 함 (push-only)"


def main() -> int:
    metrics = asyncio.run(run_l3_scenario())
    print("== L3 Passive Awareness benchmark ==")
    print(f"search_steps            = {metrics['search_steps']}  (>= 3)")
    print(f"final_answer            = {metrics['final_answer']!r}")
    print(f"radio_seen              = {metrics['radio_seen']}")
    print(f"final_answer_after_radio= {metrics['final_answer_after_radio']}")
    print(f"polled(read_resource)   = {metrics['polled']}  (False여야 함)")
    print()
    assert_benchmark(metrics)
    print("✅ L3 PASSIVE AWARENESS BENCHMARK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
