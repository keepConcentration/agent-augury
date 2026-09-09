"""CLI entrypoint: run a session from YAML and mirror a plain log (§4.2, D3).

Two modes:
  - ``agent-augury --config PATH`` — run a session from an existing YAML file.
  - ``agent-augury`` (no args) — launch the interactive setup wizard,
    which collects model settings (persisted across runs), generates a
    YAML file, asks for an initial task, and starts the session.

Additional flags:
  - ``agent-augury --reconfigure`` — discard saved model settings and run
    the full wizard from scratch, then save new settings.
  - ``agent-augury --quiet`` — suppress broadcast event output (only show
    final summary).
  - ``agent-augury --repl`` — start a REPL session that keeps the conversation
    context across multiple questions (session reuse).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.markdown import Markdown

from .agent.loop import StepResult
from .config import load_config
from .model_config import (
    load_model_config,
    model_config_exists,
)
from .session import Session
from .wizard import WizardCancelled, check_tty, run_wizard

_DEFAULT_OUTPUT_PATH = Path.home() / ".agent-augury" / "agent-augury-session.yaml"
# Windows-forbidden path chars plus invisible/format characters (e.g. U+3164).
# Backslash is NOT included — it is a valid path separator on Windows.
_INVALID_PATH_CHARS = set('<>\"|?*')
_INVISIBLE_CODEPOINTS = frozenset({0x3164, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x00A0})

# Sensitive patterns to mask in broadcast output
_SENSITIVE_PATTERNS = [
    (re.compile(r'(Authorization:\s+Bearer\s+)[^\s]+', re.IGNORECASE), r'\1***'),
    (re.compile(r'(Bearer\s+)[^\s]+', re.IGNORECASE), r'\1***'),
    (re.compile(r'(api[_-]?key["\s:=]+)[^\s"]+', re.IGNORECASE), r'\1***'),
    (re.compile(r'(token["\s:=]+)[^\s"]+', re.IGNORECASE), r'\1***'),
]

# rich Console — auto-detects TTY (plain text fallback when piped/redirected)
_console = Console()


def _mask_sensitive(text: str) -> str:
    """Mask sensitive information (tokens, API keys) in output."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


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
        if char == ':':
            # Allow colon only as drive letter separator (e.g. "C:\\")
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
    """Read a multi-line free-text input (for tasks/questions) using prompt_toolkit.

    Uses ``PromptSession(multiline=True)`` so that pasted multi-line text
    (including blank lines) is preserved.  Submission is via **Ctrl+Enter**
    or **Esc+Enter** — plain Enter inserts a newline.

    In a non-TTY environment (pipe / redirect) it falls back to reading the
    entire stdin.
    """
    print(prompt, flush=True)

    # Non-TTY fallback: read everything from stdin.
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("c-enter")
    def _submit_ctrl_enter(event: object) -> None:
        buff = event.current_buffer
        buff.validate_and_handle()

    @kb.add("escape", "enter")
    def _submit_esc_enter(event: object) -> None:
        buff = event.current_buffer
        buff.validate_and_handle()

    history_path = Path.home() / ".agent-augury" / "human_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    session = PromptSession(
        history=FileHistory(str(history_path)),
        multiline=True,
        key_bindings=kb,
    )

    # Append the key hint to the prompt text.
    hint = "  (Ctrl+Enter 또는 Esc+Enter로 제출)"
    full_prompt = f"{prompt.rstrip(chr(10))}{hint}\n"

    try:
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
        print(f"  Warning: invalid save path ({problem}). Press Enter for default or type a valid path.")


def _log_step(agent_id: str, result: StepResult) -> None:
    """Print a step summary line with rich Markdown rendering.

    Only prints when the step produced text.
    Format: ``💭 agent_id:`` header, then the text body rendered as Markdown.
    """
    if not result.text:
        return
    md = Markdown(result.text)
    _console.print(f"💭 {agent_id}:", end=" ")
    _console.print(md)


