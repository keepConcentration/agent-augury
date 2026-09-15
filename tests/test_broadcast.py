"""Tests for broadcast event logging and --quiet flag."""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from agent_augury.cli import _mask_sensitive
from agent_augury.core.server import MessageServer

# ---------------------------------------------------------------------------
# _mask_sensitive
# ---------------------------------------------------------------------------


def test_mask_sensitive_bearer_token():
    assert _mask_sensitive("Bearer abc123xyz") == "Bearer ***"


def test_mask_sensitive_api_key():
    assert _mask_sensitive('api_key="secret123"') == 'api_key="***"'


def test_mask_sensitive_token_field():
    assert _mask_sensitive("token=abc123") == "token=***"


def test_mask_sensitive_authorization():
    assert _mask_sensitive("Authorization: Bearer xyz123") == "Authorization: Bearer ***"


def test_mask_sensitive_no_match():
    assert _mask_sensitive("hello world") == "hello world"
    # Already-masked content stays as-is
    assert _mask_sensitive("Authorization: Bearer ***") == "Authorization: Bearer ***"


# ---------------------------------------------------------------------------
# MessageServer event subscription
# ---------------------------------------------------------------------------


async def test_server_subscribe_events_create_thread():
    server = MessageServer()
    events = []
    server.subscribe_events(events.append)
    await server.create_thread("test", participants=["a1", "a2"])
    assert len(events) == 1
    assert events[0]["type"] == "create_thread"
    assert events[0]["name"] == "test"


async def test_server_subscribe_events_send_message():
    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    tid = await server.create_thread("t", participants=["a1", "a2"])
    events = []
    server.subscribe_events(events.append)
    await server.send_message(tid, author="a1", content="hello", mentions=["a2"])
    assert len(events) == 1
    assert events[0]["type"] == "send_message"
    assert events[0]["author"] == "a1"
    assert events[0]["content"] == "hello"


async def test_server_subscribe_events_multiple_subscribers():
    server = MessageServer()
    events1 = []
    events2 = []
    server.subscribe_events(events1.append)
    server.subscribe_events(events2.append)
    await server.create_thread("t", participants=["a1"])
    assert len(events1) == 1
    assert len(events2) == 1


async def test_server_subscribe_events_isolates_from_subscribers():
    """Event subscribers and message subscribers are independent."""
    server = MessageServer()
    events = []
    server.subscribe_events(events.append)
    # Regular subscriber (for gates/mirrors) should not receive events
    messages = []
    server.subscribe(messages.append)
    await server.create_thread("t", participants=["a1"])
    assert len(events) == 1
    assert len(messages) == 0  # create_thread doesn't send a message


# ---------------------------------------------------------------------------
# CLI --quiet / --demo → Ink Surface
# ---------------------------------------------------------------------------


def _make_ink_recorder():
    """Stand-in for cli._run_ink_surface that records kwargs."""
    calls: list[dict] = []

    def fake_ink(**kwargs):
        calls.append(dict(kwargs))
        return 0

    return calls, fake_ink


