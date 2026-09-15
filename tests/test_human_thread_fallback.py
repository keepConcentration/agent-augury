"""Regression: mid-run Discord human.send must not die on invented thread ids."""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.fake import FakeModelBackend
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.server import MessageServer
from agent_augury.core.session import HUMAN_CHAT_THREAD_NAME, Session
from agent_augury.gateway import (
    SessionBridge,
    SessionGateway,
    SurfaceSubscription,
    make_command,
)


def _fake_agent(server: MessageServer, agent_id: str = "agent-1") -> AgentLoop:
    backend = FakeModelBackend(script=[])
    return AgentLoop(agent_id=agent_id, backend=backend, server=server)


@pytest.mark.asyncio
async def test_human_send_falls_back_when_thread_missing():
    server = MessageServer()
    server.register_human("human")
    agent = _fake_agent(server)
    session = Session(server=server, agents=[agent], has_human=True)
    await session._setup()
    human_tid = server.resolve_thread_id(HUMAN_CHAT_THREAD_NAME)
    assert human_tid

    mid = await session.human_send("c-user-test", content="approve path?")
    assert mid
    msg = server._message_index[mid]
    assert msg["thread_id"] == human_tid
    assert msg["content"] == "approve path?"


@pytest.mark.asyncio
async def test_ask_user_bad_thread_does_not_poison_recent():
    server = MessageServer()
    server.register_agent("a1")
    server.register_human("human")
    tid = await server.create_thread("plan", participants=["a1"])
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw, session=type("S", (), {"server": server})())
    bridge._recent_thread = tid
    bridge.publish_core_event(
        {
            "type": "tool",
            "tool": "ask_user",
            "agent_id": "a1",
            "args": {"thread": "c-user-test", "question": "Go?"},
            "result": '{"error": "KeyError"}',
        }
    )
    assert bridge.recent_thread == tid
    assert bridge.pending is not None
    assert bridge.pending.question == "Go?"


@pytest.mark.asyncio
async def test_bridge_mid_run_send_uses_fallback_thread():
    server = MessageServer()
    server.register_human("human")
    agent = _fake_agent(server)
    session = Session(server=server, agents=[agent], has_human=True)
    await session._setup()
    human_tid = server.resolve_thread_id(HUMAN_CHAT_THREAD_NAME)

    gw = session.gateway
    if "ink" not in gw.surfaces():
        gw.attach(SurfaceSubscription(name="ink", mode="interact", family="ui"))
    session.bridge.set_running(True)
    session.bridge._recent_thread = "c-user-test"

    result = session.bridge.handle_command(
        make_command("human.send", id="1", content="go", mentions=["agent-1"])
    )
    assert result.get("queued") is True
    for _ in range(20):
        await asyncio.sleep(0)
        msgs = [m for m in server.snapshot()["messages"] if m["author"] == "human"]
        if msgs:
            assert msgs[-1]["thread_id"] == human_tid
            return
    raise AssertionError("human message was not delivered")