async def _close_session(session: Session) -> None:
    """Close mirror and backend HTTP clients after flush (normal shutdown)."""
    if session.mirror is not None:
        await session.mirror.aclose()
    for agent in session.agents:
        aclose = getattr(agent.backend, "aclose", None)
        if aclose is not None:
            await aclose()


def _log_tool_event(event: dict[str, Any]) -> None:
    """Print a tool/broadcast event in real-time (Hermes-style, rich rendering).

    Handles: tool, read_resource, create_thread, send_message.
    """
    event_type = event.get("type")

    if event_type == "create_thread":
        tid = event["thread_id"]
        name = event["name"]
        participants = ", ".join(event["participants"])
        _console.print(f"🧵 [{tid}] create_thread {name} ({participants})")

    elif event_type == "send_message":
        author = event["author"]
        tid = event["thread_id"]
        content = _mask_sensitive(event["content"])
        delivered = event.get("delivered_to", [])
        targets = ", ".join(delivered) if delivered else "broadcast"
        # send_message content may contain Markdown — render it
        _console.print(f"💬 [{author} → {targets}][{tid}]", end=" ")
        _console.print(Markdown(content))

    elif event_type == "read_resource":
        agent_id = event["agent_id"]
        threads = event.get("threads", 0)
        messages = event.get("messages", 0)
        _console.print(f"📊 {agent_id}: read_resource (threads={threads}, messages={messages})")

    elif event_type == "tool":
        agent_id = event["agent_id"]
        tool = event["tool"]
        # D2-dedup: 서버 이벤트로 이미 출력되는 3종은 tool 이벤트에서 스킵
        if tool in ("send_message", "create_thread", "read_resource"):
            return
        args = event.get("args", {})

        # ask_user: surface the question to the human prominently.
        if tool == "ask_user":
            question = args.get("question", "")
            options = args.get("options")
            _console.print(f"👤 {agent_id} asks: {question}", style="bold")
            if options:
                _console.print("   (options: " + " / ".join(options) + ")")
            return

        # Tool icons
        icons = {
            "read_file": "📖",
            "write_file": "📝",
            "list_directory": "📁",
            "send_message": "💬",
            "create_thread": "🧵",
            "read_resource": "📊",
        }
        icon = icons.get(tool, "🔧")

        # Extract path for file tools (display only — shorten to basename).
        path = args.get("path", "")
        if path:
            # Normalize backslashes so os.path.basename shortens Windows
            # paths on any platform (D4: Windows `C:\...` was not shortened).
            short_path = os.path.basename(path.replace("\\", "/"))
            _console.print(f"{icon} {agent_id}: {tool} {short_path}")
        else:
            _console.print(f"{icon} {agent_id}: {tool}")


async def _run_repl(cfg_path: str, initial_prompt: str | None = None, *, quiet: bool = False, allow_fake: bool = False) -> int:
    """Run a REPL session: keep the conversation context across multiple questions.

    The session is created once, then ``session.run()`` is called repeatedly
    in a loop. The user's previous conversation is preserved, so they can
    continue where they left off. quit/exit/blank input exits the loop.
    """
    cfg = load_config(cfg_path, allow_fake=allow_fake)

    def on_step(agent_id: str, result: StepResult) -> None:
        if quiet:
            return
        _log_step(agent_id, result)

    def on_tool_event(event: dict[str, Any]) -> None:
        if quiet:
            return
        _log_tool_event(event)

    session = Session.from_config(cfg, on_step=on_step, on_tool_event=on_tool_event)

    try:
        # First run with the initial prompt
        steps = await session.run(initial_prompt=initial_prompt)

        if session.mirror is not None:
            await session.mirror.flush()

        gate_state = "OPEN" if (session.gate and session.gate.is_open) else ("CLOSED" if session.gate else "n/a")
        snap = session.server.snapshot()
        phase_state = session.protocol.phase if session.protocol is not None else "n/a"
        print(
            f"--- session finished: steps={steps} threads={len(snap['threads'])} "
            f"messages={len(snap['messages'])} gate={gate_state} phase={phase_state}"
        )

        # REPL loop
        while True:
            print("\n--- Next question? (enter=quit) ---")
            try:
                question = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not question or question.lower() in ("quit", "exit"):
                break

            steps = await session.run(initial_prompt=question)

            if session.mirror is not None:
                await session.mirror.flush()

            gate_state = "OPEN" if (session.gate and session.gate.is_open) else ("CLOSED" if session.gate else "n/a")
            snap = session.server.snapshot()
            phase_state = session.protocol.phase if session.protocol is not None else "n/a"
            print(
                f"--- session finished: steps={steps} threads={len(snap['threads'])} "
                f"messages={len(snap['messages'])} gate={gate_state} phase={phase_state}"
            )

        return 0
    finally:
        await session.close()





