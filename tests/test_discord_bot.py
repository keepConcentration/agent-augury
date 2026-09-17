"""Discord bot adapter tests (mock-based, no real Discord connection)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_augury.channels.discord.bot import (
    BotManager,
    DiscordBotAdapter,
    DiscordBotError,
    _format_event,
)
from tests.conftest import build_cfg

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Mock discord.Client that does not connect."""
    client = MagicMock()
    client.event = lambda func: func  # pass-through decorator
    client.user = MagicMock(name="TestBot#1234")
    client.close = AsyncMock()
    return client


@pytest.fixture
def adapter(mock_client):
    """DiscordBotAdapter with mocked internals."""
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        adapter = DiscordBotAdapter(
            agent_id="agent-1",
            token="fake-token",
            channel_id=123456789,
            token_env="BOT_TOKEN_AGENT_1",
        )
        return adapter


@pytest.mark.asyncio
async def test_start_empty_token_raises_clear_error(mock_client):
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        bot = DiscordBotAdapter(
            agent_id="coder",
            token="  ",
            channel_id=1,
            token_env="BOT_TOKEN_CODER",
        )
    with pytest.raises(DiscordBotError, match="BOT_TOKEN_CODER"):
        await bot.start()


@pytest.mark.asyncio
async def test_start_token_env_is_literal_token_raises_yaml_hint(mock_client):
    from tests.token_fakes import fake_discord_bot_token

    pasted = fake_discord_bot_token()
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        bot = DiscordBotAdapter(
            agent_id="agent-1",
            token="",
            channel_id=1,
            token_env=pasted,
        )
    with pytest.raises(DiscordBotError, match="looks like a bot token"):
        await bot.start()


@pytest.mark.asyncio
async def test_start_returns_after_ready_while_gateway_keeps_running(mock_client):
    """Regression: await client.start() forever blocked headless turn loop."""

    async def long_gateway(_token: str) -> None:
        await asyncio.sleep(3600)

    mock_client.start = long_gateway
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        bot = DiscordBotAdapter(
            agent_id="agent-1",
            token="fake-token",
            channel_id=1,
            token_env="BOT_TOKEN_AGENT_1",
        )
        start_task = asyncio.create_task(bot.start())
        await asyncio.sleep(0)
        bot._ready.set()
        await asyncio.wait_for(start_task, timeout=1.0)
        assert bot._gateway_task is not None
        assert not bot._gateway_task.done()
        await bot.close()


@pytest.mark.asyncio
async def test_bot_manager_start_all_does_not_block_on_gateway(mock_client):
    async def long_gateway(_token: str) -> None:
        await asyncio.sleep(3600)

    mock_client.start = long_gateway
    manager = BotManager()
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        for i in (1, 2):
            manager.register(
                DiscordBotAdapter(
                    agent_id=f"agent-{i}",
                    token="tok",
                    channel_id=99,
                    token_env=f"BOT_{i}",
                )
            )
        start_all = asyncio.create_task(manager.start_all())
        for _ in range(20):
            await asyncio.sleep(0)
            for bot in manager.adapters():
                if not bot._ready.is_set():
                    bot._ready.set()
        await asyncio.wait_for(start_all, timeout=2.0)
        for bot in manager.adapters():
            await bot.close()


@pytest.mark.asyncio
async def test_start_login_failure_wraps_message(mock_client):
    import discord

    mock_client.start = AsyncMock(
        side_effect=discord.LoginFailure("Improper token has been passed.")
    )
    with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
        bot = DiscordBotAdapter(
            agent_id="agent-1",
            token="not-a-real-token",
            channel_id=1,
            token_env="BOT_TOKEN_AGENT_1",
        )
    with pytest.raises(DiscordBotError, match="BOT_TOKEN_AGENT_1") as ei:
        await bot.start()
    assert "Improper token" in str(ei.value)


# ---------------------------------------------------------------------------
# DiscordBotAdapter
# ---------------------------------------------------------------------------


