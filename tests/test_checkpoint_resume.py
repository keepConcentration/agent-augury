"""Session checkpoint / resume (SESSION_RESUME_DESIGN)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_augury.config import load_config
from agent_augury.core.checkpoint import (
    CheckpointStore,
    bootstrap_session,
    config_fingerprint,
    parse_checkpoint_config,
)
from agent_augury.core.protocol.phases import P2_SPLIT, P3_EXECUTE
from agent_augury.core.session import Session


def _cfg(tmp_path: Path, *, sessions_dir: Path, with_protocol: bool = True) -> dict:
    data: dict = {
        "max_steps": 2,
        "session": {
            "checkpoint": {
                "enabled": True,
                "dir": str(sessions_dir),
                "flush_debounce_ms": 0,
                "flush_interval_s": 0,
            }
        },
        "agents": [
            {
                "id": "a1",
                "backend": {"type": "fake", "script": [{"text": "ok"}]},
            },
            {
                "id": "a2",
                "backend": {"type": "fake", "script": [{"text": "ok"}]},
            },
        ],
    }
    if with_protocol:
        data["protocol"] = {
            "gates": {
                "P2_SPLIT": "plan",
                "P3_EXECUTE": "execution",
                "P4_REVIEW": "review",
                "P5_SUBMIT": "submission",
            }
        }
    path = tmp_path / "session.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path, allow_fake=True)


def test_checkpoint_store_roundtrip(tmp_path: Path):
    store = CheckpointStore(tmp_path / "sid1", "sid1")
    conv = {
        "a1": {
            "conversation": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "xlsx task"},
                {"role": "assistant", "content": "reading file"},
            ],
            "created_threads": ["thread-1"],
            "language": "Korean",
        }
    }
    meta = store.save(
        fingerprint="abc",
        conversations=conv,
        protocol={"phase": "P3_EXECUTE", "gates": {}},
        inbox={"a1": ["msg-1"]},
        exit_reason="interrupted",
        phase="P3_EXECUTE",
    )
    assert meta["schema_version"] == 2
    m2, c2, p2, i2, approvals, corrupt = store.load()
    assert c2["a1"]["conversation"][1]["content"] == "xlsx task"
    assert p2["phase"] == "P3_EXECUTE"
    assert i2["a1"] == ["msg-1"]
    assert m2["exit_reason"] == "interrupted"
    assert approvals == []
    assert corrupt is False


def test_fingerprint_mismatch_falls_back(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions_dir=sessions)
    fp = config_fingerprint(cfg, config_path=str(tmp_path / "session.yaml"))
    store = CheckpointStore(sessions / "old", "old")
    store.save(
        fingerprint="NOT-" + fp,
        conversations={
            "a1": {"conversation": [{"role": "system", "content": "x"}], "created_threads": [], "language": ""},
            "a2": {"conversation": [{"role": "system", "content": "x"}], "created_threads": [], "language": ""},
        },
        protocol={"phase": "P1_EXPLORE"},
        inbox={},
    )
    from agent_augury.core.checkpoint import write_latest

    write_latest(sessions, "old", "NOT-" + fp)
    boot = bootstrap_session(
        cfg,
        config_path=str(tmp_path / "session.yaml"),
        cli_session_id="old",
    )
    assert boot.resumed is False
    assert boot.resume_failed
    assert "fingerprint" in (boot.resume_failed or "")


def test_parse_demo_disables_checkpoint():
    opts = parse_checkpoint_config({}, demo=True)
    assert opts.enabled is False


@pytest.mark.asyncio
async def test_session_resume_conversation_and_phase(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions_dir=sessions)
    cfg_path = str(tmp_path / "session.yaml")

    s1 = Session.open_from_config(
        cfg,
        config_path=cfg_path,
        demo=False,
        approval_bypass=True,
        allowed_roots=[str(tmp_path)],
    )
    await s1._setup()
    assert s1.protocol is not None
    s1.protocol.advance(P2_SPLIT)
    s1.protocol.advance(P3_EXECUTE)
    for agent in s1.agents:
        agent.conversation.append(
            {"role": "user", "content": "xlsx 읽어 기획 문서 작성"}
        )
        agent.conversation.append(
            {"role": "assistant", "content": "파일 경로 확인함 /data/x.xlsx"}
        )
        agent.language = "Korean"
    tid = await s1.server.create_thread("work", participants=["a1", "a2"])
    await s1.server.send_message(
        tid, author="a1", content="FYI: done reading", mentions=["a2"]
    )
    # leave undrained on a2
    assert s1.server.inbox_size("a2") >= 1
    s1.flush_checkpoint_sync(exit_reason="interrupted")
    sid = s1.session_id
    assert sid
    await s1.close()

    s2 = Session.open_from_config(
        cfg,
        config_path=cfg_path,
        demo=False,
        cli_session_id=sid,
        approval_bypass=True,
        allowed_roots=[str(tmp_path)],
    )
    assert s2._pending_bootstrap is not None
    assert s2._pending_bootstrap.resumed is True
    await s2._setup()
    assert s2._resuming is True
    assert s2.protocol is not None
    assert s2.protocol.phase == P3_EXECUTE
    texts = [m.get("content") for m in s2.agents[0].conversation]
    assert "xlsx 읽어 기획 문서 작성" in texts
    assert "파일 경로 확인함 /data/x.xlsx" in texts
    assert s2.agents[0].language == "Korean"
    # message server restored
    assert s2.server.get_thread(tid)["name"] == "work"
    await s2.close()


@pytest.mark.asyncio
async def test_new_session_skips_resume(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions_dir=sessions)
    cfg_path = str(tmp_path / "session.yaml")
    s1 = Session.open_from_config(
        cfg, config_path=cfg_path, approval_bypass=True, allowed_roots=[str(tmp_path)]
    )
    await s1._setup()
    s1.agents[0].conversation.append({"role": "user", "content": "keep me"})
    s1.flush_checkpoint_sync()
    sid = s1.session_id
    await s1.close()

    s2 = Session.open_from_config(
        cfg,
        config_path=cfg_path,
        new_session=True,
        cli_session_id=None,
        approval_bypass=True,
        allowed_roots=[str(tmp_path)],
    )
    assert s2._pending_bootstrap is not None
    assert s2._pending_bootstrap.resumed is False
    assert s2.session_id != sid
    await s2._setup()
    assert s2._resuming is False
    await s2.close()


def test_cli_rejects_new_session_with_session():
    from agent_augury.cli import main

    assert main(["--config", "x.yaml", "--new-session", "--session", "abc"]) == 1
