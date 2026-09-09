"""Wiring — YAML config loading, session assembly, run loop, CLI entry."""


import pytest
import yaml

from agent_augury.backend.fake import FakeModelBackend
from agent_augury.config import ConfigError, load_config
from agent_augury.session import Session
from tests.conftest import build_cfg


def write_cfg(tmp_path, body: dict):
    p = tmp_path / "session.yaml"
    # human 섹션이 없으면 기본값 추가 (v1.0부터 human은 필수)
    if "human" not in body and "mode" in body:
        body["human"] = {"id": "human"}
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


FAKE_CFG = build_cfg(
    max_steps=12,
    task="find the answer",
    agents=[
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
)


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------


def test_load_config_parses_agents_task(tmp_path):
    cfg = load_config(write_cfg(tmp_path, {
        "max_steps": 12,
        "task": "find the answer",
        "agents": [
            {"id": "agent-1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}},
            {"id": "agent-2", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}},
        ],
    }))
    assert [a["id"] for a in cfg["agents"]] == ["agent-1", "agent-2"]
    assert cfg["task"] == "find the answer"
    assert cfg["max_steps"] == 12


def test_load_config_rejects_missing_agents(tmp_path):
    p = write_cfg(tmp_path, {"mode": "L3"})
    with pytest.raises(ConfigError):
        load_config(p)


def test_load_config_ignores_mode_key(tmp_path):
    """mode 키는 무시됨 (v1.0부터 코드에 내장, 항상 L3)."""
    cfg = load_config(write_cfg(tmp_path, {
        "mode": "L9",
        "agents": [
            {"id": "agent-1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}},
        ],
    }))
    # mode 키는 무시되므로 정상 로드됨
    assert "mode" not in cfg


# ---------------------------------------------------------------------------
# session assembly & run
# ---------------------------------------------------------------------------


async def test_session_builds_shared_server_and_agents(tmp_path):
    session = Session.from_config(FAKE_CFG)
    assert [a.agent_id for a in session.agents] == ["agent-1", "agent-2"]
    # all agents share ONE server instance (SSOT)
    assert len({id(a.server) for a in session.agents}) == 1


async def test_run_parallel_and_task_injected_to_first_agent(tmp_path):
    session = Session.from_config(FAKE_CFG)
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
    session = Session.from_config(FAKE_CFG)
    session.on_step = lambda aid, result: events.append((aid, result.drained_count))
    await session.run()
    assert len(events) >= 4
    assert {aid for aid, _ in events} == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# $thread refs resolve against tool results
# ---------------------------------------------------------------------------


async def test_thread_reference_placeholder_resolves(tmp_path):
    session = Session.from_config(FAKE_CFG)
    await session.run()
    snap = session.server.snapshot()
    msgs = [m for m in snap["messages"] if m["author"] == "agent-2" and m["thread_id"].startswith("thread-")]
    # send_message executed with the real resolved thread id
    assert any(m["content"] == "(FYI) working" for m in msgs)


# ---------------------------------------------------------------------------
# v0.1b: gate & mirror wiring
# ---------------------------------------------------------------------------


GATE_CFG = build_cfg(
    max_steps=24,
    gate={"thread_name": "plan"},
    agents=[
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
)


async def test_session_wires_consensus_gate_and_opens_it(tmp_path):
    session = Session.from_config(GATE_CFG)
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
    session = Session.from_config(FAKE_CFG)
    assert session.gate is None


async def test_session_mirror_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AUGURY_MIRROR_URL", raising=False)
    cfg = dict(GATE_CFG, mirror={"type": "discord_webhook", "url_env": "AUGURY_MIRROR_URL"})
    session = Session.from_config(cfg)
    assert session.mirror is None  # observation silently off — core unaffected


def test_cli_accepts_gate_config(tmp_path, capsys, monkeypatch):
    from unittest.mock import patch

    from agent_augury.cli import main

    # Mock load_config to return a config with fake backends
    # (fake is no longer valid in production configs after removal from _VALID_BACKEND_TYPES)
    with patch("agent_augury.cli.load_config") as mock_load:
        mock_load.return_value = GATE_CFG
        # Mock the backend to avoid real API calls
        # Mock the TUI adapter to avoid TTY issues
        with patch("agent_augury.backends_factory.build_backend") as mock_build, \
             patch("agent_augury.cli._make_tui_adapter") as mock_tui:
            from agent_augury.backend.fake import FakeModelBackend
            mock_build.return_value = FakeModelBackend(script=["hello"])
            mock_tui.return_value = None  # TUI adapter is mocked
            rc = main(["--config", str(write_cfg(tmp_path, {}))])
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


GATE_BLOCK_CFG = build_cfg(
    max_steps=24,
    gate={"thread_name": "plan"},
    agents=[
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
)


async def test_gate_blocks_work_share_until_open(tmp_path):
    """Gate CLOSED → work-share on non-gate thread is blocked with error."""
    session = Session.from_config(GATE_BLOCK_CFG)
    await session.run()

    snap = session.server.snapshot()
    # gate must have opened
    assert session.gate.is_open
    # work-share on hunt thread must appear only AFTER gate opened
    work_msgs = [m for m in snap["messages"] if "(FYI)" in m["content"]]
    assert len(work_msgs) == 1, f"expected exactly 1 work-share after gate open, got {len(work_msgs)}"
    assert work_msgs[0]["content"] == "(FYI) 몫 재시도 완료"
    assert work_msgs[0]["seq"] > session.gate.opened_at_seq


def test_cli_runs_fake_session_and_prints_log(tmp_path, capsys, monkeypatch):
    from unittest.mock import patch

    from agent_augury.cli import main

    # Mock load_config to return a config with fake backends
    with patch("agent_augury.cli.load_config") as mock_load:
        mock_load.return_value = FAKE_CFG
        # Mock the backend to avoid real API calls
        # Mock the TUI adapter to avoid TTY issues
        with patch("agent_augury.backends_factory.build_backend") as mock_build, \
             patch("agent_augury.cli._make_tui_adapter") as mock_tui:
            from agent_augury.backend.fake import FakeModelBackend
            mock_build.return_value = FakeModelBackend(script=["hello"])
            mock_tui.return_value = None  # TUI adapter is mocked
            rc = main(["--config", str(write_cfg(tmp_path, {}))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "💭 agent-1:" in out and "💭 agent-2:" in out
    assert "steps=" in out


# ---------------------------------------------------------------------------
# v0.2: gate-aware per-phase blocking
# ---------------------------------------------------------------------------


PROTOCOL_PHASE_GATE_CFG = build_cfg(
    max_steps=30,
    protocol={
        "participants": ["a1", "a2"],
        "assembler_id": "a1",
        "gates": {
            "P2_SPLIT": "plan",
            "P3_EXECUTE": "execution",
        },
    },
    agents=[
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
)


async def test_protocol_gate_state_injected_per_phase(tmp_path):
    """Gate state from the current phase is injected into agents."""
    session = Session.from_config(PROTOCOL_PHASE_GATE_CFG)
    protocol = session.protocol
    assert protocol is not None

    await session.run()

    # Protocol should have advanced through P2 and P3 gates
    assert protocol.gate_for("P2_SPLIT").is_open
    assert protocol.gate_for("P3_EXECUTE").is_open


async def test_protocol_phase_advances_with_gates(tmp_path):
    """Full protocol run: P1 → P2 (gate) → P3 (gate) → P4."""
    session = Session.from_config(PROTOCOL_PHASE_GATE_CFG)
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


def test_load_config_rejects_fake_backend_type(tmp_path):
    """fake backend type must be rejected after removal from _VALID_BACKEND_TYPES."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
    })
    with pytest.raises(ConfigError, match="backend.type"):
        load_config(p)


def test_load_config_allow_fake_true_permits_fake_backend(tmp_path):
    """allow_fake=True permits type: fake backends (offline demo/benchmark)."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
    })
    cfg = load_config(p, allow_fake=True)
    assert cfg["agents"][0]["backend"]["type"] == "fake"


def test_load_config_allow_fake_default_rejects_fake(tmp_path):
    """allow_fake defaults to False → type: fake still rejected."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
    })
    with pytest.raises(ConfigError):
        load_config(p)


# ---------------------------------------------------------------------------
# roles / role / role_custom
# ---------------------------------------------------------------------------


def test_load_config_roles_section_valid(tmp_path):
    """roles 섹션이 정상적으로 파싱됨."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "roles": {
            "orchestrator": {
                "description": "작업을 분할·조율한다",
                "prompt": "너는 오케스트레이터다.",
            },
        },
    })
    cfg = load_config(p)
    assert "roles" in cfg
    assert cfg["roles"]["orchestrator"]["prompt"] == "너는 오케스트레이터다."


def test_load_config_role_preset_reference(tmp_path):
    """role 키가 roles 프리셋을 참조하면 정상 처리."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "role": "orchestrator", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "roles": {"orchestrator": {"prompt": "너는 오케스트레이터다."}},
    })
    cfg = load_config(p)
    assert cfg["agents"][0]["role"] == "orchestrator"


def test_load_config_role_not_in_roles_raises(tmp_path):
    """role 값이 roles 섹션에 없으면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "role": "not_exist", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "roles": {"orchestrator": {"prompt": "..."}},
    })
    with pytest.raises(ConfigError, match="not defined in 'roles'"):
        load_config(p)