class TestDiscordBotAdapter:
    def test_init_sets_agent_id_and_channel(self, adapter):
        assert adapter.agent_id == "agent-1"
        assert adapter.channel_id == 123456789

    def test_enqueue_splits_long_content(self, adapter):
        long_text = "x" * 2000
        adapter.enqueue(long_text)
        parts = []
        while not adapter._outbox.empty():
            item = adapter._outbox.get_nowait()
            parts.append(item.content if hasattr(item, "content") else item)
        assert len(parts) >= 2
        assert all(len(p) <= 1800 for p in parts)
        # Indicators are appended; body without ``(i/n)`` covers the input.
        bodies = []
        for p in parts:
            if " (" in p and p.endswith(")"):
                p = p.rsplit(" (", 1)[0]
            bodies.append(p)
        assert "".join(bodies) == long_text

    def test_enqueue_short_content_unchanged(self, adapter):
        adapter.enqueue("hello")
        item = adapter._outbox.get_nowait()
        text = item.content if hasattr(item, "content") else item
        assert text == "hello"

    def test_enqueue_from_other_thread_via_call_soon(self, adapter):
        """D4: foreign-thread enqueue schedules onto the bot loop."""
        import threading

        loop = asyncio.new_event_loop()
        adapter._loop = loop
        done = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                adapter.enqueue("from-other-thread")
            except BaseException as exc:  # noqa: BLE001 — collect for main thread
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        assert done.wait(timeout=2.0)
        assert errors == []
        # Drain the scheduled callback(s) on the owning loop.
        loop.call_soon(loop.stop)
        loop.run_forever()
        item = adapter._outbox.get_nowait()
        assert item.content == "from-other-thread"
        loop.close()
        adapter._loop = None


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
        assert item.content == "hello world"

    def test_register_overwrites_same_agent_id(self, adapter, mock_client):
        mgr = BotManager()
        mgr.register(adapter)

        # Register a second adapter with same agent_id
        with patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client):
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

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        loaded = load_config(str(path))
        assert "bots" in loaded
        assert len(loaded["bots"]) == 1

    def test_bots_section_missing_agent_id(self, tmp_path):
        """agent_id 없으면 ConfigError."""
        import yaml

        from agent_augury.config import ConfigError, load_config

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[{"token_env": "BOT_TOKEN_1", "channel_id": 123}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="agent_id"):
            load_config(str(path))

    def test_bots_section_missing_token_env(self, tmp_path):
        """token_env 없으면 ConfigError."""
        import yaml

        from agent_augury.config import ConfigError, load_config

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[{"agent_id": "a1", "channel_id": 123}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="token_env"):
            load_config(str(path))

    def test_bots_section_token_env_looks_like_bot_token(self, tmp_path):
        import yaml

        from agent_augury.config import ConfigError, load_config
        from tests.token_fakes import fake_discord_bot_token

        pasted = fake_discord_bot_token()
        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[{"agent_id": "a1", "token_env": pasted, "channel_id": 123}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="bot token"):
            load_config(str(path))

    def test_bots_section_missing_channel_id(self, tmp_path):
        """channel_id 없으면 ConfigError."""
        import yaml

        from agent_augury.config import ConfigError, load_config

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[{"agent_id": "a1", "token_env": "BOT_TOKEN_1"}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="channel_id"):
            load_config(str(path))

    def test_bots_section_invalid_channel_id(self, tmp_path):
        """channel_id가 정수 변환 불가능하면 ConfigError."""
        import yaml

        from agent_augury.config import ConfigError, load_config

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots=[
                {"agent_id": "a1", "token_env": "BOT_TOKEN_1", "channel_id": "not-a-number"}
            ],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="integer"):
            load_config(str(path))

    def test_bots_section_not_list(self, tmp_path):
        """bots가 리스트가 아니면 ConfigError."""
        import yaml

        from agent_augury.config import ConfigError, load_config

        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "X", "model": "m"}}],
            bots={"agent_id": "a1"},
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        with pytest.raises(ConfigError, match="list"):
            load_config(str(path))


# ---------------------------------------------------------------------------
# Session integration (mock bot manager)
# ---------------------------------------------------------------------------


