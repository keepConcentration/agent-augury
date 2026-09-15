"""D7 ask_user dual-path filter + B1/B2/B3 + D1 secrets scrubbing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_augury.channels.discord.mirror import DiscordWebhookMirror
from agent_augury.channels.discord.observe import (
    attach_discord_mirror,
    format_wire_for_bot,
)
from agent_augury.channels.slack.mirror import SlackWebhookMirror
from agent_augury.channels.slack.observe import attach_slack_mirror
from agent_augury.core.server import MessageServer
from agent_augury.core.session import Session, _publish_session_error
from agent_augury.gateway import (
    SessionBridge,
    SessionGateway,
    SurfaceSubscription,
    make_command,
    make_event,
)
from agent_augury.gateway.secrets import (
    SECRETS_FILE_ENV,
    is_secret_env_name,
    load_gateway_secrets,
    scrub_env_for_ink,
)
from agent_augury.gateway.translate import translate_core_event
from tests.conftest import build_cfg


def test_d7_mirror_skips_ask_user_message_prefix():
    gw = SessionGateway()
    mirror = DiscordWebhookMirror(webhook_url="https://example.test/hook")
    attach_discord_mirror(gw, mirror)

    gw.publish(
        make_event(
            "message",
            thread_id="t1",
            author="agent-1",
            content="[ask-user] Should we continue?",
        )
    )
    assert mirror.outbox == []

    gw.publish(
        make_event(
            "message",
            thread_id="t1",
            author="agent-1",
            content="normal radio",
        )
    )
    assert len(mirror.outbox) == 1


def test_d7_format_wire_skips_ask_user_message():
    assert (
        format_wire_for_bot(
            make_event(
                "message",
                author="a1",
                content="[ask-user] pick one",
            )
        )
        is None
    )
    text = format_wire_for_bot(
        make_event("human.question", agent_id="a1", question="pick one")
    )
    assert text is not None
    assert "pick one" in text
    assert "[ask-user]" not in text


def test_d7_slack_skips_ask_user_message():
    gw = SessionGateway()
    mirror = SlackWebhookMirror(webhook_url="https://example.test/slack")
    attach_slack_mirror(gw, mirror)
    gw.publish(
        make_event(
            "message",
            thread_id="t",
            author="a",
            content="[ask-user] q?",
        )
    )
    assert mirror.outbox == []


@pytest.mark.asyncio
async def test_b3_human_send_records_source():
    server = MessageServer()
    server.register_agent("a1")
    server.register_human("human")
    tid = await server.create_thread("t", participants=["a1"])
    mid = await server.human_send(
        tid,
        author="human",
        content="from discord",
        source={"surface": "discord", "user": "u1"},
    )
    msg = server._message_index[mid]
    assert msg["source"]["surface"] == "discord"

    events: list[dict] = []
    server.subscribe_events(events.append)
    await server.human_send(
        tid,
        author="human",
        content="again",
        source={"surface": "ink"},
    )
    assert events[-1]["source"]["surface"] == "ink"

    wire = translate_core_event(events[-1])
    assert wire is not None
    assert wire["type"] == "message"
    assert wire["source"]["surface"] == "ink"


def test_b3_bridge_forwards_source_to_send_fn():
    gw = SessionGateway()
    sent: list[dict | None] = []

    def send_fn(thread_id, content, *, mentions=None, source=None):
        sent.append(source)
        return "ok"

    bridge = SessionBridge(gateway=gw, send_fn=send_fn)
    bridge.install()
    bridge._recent_thread = "thr"
    gw.attach(SurfaceSubscription(name="ink", mode="interact"))
    gw.dispatch(
        make_command(
            "human.send",
            id="1",
            content="hi",
            source={"surface": "ink", "mode": "interact"},
        ),
        surface="ink",
    )
    assert sent == [{"surface": "ink", "mode": "interact"}]


def test_b1_session_phase_publishes_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": [{"text": "done"}],
                },
            }
        ],
        protocol={
            "participants": ["a1"],
            "gates": {},
        },
    )
    session = Session.from_config(cfg)
    inbox: list[dict] = []
    session.gateway.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=inbox.append)
    )
    assert session.protocol is not None
    # PhaseManager already starts at P1; advance to P2 to fire on_phase_change.
    session.protocol.advance("P2_SPLIT")
    phases = [e for e in inbox if e.get("type") == "session.phase"]
    assert phases
    assert phases[-1]["phase"] == "P2_SPLIT"


def test_b2_publish_session_error_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": [{"text": "ok"}],
                },
            }
        ],
    )
    session = Session.from_config(cfg)
    inbox: list[dict] = []
    session.gateway.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=inbox.append)
    )
    _publish_session_error(session, "boom", agent_id="a1")
    errs = [e for e in inbox if e.get("type") == "error"]
    assert len(errs) == 1
    assert errs[0]["message"] == "boom"
    assert errs[0]["agent_id"] == "a1"


def test_d1_secret_name_heuristics():
    assert is_secret_env_name("OPENROUTER_API_KEY")
    assert is_secret_env_name("DISCORD_BOT_TOKEN")
    assert is_secret_env_name("AUGURY_MIRROR_WEBHOOK_URL")
    assert not is_secret_env_name("AUGURY_CONFIG")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name(SECRETS_FILE_ENV)


def test_d1_scrub_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live")
    monkeypatch.setenv("AUGURY_CONFIG", "/tmp/cfg.yaml")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrub_env_for_ink()
    assert "OPENROUTER_API_KEY" not in env
    assert env["AUGURY_CONFIG"] == "/tmp/cfg.yaml"
    assert SECRETS_FILE_ENV in env
    secrets_path = Path(env[SECRETS_FILE_ENV])
    assert secrets_path.is_file()
    data = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert data["OPENROUTER_API_KEY"] == "sk-live"

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(SECRETS_FILE_ENV, str(secrets_path))
    loaded = load_gateway_secrets()
    assert loaded["OPENROUTER_API_KEY"] == "sk-live"
    assert __import__("os").environ["OPENROUTER_API_KEY"] == "sk-live"
    assert not secrets_path.exists()
    assert SECRETS_FILE_ENV not in __import__("os").environ
