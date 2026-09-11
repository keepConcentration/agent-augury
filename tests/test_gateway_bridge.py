"""M3 SessionBridge + translate_core_event tests."""

from __future__ import annotations

from agent_augury.gateway import (
    SessionBridge,
    SessionGateway,
    SurfaceSubscription,
    make_command,
    translate_core_event,
)


def test_translate_ask_user_to_human_question():
    wire = translate_core_event(
        {
            "type": "tool",
            "tool": "ask_user",
            "agent_id": "a1",
            "args": {
                "thread": "t1",
                "question": "Go?",
                "options": ["yes", "no"],
            },
        }
    )
    assert wire is not None
    assert wire["type"] == "human.question"
    assert wire["question"] == "Go?"
    assert wire["options"] == ["yes", "no"]
    assert wire["thread_id"] == "t1"
    assert wire["question_id"]


def test_translate_step():
    class R:
        def __init__(self) -> None:
            self.text = "hi"
            self.tool_calls: list = []

    wire = translate_core_event({"type": "step", "agent_id": "a", "result": R()})
    assert wire is not None
    assert wire["type"] == "agent.step"
    assert wire["result"]["text"] == "hi"


def test_bridge_publishes_question_and_answers():
    gw = SessionGateway()
    inbox: list[dict] = []
    gw.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=inbox.append)
    )
    sent: list[tuple] = []

    def send_fn(thread_id: str, content: str, *, mentions=None):
        sent.append((thread_id, content, mentions))
        return "ok"

    bridge = SessionBridge(gateway=gw, send_fn=send_fn)
    bridge.install()
    bridge.publish_ask_user(
        agent_id="agent-1",
        thread_id="thr",
        question="Choose",
        options=["A", "B"],
        question_id="q1",
    )
    assert any(e["type"] == "human.question" for e in inbox)
    assert bridge.pending is not None

    result = gw.dispatch(
        make_command("human.answer", id="1", content="2", question_id="q1"),
        surface="ink",
    )
    assert result["ok"] is True
    assert sent == [("thr", "B", ["agent-1"])]
    assert bridge.pending is None


def test_bridge_skip_and_interrupt():
    gw = SessionGateway()
    inbox: list[dict] = []
    gw.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=inbox.append)
    )
    interrupted = {"v": False}

    bridge = SessionBridge(
        gateway=gw,
        send_fn=lambda *_a, **_k: "ok",
        on_interrupt=lambda: interrupted.__setitem__("v", True),
    )
    bridge.install()
    bridge.set_running(True)
    bridge.publish_ask_user(
        agent_id="a",
        thread_id="t",
        question="Q",
        options=["x"],
        question_id="q",
    )
    assert gw.dispatch(make_command("human.skip", id="1"), surface="ink")["ok"]
    assert bridge.pending is None

    assert gw.dispatch(
        make_command("session.interrupt", id="2"), surface="ink"
    )["ok"]
    assert interrupted["v"] is True
    assert any(
        e.get("type") == "log" and "interrupted" in str(e.get("text", ""))
        for e in inbox
    )


def test_bridge_quit_publishes_ended():
    gw = SessionGateway()
    inbox: list[dict] = []
    gw.attach(
        SurfaceSubscription(name="ink", mode="interact", on_event=inbox.append)
    )
    quit_hit = {"v": False}
    bridge = SessionBridge(
        gateway=gw,
        send_fn=lambda *_a, **_k: "ok",
        on_quit=lambda: quit_hit.__setitem__("v", True),
    )
    bridge.install()
    result = gw.dispatch(make_command("session.quit", id="9"), surface="ink")
    assert result["ok"] is True
    assert quit_hit["v"] is True
    assert any(e.get("type") == "session.ended" for e in inbox)


def test_human_send_requires_thread():
    gw = SessionGateway()
    gw.attach(SurfaceSubscription(name="ink", mode="interact"))
    bridge = SessionBridge(gateway=gw, send_fn=lambda *_a, **_k: "ok")
    bridge.install()
    result = gw.dispatch(
        make_command("human.send", id="1", content="hi"),
        surface="ink",
    )
    assert result["ok"] is True
    # handler returns queued False via payload - make_result merges
    assert result.get("queued") is False
