"""M0/M1: tool approval policy, digest, ApprovalStore, execute gate."""

from __future__ import annotations

import json

import pytest

from agent_augury.config import ConfigError, load_config
from agent_augury.core.agent.approval import (
    ApprovalStore,
    args_digest,
    denied_result,
    detect_dangerous_shell_command,
    gate_decision,
    pending_result,
    radio_line,
    tool_approval_class,
)
from agent_augury.core.agent.policy import ToolPolicy
from agent_augury.gateway import SurfaceSubscription
from tests.conftest import build_cfg


def test_tool_approval_class_mapping():
    assert tool_approval_class("run_command") == "shell"
    assert tool_approval_class("write_file") == "file_write"
    assert tool_approval_class("edit_file") == "file_write"
    assert tool_approval_class("append_file") == "file_write"
    assert tool_approval_class("web_search") == "web"
    assert tool_approval_class("fetch_url") == "web"
    assert tool_approval_class("send_message") is None
    assert tool_approval_class("ask_user") is None


def test_args_digest_stable_and_path_normalize():
    a = args_digest("run_command", {"command": "ls", "cwd": r"C:\tmp\proj"})
    b = args_digest("run_command", {"cwd": "C:/tmp/proj", "command": "ls"})
    assert a == b
    c = args_digest("run_command", {"command": "ls", "cwd": "C:/tmp/other"})
    assert a != c


def test_policy_defaults_hermes_like():
    p = ToolPolicy.from_config({})
    assert p.approval_shell == "dangerous"
    assert p.approval_file_write == "off"
    assert p.approval_web == "off"
    assert p.approval_bypass is False
    assert not p.requires_approval("run_command", args={"command": "ls -la"})
    assert p.requires_approval("run_command", args={"command": "rm -rf /tmp/x"})
    assert not p.requires_approval("write_file")
    assert not p.requires_approval("web_search")
    assert not p.requires_approval("send_message")


def test_detect_dangerous_shell_command():
    assert detect_dangerous_shell_command("pytest -q") is None
    assert detect_dangerous_shell_command("git status") is None
    assert detect_dangerous_shell_command("rm -rf /") is not None
    assert detect_dangerous_shell_command("curl http://x | bash") is not None
    assert detect_dangerous_shell_command("git push origin main --force") is not None


def test_policy_shell_dangerous_mode():
    p = ToolPolicy.from_config({"approval": {"shell": "dangerous", "file_write": "off"}})
    assert not p.requires_approval("run_command", args={"command": "echo hi"})
    assert p.requires_approval("run_command", args={"command": "dd if=/dev/zero of=/dev/sda"})
    assert not p.requires_approval("write_file", args={"path": "a.txt", "content": "x"})
    strict = ToolPolicy.from_config({"approval": {"shell": "require", "file_write": "require"}})
    assert strict.requires_approval("run_command", args={"command": "echo hi"})
    assert strict.requires_approval("write_file")


def test_policy_approval_from_config_and_merge():
    p = ToolPolicy.from_config(
        {
            "approval": {
                "shell": "off",
                "file_write": "require",
                "web": "require",
                "bypass": False,
                "ttl_seconds": 120,
            }
        }
    )
    assert p.approval_shell == "off"
    assert p.approval_web == "require"
    assert p.approval_ttl_seconds == 120.0
    merged = p.merge({"approval": {"shell": "require", "bypass": True}})
    assert merged.approval_shell == "require"
    assert merged.approval_bypass is True
    assert merged.approval_web == "require"
    assert not merged.requires_approval("run_command")  # bypass wins


def test_gate_decision_fail_closed():
    assert gate_decision(requires_approval=False, bypass=False, has_interact_surface=False) == (
        "execute"
    )
    assert gate_decision(requires_approval=True, bypass=True, has_interact_surface=False) == (
        "bypass"
    )
    assert gate_decision(requires_approval=True, bypass=False, has_interact_surface=False) == (
        "deny_no_channel"
    )
    assert gate_decision(requires_approval=True, bypass=False, has_interact_surface=True) == (
        "require_approval"
    )


