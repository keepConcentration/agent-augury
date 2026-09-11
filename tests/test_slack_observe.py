"""M6: Slack Incoming Webhook observe surface."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from agent_augury.channel.slack_mirror import SlackWebhookMirror, slack_from_config
from agent_augury.channel.slack_observe import attach_slack_mirror
from agent_augury.config import ConfigError, load_config
from agent_augury.gateway import SessionGateway, make_event
from tests.conftest import build_cfg


def test_slack_from_config_missing_env(monkeypatch):
    monkeypatch.delenv("AUGURY_SLACK_WEBHOOK_URL", raising=False)
    assert slack_from_config({"url_env": "AUGURY_SLACK_WEBHOOK_URL"}) is None


def test_slack_from_config_disabled():
    assert slack_from_config({"enabled": False, "url_env": "X"}) is None


def test_attach_slack_enqueues_message():
    gw = SessionGateway()
    mirror = SlackWebhookMirror(webhook_url="https://hooks.slack.test/x")
    attach_slack_mirror(gw, mirror)
    assert "slack-mirror" in gw.surfaces()
    n = gw.publish(
        make_event(
            "message",
            thread_id="t1",
            author="agent-1",
            content="hello slack",
        )
    )
    assert n == 1
    assert len(mirror.outbox) == 1
    assert "hello slack" in mirror.outbox[0]
    assert "agent-1" in mirror.outbox[0]


def test_slack_observe_rejects_human_commands():
    from agent_augury.gateway import make_command

    gw = SessionGateway(on_command=lambda _c: {})
    mirror = SlackWebhookMirror(webhook_url="https://hooks.slack.test/x")
    attach_slack_mirror(gw, mirror)
    result = gw.dispatch(
        make_command("human.send", id="1", content="nope"),
        surface="slack-mirror",
    )
    assert result["ok"] is False
    assert "observe-only" in result["error"]


@pytest.mark.asyncio
async def test_slack_flush_posts_json():
    client = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(raise_for_status=lambda: None))
    mirror = SlackWebhookMirror(
        webhook_url="https://hooks.slack.test/x",
        client=client,
    )
    mirror.enqueue_text("hi")
    sent = await mirror.flush()
    assert sent == 1
    client.post.assert_awaited_once()
    args, kwargs = client.post.await_args
    assert args[0] == "https://hooks.slack.test/x"
    assert kwargs["json"] == {"text": "hi"}


def test_config_slack_requires_url_env(tmp_path):
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
        slack={"mode": "observe"},
    )
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ConfigError, match="url_env"):
        load_config(str(path))


def test_session_attaches_slack_mirror(tmp_path, monkeypatch):
    from agent_augury.session import Session

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setenv("AUGURY_SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")
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
        slack={"url_env": "AUGURY_SLACK_WEBHOOK_URL", "mode": "observe"},
    )
    path = tmp_path / "slack.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    session = Session.from_config(load_config(str(path)))
    assert session.slack_mirror is not None
    assert "slack-mirror" in session.gateway.surfaces()
