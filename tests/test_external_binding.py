"""A5 external_binding — platform thread map + pending question persist."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_augury.channels.discord.inbound import dispatch_discord_inbound
from agent_augury.config import load_config
from agent_augury.core.external_binding import (
    BINDINGS_FILENAME,
    BindingsSnapshot,
    apply_to_bridge,
    capture_from_bridge,
    load_bindings,
    platform_ref_key,
    record_platform_thread,
    save_bindings,
)
from agent_augury.gateway import SessionBridge, SessionGateway
from agent_augury.gateway.bridge import PendingQuestion
from agent_augury.core.session import Session


def test_platform_ref_key_discord_channel():
    source = {"surface": "discord", "channel": "999", "mode": "interact", "user": "1"}
    assert platform_ref_key(source) == "discord:ch:999:th:"


def test_bindings_file_roundtrip(tmp_path: Path):
    path = tmp_path / BINDINGS_FILENAME
    snap = BindingsSnapshot(
        recent_thread="thr-1",
        platform_threads={"discord:ch:1:th:": "thr-1"},
        pending_questions=[
            {
                "question_id": "q1",
                "thread_id": "thr-1",
                "agent_id": "a1",
                "question": "ok?",
                "options": [],
            }
        ],
    )
    save_bindings(path, snap)
    loaded = load_bindings(path)
    assert loaded is not None
    assert loaded.recent_thread == "thr-1"
    assert loaded.platform_threads["discord:ch:1:th:"] == "thr-1"
    assert loaded.pending_questions[0]["question_id"] == "q1"


def test_apply_to_bridge_restores_pending():
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw)
    snap = BindingsSnapshot(
        recent_thread="thr-x",
        pending_questions=[
            {
                "question_id": "q9",
                "thread_id": "thr-x",
                "agent_id": "a1",
                "question": "pick",
                "options": ["a", "b"],
            }
        ],
    )
    apply_to_bridge(bridge, snap)
    assert bridge.recent_thread == "thr-x"
    assert bridge.pending is not None
    assert bridge.pending.question_id == "q9"


def test_capture_from_bridge():
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw)
    bridge._recent_thread = "thr-2"
    bridge._pending.append(
        PendingQuestion(
            question_id="q2",
            thread_id="thr-2",
            agent_id="a1",
            question="?",
        )
    )
    cap = capture_from_bridge(bridge)
    assert cap.recent_thread == "thr-2"
    assert len(cap.pending_questions) == 1


def _cfg(tmp_path: Path, sessions_dir: Path) -> dict:
    data = {
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
            {"id": "a1", "backend": {"type": "fake", "script": [{"text": "ok"}]}},
        ],
    }
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path, allow_fake=True)


@pytest.mark.asyncio
async def test_bindings_persist_on_flush_and_restore(tmp_path: Path):
    sessions = tmp_path / "sessions"
    cfg = _cfg(tmp_path, sessions)
    cfg_path = str(tmp_path / "s.yaml")
    s1 = Session.open_from_config(
        cfg, config_path=cfg_path, approval_bypass=True, allowed_roots=[str(tmp_path)]
    )
    await s1._setup()
    assert s1._bindings is not None
    record_platform_thread(
        s1._bindings,
        {"surface": "discord", "channel": "42"},
        "thr-human",
    )
    s1.bridge._pending.append(
        PendingQuestion(
            question_id="q-flush",
            thread_id="thr-human",
            agent_id="a1",
            question="wait",
        )
    )
    s1.flush_checkpoint_sync(exit_reason="test")
    sid = s1.session_id
    await s1.close()

    path = sessions / sid / BINDINGS_FILENAME
    assert path.is_file()
    on_disk = load_bindings(path)
    assert on_disk is not None
    assert on_disk.platform_threads.get("discord:ch:42:th:") == "thr-human"
    assert on_disk.pending_questions[0]["question_id"] == "q-flush"

    s2 = Session.open_from_config(
        cfg,
        config_path=cfg_path,
        approval_bypass=True,
        allowed_roots=[str(tmp_path)],
        cli_session_id=sid,
    )
    await s2._setup()
    assert s2.bridge.pending is not None
    assert s2.bridge.pending.question_id == "q-flush"
    await s2.close()


def test_session_lookup_external_thread():
    snap = BindingsSnapshot(
        platform_threads={"discord:ch:100:th:": "thr-bound"},
    )

    class _S:
        _bindings = snap

        def lookup_external_thread(self, source: dict) -> str | None:
            from agent_augury.core.external_binding import lookup_platform_thread

            return lookup_platform_thread(self._bindings, source)

    s = _S()
    source = {"surface": "discord", "channel": "100", "user": "u1"}
    assert s.lookup_external_thread(source) == "thr-bound"


def test_discord_inbound_prefers_binding_over_empty_recent():
    """Inbound path reads session.lookup_external_thread when building human.send."""
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw)

    class _S:
        def lookup_external_thread(self, source: dict) -> str | None:
            return "thr-bound"

    bridge.session = _S()
    bridge._recent_thread = None
    bridge.send_fn = lambda *_a, **_k: "mid"
    bridge.install()
    from agent_augury.channels.discord.inbound import INBOUND_SURFACE
    from agent_augury.gateway.bus import SurfaceSubscription

    gw.attach(
        SurfaceSubscription(name=INBOUND_SURFACE, mode="interact", family="chat")
    )

    out = dispatch_discord_inbound(
        gw,
        bridge,
        "hello",
        agent_id="a1",
        user_id="u1",
        channel_id=100,
    )
    assert out.get("queued") is True
    assert out.get("thread_id") == "thr-bound"
