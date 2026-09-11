"""M2 JSONL stdio bridge tests (no Node required)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from agent_augury.gateway import (
    JsonlStdioBridge,
    SessionGateway,
    decode_line,
    encode_line,
    make_command,
    make_event,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def test_encode_decode_roundtrip():
    ev = make_event("log", text="hi")
    line = encode_line(ev)
    assert "\n" not in line
    assert decode_line(line)["text"] == "hi"


def test_bridge_fanout_and_echo():
    out = io.StringIO()
    gateway = SessionGateway()

    def on_command(cmd: dict) -> dict:
        if cmd["type"] == "human.send":
            gateway.publish(make_event("log", text=f"echo: {cmd['content']}"))
            return {"queued": True}
        if cmd["type"] == "session.quit":
            gateway.publish(make_event("session.ended", reason="quit"))
            return {}
        return {}

    gateway.on_command = on_command
    bridge = JsonlStdioBridge(gateway, surface="ink", write=out.write)
    bridge.attach()
    gateway.publish(make_event("session.started"))

    cmds = [
        encode_line(make_command("human.send", id="1", content="ping")) + "\n",
        encode_line(make_command("session.quit", id="2")) + "\n",
    ]
    code = bridge.run(cmds)
    assert code == 0

    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    dirs = [m["dir"] for m in lines]
    assert "event" in dirs and "result" in dirs
    texts = [m.get("text") for m in lines if m.get("type") == "log"]
    assert "echo: ping" in texts
    assert any(m.get("type") == "session.ended" for m in lines)


def test_hello_demo_subprocess_jsonl():
    env = {
        **dict(**{k: v for k, v in __import__("os").environ.items()}),
        "PYTHONPATH": str(SRC),
        "PYTHONUTF8": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_augury.gateway.hello_demo"],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None

    # Read startup events (session.started + log + human.question)
    started = decode_line(proc.stdout.readline())
    assert started["type"] == "session.started"
    ready = decode_line(proc.stdout.readline())
    assert ready["type"] == "log"
    question = decode_line(proc.stdout.readline())
    assert question["type"] == "human.question"
    assert question["options"]

    proc.stdin.write(
        encode_line(
            make_command(
                "human.answer",
                id="a",
                content="1",
                question_id=question["question_id"],
            )
        )
        + "\n"
    )
    proc.stdin.flush()
    echo = decode_line(proc.stdout.readline())
    assert echo["type"] == "log"
    assert "Explore" in echo["text"] or "human->" in echo["text"]
    result = decode_line(proc.stdout.readline())
    assert result["dir"] == "result" and result["ok"] is True

    proc.stdin.write(encode_line(make_command("session.quit", id="b")) + "\n")
    proc.stdin.flush()
    # interrupt log + session.ended + result (order: interrupt log, ended, result)
    lines = []
    for _ in range(3):
        lines.append(decode_line(proc.stdout.readline()))
    types = [m.get("type") or m.get("dir") for m in lines]
    assert "session.ended" in types or any(
        m.get("type") == "session.ended" for m in lines
    )
    assert any(m.get("dir") == "result" and m.get("ok") for m in lines)

    proc.stdin.close()
    code = proc.wait(timeout=5)
    assert code == 0
