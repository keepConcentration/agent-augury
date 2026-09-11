"""CLI entrypoint: run a session from YAML (REPL is the only run mode).

Modes:
  - ``agent-augury --config PATH`` — REPL session from YAML (session reuse).
  - ``agent-augury`` (no args) — interactive wizard, then REPL.

Flags:
  - ``--reconfigure`` — discard saved model settings and re-run the wizard.
  - ``--quiet`` — suppress live event output (summary only).
  - ``--demo`` — allow ``type: fake`` backends.

Design: ``docs/tui/SESSION_TUI_REDESIGN.md`` (v2.5) — full-screen TUI on TTY,
plain ``input()`` fallback otherwise. ``--repl`` / one-shot ``_run`` removed.
v1.0 (INITIAL_TASK_TUI_INTEGRATION_RESULT.md): Initial Task는 TTY에서 full-screen
TUI 첫 입력으로 통합 (결정 ② 2-A). 비-TTY는 기존 ``_prompt_multiline`` 유지.

v1.1 (TUI_UX_FIX_DESIGN.md ①): OAuth device-code 안내를 full-screen TUI 위에
직접 print하지 않고 TUI 로그 버퍼로 라우팅한다 (``_AuthNoticeRelay``).

v0.7 (AGENT_TOOLS_EXPANSION_DESIGN.md v4.1, P9): ``allowed_roots`` 배선 복구 —
프로젝트 루트를 세션에 전달해 파일/shell 도구가 경로 제한을 실제 적용한다
(기존: 미전달 → None = 무제한 접근 허점).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .agent.loop import StepResult
from .config import load_config
from .model_config import (
    load_model_config,
    model_config_exists,
)
from .session import Session
from .tui.renderer import mask_sensitive, render_event
from .wizard import WizardCancelled, check_tty, run_wizard

_DEFAULT_OUTPUT_PATH = Path.home() / ".agent-augury" / "agent-augury-session.yaml"
_INVALID_PATH_CHARS = set('<>"|?*')
_INVISIBLE_CODEPOINTS = frozenset({0x3164, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x00A0})

_INITIAL_TASK_PROMPT = "What would you like to do? [Multi-agent collaboration] "
_INITIAL_TASK_DEFAULT = "Multi-agent collaboration"

# P9: 프로젝트 루트 — cli.py → 프로젝트 루트 (agent_augury/ 하위의 cli.py 기준 parents[2])
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _AuthNoticeRelay:
    """Routes OAuth device-code notices to the active display surface.

    TUI path: lines are appended to the TUI log buffer (no direct print over
    the alternate screen). Plain REPL / non-TTY: falls back to print().
    """

    def __init__(self) -> None:
        self._tui: Any = None

    def bind_tui(self, tui: Any) -> None:
        self._tui = tui

    def __call__(self, user_code: str, verification_uri: str) -> None:
        lines = [
            f"To authenticate, enter code: {user_code}",
            f"Verification URL: {verification_uri}",
        ]
        if self._tui is not None:
            try:
                for line in lines:
                    self._tui.append_text(line)
                return
            except Exception:  # noqa: BLE001, S110 — fall back to print
                pass
        for line in lines:
            print(line, flush=True)


def _mask_sensitive(text: str) -> str:
    """Backward-compat alias used by tests / older call sites."""
    return mask_sensitive(text)


def _output_path_problem(raw: str) -> str | None:
    """Return a short reason when *raw* is not a usable save path, else None."""
    if not raw:
        return "empty"
    if not raw.strip():
        return "whitespace only"
    for i, char in enumerate(raw):
        code = ord(char)
        if code in _INVISIBLE_CODEPOINTS:
            return f"invisible character U+{code:04X}"
        if char == ":":
            if i != 1:
                return f"invalid path character {char!r}"
            continue
        if char in _INVALID_PATH_CHARS:
            return f"invalid path character {char!r}"
        if code < 32:
            return "control character"
    return None


def _resolve_output_path(raw: str | None, default: Path = _DEFAULT_OUTPUT_PATH) -> Path:
    """Map wizard save-path input to a concrete path (blank → default)."""
    if raw is None or not raw.strip():
        return default
    problem = _output_path_problem(raw)
    if problem is not None:
        print(f"  Warning: invalid save path ({problem}). Using default: {default}")
        return default
    return Path(raw)


def _prompt_multiline(prompt: str) -> str:
    """Read multi-line free-text (Enter submits). Non-TTY -> stdin.read().

    v1.0: TTY 환경에서는 더 이상 이 경로를 사용하지 않는다 (Initial Task는
    full-screen TUI 첫 입력으로 통합 — 결정 ② 2-A). 비-TTY(파이프/CI) fallback으로만 유지.
    """
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    kb = KeyBindings()

    @kb.add("enter", eager=True)
    @kb.add("c-j", eager=True)
    def _submit(event: object) -> None:
        buff = event.current_buffer  # type: ignore[attr-defined]
        buff.validate_and_handle()

    @kb.add("escape", "enter")
    @kb.add("escape", "c-j")
    def _newline(event: object) -> None:
        buff = event.current_buffer  # type: ignore[attr-defined]
        buff.insert_text("\n")

    history_path = Path.home() / ".agent-augury" / "human_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    session = PromptSession(
        history=FileHistory(str(history_path)),
        multiline=True,
        key_bindings=kb,
    )

    hint = "  (Enter to submit, Shift+Enter for newline)"
    full_prompt = f"{prompt.rstrip()}{hint} "

    try:
        with patch_stdout():
            text = session.prompt(full_prompt)
    except (EOFError, KeyboardInterrupt):
        text = ""

    return text.strip()


def _prompt_output_path(default: Path = _DEFAULT_OUTPUT_PATH) -> Path:
    """Prompt for a YAML output path; Enter uses *default*, invalid input warns."""
    while True:
        raw = input(f"\nSave config to [{default}]: ").strip()
        if not raw:
            return default
        problem = _output_path_problem(raw)
        if problem is None:
            return Path(raw)
        print(
            f"  Warning: invalid save path ({problem}). "
            "Press Enter for default or type a valid path."
        )


def _log_step(agent_id: str, result: StepResult) -> None:
    """Print a step summary (compat wrapper around render_event)."""
    ansi = render_event({"type": "step", "agent_id": agent_id, "result": result})
    if ansi:
        print(ansi)


async def _close_session(session: Session) -> None:
    """Close mirror and backend HTTP clients after flush (normal shutdown)."""
    if session.mirror is not None:
        await session.mirror.aclose()
    for agent in session.agents:
        aclose = getattr(agent.backend, "aclose", None)
        if aclose is not None:
            await aclose()


def _log_tool_event(event: dict[str, Any]) -> None:
    """Print a tool/broadcast event (compat wrapper around render_event)."""
    ansi = render_event(event)
    if ansi:
        print(ansi)


def _want_fullscreen_tui() -> bool:
    """Full-screen Application only on a real interactive TTY (not under pytest).

    Uses ``wizard.check_tty()`` so Windows console_scripts wrappers that need
    ``AttachConsole`` are treated as interactive (design §7).
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.environ.get("AUGURY_PLAIN_REPL") == "1":
        return False
    from .wizard import check_tty

    return check_tty()


