"""gateway/register.py — chat/UI surface SSOT (G3)."""

from __future__ import annotations

from agent_augury.gateway.bus import SessionGateway
from agent_augury.gateway.register import register_chat_surface, register_ui_surface
from agent_augury.gateway.types import make_event


def test_register_chat_surface_swallows_handler_errors():
    gw = SessionGateway()
    ok: list[str] = []

    def bad(_event: dict) -> None:
        raise RuntimeError("boom")

    def good(event: dict) -> None:
        ok.append(str(event.get("type")))

    register_chat_surface(
        gw,
        name="chat-a",
        mode="observe",
        on_event=bad,
        event_types=frozenset({"message"}),
    )
    register_chat_surface(
        gw,
        name="chat-b",
        mode="observe",
        on_event=good,
        event_types=frozenset({"message"}),
    )

    delivered = gw.publish(
        make_event("message", thread_id="t", content="hi", author="a1")
    )
    assert delivered == 2
    assert ok == ["message"]


def test_register_ui_surface_sets_family_ui():
    gw = SessionGateway()
    register_ui_surface(
        gw,
        name="ink",
        mode="interact",
        on_event=lambda _e: None,
    )
    sub = gw._surfaces["ink"]
    assert sub.family == "ui"
    assert sub.mode == "interact"


def test_register_chat_surface_interact_shell_no_handler():
    gw = SessionGateway()
    register_chat_surface(
        gw,
        name="discord-inbound",
        mode="interact",
        on_event=None,
        event_types=None,
    )
    sub = gw._surfaces["discord-inbound"]
    assert sub.family == "chat"
    assert sub.on_event is None
