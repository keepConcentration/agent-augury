"""Initial Task -> full-screen TUI first-input integration tests (v1.0 — 결정 ② 2-A).

Verifies cli._run_repl_tui's "Initial Task waiting mode" (initial_prompt=None):
* first plain input triggers session.run(initial_prompt=task) via on_next_turn
* Initial Task guide lines are appended to the TUI log
* /quit (on_quit) exits cleanly (return 0)
* non-waiting path (initial_prompt given) is unchanged (regression)

NOTE 1: cli.py imports SessionTUIApplication *inside* _run_repl_tui via
``from .tui.app import SessionTUIApplication``. ``from X import Y`` looks up
the ``Y`` attribute on module X, so the patch target is
``agent_augury.tui.app.SessionTUIApplication`` (NOT ``agent_augury.cli.*``).

NOTE 2: we patch with ``side_effect`` (not ``return_value``) so that the
FakeTUI constructor runs and captures the kwargs cli passes (on_next_turn /
on_quit / initial_task_mode / preserve_log_on_exit). With ``return_value``
the constructor is never invoked and ``fake.kwargs`` stays empty.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_augury.cli import _run_repl_tui


class FakeTUI:
    """Minimal stand-in for SessionTUIApplication (cli only touches these APIs)."""

    def __init__(self, session: object, **kwargs: object) -> None:
        self.session = session
        self.kwargs = dict(kwargs)
        self.appended: list[object] = []
        self.running = False
        self.shutdown_called = False

    def append_text(self, text: str) -> None:
        self.appended.append(text)

    def append_event(self, event: dict[str, object]) -> None:
        self.appended.append(event)

    def on_ask_user(self, *args: object, **kwargs: object) -> None:
        pass

    def set_running(self, running: bool) -> None:
        self.running = running

    async def run(self) -> None:
        await asyncio.sleep(0)

    def shutdown(self) -> None:
        self.shutdown_called = True


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.mirror = None
    session.gate = None
    session.protocol = None
    session.server.snapshot.return_value = {"threads": [], "messages": []}
    session.run = AsyncMock(return_value=2)
    session.close = AsyncMock()
    return session


def _patch_tui(fake: FakeTUI):
    """Patch SessionTUIApplication with a side_effect that returns *fake*
    and captures the kwargs cli passes into fake.kwargs."""
    def _constructor(session: object, **kwargs: object) -> FakeTUI:
        fake.session = session
        fake.kwargs.update(kwargs)
        return fake

    return patch(
        "agent_augury.tui.app.SessionTUIApplication", side_effect=_constructor
    )


def _captured_tui(fake: FakeTUI):
    """Return the on_next_turn / on_quit callbacks cli passed to the TUI."""
    return fake.kwargs["on_next_turn"], fake.kwargs["on_quit"]


@pytest.mark.asyncio
async def test_waiting_initial_first_input_runs_session_with_task():
    """V1: initial_prompt=None → first input (on_next_turn) → session.run(task)."""
    session = _mock_session()
    fake = FakeTUI(session)

    with _patch_tui(fake):
        tui_task = asyncio.create_task(
            _run_repl_tui(session, initial_prompt=None, quiet=True)
        )
        await asyncio.sleep(0)  # let _run_repl_tui construct the TUI

        on_next_turn, on_quit = _captured_tui(fake)
        on_next_turn("hello")   # user submits Initial Task in TUI
        on_quit()               # stop the REPL loop

        rc = await tui_task

    assert rc == 0
    session.run.assert_called_once_with(initial_prompt="hello")
    assert fake.running is False
    assert fake.shutdown_called is True


@pytest.mark.asyncio
async def test_waiting_initial_appends_guide_lines():
    """Initial Task guide lines are appended to the TUI log before first input."""
    session = _mock_session()
    fake = FakeTUI(session)

    with _patch_tui(fake):
        tui_task = asyncio.create_task(
            _run_repl_tui(session, initial_prompt=None, quiet=True)
        )
        await asyncio.sleep(0)
        on_next_turn, on_quit = _captured_tui(fake)
        on_next_turn("task")
        on_quit()
        await tui_task

    texts = [str(a) for a in fake.appended]
    assert "--- Initial Task ---" in texts
    assert any("What would you like to do?" in t for t in texts)
    assert any("Enter to submit" in t for t in texts)


@pytest.mark.asyncio
async def test_waiting_initial_quit_before_input_returns_zero():
    """If on_quit fires before any input, loop exits 0 without session.run."""
    session = _mock_session()
    fake = FakeTUI(session)

    with _patch_tui(fake):
        tui_task = asyncio.create_task(
            _run_repl_tui(session, initial_prompt=None, quiet=True)
        )
        await asyncio.sleep(0)
        _on_next_turn, on_quit = _captured_tui(fake)
        on_quit()
        rc = await tui_task

    assert rc == 0
    session.run.assert_not_called()
    assert fake.shutdown_called is True


@pytest.mark.asyncio
async def test_non_waiting_path_regression():
    """initial_prompt given → first run uses it; subsequent turns use on_next_turn."""
    session = _mock_session()
    fake = FakeTUI(session)

    with _patch_tui(fake):
        tui_task = asyncio.create_task(
            _run_repl_tui(session, initial_prompt="start", quiet=True)
        )
        await asyncio.sleep(0)
        on_next_turn, on_quit = _captured_tui(fake)
        # Initial Task guide lines must NOT appear in non-waiting mode
        texts = [str(a) for a in fake.appended]
        assert "--- Initial Task ---" not in texts

        on_next_turn("second")
        on_quit()
        rc = await tui_task

    assert rc == 0
    session.run.assert_any_call(initial_prompt="start")
    session.run.assert_any_call(initial_prompt="second")
    assert fake.shutdown_called is True