async def _human_input_loop_tui(
    session: Session,
    human_cfg: dict[str, Any],
) -> None:
    """Run the prompt_toolkit-based TUI input loop (v1.0, D1).

    Replaces ``_human_input_loop`` when ``human.interface: tui``.
    asyncio-native — no ``run_in_executor`` / threading.
    """
    from .channel.human_tui import HumanTUIAdapter

    tui_cfg = human_cfg.get("tui") or {}
    adapter = HumanTUIAdapter(session, **tui_cfg)
    try:
        await adapter.run_input_loop()
    finally:
        adapter.cleanup()





def _make_tui_adapter(session: Any, human_cfg: dict[str, Any]) -> Any:
    """Create a ``HumanTUIAdapter`` from config (lazy import)."""
    from .channel.human_tui import HumanTUIAdapter
    tui_cfg = human_cfg.get("tui") or {}
    return HumanTUIAdapter(session, **tui_cfg)


async def _run(cfg_path: str, initial_prompt: str | None = None, *, quiet: bool = False, allow_fake: bool = False) -> int:
    cfg = load_config(cfg_path, allow_fake=allow_fake)

    # v1.0: 항상 TUI 모드 (human 필수, interface 고정)
    # TUI 설정은 코드에 내장 (config에서 읽지 않음)
    human_cfg = {
        "id": "human",
        "tui": {
            "response_format": "text",
            "input_prompt": "👤 > ",
            "multiline": True,
            "pin_options": True,
            "choice_queue": False,
        },
    }
    tui_mode = True

    # D2: quiet 모드 시 step/도구 라이브 로그 억제
    def on_step(agent_id: str, result: StepResult) -> None:
        if quiet:
            return
        _log_step(agent_id, result)

    # TUI adapter reference (set below if tui_mode)
    tui_adapter = None

    def on_tool_event(event: dict[str, Any]) -> None:
        if quiet:
            return
        nonlocal tui_adapter
        event_type = event.get("type")

        # D12: TUI 모드에서 [ask-user] prefix send_message 로그 스킵
        if tui_mode and event_type == "send_message":
            content = event.get("content", "")
            if content.startswith("[ask-user]"):
                return  # skip — question is shown in pinned panel

        # D11: TUI 모드에서 ask_user tool 이벤트 → pinned 패널 표시 (로그 억제)
        if tui_mode and event_type == "tool" and event.get("tool") == "ask_user":
            if tui_adapter is not None:
                tui_adapter.on_ask_user(
                    event.get("agent_id", ""),
                    event.get("tool", ""),
                    event.get("args", {}),
                    None,
                )
            return  # suppress 👤 asks: log — shown in toolbar instead

        _log_tool_event(event)

    session = Session.from_config(cfg, on_step=on_step, on_tool_event=on_tool_event)

    try:
        # v1.0: 항상 TUI 입력 루프 (human 필수)
        human_task = None
        if session.has_human:
            print("👤 TUI mode: type messages to inject them as 'human'.", flush=True)
            tui_adapter = _make_tui_adapter(session, human_cfg)
            if tui_adapter is not None:
                human_task = asyncio.create_task(
                    _human_input_loop_tui(session, human_cfg)
                )

        steps = await session.run(initial_prompt=initial_prompt)

        if human_task is not None:
            human_task.cancel()
            try:
                await human_task
            except asyncio.CancelledError:
                pass

        if session.mirror is not None:
            await session.mirror.flush()

        gate_state = "OPEN" if (session.gate and session.gate.is_open) else ("CLOSED" if session.gate else "n/a")
        snap = session.server.snapshot()
        phase_state = session.protocol.phase if session.protocol is not None else "n/a"
        print(
            f"--- session finished: steps={steps} threads={len(snap['threads'])} "
            f"messages={len(snap['messages'])} gate={gate_state} phase={phase_state}"
        )
        return 0
    finally:
        await _close_session(session)


