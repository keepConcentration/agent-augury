"""Minimal Gateway hello + HITL demo for the Ink Surface (M2/M3).

Run as child of ``fronts/ink``:

```text
python -m agent_augury.gateway.hello_demo
```

Speaks Augury Wire JSONL on stdin/stdout. See ``stdio.py``.
"""

from __future__ import annotations

from .bridge import SessionBridge
from .bus import SessionGateway
from .stdio import JsonlStdioBridge
from .types import make_event


def main(argv: list[str] | None = None) -> int:
    from .secrets import load_gateway_secrets

    load_gateway_secrets()
    _ = argv
    gateway = SessionGateway()

    def send_fn(
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
        source: dict | None = None,
    ) -> str:
        _ = source
        who = ",".join(mentions) if mentions else "*"
        gateway.publish(
            make_event("log", text=f"human->{thread_id} [{who}]: {content}")
        )
        return "ok"

    bridge = SessionBridge(gateway=gateway, send_fn=send_fn)
    bridge.install()

    stdio = JsonlStdioBridge(gateway, surface="ink")
    stdio.attach()

    gateway.publish(
        make_event(
            "session.started",
            surface="ink",
            note="augury gateway hello (M3 HITL)",
            agents=["demo-agent"],
        )
    )
    gateway.publish(
        make_event(
            "log",
            text=(
                "Ink HITL ready - answer with 1/2, /skip, Ctrl+C interrupt, /quit"
            ),
        )
    )
    bridge.set_running(True)
    bridge.publish_ask_user(
        agent_id="demo-agent",
        thread_id="thread-demo",
        question="Pick a direction for the demo",
        options=["Explore the codebase", "Write a design note", "Skip for now"],
        question_id="q-demo-1",
    )
    return stdio.run()


if __name__ == "__main__":
    raise SystemExit(main())
