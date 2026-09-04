"""Wiring — YAML config loading, session assembly, run loop, CLI entry."""

import json

import pytest
import yaml

from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.config import ConfigError, load_config
from agent_augury.protocol.phases import P1_EXPLORE
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
    assert [a.agent_id for a in session.agents] == ["agent-1", "agent-2"]
    # all agents share ONE server instance (SSOT)
    assert len({id(a.server) for a in session.agents}) == 1


async def test_run_parallel_and_task_injected_to_first_agent(tmp_path):
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
# v0.1b: gate & mirror wiring
# ---------------------------------------------------------------------------


GATE_CFG = {
    "mode": "L3",
    "max_steps": 24,
    "gate": {"thread_name": "plan"},
    "agents": [
        {
            "id": "a1",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "create_thread", "arguments": {"name": "plan", "participants": ["a1", "a2", "a3"]}},
                    {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "PROPOSE: 분할 v1", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "(FYI) a1 몫 완료", "mentions": []}},
                ]},
            ]},
        },
        {
            "id": "a2",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "(FYI) a2 몫 완료", "mentions": []}},
                ]},
            ]},
        },
        {
            "id": "a3",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "(FYI) a3 몫 완료", "mentions": []}},
                ]},
            ]},
        },
    ],
}


async def test_session_wires_consensus_gate_and_opens_it(tmp_path):
    session = Session.from_config(load_config(write_cfg(tmp_path, GATE_CFG)))
    await session.run()

    assert session.gate is not None
    assert session.gate.is_open, f"approvals={session.gate.approvals}"
    assert session.gate.participants == ["a1", "a2", "a3"]

    # every work-share message carries seq AFTER the gate opened
    snap = session.server.snapshot()
    seq = {m["message_id"]: m["seq"] for m in snap["messages"]}
    work = [m for m in snap["messages"] if m["content"].startswith("(FYI)")]
    assert len(work) == 3
    assert all(seq[m["message_id"]] > session.gate.opened_at_seq for m in work)


async def test_session_without_gate_config_has_no_gate(tmp_path):
    session = Session.from_config(load_config(write_cfg(tmp_path, FAKE_CFG)))
    assert session.gate is None


async def test_session_mirror_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AUGURY_MIRROR_URL", raising=False)
    cfg = dict(GATE_CFG, mirror={"type": "discord_webhook", "url_env": "AUGURY_MIRROR_URL"})
    session = Session.from_config(load_config(write_cfg(tmp_path, cfg)))
    assert session.mirror is None  # observation silently off — core unaffected


def test_cli_accepts_gate_config(tmp_path, capsys):
    from agent_augury.cli import main

    rc = main(["--config", str(write_cfg(tmp_path, GATE_CFG))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gate=OPEN" in out


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


# ---------------------------------------------------------------------------
# gate-aware execution
# ---------------------------------------------------------------------------


GATE_BLOCK_CFG = {
    "mode": "L3",
    "max_steps": 24,
    "gate": {"thread_name": "plan"},
    "agents": [
        {
            "id": "a1",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "create_thread", "arguments": {"name": "plan", "participants": ["a1", "a2"]}},
                    {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "PROPOSE: v1", "mentions": []}},
                ]},
                # gate still CLOSED → work-share on hunt thread blocked
                {"tool_calls": [
                    {"name": "create_thread", "arguments": {"name": "hunt", "participants": ["a1", "a2"]}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread:1", "content": "(FYI) 내 몫 완료", "mentions": []}},
                ]},
                # now approve → gate opens → work-share allowed
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread:0", "content": "APPROVE: ok", "mentions": []}},
                ]},
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread:1", "content": "(FYI) 몫 재시도 완료", "mentions": []}},
                ]},
            ]},
        },
        {
            "id": "a2",
            "backend": {"type": "fake", "script": [
                {"tool_calls": [
                    {"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}},
                ]},
            ]},
        },
    ],
}


async def test_gate_blocks_work_share_until_open(tmp_path):
    """Gate CLOSED → work-share on non-gate thread is blocked with error."""
    session = Session.from_config(load_config(write_cfg(tmp_path, GATE_BLOCK_CFG)))
    await session.run()

    snap = session.server.snapshot()
    # gate must have opened
    assert session.gate.is_open
    # work-share on hunt thread must appear only AFTER gate opened
    work_msgs = [m for m in snap["messages"] if "(FYI)" in m["content"]]
    assert len(work_msgs) == 1, f"expected exactly 1 work-share after gate open, got {len(work_msgs)}"
    assert work_msgs[0]["content"] == "(FYI) 몫 재시도 완료"
    assert work_msgs[0]["seq"] > session.gate.opened_at_seq


