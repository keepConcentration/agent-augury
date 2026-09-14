"""Chat surface Wire formatting (Discord/Slack — not Ink)."""

from __future__ import annotations

from agent_augury.channel.chat_surface_format import format_wire_for_chat_surface
from agent_augury.gateway.types import make_event


def test_agent_step_per_bot_is_body_only():
    text = format_wire_for_chat_surface(
        make_event(
            "agent.step",
            agent_id="agent-3",
            result={"text": "안녕하세요! 무엇을 도와드릴까요?"},
        ),
        recipient_agent_id="agent-3",
    )
    assert text == "안녕하세요! 무엇을 도와드릴까요?"
    assert "agent-3" not in text
    assert "💭" not in text


def test_split_discord_content_prefers_newlines():
    from agent_augury.channel.discord_bot import split_discord_content

    body = ("line\n" * 50) + ("x" * 2000)
    parts = split_discord_content(body, limit=500)
    assert len(parts) >= 2
    assert all(len(p) <= 500 for p in parts)
    assert "".join(parts).replace("\n", "") in body.replace("\n", "")


def test_agent_step_shared_webhook_keeps_agent_label():
    text = format_wire_for_chat_surface(
        make_event(
            "agent.step",
            agent_id="agent-3",
            result={"text": "hello"},
        ),
        recipient_agent_id=None,
    )
    assert text == "agent-3: hello"


def test_human_question_per_bot_no_agent_prefix():
    text = format_wire_for_chat_surface(
        make_event("human.question", agent_id="agent-1", question="Pick one"),
        recipient_agent_id="agent-1",
    )
    assert text == "Pick one"
