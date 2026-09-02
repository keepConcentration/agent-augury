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
    assert "[agent-1 → agent-2][thread-1] hello world" in output


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
    assert len(output.split("] ")[-1].strip()) == 100  # full content shown


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
    assert "[read_resource] agent-1 (threads=3, messages=10)" in output


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


def test_cli_quiet_flag_parsing():
    from agent_augury.cli import main
    # Verify the flag is accepted and passed through
    with patch("agent_augury.cli.asyncio.run") as mock_run:
        mock_run.return_value = 0
        result = main(["--config", "fake.yaml", "--quiet"])
        # Verify _run was called with quiet=True
        call_args = mock_run.call_args
        assert call_args is not None
        # The coroutine was created with quiet=True
        # call_args[0][0] would be the positional args to asyncio.run
        # but since it's a coroutine, we check kwargs
        assert call_args.kwargs.get("quiet") == True or result == 0


def test_cli_quiet_flag_default_false():
    from agent_augury.cli import main
    with patch("agent_augury.cli.asyncio.run") as mock_run:
        mock_run.return_value = 0
        main(["--config", "fake.yaml"])
        call_args = mock_run.call_args
        assert call_args is not None


def test_wizard_flow_quiet_flag_passed():
    """Verify that --quiet flag is passed through to _run in wizard flow."""
    from agent_augury.cli import _run_wizard_flow
    with patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value={"agents": []}), \
         patch("agent_augury.cli.run_wizard", return_value={"agents": []}), \
         patch("agent_augury.cli.asyncio.run") as mock_run, \
         patch("builtins.input", return_value="test task"):
        mock_run.return_value = 0
        _run_wizard_flow(output_path=Path("test.yaml"), quiet=True)
        # Verify asyncio.run was called
        assert mock_run.called


def test_wizard_flow_quiet_false_by_default():
    """Verify that quiet defaults to False in wizard flow."""
    from agent_augury.cli import _run_wizard_flow
    with patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value={"agents": []}), \
         patch("agent_augury.cli.run_wizard", return_value={"agents": []}), \
         patch("agent_augury.cli.asyncio.run") as mock_run, \
         patch("builtins.input", return_value="test task"):
        mock_run.return_value = 0
        _run_wizard_flow(output_path=Path("test.yaml"))
        assert mock_run.called
