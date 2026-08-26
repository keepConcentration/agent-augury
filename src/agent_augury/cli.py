"""CLI entrypoint: run a session from YAML and mirror a plain log (§4.2, D3)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from .agent.loop import StepResult
from .config import load_config
from .session import Session


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-augury")
    parser.add_argument("--config", required=True, help="path to session YAML")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args.config))
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
