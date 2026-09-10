"""REPL session tests: session reuse, conversation persistence, exit conditions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agent_augury.cli import _run_repl
from agent_augury.session import Session

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: Path, task: str = "test") -> Path:
    cfg = {
        "max_steps": 5,
        "task": task,
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "model": "gpt-4o-mini",
                },
            }
        ],
    }
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_path


def _mock_session() -> MagicMock:
    session_instance = MagicMock()
    session_instance.mirror = None
    session_instance.gate = None
    session_instance.protocol = None
    session_instance.server.snapshot.return_value = {
        "threads": [],
        "messages": [],
    }
    session_instance.run = AsyncMock(return_value=1)
    session_instance.close = AsyncMock()
    return session_instance


# ---------------------------------------------------------------------------
# Session.run() reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_run_called_twice_conversation_accumulates(tmp_path):
    """Calling session.run() twice must append to the same conversation."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            session_instance.run = AsyncMock(return_value=3)
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", "/quit"]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.run.assert_any_call(initial_prompt="start")
            session_instance.run.assert_any_call(initial_prompt="hello")
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_exit_on_quit(tmp_path):
    """'/quit' must exit the REPL loop (plain quit is a normal prompt)."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", "/quit"]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_exit_on_exit(tmp_path):
    """'/exit' must exit the REPL loop."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", "/exit"]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_blank_input_ignored(tmp_path):
    """Blank input must be ignored (no extra session.run) and loop continues."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", "", "", "/quit"]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            # start + hello only (blanks ignored)
            assert session_instance.run.call_count == 2
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_plain_quit_is_next_prompt(tmp_path):
    """Plain 'quit' is a normal next-turn prompt (v0.5.1), not an exit token."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["quit", "/quit"]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.run.assert_any_call(initial_prompt="start")
            session_instance.run.assert_any_call(initial_prompt="quit")
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_eof_exits(tmp_path):
    """EOFError on input must exit the REPL loop gracefully."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", EOFError]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.close.assert_called_once()


@pytest.mark.asyncio
async def test_repl_keyboard_interrupt_exits(tmp_path):
    """KeyboardInterrupt on input must exit the REPL loop gracefully."""
    cfg_path = _make_cfg(tmp_path)

    with patch("agent_augury.cli.load_config") as mock_load:
        import agent_augury.config as config_mod

        mock_load.return_value = config_mod.load_config(cfg_path)

        with patch("agent_augury.cli.Session") as MockSession:
            session_instance = _mock_session()
            MockSession.from_config.return_value = session_instance

            with (
                patch("builtins.input", side_effect=["hello", KeyboardInterrupt]),
                patch("builtins.print"),
            ):
                await _run_repl(str(cfg_path), initial_prompt="start", quiet=True)

            assert session_instance.run.call_count == 2
            session_instance.close.assert_called_once()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_setup_called_once():
    """Session._setup() must be called only once even if run() is called multiple times."""
    session = Session(server=MagicMock(), agents=[])
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
    session = Session(server=MagicMock(), agents=[])
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


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_config_always_uses_repl(tmp_path, monkeypatch):
    """--config always starts a REPL session (no --repl flag required)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agent_augury.cli import main

    cfg = {
        "mode": "L3",
        "max_steps": 5,
        "task": "smoke test",
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "model": "gpt-4o-mini",
                },
            },
        ],
    }
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    with patch("agent_augury.cli.asyncio.run", return_value=0) as mock_run:
        rc = main(["--config", str(cfg_path)])

    assert rc == 0
    mock_run.assert_called_once()


def test_cli_legacy_repl_flag_still_accepted(tmp_path, monkeypatch):
    """Legacy --repl is accepted (suppressed) and still runs REPL."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agent_augury.cli import main

    cfg = {
        "mode": "L3",
        "max_steps": 5,
        "task": "smoke test",
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "model": "gpt-4o-mini",
                },
            },
        ],
    }
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    with patch("agent_augury.cli.asyncio.run", return_value=0) as mock_run:
        rc = main(["--config", str(cfg_path), "--repl"])

    assert rc == 0
    mock_run.assert_called_once()


def test_cli_wizard_default_is_repl(tmp_path, monkeypatch):
    """Wizard mode (no --config) defaults to REPL."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agent_augury.cli import main

    output = tmp_path / "wizard_out.yaml"
    inputs = iter(
        [
            "a1",
            "1",
            "",
            "OPENAI_API_KEY",
            "gpt-4o-mini",
            "n",
            "e2e task",
            "",
        ]
    )

    with (
        patch("builtins.input", side_effect=lambda *args: next(inputs)),
        patch("agent_augury.cli.check_tty", return_value=True),
        patch("agent_augury.wizard.save_model_config"),
        patch("agent_augury.cli.model_config_exists", return_value=False),
        patch("agent_augury.cli.asyncio.run", return_value=0) as mock_run,
        patch("agent_augury.cli._prompt_multiline", return_value="e2e task"),
    ):
        rc = main(["--output", str(output)])

    assert rc == 0
    mock_run.assert_called_once()
