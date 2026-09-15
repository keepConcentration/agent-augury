"""A6 V1: ChatDisplayPolicy + config + observe wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent_augury.channels.discord.bot import BotManager, DiscordBotAdapter
from agent_augury.channels.discord.mirror import DiscordWebhookMirror
from agent_augury.channels.discord.observe import attach_discord_bots, attach_discord_mirror
from agent_augury.channels.display import (
    ChatDisplayPolicy,
    resolve_chat_display_policy,
)
from agent_augury.channels.slack.mirror import SlackWebhookMirror
from agent_augury.channels.slack.observe import attach_slack_mirror
from agent_augury.config import ConfigError, load_config
from agent_augury.gateway import SessionGateway, make_event


def test_policy_summary_skips_tool_and_truncates_step():
    policy = ChatDisplayPolicy("summary")
    tool = make_event("tool", agent_id="a", tool="read_file")
    assert policy.allow(tool) is False
    long_body = "x" * 400
    step = make_event(
        "agent.step",
        agent_id="a",
        result={"text": long_body},
    )
    assert policy.allow(step) is True
    out = policy.format_wire(step, recipient_agent_id="a")
    assert out is not None
    assert len(out) <= 280
    assert out.endswith("…")


def test_policy_quiet_hitl_only():
    policy = ChatDisplayPolicy("quiet")
    assert policy.allow(make_event("agent.step", agent_id="a", result={"text": "hi"})) is False
    assert policy.allow(make_event("log", text="noise")) is False
    assert policy.allow(
        make_event("human.question", agent_id="a", question="?")
    ) is True
    assert policy.allow(
        make_event("message", author="human", content="hello")
    ) is True
    assert policy.allow(
        make_event("message", author="agent-1", content="hello")
    ) is False


def test_resolve_per_platform_override():
    cfg = {
        "display": {"chat": "quiet"},
        "surfaces": {"discord": {"display": "full"}},
    }
    assert resolve_chat_display_policy(cfg, "discord").mode == "full"
    assert resolve_chat_display_policy(cfg, "slack").mode == "quiet"


def test_discord_bots_summary_no_log():
    gw = SessionGateway()
    mgr = BotManager()
    client = MagicMock()
    client.event = lambda func: func
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=client):
        bot = DiscordBotAdapter(agent_id="agent-1", token="t", channel_id=1)
    mgr.register(bot)
    attach_discord_bots(gw, mgr, display=ChatDisplayPolicy("summary"))

    gw.publish(make_event("log", text="verbose"))
    assert bot._outbox.empty()

    gw.publish(
        make_event(
            "agent.step",
            agent_id="agent-1",
            result={"text": "visible"},
        )
    )
    item = bot._outbox.get_nowait()
    text = item.content if hasattr(item, "content") else item
    assert "visible" in text


def test_slack_quiet_skips_tool():
    gw = SessionGateway()
    mirror = SlackWebhookMirror(webhook_url="https://example.test/hook")
    attach_slack_mirror(gw, mirror, display=ChatDisplayPolicy("quiet"))

    gw.publish(make_event("tool", agent_id="a", tool="read_file"))
    assert mirror.outbox == []

    gw.publish(make_event("human.question", agent_id="a", question="ok?"))
    assert len(mirror.outbox) == 1


def test_mirror_quiet_human_message_only():
    gw = SessionGateway()
    mirror = DiscordWebhookMirror(webhook_url="https://example.test/hook")
    attach_discord_mirror(gw, mirror, display=ChatDisplayPolicy("quiet"))

    gw.publish(make_event("message", author="agent-1", content="bot says hi"))
    assert mirror.outbox == []

    gw.publish(make_event("message", author="human", content="user says hi"))
    assert len(mirror.outbox) == 1


def _minimal_openai_cfg(**extra) -> dict:
    cfg = {
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "http://x",
                    "api_key_env": "TEST_API_KEY",
                    "model": "m",
                },
            }
        ],
    }
    cfg.update(extra)
    return cfg


def test_load_config_display_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk")
    path = tmp_path / "c.yaml"
    import yaml

    path.write_text(
        yaml.safe_dump(_minimal_openai_cfg(display={"chat": "summary"})),
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert resolve_chat_display_policy(loaded, "discord").mode == "summary"


def test_load_config_invalid_display_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk")
    path = tmp_path / "c.yaml"
    import yaml

    path.write_text(
        yaml.safe_dump(_minimal_openai_cfg(display={"chat": "loud"})),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="display.chat"):
        load_config(path)
