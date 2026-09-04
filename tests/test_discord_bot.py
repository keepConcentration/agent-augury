"""Discord bot adapter tests (mock-based, no real Discord connection)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_augury.channel.discord_bot import (
    BotManager,
    DiscordBotAdapter,
    _format_event,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock discord.Client that does not connect."""
    client = MagicMock()
    client.event = lambda func: func  # pass-through decorator
    client.user = MagicMock(name="TestBot#1234")
    return client


@pytest.fixture
def adapter(mock_client):
    """DiscordBotAdapter with mocked internals."""
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=mock_client):
        adapter = DiscordBotAdapter(
            agent_id="agent-1",
            token="fake-token",
            channel_id=123456789,
        )
        return adapter


# ---------------------------------------------------------------------------
# DiscordBotAdapter
# ---------------------------------------------------------------------------


class TestDiscordBotAdapter:
    def test_init_sets_agent_id_and_channel(self, adapter):
        assert adapter.agent_id == "agent-1"
        assert adapter.channel_id == 123456789

    def test_enqueue_truncates_long_content(self, adapter):
        long_text = "x" * 2000
        adapter.enqueue(long_text)
        # Should be truncated to _MAX_CONTENT + "…"
        item = adapter._outbox.get_nowait()
        assert len(item) == 1801  # 1800 + "…"
        assert item.endswith("…")

    def test_enqueue_short_content_unchanged(self, adapter):
        adapter.enqueue("hello")
        item = adapter._outbox.get_nowait()
        assert item == "hello"


# ---------------------------------------------------------------------------
# BotManager
# ---------------------------------------------------------------------------


class TestBotManager:
    def test_register_and_len(self, adapter):
        mgr = BotManager()
        assert len(mgr) == 0
        mgr.register(adapter)
        assert len(mgr) == 1
        assert "agent-1" in mgr

    def test_route_event_unknown_agent_is_noop(self):
        mgr = BotManager()
        # Should not raise
        mgr.route_event("ghost-agent", "hello")

    def test_route_event_known_agent_enqueues(self, adapter):
        mgr = BotManager()
        mgr.register(adapter)
        mgr.route_event("agent-1", "hello world")
        item = adapter._outbox.get_nowait()
        assert item == "hello world"

    def test_register_overwrites_same_agent_id(self, adapter, mock_client):
        mgr = BotManager()
        mgr.register(adapter)

        # Register a second adapter with same agent_id
        with patch("agent_augury.channel.discord_bot.discord.Client", return_value=mock_client):
            adapter2 = DiscordBotAdapter(
                agent_id="agent-1",
                token="other-token",
                channel_id=999,
            )
        mgr.register(adapter2)
        assert len(mgr) == 1


# ---------------------------------------------------------------------------
# _format_event
# ---------------------------------------------------------------------------


