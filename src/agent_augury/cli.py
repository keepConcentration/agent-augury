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
    final summary).  **Currently unimplemented**: accepted for CLI
    compatibility but output is not suppressed yet.
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

from .agent.loop import StepResult
from .config import load_config
from .model_config import (
    clear_model_config,
    load_model_config,
    model_config_exists,
    save_model_config,
)
from .session import Session
from .wizard import WizardCancelled, check_tty, run_wizard

_DEFAULT_OUTPUT_PATH = Path("agent-augury-session.yaml")
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


def _mask_sensitive(text: str) -> str:
    """Mask sensitive information (tokens, API keys) in output."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class BroadcastLogger:
    """Logs agent-to-agent broadcast events to stderr in real-time.

    Subscribes to the MessageServer event stream and prints a one-line
    summary of each create_thread / send_message / read_resource event.
    No ANSI color codes — plain text only.
    """

    def __init__(self, *, quiet: bool = False) -> None:
        self.quiet = quiet
        self._seen_threads: dict[str, str] = {}  # thread_id -> name

    def __call__(self, event: dict[str, Any]) -> None:
        """Handle a broadcast event (called by MessageServer)."""
        if self.quiet:
            return
        handler = getattr(self, f"_on_{event['type']}", None)
        if handler is not None:
            handler(event)

    def _on_create_thread(self, event: dict[str, Any]) -> None:
        tid = event["thread_id"]
        name = event["name"]
        self._seen_threads[tid] = name
        participants = ", ".join(event["participants"])
        self._print(f"🧵 [{tid}] create_thread {name} ({participants})")

    def _on_send_message(self, event: dict[str, Any]) -> None:
        author = event["author"]
        tid = event["thread_id"]
        content = event["content"]
        # No truncation — show full content
        content = _mask_sensitive(content)
        delivered = event.get("delivered_to", [])
        targets = ", ".join(delivered) if delivered else "broadcast"
        self._print(f"💬 [{author} → {targets}][{tid}] {content}")

    def _on_read_resource(self, event: dict[str, Any]) -> None:
        agent_id = event["agent_id"]
        threads = event["threads"]
        messages = event["messages"]
        self._print(f"📊 {agent_id}: read_resource (threads={threads}, messages={messages})")

    def _print(self, line: str) -> None:
        """Print a broadcast line to stderr with flush."""
        print(line, file=sys.stderr, flush=True)


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
    """Print a step summary line (no ANSI codes).

    Only prints when the step produced text; steps with no text are silent.
    Format: ``💭 agent_id: text`` (newlines flattened to a single space).
    """
    if not result.text:
        return
    text = result.text.replace("\n", " ")
    print(f"💭 {agent_id}: {text}", flush=True)


async def _close_session(session: Session) -> None:
    """Close mirror and backend HTTP clients after flush (normal shutdown)."""
    if session.mirror is not None:
        await session.mirror.aclose()
    for agent in session.agents:
        aclose = getattr(agent.backend, "aclose", None)
        if aclose is not None:
            await aclose()


def _log_tool_event(event: dict[str, Any]) -> None:
    """Print a tool/broadcast event in real-time (Hermes-style, no ANSI codes).

    Handles: tool, read_resource, create_thread, send_message.
    """
    event_type = event.get("type")

    if event_type == "create_thread":
        tid = event["thread_id"]
        name = event["name"]
        participants = ", ".join(event["participants"])
        print(f"🧵 [{tid}] create_thread {name} ({participants})", flush=True)

    elif event_type == "send_message":
        author = event["author"]
        tid = event["thread_id"]
        content = _mask_sensitive(event["content"])
        delivered = event.get("delivered_to", [])
        targets = ", ".join(delivered) if delivered else "broadcast"
        print(f"💬 [{author} → {targets}][{tid}] {content}", flush=True)

    elif event_type == "read_resource":
        agent_id = event["agent_id"]
        threads = event.get("threads", 0)
        messages = event.get("messages", 0)
        print(f"📊 {agent_id}: read_resource (threads={threads}, messages={messages})", flush=True)

    elif event_type == "tool":
        agent_id = event["agent_id"]
        tool = event["tool"]
        # D2-dedup: 서버 이벤트로 이미 출력되는 3종은 tool 이벤트에서 스킵
        if tool in ("send_message", "create_thread", "read_resource"):
            return
        args = event.get("args", {})

        # Tool icons
        icons = {
            "read_file": "📖",
            "write_file": "📝",
            "list_directory": "📁",
            "send_message": "💬",
            "create_thread": "🧵",
            "read_resource": "📊",
            "wait_for_mention": "⏳",
        }
        icon = icons.get(tool, "🔧")

        # Extract path for file tools (display only — shorten to basename).
        path = args.get("path", "")
        if path:
            # Normalize backslashes so os.path.basename shortens Windows
            # paths on any platform (D4: Windows `C:\...` was not shortened).
            short_path = os.path.basename(path.replace("\\", "/"))
            print(f"{icon} {agent_id}: {tool} {short_path}", flush=True)
        else:
            print(f"{icon} {agent_id}: {tool}", flush=True)


async def _run(cfg_path: str, initial_prompt: str | None = None, *, quiet: bool = False) -> int:
    cfg = load_config(cfg_path)

    # D2: quiet 모드 시 step/도구 라이브 로그 억제
    def on_step(agent_id: str, result: StepResult) -> None:
        if quiet:
            return
        _log_step(agent_id, result)

    def on_tool_event(event: dict[str, Any]) -> None:
        if quiet:
            return
        _log_tool_event(event)

    session = Session.from_config(cfg, on_step=on_step, on_tool_event=on_tool_event)

    # No BroadcastLogger — all output goes through unified queue
    # broadcast = BroadcastLogger(quiet=quiet)
    # session.server.subscribe_events(broadcast)

    try:
        steps = await session.run(initial_prompt=initial_prompt)

        if session.mirror is not None:
            await session.mirror.flush()

        gate_state = "OPEN" if (session.gate and session.gate.is_open) else ("CLOSED" if session.gate else "n/a")
        snap = session.server.snapshot()
        print(
            f"--- session finished: steps={steps} threads={len(snap['threads'])} "
            f"messages={len(snap['messages'])} gate={gate_state}"
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
                "mode": existing.get("mode", "L3"),
                "max_steps": existing.get("max_steps", 20),
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
                if existing is not None:
                    output_path = _DEFAULT_OUTPUT_PATH
                else:
                    output_path = _prompt_output_path()
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nConfig saved to: {output_path}")
    except WizardCancelled:
        print("\nWizard cancelled.")
        return 130  # standard Ctrl+C exit code

    # Collect the initial task from the user, then start the session.
    print("\n--- Initial Task ---")
    task = input("What would you like to do? [Multi-agent collaboration]: ").strip()
    if not task:
        task = "Multi-agent collaboration"

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
        help="suppress broadcast event output (only show final summary) — currently unimplemented",
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
            return asyncio.run(_run(args.config, quiet=args.quiet))
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # Mode 2: interactive wizard.
    output_path = Path(args.output) if args.output else None
    try:
        return _run_wizard_flow(output_path, force_reconfigure=args.reconfigure, quiet=args.quiet)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
