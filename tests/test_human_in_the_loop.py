"""Human-in-the-loop — v1.0 통합 검증 (USER_INTERVENTION_DESIGN.md §6 시나리오 A).

에이전트가 ``ask_user``로 질문 → ``human_send`` 응답 주입 → 다음 ``step()``에서
``[radio]``로 흡수 → 최종 결과에 응답이 반영되는 흐름을 검증한다.
"""

from __future__ import annotations

import pytest

from agent_augury.session import Session
from tests.conftest import build_cfg


def _hitl_cfg() -> dict:
    """에이전트 1개 + human 섹션. fake backend가 ask_user를 호출하도록 스크립팅."""
    return build_cfg(
        max_steps=20,
        task="DB 선택을 확인해줘",
        human={"id": "human"},
        agents=[
            {
                "id": "agent-1",
                "backend": {
                    "type": "fake",
                    "script": [
                        # step 1: 스레드 생성 + ask_user 도구 호출 (같은 턴에서 순서대로)
                        {
                            "tool_calls": [
                                {
                                    "name": "create_thread",
                                    "arguments": {"name": "work", "participants": ["agent-1"]},
                                },
                                {
                                    "name": "ask_user",
                                    "arguments": {
                                        "thread": "$thread:0",
                                        "question": "DB는 뭘 쓸까?",
                                        "options": ["postgres", "mysql"],
                                    },
                                },
                            ]
                        },
                        # step 2: 질문 결과를 받고 최종 답 (응답 반영)
                        "DB는 postgres로 결정됐습니다.",
                    ],
                },
            },
        ],
    )


@pytest.mark.asyncio
async def test_human_in_the_loop_ask_user_absorb_reply():
    """시나리오 A: ask_user → human_send 응답 → [radio] 흡수 → 최종 결과 반영."""
    session = Session.from_config(_hitl_cfg())
    assert session.has_human is True

    # 1) 첫 run: agent-1이 create_thread + ask_user 호출 → human inbox에 질문 push.
    #    agent-1은 script의 첫 entry(ask_user tool_calls)를 소진하고, text가 없고
    #    pending도 없으므로 정상 종료한다.
    await session.run(initial_prompt="DB 선택을 확인해줘")

    # human inbox에 질문이 있어야 한다.
    assert session.server.inbox_size("human") >= 1

    # ask_user가 만든 스레드 id를 찾는다.
    threads = session.server.snapshot()["threads"]
    tid = threads[0]["thread_id"]

    # 2) 사용자 응답 주입 → agent-1 inbox로 push.
    await session.human_send(tid, content="postgres로 결정")

    # 3) 두 번째 run: agent-1이 남은 script(최종 답)를 실행하면서
    #    inbox의 human 응답을 [radio]로 흡수한다.
    await session.run()

    agent = session.agents[0]
    # agent의 대화 기록에서 human 응답이 [radio] 형태로 들어갔는지 확인
    radio_blocks = [
        m for m in agent.conversation
        if m["role"] == "user" and "from human" in m["content"]
    ]
    assert len(radio_blocks) >= 1
    assert "postgres로 결정" in radio_blocks[-1]["content"]

    await session.close()





@pytest.mark.asyncio
async def test_ask_user_tool_spec_exposed_with_human():
    """human 섹션이 있으면 ask_user 도구가 tool_specs에 노출된다."""
    session = Session.from_config(_hitl_cfg())
    agent = session.agents[0]
    names = [t["name"] for t in agent.tool_specs]
    assert "ask_user" in names
    await session.close()


@pytest.mark.asyncio
async def test_ask_user_tool_spec_always_present():
    """ask_user 도구는 항상 노출 (human이 코드에 내장되어 항상 켜짐)."""
    cfg = build_cfg(
        agents=[{"id": "agent-1", "backend": {"type": "fake", "script": ["ok"]}}],
    )
    session = Session.from_config(cfg)
    agent = session.agents[0]
    # ask_user 도구 spec은 항상 존재 (툴박스에 고정)
    names = [t["name"] for t in agent.tool_specs]
    assert "ask_user" in names
    # has_human이 항상 True이므로 system prompt에 HITL 블록이 포함됨
    sys_content = agent.conversation[0]["content"]
    assert "Human-in-the-loop rules" in sys_content
    await session.close()


@pytest.mark.asyncio
async def test_hitl_prompt_blocks_included_when_human():
    """human 섹션이 있으면 system prompt에 HITL 규칙이 주입된다."""
    session = Session.from_config(_hitl_cfg())
    agent = session.agents[0]
    sys_content = agent.conversation[0]["content"]
    assert "Human-in-the-loop rules" in sys_content
    assert "ask_user" in sys_content
    await session.close()
