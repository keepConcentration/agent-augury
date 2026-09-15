"""M4: approvals persist, quarantine, sessions CLI, compact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agent_augury.core.agent.approval import ApprovalStore
from agent_augury.backend.base import Completion
from agent_augury.core.checkpoint import (
    CheckpointStore,
    bootstrap_session,
    config_fingerprint,
    list_quarantine,
    list_sessions,
    show_session,
)
from agent_augury.core.compact import (
    approx_chars,
    compact_conversation,
    compact_conversation_async,
)
from agent_augury.config import load_config
from agent_augury.core.session import Session
from agent_augury.sessions_cli import run_sessions_cli


def _cfg(tmp_path: Path, sessions_dir: Path) -> dict:
    data = {
        "max_steps": 2,
        "session": {
            "checkpoint": {
                "enabled": True,
                "dir": str(sessions_dir),
                "flush_debounce_ms": 0,
                "flush_interval_s": 0,
                "approvals_persist": True,
                "compact": {
                    "enabled": True,
                    "soft_limit_chars": 5000,
                    "keep_tail_chars": 2000,
                    "keep_tail_messages": 5,
                },
            }
        },
        "agents": [
            {
                "id": "a1",
                "backend": {"type": "fake", "script": [{"text": "ok"}]},
            }
        ],
    }
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path, allow_fake=True)


def test_approval_store_export_import():
    store = ApprovalStore()
    rec, created = store.request_or_join(
        "a1", "run_command", {"command": "echo hi"}, ttl_seconds=600, now=1000.0
    )
    assert created
    raw = store.export_pending(now=1000.0)
    assert len(raw) == 1
    assert raw[0]["approval_id"] == rec.approval_id

    store2 = ApprovalStore()
    alive, expired = store2.import_pending(raw, now=1000.0)
    assert len(alive) == 1
    assert len(expired) == 0
    assert store2.get(rec.approval_id) is not None

    alive2, expired2 = ApprovalStore().import_pending(raw, now=99999.0)
    assert len(alive2) == 0
    assert len(expired2) == 1


@pytest.mark.asyncio
async def test_pending_approval_roundtrip(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions)
    cfg_path = str(tmp_path / "s.yaml")
    s1 = Session.open_from_config(
        cfg, config_path=cfg_path, approval_bypass=True, allowed_roots=[str(tmp_path)]
    )
    await s1._setup()
    rec, _ = s1.approvals.request_or_join(
        "a1", "run_command", {"command": "ls"}, ttl_seconds=600
    )
    s1.flush_checkpoint_sync(exit_reason="interrupted")
    sid = s1.session_id
    await s1.close()

    s2 = Session.open_from_config(
        cfg,
        config_path=cfg_path,
        cli_session_id=sid,
        approval_bypass=True,
        allowed_roots=[str(tmp_path)],
    )
    assert s2._pending_bootstrap and s2._pending_bootstrap.resumed
    await s2._setup()
    got = s2.approvals.get(rec.approval_id)
    assert got is not None
    assert got.state == "pending"
    await s2.close()


def test_quarantine_on_corrupt_json(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sid = "bad-sess"
    d = sessions / sid
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{not json", encoding="utf-8")
    (d / "conversations.json").write_text("{}", encoding="utf-8")

    cfg = {
        "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["ok"]}}],
        "session": {"checkpoint": {"dir": str(sessions), "enabled": True}},
    }
    # load_config not needed; bootstrap takes raw-like dict with agents
    boot = bootstrap_session(cfg, cli_session_id=sid)
    assert boot.resumed is False
    assert boot.resume_failed and "quarantined" in boot.resume_failed
    assert not d.exists()
    assert list_quarantine(sessions)


def test_fingerprint_mismatch_not_quarantined(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions)
    fp = config_fingerprint(cfg, config_path=str(tmp_path / "s.yaml"))
    store = CheckpointStore(sessions / "old", "old")
    store.save(
        fingerprint="NOT-" + fp,
        conversations={
            "a1": {
                "conversation": [{"role": "system", "content": "x"}],
                "created_threads": [],
                "language": "",
            }
        },
        protocol={},
        inbox={},
    )
    boot = bootstrap_session(
        cfg, config_path=str(tmp_path / "s.yaml"), cli_session_id="old"
    )
    assert boot.resumed is False
    assert (sessions / "old").exists()  # left in place
    assert not list_quarantine(sessions)


def test_sessions_list_show_rm(tmp_path: Path, capsys):
    sessions = tmp_path / "sessions"
    store = CheckpointStore(sessions / "abc12345-full", "abc12345-full")
    store.save(
        fingerprint="fp",
        conversations={
            "a1": {
                "conversation": [{"role": "system", "content": "s"}],
                "created_threads": [],
                "language": "",
            }
        },
        protocol={"phase": "P3_EXECUTE"},
        inbox={},
        phase="P3_EXECUTE",
    )
    rows = list_sessions(sessions)
    assert len(rows) == 1
    info = show_session("abc12345-full", base_dir=sessions)
    assert info["protocol_phase"] == "P3_EXECUTE"
    assert run_sessions_cli(["--dir", str(sessions), "list"]) == 0
    out = capsys.readouterr().out
    assert "abc12345" in out
    assert (
        run_sessions_cli(["--dir", str(sessions), "rm", "abc12345-full", "--yes"]) == 0
    )
    assert list_sessions(sessions) == []


def test_compact_reduces_size():
    conv = [{"role": "system", "content": "sys"}]
    for i in range(80):
        conv.append({"role": "user", "content": f"msg {i} " + ("x" * 200)})
        conv.append(
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": f"t{i}",
                "content": "y" * 500,
            }
        )
    before = approx_chars(conv)
    new_conv, meta = compact_conversation(
        conv,
        soft_limit_chars=10_000,
        keep_tail_chars=3_000,
        keep_tail_messages=6,
        agent_id="a1",
        phase="P3_EXECUTE",
    )
    assert meta is not None
    assert approx_chars(new_conv) < before
    assert new_conv[0]["role"] == "system"
    assert any(
        isinstance(m.get("content"), str)
        and m["content"].startswith("[checkpoint compact]")
        for m in new_conv
    )


@pytest.mark.asyncio
async def test_flush_compacts_large_conversation(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions)
    cfg_path = str(tmp_path / "s.yaml")
    s1 = Session.open_from_config(
        cfg, config_path=cfg_path, approval_bypass=True, allowed_roots=[str(tmp_path)]
    )
    await s1._setup()
    for i in range(40):
        s1.agents[0].conversation.append(
            {"role": "user", "content": f"blob {i} " + ("z" * 300)}
        )
    before = approx_chars(s1.agents[0].conversation)
    s1.flush_checkpoint_sync()
    after = approx_chars(s1.agents[0].conversation)
    assert after < before
    meta = json.loads(
        (sessions / s1.session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta.get("compactions")
    await s1.close()


class _OkBackend:
    async def complete(self, messages, tools):
        assert tools == []
        return Completion(text="LLM summary: touched /tmp/x and finished P3.")


class _FailBackend:
    async def complete(self, messages, tools):
        raise RuntimeError("boom")


def _big_conv() -> list[dict]:
    conv = [{"role": "system", "content": "sys"}]
    for i in range(80):
        conv.append({"role": "user", "content": f"msg {i} " + ("x" * 200)})
        conv.append(
            {
                "role": "tool",
                "name": "run_command",
                "tool_call_id": f"t{i}",
                "content": "y" * 500,
            }
        )
    return conv


@pytest.mark.asyncio
async def test_compact_llm_summary_success():
    new_conv, meta = await compact_conversation_async(
        _big_conv(),
        soft_limit_chars=10_000,
        keep_tail_chars=3_000,
        keep_tail_messages=6,
        agent_id="a1",
        phase="P3_EXECUTE",
        llm_summary=True,
        backend=_OkBackend(),
    )
    assert meta is not None
    assert meta["llm_summary"] is True
    compact_msgs = [
        m
        for m in new_conv
        if isinstance(m.get("content"), str)
        and m["content"].startswith("[checkpoint compact]")
    ]
    assert compact_msgs
    assert "LLM summary" in compact_msgs[0]["content"]


@pytest.mark.asyncio
async def test_compact_llm_summary_falls_back_on_failure():
    new_conv, meta = await compact_conversation_async(
        _big_conv(),
        soft_limit_chars=10_000,
        keep_tail_chars=3_000,
        keep_tail_messages=6,
        agent_id="a1",
        phase="P3_EXECUTE",
        llm_summary=True,
        backend=_FailBackend(),
    )
    assert meta is not None
    assert meta["llm_summary"] is False
    assert any(
        isinstance(m.get("content"), str)
        and "agent=a1" in m["content"]
        and m["content"].startswith("[checkpoint compact]")
        for m in new_conv
    )


@pytest.mark.asyncio
async def test_async_flush_records_compactions_meta(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions)
    cfg["session"]["checkpoint"]["compact"]["llm_summary"] = True
    cfg_path = str(tmp_path / "s.yaml")
    s1 = Session.open_from_config(
        cfg, config_path=cfg_path, approval_bypass=True, allowed_roots=[str(tmp_path)]
    )
    await s1._setup()

    class _SumBackend:
        async def complete(self, messages, tools):
            return Completion(text="async flush llm summary")

    s1.agents[0].backend = _SumBackend()
    for i in range(40):
        s1.agents[0].conversation.append(
            {"role": "user", "content": f"blob {i} " + ("z" * 300)}
        )
    await s1.flush_checkpoint()
    meta = json.loads(
        (sessions / s1.session_id / "meta.json").read_text(encoding="utf-8")
    )
    assert meta.get("compactions")
    assert meta["compactions"][0].get("llm_summary") is True
    assert any(
        isinstance(m.get("content"), str) and "async flush llm summary" in m["content"]
        for m in s1.agents[0].conversation
    )
    await s1.close()
