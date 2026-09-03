"""Tests for broadcast event logging and --quiet flag."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_augury.cli import BroadcastLogger, _mask_sensitive
from agent_augury.server import MessageServer


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
# BroadcastLogger
# ---------------------------------------------------------------------------


def test_broadcast_logger_create_thread():
    logger = BroadcastLogger()
    event = {
        "type": "create_thread",
        "thread_id": "thread-1",
        "name": "plan",
        "participants": ["agent-1", "agent-2"],
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    assert "[thread-1] create_thread plan (agent-1, agent-2)" in output


def test_broadcast_logger_send_message():
    logger = BroadcastLogger()
    # Pre-populate thread name
    logger._seen_threads["thread-1"] = "plan"
    event = {
        "type": "send_message",
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "author": "agent-1",
        "content": "hello world",
        "mentions": ["agent-2"],
        "delivered_to": ["agent-2"],
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    # No ANSI codes — plain text format
    assert "\033[" not in output
    assert "hello world" in output
    assert "[agent-1 → agent-2][thread-1]" in output


def test_broadcast_logger_send_message_no_truncation():
    logger = BroadcastLogger()
    logger._seen_threads["thread-1"] = "plan"
    long_content = "a" * 100
    event = {
        "type": "send_message",
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "author": "agent-1",
        "content": long_content,
        "mentions": [],
        "delivered_to": ["agent-2", "agent-3"],
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    assert long_content in output
    assert "[agent-1 → agent-2, agent-3][thread-1]" in output
    # ANSI codes add overhead; check content length separately
    content_start = output.find(long_content)
    assert content_start != -1


def test_broadcast_logger_send_message_masks_sensitive():
    logger = BroadcastLogger()
    logger._seen_threads["thread-1"] = "plan"
    event = {
        "type": "send_message",
        "message_id": "msg-1",
        "thread_id": "thread-1",
        "author": "agent-1",
        "content": "my token=secret123",
        "mentions": [],
        "delivered_to": ["agent-2"],
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    assert "secret123" not in output
    assert "***" in output
    assert "[agent-1 → agent-2][thread-1]" in output


def test_broadcast_logger_read_resource():
    logger = BroadcastLogger()
    event = {
        "type": "read_resource",
        "agent_id": "agent-1",
        "threads": 3,
        "messages": 10,
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    assert "📊 agent-1: read_resource (threads=3, messages=10)" in output


def test_broadcast_logger_quiet_mode():
    logger = BroadcastLogger(quiet=True)
    event = {
        "type": "create_thread",
        "thread_id": "thread-1",
        "name": "plan",
        "participants": ["agent-1"],
        "timestamp": 1234567890,
    }
    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        logger(event)
        output = mock_stderr.getvalue()
    assert output == ""


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
# CLI --quiet flag
# ---------------------------------------------------------------------------


def _make_run_recorder():
    """Build an async stand-in for cli._run that records its arguments.

    ``main()`` / ``_run_wizard_flow()`` call ``asyncio.run(_run(...))``;
    substituting ``_run`` with this coroutine function lets us assert that
    ``quiet`` (and the config path / initial task) actually reach the run
    layer instead of only checking that ``asyncio.run`` was invoked.
    """
    calls = []

    async def fake_run(cfg_path, initial_prompt=None, *, quiet=False):
        calls.append({"cfg_path": cfg_path, "initial_prompt": initial_prompt, "quiet": quiet})
        return 0

    return calls, fake_run


def test_cli_quiet_flag_parsing():
    """--quiet must reach _run(quiet=True) when --config is used (T1)."""
    from agent_augury.cli import main

    calls, fake_run = _make_run_recorder()
    with patch("agent_augury.cli._run", fake_run):
        result = main(["--config", "fake.yaml", "--quiet"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["cfg_path"] == "fake.yaml"
    assert calls[0]["quiet"] is True
    assert calls[0]["initial_prompt"] is None


def test_cli_quiet_flag_default_false():
    """Without --quiet, _run must receive quiet=False (T1 default)."""
    from agent_augury.cli import main

    calls, fake_run = _make_run_recorder()
    with patch("agent_augury.cli._run", fake_run):
        result = main(["--config", "fake.yaml"])

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is False


VALID_MODEL_CONFIG = {
    "mode": "L3",
    "max_steps": 10,
    "agents": [
        {"id": "agent-1", "backend": {"type": "fake", "script": ["done"]}},
    ],
}


def test_wizard_flow_quiet_flag_passed(tmp_path):
    """--quiet in wizard flow must reach _run(quiet=True) (T2/T8).

    Uses a *valid* saved model config (non-empty agents) so the reuse
    branch is exercised with a config that would actually load — the old
    ``{"agents": []}`` mock masked the real ConfigError edge (T8).
    """
    from agent_augury.cli import _run_wizard_flow

    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_run = _make_run_recorder()
    with patch("agent_augury.cli._run", fake_run), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=VALID_MODEL_CONFIG), \
         patch("builtins.input", return_value="test task"):
        result = _run_wizard_flow(output_path=out_path, quiet=True)

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is True
    assert calls[0]["cfg_path"] == str(out_path)
    assert calls[0]["initial_prompt"] == "test task"


def test_wizard_flow_quiet_false_by_default(tmp_path):
    """Wizard flow without --quiet must pass quiet=False to _run (T2)."""
    from agent_augury.cli import _run_wizard_flow

    out_path = tmp_path / "wizard_out.yaml"
    calls, fake_run = _make_run_recorder()
    with patch("agent_augury.cli._run", fake_run), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=VALID_MODEL_CONFIG), \
         patch("builtins.input", return_value="test task"):
        result = _run_wizard_flow(output_path=out_path)

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["quiet"] is False
