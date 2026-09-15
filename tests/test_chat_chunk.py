"""Hermes-style chat chunk splitter (D2)."""

from __future__ import annotations

from agent_augury.channels.chunk import split_chat_content
from agent_augury.channels.slack.mirror import SlackWebhookMirror, _MAX_CONTENT


def test_short_message_unchanged():
    assert split_chat_content("hello", limit=100) == ["hello"]


def test_empty_returns_empty():
    assert split_chat_content("") == []
    assert split_chat_content("   ") == ["   "]  # whitespace preserved; transport may strip


def test_slack_enqueue_splits_long_text():
    mirror = SlackWebhookMirror(webhook_url="https://example.test/hook")
    mirror.enqueue_text("y" * (_MAX_CONTENT + 200))
    assert len(mirror.outbox) >= 2
    assert all(len(c) <= _MAX_CONTENT for c in mirror.outbox)


def test_indicators_on_multi_chunk():
    parts = split_chat_content("a\n" * 200, limit=80)
    assert len(parts) >= 2
    assert parts[0].endswith(f"(1/{len(parts)})")
    assert parts[-1].endswith(f"({len(parts)}/{len(parts)})")


def test_indicators_can_be_disabled():
    parts = split_chat_content("a\n" * 200, limit=80, indicators=False)
    assert len(parts) >= 2
    assert "(1/" not in parts[0]