class TestSessionBotManagerIntegration:
    def test_session_from_config_with_bots(self, tmp_path, monkeypatch):
        """Session.from_config가 bots 섹션을 파싱하여 bot_manager를 생성."""
        from unittest.mock import MagicMock, patch

        import yaml

        from agent_augury.config import load_config
        from agent_augury.core.session import Session

        monkeypatch.setenv("TEST_API_KEY", "sk-test")
        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "TEST_API_KEY", "model": "m"}}],
            bots=[
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.event = lambda func: func

        with (
            patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client),
            patch.dict("os.environ", {"BOT_TOKEN_1": "fake-token"}),
        ):
                session = Session.from_config(load_config(str(path)))

        assert session.bot_manager is not None
        assert len(session.bot_manager) == 1
        assert "a1" in session.bot_manager

    def test_session_from_config_without_bots(self, tmp_path, monkeypatch):
        """bots 섹션 없으면 bot_manager는 None."""
        import yaml

        from agent_augury.config import load_config
        from agent_augury.core.session import Session

        monkeypatch.setenv("TEST_API_KEY", "sk-test")
        cfg = build_cfg(
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "TEST_API_KEY", "model": "m"}}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        session = Session.from_config(load_config(str(path)))
        assert session.bot_manager is None

    @pytest.mark.asyncio
    async def test_session_run_calls_start_and_stop_all(self, tmp_path, monkeypatch):
        """Session.run()이 start_all()/stop_all()을 호출하는지 검증."""
        from unittest.mock import MagicMock, patch

        import yaml

        from agent_augury.config import load_config
        from agent_augury.core.session import Session

        monkeypatch.setenv("TEST_API_KEY", "sk-test")
        cfg = build_cfg(
            max_steps=5,
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "TEST_API_KEY", "model": "m"}}],
            bots=[
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.event = lambda func: func

        with (
            patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client),
            patch.dict("os.environ", {"BOT_TOKEN_1": "fake-token"}),
        ):
                session = Session.from_config(load_config(str(path)))

        # Mock start_all / stop_all to track calls
        session.bot_manager.start_all = AsyncMock()
        session.bot_manager.stop_all = AsyncMock()
        # base_url points nowhere; this test is about bot lifecycle, not retries
        session.max_backend_retries = 0

        await session.run()

        session.bot_manager.start_all.assert_awaited_once()
        # stop_all is NOT called by run() anymore — it's called by close()
        session.bot_manager.stop_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_run_calls_stop_all_on_exception(self, tmp_path, monkeypatch):
        """run() 중 예외 발생해도 start_all은 호출되었는지 검증.

        새 라이프사이클에서는 stop_all()이 close()에서 호출됩니다.
        """
        from unittest.mock import MagicMock, patch

        import yaml

        from agent_augury.config import load_config
        from agent_augury.core.session import Session

        monkeypatch.setenv("TEST_API_KEY", "sk-test")
        cfg = build_cfg(
            max_steps=5,
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "TEST_API_KEY", "model": "m"}}],
            bots=[
                {
                    "agent_id": "a1",
                    "token_env": "BOT_TOKEN_1",
                    "channel_id": 123456789,
                }
            ],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.event = lambda func: func

        with (
            patch("agent_augury.channels.discord.bot.discord.Client", return_value=mock_client),
            patch.dict("os.environ", {"BOT_TOKEN_1": "fake-token"}),
        ):
                session = Session.from_config(load_config(str(path)))

        # Force an exception during agent execution
        async def raise_exc(*args, **kwargs):
            raise RuntimeError("simulated failure")

        # Patch run_agent's step to raise
        _ = session._run_impl  # kept for debugging context

        async def failing_run(initial_prompt=None):
            # start_all already called by run(), now simulate failure
            raise RuntimeError("simulated failure")

        session._run_impl = failing_run

        session.bot_manager.start_all = AsyncMock()
        session.bot_manager.stop_all = AsyncMock()

        with pytest.raises(RuntimeError, match="simulated failure"):
            await session.run()

        # start_all was called, stop_all was NOT called by run()
        session.bot_manager.start_all.assert_awaited_once()
        session.bot_manager.stop_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_run_no_bots_is_safe(self, tmp_path, monkeypatch):
        """bot_manager 없을 때 run()이 정상 동작하는지 확인."""
        import yaml

        from agent_augury.config import load_config
        from agent_augury.core.session import Session

        monkeypatch.setenv("TEST_API_KEY", "sk-test")
        cfg = build_cfg(
            max_steps=5,
            agents=[{"id": "a1", "backend": {"type": "openai", "base_url": "http://x/v1", "api_key_env": "TEST_API_KEY", "model": "m"}}],
        )
        path = tmp_path / "test.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

        session = Session.from_config(load_config(str(path)))
        assert session.bot_manager is None
        # base_url points nowhere; this test is about bot_manager, not retries
        session.max_backend_retries = 0

        # Should complete without error
        await session.run()

    @pytest.mark.asyncio
    async def test_session_run_empty_bot_manager_is_safe(self, tmp_path):
        """bot_manager에 봇이 0개일 때 run()이 정상 동작하는지 확인."""
        from agent_augury.backend.fake import FakeModelBackend
        from agent_augury.channels.discord.bot import BotManager
        from agent_augury.core.agent.loop import AgentLoop
        from agent_augury.core.server import MessageServer
        from agent_augury.core.session import Session

        server = MessageServer()
        server.register_agent("a1")
        agent = AgentLoop(
            agent_id="a1",
            server=server,
            backend=FakeModelBackend(script=["hi"]),
        )
        session = Session(
            server=server,
            agents=[agent],
            max_steps=5,
            bot_manager=BotManager(),  # empty
        )

        # Should complete without error
        await session.run()