def test_cli_quiet_flag_parsing():
    """--quiet must reach _run_ink_surface(quiet=True) when --config is used."""
    from agent_augury.cli import main

    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink):
        result = main(["--config", "fake.yaml", "--quiet"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["config"] == "fake.yaml"
    assert calls[0]["quiet"] is True
    assert calls[0]["mode"] == "session"


def test_cli_quiet_flag_default_false():
    """Without --quiet, _run_ink_surface must receive quiet=False."""
    from agent_augury.cli import main

    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink):
        result = main(["--config", "fake.yaml"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is False


def test_cli_demo_flag_passed():
    """--demo must reach _run_ink_surface(demo=True) when --config is used."""
    from agent_augury.cli import main

    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink):
        result = main(["--config", "fake.yaml", "--demo"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["demo"] is True
    assert calls[0]["config"] == "fake.yaml"


def test_cli_demo_flag_default_false():
    """Without --demo, _run_ink_surface must receive demo=False."""
    from agent_augury.cli import main

    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink):
        result = main(["--config", "fake.yaml"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["demo"] is False


VALID_MODEL_CONFIG = {
    "mode": "L3",
    "max_steps": 10,
    "agents": [
        {"id": "agent-1", "backend": {"type": "fake", "script": ["done"]}},
    ],
}


def test_wizard_flow_quiet_flag_passed(tmp_path):
    """--quiet in wizard flow must reach _run_ink_surface(quiet=True)."""
    from agent_augury.cli import _run_wizard_flow

    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=VALID_MODEL_CONFIG), \
         patch("builtins.input", return_value=""):
        result = _run_wizard_flow(output_path=out_path, quiet=True)

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is True
    assert calls[0]["config"] == str(out_path)
    assert calls[0]["mode"] == "session"


def test_wizard_flow_quiet_false_by_default(tmp_path):
    """Wizard flow without --quiet must pass quiet=False to Ink."""
    from agent_augury.cli import _run_wizard_flow

    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=VALID_MODEL_CONFIG), \
         patch("builtins.input", return_value=""):
        result = _run_wizard_flow(output_path=out_path)

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is False


def test_wizard_flow_stops_when_api_key_env_missing(tmp_path, monkeypatch):
    """Saved openai/openrouter config without env set must not start the session."""
    from agent_augury.cli import _run_wizard_flow

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {
        "max_steps": 0,
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "model": "anthropic/claude-sonnet-4",
                },
            }
        ],
    }
    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=cfg), \
         patch("builtins.input", return_value=""), \
         patch("getpass.getpass", return_value=""):
        result = _run_wizard_flow(output_path=out_path, quiet=True)

    assert result == 1
    assert calls == []
    assert out_path.exists()


def test_wizard_flow_prompts_for_missing_api_key(tmp_path, monkeypatch):
    """When the env var is unset, prompt for the key and continue the session."""
    from agent_augury.cli import _run_wizard_flow

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {
        "max_steps": 0,
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "model": "anthropic/claude-sonnet-4",
                },
            }
        ],
    }
    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_ink = _make_ink_recorder()
    with patch("agent_augury.cli._run_ink_surface", fake_ink), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=cfg), \
         patch("builtins.input", return_value=""), \
         patch("getpass.getpass", return_value="sk-or-test-key"):
        result = _run_wizard_flow(output_path=out_path, quiet=True)

    assert result == 0
    assert len(calls) == 1
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-test-key"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# D2: _log_tool_event skips server-event-driven tools
# ---------------------------------------------------------------------------


def test_log_tool_event_skips_server_event_tools():
    """send_message/create_thread/read_resource are already printed via
    server events, so _log_tool_event must skip them in the tool branch
    to avoid duplicate output."""
    from contextlib import redirect_stdout

    from agent_augury.cli import _log_tool_event

    event = {
        "type": "tool",
        "agent_id": "agent-1",
        "tool": "send_message",
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_tool_event(event)
    assert buf.getvalue() == ""


def test_log_tool_event_skips_create_tool_event():
    """create_message tool event is skipped (already covered by server event)."""
    from contextlib import redirect_stdout

    from agent_augury.cli import _log_tool_event

    event = {
        "type": "tool",
        "agent_id": "agent-1",
        "tool": "create_thread",
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_tool_event(event)
    assert buf.getvalue() == ""


def test_log_tool_event_skips_read_resource_tool_event():
    """read_resource tool event is skipped (already covered by server event)."""
    from contextlib import redirect_stdout

    from agent_augury.cli import _log_tool_event

    event = {
        "type": "tool",
        "agent_id": "agent-1",
        "tool": "read_resource",
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_tool_event(event)
    assert buf.getvalue() == ""


def test_log_tool_event_file_tools_still_printed():
    """File tools (read_file, write_file, list_directory) are NOT server
    events, so they must still be printed by _log_tool_event."""
    from contextlib import redirect_stdout

    from agent_augury.cli import _log_tool_event

    event = {
        "type": "tool",
        "agent_id": "agent-1",
        "tool": "read_file",
        "args": {"path": "/tmp/test.txt"},
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_tool_event(event)
    output = buf.getvalue()
    assert "read_file" in output
    assert "agent-1" in output