class TestFormatEvent:
    def test_create_thread(self):
        event = {
            "type": "create_thread",
            "name": "plan",
            "participants": ["agent-1", "agent-2"],
        }
        result = _format_event(event)
        assert result == "🧵 create_thread **plan** (agent-1, agent-2)"

    def test_send_message(self):
        event = {
            "type": "send_message",
            "author": "agent-1",
            "content": "hello world",
        }
        result = _format_event(event)
        assert result == "💬 agent-1: hello world"

    def test_send_message_truncates(self):
        event = {
            "type": "send_message",
            "author": "agent-1",
            "content": "x" * 2000,
        }
        result = _format_event(event)
        assert len(result) == 1800 + len("💬 agent-1: ") + 1  # +1 for "…"
        assert result.endswith("…")

    def test_tool_read_file(self):
        event = {
            "type": "tool",
            "agent_id": "agent-1",
            "tool": "read_file",
        }
        result = _format_event(event)
        assert result == "📖 agent-1: read_file(...)"

    def test_tool_write_file(self):
        event = {
            "type": "tool",
            "agent_id": "agent-1",
            "tool": "write_file",
        }
        result = _format_event(event)
        assert result == "📝 agent-1: write_file(...)"

    def test_tool_unknown(self):
        event = {
            "type": "tool",
            "agent_id": "agent-1",
            "tool": "custom_tool",
        }
        result = _format_event(event)
        assert result == "🔧 agent-1: custom_tool(...)"

    def test_read_resource(self):
        event = {
            "type": "read_resource",
            "agent_id": "agent-1",
            "threads": 3,
            "messages": 12,
        }
        result = _format_event(event)
        assert result == "📊 agent-1: read_resource (threads=3, messages=12)"

    def test_step_with_text(self):
        result_mock = MagicMock()
        result_mock.text = "I'll start exploring..."
        event = {
            "type": "step",
            "agent_id": "agent-1",
            "result": result_mock,
        }
        result = _format_event(event)
        assert result == "💭 agent-1: I'll start exploring..."

    def test_step_without_text(self):
        result_mock = MagicMock()
        result_mock.text = None
        event = {
            "type": "step",
            "agent_id": "agent-1",
            "result": result_mock,
        }
        result = _format_event(event)
        assert result is None

    def test_unknown_type(self):
        event = {"type": "unknown_thing"}
        result = _format_event(event)
        assert result is None


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestBotsConfigValidation:
    def test_bots_section_valid(self, tmp_path):
        """bots 섹션이 정상적으로 파싱되는지 확인."""
        import yaml
        from agent_augury.config import load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        loaded = load_config(str(path))
        assert "bots" in loaded
        assert len(loaded["bots"]) == 1

    def test_bots_section_missing_agent_id(self, tmp_path):
        """agent_id 없으면 ConfigError."""
        import yaml
        from agent_augury.config import ConfigError, load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [{"token_env": "BOT_TOKEN_1", "channel_id": 123}],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="agent_id"):
            load_config(str(path))

    def test_bots_section_missing_token_env(self, tmp_path):
        """token_env 없으면 ConfigError."""
        import yaml
        from agent_augury.config import ConfigError, load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [{"agent_id": "a1", "channel_id": 123}],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="token_env"):
            load_config(str(path))

    def test_bots_section_missing_channel_id(self, tmp_path):
        """channel_id 없으면 ConfigError."""
        import yaml
        from agent_augury.config import ConfigError, load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [{"agent_id": "a1", "token_env": "BOT_TOKEN_1"}],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="channel_id"):
            load_config(str(path))

    def test_bots_section_invalid_channel_id(self, tmp_path):
        """channel_id가 정수 변환 불가능하면 ConfigError."""
        import yaml
        from agent_augury.config import ConfigError, load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [
                {"agent_id": "a1", "token_env": "BOT_TOKEN_1", "channel_id": "not-a-number"}
            ],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="integer"):
            load_config(str(path))

    def test_bots_section_not_list(self, tmp_path):
        """bots가 리스트가 아니면 ConfigError."""
        import yaml
        from agent_augury.config import ConfigError, load_config

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": {"agent_id": "a1"},
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="list"):
            load_config(str(path))


# ---------------------------------------------------------------------------
# Session integration (mock bot manager)
# ---------------------------------------------------------------------------


class TestSessionBotManagerIntegration:
    def test_session_from_config_with_bots(self, tmp_path):
        """Session.from_config가 bots 섹션을 파싱하여 bot_manager를 생성."""
        from unittest.mock import MagicMock, patch

        import yaml
        from agent_augury.session import Session

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
            "bots": [
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.event = lambda func: func

        with patch("agent_augury.channel.discord_bot.discord.Client", return_value=mock_client):
            with patch.dict("os.environ", {"BOT_TOKEN_1": "fake-token"}):
                session = Session.from_config(load_config(str(path)))

        assert session.bot_manager is not None
        assert len(session.bot_manager) == 1
        assert "a1" in session.bot_manager

    def test_session_from_config_without_bots(self, tmp_path):
        """bots 섹션 없으면 bot_manager는 None."""
        import yaml
        from agent_augury.session import Session

        cfg = {
            "mode": "L3",
            "agents": [{"id": "a1", "backend": {"type": "fake", "script": ["hi"]}}],
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        session = Session.from_config(load_config(str(path)))
        assert session.bot_manager is None


# Helper to avoid circular import in tests
def load_config(path):
    from agent_augury.config import load_config as _load

    return _load(path)