def _session_summary_line(session: Session, steps: int) -> str:
    gate_state = (
        "OPEN"
        if (session.gate and session.gate.is_open)
        else ("CLOSED" if session.gate else "n/a")
    )
    snap = session.server.snapshot()
    phase_state = session.protocol.phase if session.protocol is not None else "n/a"
    return (
        f"--- session finished: steps={steps} threads={len(snap['threads'])} "
        f"messages={len(snap['messages'])} gate={gate_state} phase={phase_state}"
    )


def _is_slash_quit(text: str) -> bool:
    """Exit tokens shared by TUI router and plain REPL (v0.5.1).

    Plain ``quit``/``exit`` are *not* quit — they are sent as the next prompt.
    """
    t = text.strip().lower()
    return t in ("/quit", "/exit")


async def _run_repl_plain(
    session: Session,
    *,
    initial_prompt: str | None,
    quiet: bool,
    on_step: Any,
    on_tool_event: Any,
) -> int:
    """Plain REPL: input() loop. Blank = ignored; /quit or EOF exits."""
    steps = await session.run(initial_prompt=initial_prompt)
    if session.mirror is not None:
        await session.mirror.flush()
    if not quiet:
        print(_session_summary_line(session, steps))

    while True:
        if not quiet:
            print("\n--- Next question? (/quit to exit) ---")
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue  # blank = ignored
        if _is_slash_quit(question):
            break
        # Plain quit/exit are normal prompts (v0.5.1) — not exit tokens.
        steps = await session.run(initial_prompt=question)
        if session.mirror is not None:
            await session.mirror.flush()
        if not quiet:
            print(_session_summary_line(session, steps))
    return 0