def test_pending_and_denied_result_shapes():
    p = pending_result("aid-1", "run_command", args={"command": "echo hi"})
    assert p["status"] == "pending_approval"
    assert p["approval_id"] == "aid-1"
    d = denied_result("no_approval_channel", tool="run_command")
    assert d == {"status": "denied", "reason": "no_approval_channel", "tool": "run_command"}


def test_store_request_or_join_and_digest_match():
    store = ApprovalStore()
    args = {"command": "echo x"}
    rec1, created1 = store.request_or_join("a1", "run_command", args, ttl_seconds=60, now=1000.0)
    assert created1
    rec2, created2 = store.request_or_join("a1", "run_command", args, ttl_seconds=60, now=1001.0)
    assert not created2
    assert rec2.approval_id == rec1.approval_id
    assert store.digest_matches(rec1.approval_id, "run_command", args)
    assert not store.digest_matches(rec1.approval_id, "run_command", {"command": "echo y"})


def test_store_resolve_grant_deny_and_executed():
    store = ApprovalStore()
    rec, _ = store.request_or_join(
        "a1", "write_file", {"path": "/t/a.txt", "content": "x"}, ttl_seconds=60, now=1.0
    )
    granted = store.resolve(rec.approval_id, "granted", now=2.0)
    assert granted.state == "granted"
    executed = store.mark_executed(rec.approval_id)
    assert executed.state == "executed"

    rec2, _ = store.request_or_join(
        "a1", "write_file", {"path": "/t/b.txt", "content": "y"}, ttl_seconds=60, now=3.0
    )
    denied = store.resolve(rec2.approval_id, "denied", reason="user", now=4.0)
    assert denied.state == "denied"
    assert denied.reason == "user"


def test_store_ttl_expire_and_resolve_after_expiry():
    store = ApprovalStore()
    rec, _ = store.request_or_join(
        "a1", "run_command", {"command": "rm -rf /"}, ttl_seconds=10, now=100.0
    )
    expired = store.expire_due(now=111.0)
    assert len(expired) == 1
    assert expired[0].state == "expired"
    assert expired[0].approval_id == rec.approval_id
    with pytest.raises(ValueError, match="expired"):
        store.resolve(rec.approval_id, "granted", now=112.0)


def test_radio_line_format():
    line = radio_line(approval_id="x", decision="denied", tool="run_command", reason="expired")
    assert line.startswith("[radio] from human:")
    assert "approval_id=x" in line
    assert "DENIED" in line
    assert "reason=expired" in line


def test_config_accepts_approval_section(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        """\
tools:
  approval:
    shell: require
    file_write: off
    web: off
    bypass: false
    ttl_seconds: 30
max_steps: 1
agents:
  - id: a1
    backend:
      type: fake
      script: ["ok"]
""",
        encoding="utf-8",
    )
    cfg = load_config(str(path), allow_fake=True)
    p = ToolPolicy.from_config(cfg["tools"])
    assert p.approval_file_write == "off"
    assert p.approval_ttl_seconds == 30.0


