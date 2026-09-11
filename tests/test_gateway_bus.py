"""SessionGateway M1 tests — fan-out + observe-only human.* rejection."""

from __future__ import annotations

import pytest

from agent_augury.gateway import (
    SessionGateway,
    SurfaceSubscription,
    make_command,
    make_event,
)
from agent_augury.gateway.types import WireError, validate_message


def test_make_event_and_validate():
    ev = make_event("log", text="hello")
    assert ev["dir"] == "event"
    assert validate_message(ev)["text"] == "hello"


def test_unknown_event_type_rejected():
    with pytest.raises(WireError):
        make_event("nope.thing")


def test_fan_out_to_ui_and_chat_observe():
    gw = SessionGateway()
    ui_inbox: list[dict] = []
    chat_inbox: list[dict] = []

    gw.attach(
        SurfaceSubscription(
            name="ink",
            mode="interact",
            family="ui",
            on_event=ui_inbox.append,
        )
    )
    gw.attach(
        SurfaceSubscription(
            name="discord",
            mode="observe",
            family="chat",
            on_event=chat_inbox.append,
        )
    )

    n = gw.publish(make_event("log", text="line"))
    assert n == 2
    assert ui_inbox[0]["text"] == "line"
    assert chat_inbox[0]["text"] == "line"


def test_event_type_filter():
    gw = SessionGateway()
    inbox: list[dict] = []
    gw.attach(
        SurfaceSubscription(
            name="discord",
            mode="observe",
            family="chat",
            on_event=inbox.append,
            event_types=frozenset({"log", "human.question"}),
        )
    )
    assert gw.publish(make_event("agent.step", agent_id="a", result={})) == 0
    assert gw.publish(make_event("log", text="x")) == 1
    assert len(inbox) == 1


def test_observe_surface_cannot_human_send():
    gw = SessionGateway(on_command=lambda _c: {"handled": True})
    gw.attach(
        SurfaceSubscription(name="discord", mode="observe", family="chat")
    )
    cmd = make_command("human.send", id="1", content="hi")
    result = gw.dispatch(cmd, surface="discord")
    assert result["ok"] is False
    assert "observe-only" in result["error"]


def test_interact_surface_can_human_send():
    seen: list[dict] = []

    def handler(cmd: dict) -> dict:
        seen.append(cmd)
        return {"queued": True}

    gw = SessionGateway(on_command=handler)
    gw.attach(SurfaceSubscription(name="ink", mode="interact", family="ui"))
    cmd = make_command("human.send", id="2", content="hi", thread_id="t1")
    result = gw.dispatch(cmd, surface="ink")
    assert result["ok"] is True
    assert result["queued"] is True
    assert seen[0]["content"] == "hi"


def test_unknown_surface_dispatch():
    gw = SessionGateway(on_command=lambda _c: None)
    cmd = make_command("session.quit", id="3")
    result = gw.dispatch(cmd, surface="ghost")
    assert result["ok"] is False
    assert "unknown surface" in result["error"]


def test_detach():
    gw = SessionGateway()
    gw.attach(
        SurfaceSubscription(name="ink", on_event=lambda _e: None)
    )
    assert gw.surfaces() == ["ink"]
    gw.detach("ink")
    assert gw.surfaces() == []
    assert gw.publish(make_event("log", text="x")) == 0