async def _run_repl_tui(
    session: Session,
    *,
    initial_prompt: str | None,
    quiet: bool,
    auth_relay: _AuthNoticeRelay | None = None,
) -> int:
    """Full-screen Application + session reuse loop.

    v1.0: initial_prompt=None이면 "Initial Task 대기 모드"로 진입 — TUI 입력줄에서
    첫 입력을 받아 session.run(initial_prompt=task) 트리거 (결정 ② 2-A).
    v1.1: auth_relay가 주어지면 TUI 로그로 OAuth 안내를 라우팅 (TUI_UX_FIX_DESIGN.md ①).
    """
    from .tui.app import SessionTUIApplication

    next_turn: asyncio.Queue[str | None] = asyncio.Queue()
    quit_flag = asyncio.Event()
    waiting_initial = initial_prompt is None      # ★ v1.0: Initial Task 대기 모드

    def on_quit() -> None:
        # Always interrupt a live run so session_loop is not stuck in await run().
        session.request_interrupt()
        quit_flag.set()
        next_turn.put_nowait(None)

    def on_interrupt() -> None:
        """Ctrl+C while running: stop agents; keep TUI / REPL open for resume."""
        session.request_interrupt()

    def on_next_turn(text: str) -> None:
        next_turn.put_nowait(text)

    tui = SessionTUIApplication(
        session,
        on_quit=on_quit,
        on_next_turn=on_next_turn,
        on_interrupt=on_interrupt,
        initial_task_mode=waiting_initial,       # ★ v1.0
        preserve_log_on_exit=True,
    )

    # ★ v1.1: OAuth 안내를 TUI 로그 버퍼로 라우팅
    if auth_relay is not None:
        auth_relay.bind_tui(tui)

    if waiting_initial:
        # TUI 로그에 Initial Task 안내 (v1.0 §2.1)
        tui.append_text("--- Initial Task ---")
        tui.append_text(_INITIAL_TASK_PROMPT.rstrip())
        tui.append_text("(Enter to submit, Shift+Enter for newline)")

    def on_step(agent_id: str, result: StepResult) -> None:
        if quiet:
            return
        tui.append_event({"type": "step", "agent_id": agent_id, "result": result})

    def on_tool_event(event: dict[str, Any]) -> None:
        if quiet:
            return
        if event.get("type") == "tool" and event.get("tool") == "ask_user":
            tui.on_ask_user(
                event.get("agent_id", ""),
                event.get("tool", ""),
                event.get("args", {}),
                None,
            )
            return
        tui.append_event(event)

    session.on_step = on_step
    session.on_tool_event = on_tool_event

    async def session_loop() -> int:
        async def do_run(prompt: str | None) -> int:
            tui.set_running(True)
            try:
                return await session.run(initial_prompt=prompt)
            finally:
                tui.set_running(False)

        steps = 0
        if waiting_initial:
            # ★ v1.0: 첫 입력을 TUI 입력줄에서 대기 → session.run(initial_prompt=task)
            task = await next_turn.get()
            if task is None:
                return 0
            steps = await do_run(task)
        else:
            steps = await do_run(initial_prompt)

        if quit_flag.is_set():
            tui.shutdown()
            return 0

        if session.mirror is not None:
            await session.mirror.flush()
        if not quiet:
            # Interrupted runs still get a short summary; user may resume.
            if session.interrupted():
                tui.append_text("⏹ run interrupted")
            tui.append_text(_session_summary_line(session, steps))

        while not quit_flag.is_set():
            question = await next_turn.get()
            if question is None:
                break
            steps = await do_run(question)
            if quit_flag.is_set():
                break
            if session.mirror is not None:
                await session.mirror.flush()
            if not quiet:
                if session.interrupted():
                    tui.append_text("⏹ run interrupted")
                tui.append_text(_session_summary_line(session, steps))
        tui.shutdown()
        return 0

    tui_task = asyncio.create_task(tui.run())
    try:
        rc = await session_loop()
    finally:
        tui.shutdown()
        if not tui_task.done():
            tui_task.cancel()
            try:
                await tui_task
            except asyncio.CancelledError:
                pass
    return rc