def test_load_config_role_without_roles_section_raises(tmp_path):
    """roles 섹션 없이 role 키를 쓰면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "role": "orchestrator", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
    })
    with pytest.raises(ConfigError, match="not defined in 'roles'"):
        load_config(p)


def test_load_config_role_and_role_custom_both_raises(tmp_path):
    """role과 role_custom을 동시에 지정하면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{
            "id": "a1",
            "role": "orchestrator",
            "role_custom": "너는 QA다.",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"},
        }],
        "roles": {"orchestrator": {"prompt": "..."}},
    })
    with pytest.raises(ConfigError, match="cannot specify both"):
        load_config(p)


def test_load_config_role_custom_valid(tmp_path):
    """role_custom은 roles 섹션 없이도 사용 가능."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{
            "id": "a1",
            "role_custom": "너는 QA 리뷰어다.",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"},
        }],
    })
    cfg = load_config(p)
    assert cfg["agents"][0]["role_custom"] == "너는 QA 리뷰어다."


def test_load_config_role_custom_empty_raises(tmp_path):
    """role_custom이 빈 문자열이면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{
            "id": "a1",
            "role_custom": "   ",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"},
        }],
    })
    with pytest.raises(ConfigError, match="non-empty string"):
        load_config(p)


def test_load_config_roles_not_mapping_raises(tmp_path):
    """roles가 맵이 아니면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "roles": ["orchestrator"],
    })
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(p)


def test_load_config_roles_unknown_key_raises(tmp_path):
    """roles 항목에 알 수 없는 키가 있으면 ConfigError."""
    p = write_cfg(tmp_path, {
        "mode": "L3",
        "agents": [{"id": "a1", "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X", "model": "m"}}],
        "roles": {"orchestrator": {"unknown_key": "value"}},
    })
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(p)


# ---------------------------------------------------------------------------
# Session role integration
# ---------------------------------------------------------------------------


def test_session_role_preset_injected_into_system_prompt(tmp_path):
    """role 프리셋을 지정한 agent의 system prompt에 역할이 주입됨."""
    import os

    from agent_augury.session import Session

    p = write_cfg(tmp_path, {
        "mode": "L3",
        "max_steps": 1,
        "task": "test",
        "agents": [{
            "id": "a1",
            "role": "orchestrator",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X_KEY", "model": "m"},
        }],
        "roles": {"orchestrator": {"prompt": "너는 오케스트레이터다."}},
    })
    cfg = load_config(p)
    os.environ["X_KEY"] = "sk-test"
    try:
        session = Session.from_config(cfg)
        # system prompt에 role이 주입되었는지 확인
        sys_msg = session.agents[0].conversation[0]
        assert sys_msg["role"] == "system"
        assert "Your role:" in sys_msg["content"]
        assert "너는 오케스트레이터다." in sys_msg["content"]
    finally:
        del os.environ["X_KEY"]


def test_session_role_custom_injected_into_system_prompt(tmp_path):
    """role_custom을 지정한 agent의 system prompt에 커스텀 역할이 주입됨."""
    import os

    from agent_augury.session import Session

    p = write_cfg(tmp_path, {
        "mode": "L3",
        "max_steps": 1,
        "task": "test",
        "agents": [{
            "id": "a1",
            "role_custom": "너는 QA 리뷰어다.",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X_KEY", "model": "m"},
        }],
    })
    cfg = load_config(p)
    os.environ["X_KEY"] = "sk-test"
    try:
        session = Session.from_config(cfg)
        sys_msg = session.agents[0].conversation[0]
        assert "Your role:" in sys_msg["content"]
        assert "너는 QA 리뷰어다." in sys_msg["content"]
    finally:
        del os.environ["X_KEY"]


def test_session_no_role_backward_compatible(tmp_path):
    """role이 없는 agent는 기존과 동일하게 동작 (하위호환)."""
    import os

    from agent_augury.session import Session

    p = write_cfg(tmp_path, {
        "mode": "L3",
        "max_steps": 1,
        "task": "test",
        "agents": [{
            "id": "a1",
            "backend": {"type": "openai", "base_url": "http://x", "api_key_env": "X_KEY", "model": "m"},
        }],
    })
    cfg = load_config(p)
    os.environ["X_KEY"] = "sk-test"
    try:
        session = Session.from_config(cfg)
        sys_msg = session.agents[0].conversation[0]
        assert sys_msg["role"] == "system"
        assert "Your role:" not in sys_msg["content"]
    finally:
        del os.environ["X_KEY"]
