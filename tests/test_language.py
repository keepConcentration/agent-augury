"""Session-level language detection integration tests (v0.3)."""

import asyncio

from agent_augury.agent.loop import AgentLoop
from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.server import MessageServer
from agent_augury.session import Session


class ScriptedBackend(ModelBackend):
    """Returns pre-scripted completions; finishes after exhausting the script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append(messages)
        return self.script.pop(0)


def make_agent(server, agent_id, script):
    return AgentLoop(
        agent_id=agent_id,
        server=server,
        backend=ScriptedBackend(script),
    )


def test_session_detects_korean_and_injects_language():
    """When initial prompt is Korean, all agents get language='Korean'."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agents = [
        make_agent(server, "agent-1", [Completion(text="done")]),
        make_agent(server, "agent-2", [Completion(text="done")]),
    ]

    session = Session(server=server, agents=agents)

    # Run with Korean initial prompt
    asyncio.run(session.run(initial_prompt="안녕하세요, 무엇을 도와드릴까요?"))

    # Both agents should have language set to Korean
    for agent in agents:
        assert agent.language == "Korean"
        # System prompt should contain the language instruction
        assert "Language instruction" in agent.conversation[0]["content"]
        assert "Korean" in agent.conversation[0]["content"]


def test_session_detects_english_and_injects_language():
    """When initial prompt is English, all agents get language='English'."""
    server = MessageServer()
    server.register_agent("agent-1")
    server.register_agent("agent-2")

    agents = [
        make_agent(server, "agent-1", [Completion(text="done")]),
        make_agent(server, "agent-2", [Completion(text="done")]),
    ]

    session = Session(server=server, agents=agents)

    asyncio.run(session.run(initial_prompt="Hello, how can I help?"))

    for agent in agents:
        assert agent.language == "English"
        assert "Language instruction" in agent.conversation[0]["content"]
        assert "English" in agent.conversation[0]["content"]


def test_session_empty_prompt_no_language():
    """Empty/None initial prompt → no language instruction injected."""
    server = MessageServer()
    server.register_agent("agent-1")

    agents = [
        make_agent(server, "agent-1", [Completion(text="done")]),
    ]

    session = Session(server=server, agents=agents)

    asyncio.run(session.run(initial_prompt=""))

    for agent in agents:
        assert agent.language == ""
        assert "Language instruction" not in agent.conversation[0]["content"]


def test_session_task_field_triggers_detection():
    """When initial_prompt is None but task is set, task is used for detection."""
    server = MessageServer()
    server.register_agent("agent-1")

    agents = [
        make_agent(server, "agent-1", [Completion(text="done")]),
    ]

    session = Session(server=server, agents=agents, task="한국어로 대화합시다")

    asyncio.run(session.run())

    assert agents[0].language == "Korean"
    assert "Language instruction" in agents[0].conversation[0]["content"]
