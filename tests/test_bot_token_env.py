"""Tests for Discord token_env name validation and .env helpers."""

from __future__ import annotations

import os

from agent_augury.bot_token_env import (
    discord_token_shape_hint,
    looks_like_discord_bot_token,
    normalize_discord_token,
    store_bot_token,
    upsert_dotenv_value,
    validate_token_env_name,
)


def test_env_names_accepted():
    assert validate_token_env_name("BOT_TOKEN_AGENT_1") is None
    assert validate_token_env_name("DISCORD_TOKEN") is None
    assert not looks_like_discord_bot_token("BOT_TOKEN_AGENT_1")


def test_discord_token_shape_rejected():
    fake = "REDACTED_DISCORD_BOT_TOKEN_DUMMY"
    assert looks_like_discord_bot_token(fake)
    err = validate_token_env_name(fake)
    assert err is not None
    assert "bot token" in err


def test_invalid_env_characters():
    err = validate_token_env_name("bad-name")
    assert err is not None


def test_normalize_discord_token_strips_bot_prefix_and_quotes():
    raw = 'Bot "abc.def.ghi"'
    assert normalize_discord_token(raw) == "abc.def.ghi"
    assert discord_token_shape_hint("short") is not None


def test_upsert_and_store_bot_token(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.delenv("BOT_TOKEN_X", raising=False)
    upsert_dotenv_value("BOT_TOKEN_X", "first", path=path)
    store_bot_token("BOT_TOKEN_X", "second-token-value", path=path)
    text = path.read_text(encoding="utf-8")
    assert "BOT_TOKEN_X=" in text
    assert "second-token-value" in text
    assert "first" not in text
    assert os.environ["BOT_TOKEN_X"] == "second-token-value"
