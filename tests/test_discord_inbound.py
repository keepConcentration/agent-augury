"""M5: Discord inbound HITL (opt-in) via Gateway."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_augury.channel.discord_bot import (
    BotManager,
    DiscordBotAdapter,
    accept_inbound_message,
    normalize_inbound_content,
)
from agent_augury.channel.discord_inbound import (
    INBOUND_SURFACE,
    attach_discord_inbound,
    dispatch_discord_inbound,
)
from agent_augury.config import ConfigError, load_config
from agent_augury.gateway import SessionBridge, SessionGateway
from tests.conftest import build_cfg


def test_accept_inbound_filters_bots_and_wrong_channel():
    bot_msg = SimpleNamespace(
        author=SimpleNamespace(bot=True, id=1),
        channel=SimpleNamespace(id=10),
    )
    assert accept_inbound_message(bot_msg, channel_id=10, bot_user_id=99) is False

    wrong = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=2),
        channel=SimpleNamespace(id=11),
    )
    assert accept_inbound_message(wrong, channel_id=10, bot_user_id=99) is False

    ok = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=2),
        channel=SimpleNamespace(id=10),
    )
    assert accept_inbound_message(ok, channel_id=10, bot_user_id=99) is True


def test_normalize_strips_mentions():
    assert normalize_inbound_content("<@123> hello  there", bot_user_id=123) == "hello there"


def test_dispatch_human_send_when_no_pending():
    gw = SessionGateway()
    sent: list[tuple] = []

    def send_fn(thread_id: str, content: str, *, mentions=None):
        sent.append((thread_id, content, mentions))
        return "ok"

    bridge = SessionBridge(gateway=gw, send_fn=send_fn)
    bridge.install()
    bridge._recent_thread = "thr-1"
    gw.attach(
        __import__(
            "agent_augury.gateway", fromlist=["SurfaceSubscription"]
        ).SurfaceSubscription(
            name=INBOUND_SURFACE,
            mode="interact",
            family="chat",
        )
    )

    result = dispatch_discord_inbound(
        gw,
        bridge,
        "go ahead",
        agent_id="agent-1",
        user_id="u9",
        channel_id=42,
    )
    assert result["ok"] is True
    assert sent == [("thr-1", "go ahead", ["agent-1"])]


def test_dispatch_human_answer_when_pending():
    gw = SessionGateway()
    sent: list[tuple] = []
    bridge = SessionBridge(
        gateway=gw,
        send_fn=lambda tid, content, *, mentions=None: sent.append(
            (tid, content, mentions)
        )
        or "ok",
    )
    bridge.install()
    from agent_augury.gateway import SurfaceSubscription

    gw.attach(
        SurfaceSubscription(name=INBOUND_SURFACE, mode="interact", family="chat")
    )
    bridge.publish_ask_user(
        agent_id="agent-1",
        thread_id="t-ask",
        question="Pick",
        options=["A", "B"],
        question_id="q1",
    )
    result = dispatch_discord_inbound(
        gw, bridge, "2", agent_id="agent-1", user_id="u", channel_id=1
    )
    assert result["ok"] is True
    assert sent == [("t-ask", "B", ["agent-1"])]
    assert bridge.pending is None


def test_attach_inbound_sets_interact_surface():
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw, send_fn=lambda *_a, **_k: "ok")
    bridge.install()
    mgr = BotManager()
    client = MagicMock()
    client.event = lambda func: func
    client.user = MagicMock(id=999)
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=client):
        bot = DiscordBotAdapter(
            agent_id="a1",
            token="t",
            channel_id=123,
            inbound=True,
        )
    mgr.register(bot)
    assert attach_discord_inbound(gw, bridge, mgr) is True
    assert INBOUND_SURFACE in gw.surfaces()
    assert bot._inbound_handler is not None


def test_attach_inbound_noop_when_disabled():
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw, send_fn=lambda *_a, **_k: "ok")
    mgr = BotManager()
    client = MagicMock()
    client.event = lambda func: func
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=client):
        bot = DiscordBotAdapter(
            agent_id="a1", token="t", channel_id=1, inbound=False
        )
    mgr.register(bot)
    assert attach_discord_inbound(gw, bridge, mgr) is False
    assert INBOUND_SURFACE not in gw.surfaces()


def test_config_inbound_must_be_bool(tmp_path):
    cfg = build_cfg(
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "http://x/v1",
                    "api_key_env": "X",
                    "model": "m",
                },
            }
        ],
        bots=[
            {
                "agent_id": "a1",
                "token_env": "BOT_TOKEN_1",
                "channel_id": 123,
                "inbound": "yes",
            }
        ],
    )
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ConfigError, match="inbound"):
        load_config(str(path))


def test_session_wires_inbound_surface(tmp_path, monkeypatch):
    from agent_augury.session import Session

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setenv("BOT_TOKEN_1", "fake")
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
        bots=[
            {
                "agent_id": "a1",
                "token_env": "BOT_TOKEN_1",
                "channel_id": 123456789,
                "inbound": True,
            }
        ],
    )
    path = tmp_path / "in.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    client = MagicMock()
    client.event = lambda func: func
    client.user = MagicMock(id=1)
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=client):
        session = Session.from_config(load_config(str(path)))
    assert session.bot_manager is not None
    assert INBOUND_SURFACE in session.gateway.surfaces()
    assert "discord-bots" in session.gateway.surfaces()
    bot = session.bot_manager.adapters()[0]
    assert bot.inbound is True
    assert bot._inbound_handler is not None
