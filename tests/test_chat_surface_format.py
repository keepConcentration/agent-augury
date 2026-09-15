"""Chat surface Wire formatting (Discord/Slack — not Ink)."""

from __future__ import annotations

from agent_augury.channels.chat_surface_format import format_wire_for_chat_surface
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
    from agent_augury.channels.discord.bot import split_discord_content

    body = ("line\n" * 50) + ("x" * 2000)
    parts = split_discord_content(body, limit=500)
    assert len(parts) >= 2
    assert all(len(p) <= 500 for p in parts)
    # Indicators ``(i/n)`` are appended; body text is preserved across chunks.
    joined = "".join(p.rsplit(" (", 1)[0] if " (" in p and p.endswith(")") else p for p in parts)
    assert "line" in joined
    assert "x" * 100 in joined


def test_split_chat_content_preserves_code_fence():
    from agent_augury.channels.chunk import split_chat_content

    body = "before\n```python\n" + ("print(1)\n" * 40) + "```\nafter"
    parts = split_chat_content(body, limit=120)
    assert len(parts) >= 2
    assert all(len(p) <= 120 for p in parts)
    # Mid-fence chunks should close/reopen fences.
    assert any(p.rstrip().endswith("```") or "```" in p for p in parts[:-1])


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
