"""Headless Core runner + CLI flag tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent_augury.gateway import (
    HeadlessRunner,
    SurfaceSubscription,
    emit_startup_warnings,
    make_command,
)
from agent_augury.gateway.headless import run_headless_session
from agent_augury.core.session import Session
from tests.conftest import build_cfg


def _fake_session(tmp_path, *, task: str = "hello", max_steps: int = 4) -> Session:
    cfg = build_cfg(
        max_steps=max_steps,
        task=task,
        human={"id": "human"},
        agents=[
            {
                "id": "a1",
                "backend": {
                    "type": "fake",
                    "script": ["done"],
                },
            }
        ],
    )
    return Session.from_config(cfg, allowed_roots=[str(tmp_path)], approval_bypass=True)


def test_cli_headless_defaults_to_wizard_config(tmp_path, monkeypatch):
    from agent_augury import cli
    from agent_augury.cli import main

    cfg = tmp_path / "agent-augury-session.yaml"
    cfg.write_text("max_steps: 1\nagents: []\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_DEFAULT_OUTPUT_PATH", cfg)

    with patch(
        "agent_augury.gateway.headless.run_headless_session", return_value=0
    ) as run:
        assert main(["--headless"]) == 0
    run.assert_called_once()
    assert run.call_args.args[0] == str(cfg)


def test_cli_headless_missing_default_config(tmp_path, monkeypatch, capsys):
    from agent_augury import cli
    from agent_augury.cli import main

    missing = tmp_path / "missing.yaml"
    monkeypatch.setattr(cli, "_DEFAULT_OUTPUT_PATH", missing)
    assert main(["--headless"]) == 1
    err = capsys.readouterr().err
    assert "no config" in err
    assert str(missing) in err


def test_cli_headless_reconfigure_runs_wizard_then_headless(tmp_path, monkeypatch):
    from agent_augury import cli
    from agent_augury.cli import main

    out = tmp_path / "session.yaml"
    monkeypatch.setattr(cli, "_DEFAULT_OUTPUT_PATH", out)

    with patch("agent_augury.cli.check_tty", return_value=True), patch(
        "agent_augury.cli._run_wizard_flow", return_value=0
    ) as wiz:
        assert main(["--headless", "--reconfigure", "--quiet"]) == 0
    wiz.assert_called_once()
    kwargs = wiz.call_args.kwargs
    assert kwargs["force_reconfigure"] is True
    assert kwargs["headless"] is True
    assert kwargs["quiet"] is True
    assert kwargs["auto_start"] is True


def test_cli_headless_reconfigure_rejects_config(capsys):
    from agent_augury.cli import main

    assert main(["--headless", "--reconfigure", "--config", "x.yaml"]) == 1
    assert "--config" in capsys.readouterr().err


def test_cli_headless_rejects_ink_combo(capsys):
    from agent_augury.cli import main

    assert main(["--headless", "--ink", "--config", "x.yaml"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_cli_no_auto_start_requires_headless(capsys):
    from agent_augury.cli import main

    assert main(["--no-auto-start", "--config", "x.yaml"]) == 1
    assert "--headless" in capsys.readouterr().err


def test_cli_headless_routes_to_runner():
    from agent_augury.cli import main

    with patch(
        "agent_augury.gateway.headless.run_headless_session", return_value=0
    ) as run:
        result = main(
            ["--headless", "--config", "examples/demo.yaml", "--demo", "--quiet"]
        )
    assert result == 0
    run.assert_called_once()
    args, kwargs = run.call_args
    assert args[0] == "examples/demo.yaml"
    assert kwargs["demo"] is True
    assert kwargs["quiet"] is True
    assert kwargs["auto_start"] is True


def test_cli_headless_no_auto_start_flag():
    from agent_augury.cli import main

    with patch(
        "agent_augury.gateway.headless.run_headless_session", return_value=0
    ) as run:
        result = main(
            [
                "--headless",
                "--no-auto-start",
                "--config",
                "examples/demo.yaml",
                "--demo",
            ]
        )
    assert result == 0
    assert run.call_args.kwargs["auto_start"] is False


def test_cli_default_config_still_ink():
    from agent_augury.cli import main

    with patch("agent_augury.cli._run_ink_surface", return_value=0) as ink:
        result = main(["--config", "fake.yaml", "--demo"])
    assert result == 0
    ink.assert_called_once()
    assert ink.call_args.kwargs["mode"] == "session"


def test_emit_startup_warnings_no_channel(tmp_path, capsys):
    session = _fake_session(tmp_path)
    emit_startup_warnings(session)
    err = capsys.readouterr().err
    assert "no Discord/Slack/mirror" in err
    assert "no interact surface" in err


@pytest.mark.asyncio
async def test_headless_runner_auto_start_and_idle_turn(tmp_path):
    session = _fake_session(tmp_path, task="first turn")
    # Interact surface so warnings stay quiet in this path.
    session.gateway.attach(
        SurfaceSubscription(name="test-interact", mode="interact", family="chat")
    )
    runner = HeadlessRunner(session, quiet=True, auto_start=True)

    async def _drive() -> int:
        task = asyncio.create_task(runner.run())
        # Wait until first auto-start run finishes (bridge idle).
        for _ in range(200):
            if not runner.bridge._running and runner._loop is not None:
                break
            await asyncio.sleep(0.01)
        # Idle human.send → second turn.
        session.gateway.dispatch(
            make_command("human.send", id="t2", content="second turn"),
            surface="test-interact",
        )
        await asyncio.sleep(0.05)
        runner._on_quit()
        return await task

    rc = await _drive()
    assert rc == 0


@pytest.mark.asyncio
async def test_headless_runner_waits_without_auto_start(tmp_path):
    session = _fake_session(tmp_path, task="should not auto")
    session.gateway.attach(
        SurfaceSubscription(name="test-interact", mode="interact", family="chat")
    )
    runner = HeadlessRunner(session, quiet=True, auto_start=False)
    runs: list[str | None] = []
    orig = runner._do_run

    async def _track(prompt: str | None) -> int:
        runs.append(prompt)
        return await orig(prompt)

    runner._do_run = _track  # type: ignore[method-assign]

    async def _drive() -> int:
        task = asyncio.create_task(runner.run())
        for _ in range(200):
            if runner._loop is not None:
                break
            await asyncio.sleep(0.01)
        assert runs == []
        session.gateway.dispatch(
            make_command("human.send", id="1", content="go"),
            surface="test-interact",
        )
        for _ in range(200):
            if runs:
                break
            await asyncio.sleep(0.01)
        runner._on_quit()
        return await task

    assert await _drive() == 0
    assert runs == ["go"]


def test_run_headless_session_missing_config(tmp_path):
    missing = tmp_path / "nope.yaml"
    assert run_headless_session(str(missing), demo=True) == 1


@pytest.mark.asyncio
async def test_headless_runner_single_auto_run(tmp_path):
    session = _fake_session(tmp_path, task="smoke")
    session.gateway.attach(
        SurfaceSubscription(name="test-interact", mode="interact", family="chat")
    )

    class _OneShot(HeadlessRunner):
        async def _session_loop(self) -> int:
            await self.session._setup()
            steps = await self._do_run(None)
            await self._after_run(steps)
            return 0

    runner = _OneShot(session, quiet=True, auto_start=True)
    assert await runner.run() == 0
