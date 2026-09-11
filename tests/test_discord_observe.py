"""M4: Discord observe surfaces attached to SessionGateway."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_augury.channel.discord_bot import BotManager, DiscordBotAdapter
from agent_augury.channel.discord_mirror import DiscordWebhookMirror
from agent_augury.channel.discord_observe import (
    attach_discord_bots,
    attach_discord_mirror,
    format_wire_for_bot,
)
from agent_augury.gateway import SessionGateway, make_event
from agent_augury.gateway.translate import translate_core_event


def test_mirror_receives_message_via_gateway():
    gw = SessionGateway()
    mirror = DiscordWebhookMirror(webhook_url="https://example.test/hook")
    attach_discord_mirror(gw, mirror)

    n = gw.publish(
        make_event(
            "message",
            thread_id="t1",
            author="agent-1",
            content="hello mirror",
        )
    )
    assert n == 1
    assert len(mirror.outbox) == 1
    assert "agent-1" in mirror.outbox[0]
    assert "hello mirror" in mirror.outbox[0]


def test_mirror_ignores_non_message_and_observe_rejects_human():
    gw = SessionGateway(on_command=lambda _c: {})
    mirror = DiscordWebhookMirror(webhook_url="https://example.test/hook")
    attach_discord_mirror(gw, mirror)

    assert gw.publish(make_event("log", text="nope")) == 0
    assert mirror.outbox == []

    from agent_augury.gateway import make_command

    result = gw.dispatch(
        make_command("human.send", id="1", content="x"),
        surface="discord-mirror",
    )
    assert result["ok"] is False
    assert "observe-only" in result["error"]


def test_bots_route_via_gateway():
    gw = SessionGateway()
    mgr = BotManager()
    client = MagicMock()
    client.event = lambda func: func
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=client):
        bot = DiscordBotAdapter(
            agent_id="agent-1",
            token="t",
            channel_id=1,
        )
    mgr.register(bot)
    attach_discord_bots(gw, mgr)

    gw.publish(
        make_event(
            "tool",
            agent_id="agent-1",
            tool="read_file",
            args={},
        )
    )
    item = bot._outbox.get_nowait()
    assert "agent-1" in item
    assert "read_file" in item


def test_translate_send_message_preserves_author_for_mirror():
    wire = translate_core_event(
        {
            "type": "send_message",
            "thread_id": "t",
            "author": "human",
            "content": "hi",
            "message_id": "m1",
        }
    )
    assert wire is not None
    assert wire["type"] == "message"
    assert wire["author"] == "human"
    line = DiscordWebhookMirror.format_line(
        {
            "thread_id": wire["thread_id"],
            "author": wire["author"],
            "content": wire["content"],
        }
    )
    assert "human" in line


def test_format_wire_create_thread_parity():
    text = format_wire_for_bot(
        make_event(
            "thread.created",
            name="plan",
            participants=["a", "b"],
            thread_id="t",
        )
    )
    assert text is not None
    assert "create_thread" in text
    assert "plan" in text


def test_session_attaches_mirror_to_gateway(tmp_path, monkeypatch):
    """M4: mirror is a Gateway observe surface, not server.subscribe."""
    import yaml

    from agent_augury.config import load_config
    from agent_augury.session import Session
    from tests.conftest import build_cfg

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setenv("AUGURY_MIRROR_URL", "https://example.test/hook")
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "http://x/v1",
                    "api_key_env": "TEST_API_KEY",
                    "model": "m",
                },
            }
        ],
        mirror={"type": "discord_webhook", "url_env": "AUGURY_MIRROR_URL"},
    )
    path = tmp_path / "m4.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    session = Session.from_config(load_config(str(path)))
    assert session.mirror is not None
    assert "discord-mirror" in session.gateway.surfaces()
    # Message subscribers should not include the mirror (gate may still subscribe)
    assert session.mirror.on_message not in session.server._subscribers
