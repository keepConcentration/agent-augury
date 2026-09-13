"""M7 SessionStdioRunner — real Core session over JSONL (no Node required)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from agent_augury.gateway.stdio import decode_line, encode_line
from agent_augury.gateway.types import make_command

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
DEMO = REPO_ROOT / "examples" / "demo.yaml"


def _env() -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items()},
        "PYTHONPATH": str(SRC),
        "PYTHONUTF8": "1",
    }


def test_session_stdio_no_auto_start_quit():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_augury.gateway.session_stdio",
            "--config",
            str(DEMO),
            "--demo",
            "--no-auto-start",
        ],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    assert proc.stdin is not None and proc.stdout is not None

    started = decode_line(proc.stdout.readline())
    assert started["type"] == "session.started"
    assert isinstance(started.get("agents"), list)
    assert len(started["agents"]) >= 1
    ready = decode_line(proc.stdout.readline())
    assert ready["type"] == "log"

    proc.stdin.write(encode_line(make_command("session.quit", id="1")) + "\n")
    proc.stdin.flush()

    ended = None
    for _ in range(10):
        line = proc.stdout.readline()
        if not line:
            break
        msg = decode_line(line)
        if msg.get("type") == "session.ended":
            ended = msg
            break
    assert ended is not None
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def test_session_stdio_auto_start_demo_then_quit():
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_augury.gateway.session_stdio",
            "--config",
            str(DEMO),
            "--demo",
        ],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=_env(),
    )
    assert proc.stdin is not None and proc.stdout is not None

    started = decode_line(proc.stdout.readline())
    assert started["type"] == "session.started"
    assert isinstance(started.get("agents"), list)
    assert len(started["agents"]) >= 1

    saw_work = False
    for _ in range(80):
        line = proc.stdout.readline()
        if not line:
            break
        msg = decode_line(line)
        typ = msg.get("type")
        if typ in ("agent.step", "message", "thread.created", "tool"):
            saw_work = True
        if typ == "log" and "session:" in str(msg.get("text", "")):
            break

    assert saw_work, (proc.stderr.read() if proc.stderr else "")

    proc.stdin.write(encode_line(make_command("session.quit", id="2")) + "\n")
    proc.stdin.flush()

    ended = False
    for _ in range(20):
        line = proc.stdout.readline()
        if not line:
            break
        if decode_line(line).get("type") == "session.ended":
            ended = True
            break
    assert ended
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0


def test_cli_force_ink_with_config():
    from agent_augury.cli import main

    with patch("agent_augury.cli._run_ink_surface", return_value=0) as ink:
        result = main(["--ink", "--config", "examples/demo.yaml", "--demo"])
    assert result == 0
    ink.assert_called_once()
    kwargs = ink.call_args.kwargs
    assert kwargs["mode"] == "session"
    assert kwargs["config"] == "examples/demo.yaml"
    assert kwargs["demo"] is True


def test_cli_plain_surface_removed():
    """Plain REPL is gone — --config always launches Ink (patched here)."""
    from agent_augury.cli import main

    with patch("agent_augury.cli._run_ink_surface", return_value=0) as ink:
        result = main(["--config", "fake.yaml", "--demo"])
    assert result == 0
    ink.assert_called_once()
    kwargs = ink.call_args.kwargs
    assert kwargs["mode"] == "session"
    assert kwargs["config"] == "fake.yaml"
    assert kwargs["demo"] is True

