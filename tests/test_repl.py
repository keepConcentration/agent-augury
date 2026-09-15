"""Session lifecycle tests (CLI is Ink-only; no plain REPL)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_augury.core.session import Session


def _mock_server() -> MagicMock:
    server = MagicMock()
    server.create_thread = AsyncMock(return_value="thread-human")
    server.close = AsyncMock()
    return server


@pytest.mark.asyncio
async def test_session_setup_called_once():
    """Session._setup() must be called only once even if run() is called multiple times."""
    session = Session(server=_mock_server(), agents=[])
    session._setup_done = False
    session._closed = False

    setup_called = [0]

    async def mock_run_impl(initial_prompt=None):
        return 0

    original_setup = session._setup

    async def counting_setup():
        setup_called[0] += 1
        await original_setup()

    with (
        patch.object(session, "_setup", side_effect=counting_setup),
        patch.object(session, "_run_impl", side_effect=mock_run_impl),
    ):
        await session.run(initial_prompt="first")
        await session.run(initial_prompt="second")
        await session.run(initial_prompt="third")

    assert session._setup_done is True
    assert setup_called[0] == 3


@pytest.mark.asyncio
async def test_session_close_called_once():
    """Session.close() must be idempotent — subsequent calls are no-ops."""
    session = Session(server=_mock_server(), agents=[])
    session._closed = False

    close_called = [0]
    original_close = session.close

    async def counting_close():
        close_called[0] += 1
        await original_close()

    with patch.object(session, "close", side_effect=counting_close):
        await session.close()
        await session.close()
        await session.close()

    assert session._closed is True
    assert close_called[0] == 3