async def _run_repl(
    cfg_path: str,
    initial_prompt: str | None = None,
    *,
    quiet: bool = False,
    allow_fake: bool = False,
) -> int:
    """Unique session entrypoint — REPL with session reuse (v2.5)."""
    cfg = load_config(cfg_path, allow_fake=allow_fake)

    use_tui = _want_fullscreen_tui()

    # Placeholder callbacks — TUI path rebinds on session before run().
    def on_step(agent_id: str, result: StepResult) -> None:
        if quiet:
            return
        _log_step(agent_id, result)

    def on_tool_event(event: dict[str, Any]) -> None:
        if quiet:
            return
        _log_tool_event(event)

    # ★ v1.1: OAuth 안내 라우팅 (TUI 로그 / plain print)
    auth_relay = _AuthNoticeRelay()

    # ★ v0.7 (P9): allowed_roots 배선 복구 — 프로젝트 루트를 기본 root로 전달.
    # config tools.file.allowed_roots 로 확장 가능 (합집합은 ToolPolicy.from_config).
    session = Session.from_config(
        cfg,
        on_step=on_step,
        on_tool_event=on_tool_event,
        allowed_roots=[str(PROJECT_ROOT)],
        on_user_code=auth_relay,
    )
    try:
        if use_tui:
            return await _run_repl_tui(
                session,
                initial_prompt=initial_prompt,
                quiet=quiet,
                auth_relay=auth_relay,
            )
        return await _run_repl_plain(
            session,
            initial_prompt=initial_prompt,
            quiet=quiet,
            on_step=on_step,
            on_tool_event=on_tool_event,
        )
    finally:
        await session.close()


# Backward-compat alias — older tests patched ``_run``.
_run = _run_repl


def _save_config(cfg: dict[str, Any], output_path: Path) -> None:
    """Write a config dict to a YAML file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _run_wizard_flow(
    output_path: Path | None = None,
    force_reconfigure: bool = False,
    quiet: bool = False,
) -> int:
    """Run the interactive wizard, save the YAML, then start a REPL session.

    v1.0 (결정 ② 2-A): TTY면 위저드 직후 바로 full-screen TUI 시작 → Initial Task를
    TUI 입력줄에서 받는다 (initial_prompt=None → _run_repl_tui의 대기 모드).
    비-TTY(파이프/CI)는 기존 인라인 _prompt_multiline 유지 (회귀 0).
    """
    if not check_tty():
        print(
            "error: interactive wizard requires a TTY. "
            "Use --config PATH to run a pre-built config, "
            "or run from an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    try:
        existing = None
        if not force_reconfigure and model_config_exists():
            existing = load_model_config()
            if existing is None:
                existing = None

        if existing is not None and not force_reconfigure:
            cfg = {
                "max_steps": existing.get("max_steps", 0),
                "agents": existing["agents"],
            }
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nUsing saved model config. Config saved to: {output_path}")
        else:
            cfg = run_wizard(
                existing_model_config=existing, force_reconfigure=force_reconfigure
            )
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nConfig saved to: {output_path}")
    except WizardCancelled:
        print("\nWizard cancelled.")
        return 130

    # ★ v1.0: TTY면 Initial Task를 full-screen TUI 첫 입력으로 통합 (결정 ② 2-A)
    if _want_fullscreen_tui():
        return asyncio.run(_run_repl(str(output_path), initial_prompt=None, quiet=quiet))

    # 비-TTY fallback: 기존 인라인 _prompt_multiline 유지 (회귀 0)
    print("\n--- Initial Task ---")
    task = _prompt_multiline(_INITIAL_TASK_PROMPT)
    if not task:
        task = _INITIAL_TASK_DEFAULT

    return asyncio.run(_run_repl(str(output_path), initial_prompt=task, quiet=quiet))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-augury")
    parser.add_argument(
        "--config",
        required=False,
        help="path to session YAML (omit to launch interactive wizard)",
    )
    parser.add_argument(
        "--output",
        required=False,
        default=None,
        help="wizard output path (only valid without --config)",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        default=False,
        help="discard saved model settings and re-run the wizard from scratch",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="suppress broadcast event output (only show final summary)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="allow type:fake backends in config (offline demo/benchmark)",
    )
    # Accepted but ignored — REPL is always on (v2.5). Kept so old scripts don't fail.
    parser.add_argument(
        "--repl",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if args.output is not None and args.config is not None:
        print("error: --output is only valid without --config", file=sys.stderr)
        return 1

    if args.config is not None:
        if args.reconfigure:
            print(
                "error: --reconfigure is only valid without --config",
                file=sys.stderr,
            )
            return 1
        try:
            return asyncio.run(
                _run_repl(
                    args.config,
                    quiet=args.quiet,
                    allow_fake=args.demo,
                )
            )
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            print(f"error: {exc}", file=sys.stderr)
            return 1

    output_path = Path(args.output) if args.output else None
    try:
        return _run_wizard_flow(
            output_path, force_reconfigure=args.reconfigure, quiet=args.quiet
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