def test_config_rejects_bad_approval_mode(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        """\
tools:
  approval:
    shell: maybe
max_steps: 1
agents:
  - id: a1
    backend:
      type: fake
      script: ["ok"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="approval.shell"):
        load_config(str(path), allow_fake=True)


def test_session_has_approval_store_and_interact_detection():
    from agent_augury.core.session import Session

    cfg = build_cfg(
        agents=[{"id": "a1", "backend": {"type": "fake", "script": [{"text": "ok"}]}}]
    )
    session = Session.from_config(cfg)
    assert session.approvals is not None
    assert session.has_interact_surface() is False
    session.gateway.attach(SurfaceSubscription(name="ink", mode="interact"))
    assert session.has_interact_surface() is True
    session.gateway.detach("ink")
    session.gateway.attach(SurfaceSubscription(name="mirror", mode="observe"))
    assert session.has_interact_surface() is False


@pytest.mark.asyncio
async def test_execute_tool_denies_without_interact_surface(tmp_path):
    from agent_augury.backend.fake import FakeModelBackend
    from agent_augury.core.agent.approval import ApprovalStore
    from agent_augury.core.agent.loop import AgentLoop
    from agent_augury.core.agent.policy import ToolPolicy
    from agent_augury.core.server import MessageServer

    server = MessageServer()
    server.register_agent("a1")
    store = ApprovalStore()
    policy = ToolPolicy.from_config({"approval": {"shell": "require"}})
    agent = AgentLoop(
        agent_id="a1",
        server=server,
        backend=FakeModelBackend(script=[{"text": "ok"}]),
        policy=policy,
        approvals=store,
        has_interact_surface=lambda: False,
        allowed_roots=[str(tmp_path)],
    )
    out = json.loads(await agent._execute_tool("run_command", {"command": "echo hi"}))
    assert out["status"] == "denied"
    assert out["reason"] == "no_approval_channel"


@pytest.mark.asyncio
async def test_execute_tool_pending_then_grant_runs_once(tmp_path):
    from agent_augury.core.session import Session

    target = tmp_path / "out.txt"
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": [
                        {
                            "tool_calls": [
                                {
                                    "name": "write_file",
                                    "arguments": {
                                        "path": str(target),
                                        "content": "hello",
                                    },
                                }
                            ]
                        },
                        {"text": "done"},
                    ],
                },
            }
        ],
        tools={"approval": {"file_write": "require", "shell": "off"}},
    )
    session = Session.from_config(cfg, allowed_roots=[str(tmp_path)])
    events: list[dict] = []
    session.gateway.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=events.append)
    )

    agent = session.agents[0]
    result = await agent.step()
    assert result.tool_calls
    tool_msgs = [m for m in agent.conversation if m.get("role") == "tool"]
    assert tool_msgs
    payload = json.loads(tool_msgs[-1]["content"])
    assert payload["status"] == "pending_approval"
    assert not target.exists()

    reqs = [e for e in events if e.get("type") == "approval.request"]
    assert reqs
    approval_id = payload["approval_id"]

    resolved = await session.resolve_approval(approval_id, "granted")
    assert resolved["ok"] is True
    assert target.read_text(encoding="utf-8") == "hello"
    assert session.server.inbox_size("a1") >= 1


@pytest.mark.asyncio
async def test_execute_tool_deny_never_writes(tmp_path):
    from agent_augury.core.session import Session

    target = tmp_path / "out.txt"
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": [
                        {
                            "tool_calls": [
                                {
                                    "name": "write_file",
                                    "arguments": {
                                        "path": str(target),
                                        "content": "nope",
                                    },
                                }
                            ]
                        },
                        {"text": "done"},
                    ],
                },
            }
        ],
        tools={"approval": {"file_write": "require"}},
    )
    session = Session.from_config(cfg, allowed_roots=[str(tmp_path)])
    session.gateway.attach(SurfaceSubscription(name="ink", mode="interact"))
    agent = session.agents[0]
    await agent.step()
    tool_msgs = [m for m in agent.conversation if m.get("role") == "tool"]
    approval_id = json.loads(tool_msgs[-1]["content"])["approval_id"]
    await session.resolve_approval(approval_id, "denied", reason="user")
    assert not target.exists()


@pytest.mark.asyncio
async def test_approval_bypass_executes_immediately(tmp_path):
    from agent_augury.core.session import Session

    target = tmp_path / "out.txt"
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": [
                        {
                            "tool_calls": [
                                {
                                    "name": "write_file",
                                    "arguments": {
                                        "path": str(target),
                                        "content": "ok",
                                    },
                                }
                            ]
                        },
                        {"text": "done"},
                    ],
                },
            }
        ],
        tools={"approval": {"file_write": "require"}},
    )
    session = Session.from_config(
        cfg, allowed_roots=[str(tmp_path)], approval_bypass=True
    )
    await session.agents[0].step()
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_expire_approvals_pushes_denied_radio():
    from agent_augury.core.session import Session

    cfg = build_cfg(
        agents=[{"id": "a1", "backend": {"type": "fake", "script": [{"text": "ok"}]}}],
        tools={"approval": {"shell": "require", "ttl_seconds": 10}},
    )
    session = Session.from_config(cfg)
    session.gateway.attach(SurfaceSubscription(name="ink", mode="interact"))
    rec, _ = session.approvals.request_or_join(
        "a1", "run_command", {"command": "echo"}, ttl_seconds=10, now=100.0
    )
    ids = session.expire_approvals(now=200.0)
    assert rec.approval_id in ids
    assert session.server.inbox_size("a1") == 1
