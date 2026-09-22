"""M7 SessionStdioRunner — real Core session over JSONL (no Node required)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from agent_augury.gateway.stdio import decode_line, encode_line
from agent_augury.gateway.types import make_command

from .conftest import popen_python_module

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
    proc = popen_python_module(
        [
            "-m",
            "agent_augury.gateway.session_stdio",
            "--config",
            str(DEMO),
            "--demo",
            "--no-auto-start",
        ],
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
    proc = popen_python_module(
        [
            "-m",
            "agent_augury.gateway.session_stdio",
            "--config",
            str(DEMO),
            "--demo",
        ],
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


def test_file_root_prefers_launch_dir_over_cwd(tmp_path, monkeypatch):
    """Wheel install: the gateway's own cwd is the Ink cache, so the sandbox
    must come from the CLI's launch dir (live session 2026-09-22)."""
    from agent_augury.gateway import session_stdio as m

    launch = tmp_path / "my-project"
    launch.mkdir()
    cache = tmp_path / "ink-cache"
    cache.mkdir()
    monkeypatch.chdir(cache)
    monkeypatch.setattr(m, "resolve_project_root", lambda: None)

    monkeypatch.setenv(m.FILE_ROOT_ENV, str(launch))
    assert m._resolve_file_root() == (launch.resolve(), "launch directory")

    monkeypatch.delenv(m.FILE_ROOT_ENV)
    root, source = m._resolve_file_root()
    assert source == "CWD guess"
    assert root == cache.resolve()


def test_cli_passes_launch_dir_to_gateway(tmp_path, monkeypatch):
    """cli.py must hand its cwd down; without it the child cannot know it."""
    from pathlib import Path

    from agent_augury import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "ensure_ink_front", lambda: (tmp_path, None))
    monkeypatch.setattr(cli, "_ink_tsx_command", lambda d: ["true"])
    monkeypatch.setattr(cli, "_clear_tty", lambda: None)
    captured: dict = {}

    def fake_call(cmd, cwd=None, env=None):
        captured.update(env or {})
        return 0

    monkeypatch.setattr("subprocess.call", fake_call)
    cli._run_ink_surface(mode="hello")
    assert captured["AUGURY_FILE_ROOT"] == str(Path(tmp_path).resolve())


def test_cli_config_launches_ink_session():
    from agent_augury.cli import main

    with patch("agent_augury.cli._run_ink_surface", return_value=0) as ink:
        result = main(["--config", "examples/demo.yaml", "--demo"])
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
