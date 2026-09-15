"""A2: surfaces: YAML expands to legacy mirror/bots/slack."""

from __future__ import annotations

import pytest
import yaml

from agent_augury.config import ConfigError, load_config, normalize_surfaces
from agent_augury.core.session import Session
from tests.conftest import build_cfg


def _write(tmp_path, cfg: dict) -> str:
    path = tmp_path / "surfaces.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def test_normalize_surfaces_expands_discord_and_slack():
    data: dict = {
        "surfaces": {
            "ink": {"enabled": True},
            "discord": {
                "enabled": True,
                "mode": "interact",
                "agents": ["a1"],
                "mirror": {"url_env": "MIRROR_URL"},
                "bots": [
                    {
                        "agent_id": "a1",
                        "token_env": "T1",
                        "channel_id": 1,
                    },
                    {
                        "agent_id": "a2",
                        "token_env": "T2",
                        "channel_id": 2,
                    },
                ],
            },
            "slack": {
                "enabled": True,
                "mode": "observe",
                "url_env": "SLACK_URL",
            },
        }
    }
    normalize_surfaces(data)
    assert data["mirror"] == {"type": "discord_webhook", "url_env": "MIRROR_URL"}
    assert len(data["bots"]) == 1
    assert data["bots"][0]["agent_id"] == "a1"
    assert data["bots"][0]["inbound"] is True  # interact default
    assert data["slack"]["url_env"] == "SLACK_URL"


def test_normalize_surfaces_conflict_with_legacy_mirror():
    data = {
        "mirror": {"type": "discord_webhook", "url_env": "X"},
        "surfaces": {
            "discord": {
                "enabled": True,
                "mirror": {"url_env": "Y"},
            }
        },
    }
    with pytest.raises(ConfigError, match="conflicts"):
        normalize_surfaces(data)


def test_normalize_surfaces_disabled_discord_leaves_legacy():
    data = {
        "mirror": {"type": "discord_webhook", "url_env": "X"},
        "surfaces": {"discord": {"enabled": False}},
    }
    normalize_surfaces(data)
    assert data["mirror"]["url_env"] == "X"


def test_load_config_surfaces_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk")
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
        "surfaces": {
            "discord": {
                "enabled": True,
                "mode": "observe",
                "mirror": {
                    "type": "discord_webhook",
                    "url_env": "AUGURY_MIRROR_URL",
                },
                "bots": [
                    {
                        "agent_id": "a1",
                        "token_env": "DISCORD_TOKEN",
                        "channel_id": "42",
                    }
                ],
            },
            "slack": {"enabled": True, "url_env": "AUGURY_SLACK_URL"},
        },
    }
    loaded = load_config(_write(tmp_path, cfg))
    assert loaded["mirror"]["url_env"] == "AUGURY_MIRROR_URL"
    assert loaded["bots"][0]["channel_id"] == "42"
    assert loaded["bots"][0].get("inbound") is not True
    assert loaded["slack"]["url_env"] == "AUGURY_SLACK_URL"


def test_session_from_surfaces_expanded_config(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    monkeypatch.setenv("TEST_API_KEY", "sk")
    monkeypatch.setenv("AUGURY_MIRROR_URL", "https://example.test/hook")
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
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
        mirror={"type": "discord_webhook", "url_env": "AUGURY_MIRROR_URL"},
        bots=[
            {
                "agent_id": "a1",
                "token_env": "DISCORD_TOKEN",
                "channel_id": 99,
            }
        ],
    )
    # Simulate post-normalize_surfaces shape (Session.from_config uses legacy keys).
    # discord.Client requires an event loop; mock it like other bot unit tests.
    mock_client = MagicMock()
    mock_client.event = lambda func: func
    with patch(
        "agent_augury.channels.discord.bot.discord.Client",
        return_value=mock_client,
    ):
        session = Session.from_config(cfg)
    assert session.mirror is not None
    assert session.bot_manager is not None
    assert "discord-mirror" in session.gateway.surfaces()
    assert "discord-bots" in session.gateway.surfaces()


def test_load_config_rejects_unknown_surfaces_key(tmp_path):
    cfg = {
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "http://x",
                    "api_key_env": "K",
                    "model": "m",
                },
            }
        ],
        "surfaces": {"teams": {"enabled": True}},
    }
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(_write(tmp_path, cfg))
