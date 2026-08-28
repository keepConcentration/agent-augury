"""CLI entrypoint: run a session from YAML and mirror a plain log (§4.2, D3).

Two modes:
  - ``agent-augury --config PATH`` — run a session from an existing YAML file.
  - ``agent-augury`` (no args) — launch the interactive setup wizard, which
    generates a YAML file and optionally runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

from .agent.loop import StepResult
from .config import load_config
from .session import Session
from .wizard import WizardCancelled, check_tty, run_wizard


def _log_step(agent_id: str, result: StepResult) -> None:
    parts = [f"[{agent_id}] step drained={result.drained_count}"]
    if result.tool_calls:
        names = ",".join(c.name for c in result.tool_calls)
        parts.append(f"tools=({names})")
    if result.text:
        text = result.text.replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"text={text!r}")
    print(" ".join(parts), flush=True)


async def _run(cfg_path: str) -> int:
    cfg = load_config(cfg_path)
    session = Session.from_config(cfg)
    session.on_step = _log_step
    steps = await session.run()

    if session.mirror is not None:
        await session.mirror.flush()

    gate_state = "OPEN" if (session.gate and session.gate.is_open) else ("CLOSED" if session.gate else "n/a")
    snap = session.server.snapshot()
    print(
        f"--- session finished: steps={steps} threads={len(snap['threads'])} "
        f"messages={len(snap['messages'])} gate={gate_state}"
    )
    return 0


def _save_config(cfg: dict[str, Any], output_path: Path) -> None:
    """Write a config dict to a YAML file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _run_wizard_and_save(output_path: Path | None = None) -> int:
    """Run the interactive wizard, save the YAML, and optionally run it."""
    if not check_tty():
        print(
            "error: interactive wizard requires a TTY. "
            "Use --config PATH to run a pre-built config, "
            "or run from an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    try:
        cfg = run_wizard()
    except WizardCancelled:
        print("\nWizard cancelled.")
        return 130  # standard Ctrl+C exit code

    # Determine output path.
    if output_path is None:
        default_path = Path("agent-augury-session.yaml")
        raw = input(f"\nSave config to [{default_path}]: ").strip()
        output_path = Path(raw) if raw else default_path

    _save_config(cfg, output_path)
    print(f"\nConfig saved to: {output_path}")

    # Ask whether to run now.
    run_now = input("\nRun session now? (y/n) [y]: ").strip().lower()
    if run_now in ("", "y", "yes"):
        return asyncio.run(_run(str(output_path)))

    print(f"\nTo run later:  agent-augury --config {output_path}")
    return 0


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
    args = parser.parse_args(argv)

    # Validate flag combinations before anything else.
    if args.output is not None and args.config is not None:
        print("error: --output is only valid without --config", file=sys.stderr)
        return 1

    # Mode 1: run from existing config.
    if args.config is not None:
        try:
            return asyncio.run(_run(args.config))
        except Exception as exc:  # noqa: BLE001 — CLI boundary
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # Mode 2: interactive wizard.
    output_path = Path(args.output) if args.output else None
    try:
        return _run_wizard_and_save(output_path)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
