"""SessionBridge.bind_session + bootstrap_gateway_host (G1–G2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_augury.gateway.bridge import BridgeBindOptions, SessionBridge
from agent_augury.gateway.bus import SessionGateway, SurfaceSubscription
from agent_augury.gateway.host import bootstrap_gateway_host


@dataclass
class _FakeStepResult:
    text: str = "hello"


class _FakeSession:
    def __init__(self) -> None:
        self.on_step: Any = None


def _bridge_with_inbox() -> tuple[SessionBridge, list[dict[str, Any]]]:
    gw = SessionGateway()
    inbox: list[dict[str, Any]] = []
    gw.attach(
        SurfaceSubscription(name="test", mode="observe", on_event=inbox.append)
    )
    return SessionBridge(gateway=gw), inbox


def test_bind_session_publishes_agent_step():
    bridge, inbox = _bridge_with_inbox()
    session = _FakeSession()
    bridge.bind_session(session)

    assert session.on_step is not None
    session.on_step("a1", _FakeStepResult(text="hi"))

    assert len(inbox) == 1
    assert inbox[0]["type"] == "agent.step"
    assert inbox[0]["agent_id"] == "a1"
    assert inbox[0]["result"]["text"] == "hi"


def test_bind_session_chains_prev_on_step():
    bridge, inbox = _bridge_with_inbox()
    session = _FakeSession()
    seen: list[str] = []

    def prev(_agent_id: str, _result: Any) -> None:
        seen.append("prev")

    session.on_step = prev
    bridge.bind_session(session)
    session.on_step("a1", _FakeStepResult())

    assert len(inbox) == 1
    assert seen == ["prev"]


def test_bind_session_suppress_agent_steps():
    bridge, inbox = _bridge_with_inbox()
    session = _FakeSession()
    bridge.bind_session(
        session,
        options=BridgeBindOptions(suppress_agent_steps=True),
    )
    session.on_step("a1", _FakeStepResult())
    assert inbox == []


def test_bind_session_wire_agent_steps_false():
    bridge, inbox = _bridge_with_inbox()
    session = _FakeSession()
    bridge.bind_session(
        session,
        options=BridgeBindOptions(wire_agent_steps=False),
    )
    assert session.on_step is None
    assert inbox == []


def test_bind_session_idempotent():
    bridge, inbox = _bridge_with_inbox()
    session = _FakeSession()
    bridge.bind_session(session)
    first = session.on_step
    bridge.bind_session(session)
    assert session.on_step is first
    session.on_step("a1", _FakeStepResult())
    assert len(inbox) == 1


def test_bootstrap_gateway_host_installs_command_handler():
    gw = SessionGateway()
    bridge = SessionBridge(gateway=gw)
    session = _FakeSession()
    bootstrap_gateway_host(session, bridge)
    assert gw.on_command is not None
    assert getattr(gw.on_command, "__self__", None) is bridge
    assert bridge.session is session