def test_cli_runs_fake_session_and_prints_log(tmp_path, capsys):
    from agent_augury.cli import main

    rc = main(["--config", str(write_cfg(tmp_path, FAKE_CFG))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "💭 agent-1:" in out and "💭 agent-2:" in out
    assert "steps=" in out


# ---------------------------------------------------------------------------
# v0.2: gate-aware per-phase blocking
# ---------------------------------------------------------------------------


PROTOCOL_PHASE_GATE_CFG = {
    "mode": "L3",
    "max_steps": 30,
    "protocol": {
        "participants": ["a1", "a2"],
        "assembler_id": "a1",
        "gates": {
            "P2_SPLIT": "plan",
            "P3_EXECUTE": "execution",
        },
    },
    "agents": [
        {
            "id": "a1",
            "backend": {
                "type": "fake",
                "script": [
                    # P1: send READY: to finish exploration
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "READY:",
                            "mentions": [],
                        }},
                    ]},
                    # P2: propose on plan thread
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "PROPOSE: a1→검색, a2→정리",
                            "mentions": [],
                        }},
                    ]},
                    # P2: approve → P2 gate opens → P3
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "APPROVE: ok",
                            "mentions": [],
                        }},
                    ]},
                    # P3: post work log on execution thread
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "(FYI) a1 작업 완료",
                            "mentions": [],
                        }},
                    ]},
                    # P3: approve → P3 gate opens → P4
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "APPROVE: ok",
                            "mentions": [],
                        }},
                    ]},
                    {"text": "done"},
                ],
            },
        },
        {
            "id": "a2",
            "backend": {
                "type": "fake",
                "script": [
                    # P1: send READY: to finish exploration
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "READY:",
                            "mentions": [],
                        }},
                    ]},
                    # P2: approve split (after a1's PROPOSE)
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:plan",
                            "content": "APPROVE: ok",
                            "mentions": [],
                        }},
                    ]},
                    # P3: post work log
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "(FYI) a2 작업 완료",
                            "mentions": [],
                        }},
                    ]},
                    # P3: approve
                    {"tool_calls": [
                        {"name": "send_message", "arguments": {
                            "thread": "$thread_by_name:execution",
                            "content": "APPROVE: ok",
                            "mentions": [],
                        }},
                    ]},
                    {"text": "done"},
                ],
            },
        },
    ],
}


async def test_protocol_gate_state_injected_per_phase(tmp_path):
    """Gate state from the current phase is injected into agents."""
    session = Session.from_config(load_config(write_cfg(tmp_path, PROTOCOL_PHASE_GATE_CFG)))
    protocol = session.protocol
    assert protocol is not None

    await session.run()

    # Protocol should have advanced through P2 and P3 gates
    assert protocol.gate_for("P2_SPLIT").is_open
    assert protocol.gate_for("P3_EXECUTE").is_open


async def test_protocol_phase_advances_with_gates(tmp_path):
    """Full protocol run: P1 → P2 (gate) → P3 (gate) → P4."""
    session = Session.from_config(load_config(write_cfg(tmp_path, PROTOCOL_PHASE_GATE_CFG)))
    protocol = session.protocol
    assert protocol is not None

    await session.run()

    # Should have reached at least P4 (P3 gate opens → advance to P4)
    assert protocol.phase in ("P4_REVIEW", "P5_SUBMIT", "COMPLETED")


# ---------------------------------------------------------------------------
# D8: load_config backend key validation
# ---------------------------------------------------------------------------


def test_load_config_openai_backend_missing_base_url_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "api_key_env": "X"}}],
    })
    with pytest.raises(ConfigError, match="openai backend requires 'base_url'"):
        load_config(p)


def test_load_config_openai_backend_missing_api_key_env_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "base_url": "http://x"}}],
    })
    with pytest.raises(ConfigError, match="openai backend requires 'api_key_env'"):
        load_config(p)


def test_load_config_nous_backend_missing_base_url_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "nous", "api_key_env": "X"}}],
    })
    with pytest.raises(ConfigError, match="nous backend requires 'base_url'"):
        load_config(p)


def test_load_config_nous_backend_missing_api_key_env_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "nous", "base_url": "http://x"}}],
    })
    with pytest.raises(ConfigError, match="nous backend requires 'api_key_env'"):
        load_config(p)


def test_load_config_nous_oauth_backend_missing_model_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "nous_oauth"}}],
    })
    with pytest.raises(ConfigError, match="nous_oauth backend requires 'model'"):
        load_config(p)


def test_load_config_mirror_missing_url_env_raises(tmp_path):
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "mirror": {"type": "discord_webhook"},
    })
    with pytest.raises(ConfigError, match="mirror requires 'url_env'"):
        load_config(p)