def _save_config(cfg: dict[str, Any], output_path: Path) -> None:
    """Write a config dict to a YAML file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _run_wizard_flow(
    output_path: Path | None = None,
    force_reconfigure: bool = False,
    quiet: bool = False,
    repl: bool = True,
) -> int:
    """Run the interactive wizard, save the YAML, then start a session."""
    if not check_tty():
        print(
            "error: interactive wizard requires a TTY. "
            "Use --config PATH to run a pre-built config, "
            "or run from an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    try:
        # Check for existing model config (unless force reconfigure).
        existing = None
        if not force_reconfigure and model_config_exists():
            existing = load_model_config()
            if existing is None:
                # Invalid or corrupted — ignore and re-collect.
                existing = None

        # If we have a valid existing config, skip the wizard entirely.
        # The user just wants to run the session, not reconfigure.
        if existing is not None and not force_reconfigure:
            # Build config from saved model settings
            cfg = {
                "max_steps": existing.get("max_steps", 0),
                "agents": existing["agents"],
            }
            # Use default output path
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nUsing saved model config. Config saved to: {output_path}")
        else:
            # Run full wizard for new setup or reconfigure
            cfg = run_wizard(existing_model_config=existing, force_reconfigure=force_reconfigure)
            # Determine output path.
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nConfig saved to: {output_path}")
    except WizardCancelled:
        print("\nWizard cancelled.")
        return 130  # standard Ctrl+C exit code

    # Collect the initial task from the user, then start the session.
    print("\n--- Initial Task ---")
    task = _prompt_multiline(
        "What would you like to do? [Multi-agent collaboration] "
        "(paste multi-line text, then press Enter on an empty line to finish):"
    )
    if not task:
        task = "Multi-agent collaboration"

    if repl:
        return asyncio.run(_run_repl(str(output_path), initial_prompt=task, quiet=quiet))
    else:
        return asyncio.run(_run(str(output_path), initial_prompt=task, quiet=quiet))


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
    parser.add_argument(
        "--repl",
        action="store_true",
        default=False,
        help="start a REPL session that keeps conversation context across multiple questions",
    )
    args = parser.parse_args(argv)

    # Validate flag combinations before anything else.
    if args.output is not None and args.config is not None:
        print("error: --output is only valid without --config", file=sys.stderr)
        return 1

    # Mode 1: run from existing config.
    if args.config is not None:
        if args.reconfigure:
            print(
                "error: --reconfigure is only valid without --config",
                file=sys.stderr,
            )
            return 1
        try:
            if args.repl:
                return asyncio.run(_run_repl(args.config, quiet=args.quiet, allow_fake=args.demo))
            return asyncio.run(_run(args.config, quiet=args.quiet, allow_fake=args.demo))
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # Mode 2: interactive wizard.
    output_path = Path(args.output) if args.output else None
    try:
        return _run_wizard_flow(output_path, force_reconfigure=args.reconfigure, quiet=args.quiet, repl=args.repl)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
