"""Wiring — YAML config loading, session assembly, run loop, CLI entry."""

import json

import pytest
import yaml

from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.config import ConfigError, load_config
from agent_augury.session import Session


def write_cfg(tmp_path, body: dict):
    p = tmp_path / "session.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


FAKE_CFG = {
    "mode": "L3",
    "max_steps": 12,
    "task": "find the answer",
    "agents": [
        {"id": "agent-1", "backend": {"type": "fake", "script": ["thinking...", "answer: 43"]}},
        {
            "id": "agent-2",
            "backend": {
                "type": "fake",
                "script": [
                    {"tool_calls": [
                        {"name": "create_thread", "arguments": {"name": "work", "participants": ["agent-1", "agent-2"]}},
                        {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "(FYI) working", "mentions": []}},
                    ]},
                    "done coordinating",
                ],
            },
        },
    ],
}


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------


def test_load_config_parses_mode_agents_task(tmp_path):
    cfg = load_config(write_cfg(tmp_path, FAKE_CFG))
    assert cfg["mode"] == "L3"
    assert [a["id"] for a in cfg["agents"]] == ["agent-1", "agent-2"]
    assert cfg["task"] == "find the answer"
    assert cfg["max_steps"] == 12


def test_load_config_rejects_missing_agents(tmp_path):
    p = write_cfg(tmp_path, {"mode": "L3"})
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_config_rejects_bad_mode(tmp_path):
    bad = dict(FAKE_CFG, mode="L9")
    with pytest.raises(ConfigError):
        load_config(write_cfg(tmp_path, bad))


# ---------------------------------------------------------------------------
# session assembly & run
# ---------------------------------------------------------------------------


async def test_session_builds_shared_server_and_agents(tmp_path):
    session = Session.from_config(load_config(write_cfg(tmp_path, FAKE_CFG)))
    assert session.server.mode == "L3"
    assert [a.agent_id for a in session.agents] == ["agent-1", "agent-2"]
    # all agents share ONE server instance (SSOT)
    assert len({id(a.server) for a in session.agents}) == 1


async def test_run_round_robin_and_task_injected_to_first_agent(tmp_path):
    session = Session.from_config(load_config(write_cfg(tmp_path, FAKE_CFG)))
    steps = await session.run()

    agent1 = session.agents[0]
    # task was injected as the opening user turn of agent-1
    sys_then_user = [m["role"] for m in agent1.conversation[:2]]
    assert sys_then_user == ["system", "user"]
    assert "find the answer" in agent1.conversation[1]["content"]
    # both agents ran their scripted completions to exhaustion
    assert isinstance(agent1.backend, FakeModelBackend)
    assert agent1.backend.call_count == 2
    assert session.agents[1].backend.call_count == 2
    assert steps >= 4
    # the created thread exists in the SSOT
    snap = session.server.snapshot()
    assert any(t["name"] == "work" for t in snap["threads"])
    # broadcast fanned out: agent-1 received agent-2's FYI in its inbox,
    # then drained it on its next step
    assert any(
        m["role"] == "user" and "[radio]" in (m.get("content") or "")
        for m in agent1.conversation
    )


async def test_on_step_callback_fires_per_step(tmp_path):
    events = []
    session = Session.from_config(load_config(write_cfg(tmp_path, FAKE_CFG)))
    session.on_step = lambda aid, result: events.append((aid, result.drained_count))
    await session.run()
    assert len(events) >= 4
    assert {aid for aid, _ in events} == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# $thread refs resolve against tool results
# ---------------------------------------------------------------------------


async def test_thread_reference_placeholder_resolves(tmp_path):
    session = Session.from_config(load_config(write_cfg(tmp_path, FAKE_CFG)))
    await session.run()
    snap = session.server.snapshot()
    msgs = [m for m in snap["messages"] if m["author"] == "agent-2" and m["thread_id"].startswith("thread-")]
    # send_message executed with the real resolved thread id
    assert any(m["content"] == "(FYI) working" for m in msgs)


# ---------------------------------------------------------------------------
# real-backend construction from env var names
# ---------------------------------------------------------------------------


def test_build_real_backend_reads_api_key_from_env(tmp_path, monkeypatch):
    from agent_augury.backends_factory import build_backend

    monkeypatch.setenv("AUGURY_TEST_KEY", "sk-test")
    backend = build_backend(
        {"type": "openai", "model": "m", "base_url": "http://x/v1", "api_key_env": "AUGURY_TEST_KEY"}
    )
    assert backend.api_key == "sk-test"


def test_build_real_backend_missing_env_raises(tmp_path, monkeypatch):
    from agent_augury.backends_factory import build_backend

    monkeypatch.delenv("AUGURY_MISSING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AUGURY_MISSING_KEY"):
        build_backend({"type": "openai", "model": "m", "base_url": "http://x/v1", "api_key_env": "AUGURY_MISSING_KEY"})


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_runs_fake_session_and_prints_log(tmp_path, capsys, monkeypatch):
    from agent_augury.cli import main

    cfg_path = write_cfg(tmp_path, FAKE_CFG)
    rc = main(["--config", str(cfg_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[agent-1]" in out and "[agent-2]" in out
    assert "steps=" in out
