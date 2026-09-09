"""시나리오 C — reserved name 'human' + 발신 경로 경계 검증.

USER_INTERVENTION_DESIGN.md §3.2 (S1~S4) 및 §6 시나리오 C.
"""

from __future__ import annotations

import pytest

from agent_augury.server import MessageServer, ReservedNameError

# ---------------------------------------------------------------------------
# S1 — agent id 'human' (및 대소문자 변형)은 등록 시 거부
# ---------------------------------------------------------------------------


def test_register_agent_rejects_reserved_name_human():
    server = MessageServer()
    with pytest.raises(ReservedNameError):
        server.register_agent("human")


def test_register_agent_rejects_reserved_name_case_insensitive():
    server = MessageServer()
    with pytest.raises(ReservedNameError):
        server.register_agent("Human")
    with pytest.raises(ReservedNameError):
        server.register_agent("HUMAN")


def test_config_loader_rejects_agent_id_human(tmp_path):
    import yaml

    from agent_augury.config import ConfigError, load_config

    cfg_path = tmp_path / "bad.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "mode": "L3",
                "agents": [
                    {
                        "id": "human",
                        "backend": {
                            "type": "nous_oauth",
                            "model": "deepseek/deepseek-v4-flash",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    assert "reserved" in str(excinfo.value)


def test_config_loader_rejects_agent_id_human_case_insensitive(tmp_path):
    import yaml

    from agent_augury.config import ConfigError, load_config

    cfg_path = tmp_path / "bad2.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "mode": "L3",
                "agents": [
                    {
                        "id": "Human",
                        "backend": {
                            "type": "nous_oauth",
                            "model": "deepseek/deepseek-v4-flash",
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(cfg_path)


# ---------------------------------------------------------------------------
# S2 — register_human은 정상 동작, 에이전트 레지스트리와 분리
# ---------------------------------------------------------------------------


def test_register_human_separate_registry():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_human()
    # human은 _humans에만 있고 _agents에는 없다
    assert "human" in server._humans
    assert "human" not in server._agents
    assert "agent-1" in server._agents


def test_register_human_rejects_non_default_id():
    server = MessageServer()
    with pytest.raises(ValueError):
        server.register_human("operator")


# ---------------------------------------------------------------------------
# S3 — send_message(author='human') 위조 차단
# ---------------------------------------------------------------------------


async def test_send_message_rejects_human_author_forgery():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_human()
    tid = await server.create_thread("plan", participants=["agent-1"])
    with pytest.raises(ValueError):
        await server.send_message(tid, author="human", content="forged")


# ---------------------------------------------------------------------------
# S4 — human_send(author='agent-x') 위조 차단 + 정상 경로
# ---------------------------------------------------------------------------


async def test_human_send_rejects_agent_author_forgery():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_human()
    tid = await server.create_thread("plan", participants=["agent-1"])
    with pytest.raises(ValueError):
        await server.human_send(tid, author="agent-1", content="forged")


async def test_human_send_delivers_to_agent_inbox():
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_human()
    tid = await server.create_thread("plan", participants=["agent-1"])
    mid = await server.human_send(tid, author="human", content="옵션 B로 진행")
    assert isinstance(mid, str)
    # agent-1 inbox에 push됨
    assert server.inbox_size("agent-1") == 1


async def test_ask_user_routes_to_human_via_mentions():
    """에이전트가 ask_user → human inbox로 push되는 경로 검증."""
    from agent_augury.agent.tools import ToolBox

    server = MessageServer()
    server.register_agent("agent-1")
    server.register_human()
    tid = await server.create_thread("plan", participants=["agent-1"])

    toolbox = ToolBox(server)
    result = await toolbox.execute(
        "agent-1",
        "ask_user",
        {"thread": tid, "question": "DB는 뭘로 할까?", "options": ["postgres", "mysql"]},
    )
    assert '"question_delivered"' in result
    # human inbox에 질문 메시지 push됨
    assert server.inbox_size("human") == 1
