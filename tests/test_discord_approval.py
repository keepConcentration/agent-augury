"""Discord approval button helpers."""

from __future__ import annotations

from agent_augury.channel.discord_approval import (
    ToolApprovalView,
    approval_prompt_text,
    make_approval_custom_id,
    parse_approval_custom_id,
)
from agent_augury.gateway.types import make_event


def test_custom_id_roundtrip():
    cid = make_approval_custom_id("fbd0c0af-ccbb-4086-a76d-0874099e5187", "granted")
    assert parse_approval_custom_id(cid) == (
        "fbd0c0af-ccbb-4086-a76d-0874099e5187",
        "granted",
    )
    cid2 = make_approval_custom_id("ap-1", "denied")
    assert parse_approval_custom_id(cid2) == ("ap-1", "denied")
    assert parse_approval_custom_id("other") is None


def test_approval_prompt_mentions_buttons():
    text = approval_prompt_text(
        make_event(
            "approval.request",
            approval_id="ap-1",
            agent_id="agent-1",
            tool="write_file",
            args_preview={"path": "C:/tmp/x.md"},
        ),
        recipient_agent_id="agent-1",
    )
    assert "write_file" in text
    assert "buttons" in text.lower()
    assert "1/approve" not in text


def test_approval_view_has_two_buttons():
    import asyncio

    async def _run():
        view = ToolApprovalView("ap-9")
        assert len(view.children) == 2
        ids = {getattr(c, "custom_id", "") for c in view.children}
        assert make_approval_custom_id("ap-9", "granted") in ids
        assert make_approval_custom_id("ap-9", "denied") in ids

    asyncio.run(_run())


def test_observe_routes_approval_with_view():
    from unittest.mock import MagicMock, patch

    from agent_augury.channel.discord_bot import BotManager, DiscordBotAdapter, OutboundMessage
    from agent_augury.channel.discord_observe import attach_discord_bots
    from agent_augury.gateway import SessionGateway, make_event

    gw = SessionGateway()
    mgr = BotManager()
    client = MagicMock()
    client.event = lambda func: func
    with patch("agent_augury.channel.discord_bot.discord.Client", return_value=client):
        bot = DiscordBotAdapter(agent_id="agent-1", token="t", channel_id=1)
    mgr.register(bot)
    attach_discord_bots(gw, mgr)
    gw.publish(
        make_event(
            "approval.request",
            approval_id="ap-42",
            agent_id="agent-1",
            tool="run_command",
            args_preview={"command": "ls"},
        )
    )
    item = bot._outbox.get_nowait()
    assert isinstance(item, OutboundMessage)
    assert item.view_factory is not None
    assert "run_command" in item.content
    assert "ls" in item.content
    import asyncio

    async def _make():
        return item.view_factory()

    view = asyncio.run(_make())
    assert view is not None
    assert len(view.children) == 2
