"""CLI helpers for ``agent-augury sessions`` (M4c)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core.checkpoint import (
    DEFAULT_SESSIONS_DIR,
    CheckpointError,
    list_quarantine,
    list_sessions,
    remove_session,
    show_session,
)


def _fmt_time(ts: Any) -> str:
    if ts is None:
        return "-"
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(float(ts), tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    except (TypeError, ValueError, OSError):
        return str(ts)


def cmd_list(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser() if args.dir else DEFAULT_SESSIONS_DIR
    rows = list_sessions(base)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"(no sessions in {base})")
        return 0
    print(
        f"{'':1}{'ID':<10} {'PHASE':<14} {'UPDATED':<22} {'STATUS':<10} {'BYTES':>10}  AGENTS"
    )
    for r in rows:
        star = "*" if r.get("is_latest") else " "
        agents = ",".join(r.get("agent_ids") or []) or "-"
        print(
            f"{star}{r['short_id']:<10} {(r.get('phase') or '-')!s:<14} "
            f"{_fmt_time(r.get('updated_at')):<22} {r.get('status') or '-':<10} "
            f"{int(r.get('approx_bytes') or 0):>10}  {agents}"
        )
        print(f"  {r['session_id']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser() if args.dir else DEFAULT_SESSIONS_DIR
    try:
        info = show_session(args.session_id, base_dir=base)
    except CheckpointError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser() if args.dir else DEFAULT_SESSIONS_DIR
    sid = args.session_id
    if not args.yes:
        ans = input(f"Delete session {sid}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("cancelled")
            return 130
    try:
        remove_session(sid, base_dir=base)
    except CheckpointError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"removed {sid}")
    return 0


def cmd_quarantine_list(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser() if args.dir else DEFAULT_SESSIONS_DIR
    rows = list_quarantine(base)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"(no quarantined sessions in {base / 'quarantine'})")
        return 0
    for r in rows:
        print(f"{r['name']}  {r.get('approx_bytes', 0)} bytes")
        if r.get("reason"):
            for line in str(r["reason"]).splitlines():
                print(f"  {line}")
    return 0


def build_sessions_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-augury sessions")
    p.add_argument(
        "--dir",
        default=None,
        help="sessions root (default: ~/.agent-augury/sessions)",
    )
    sub = p.add_subparsers(dest="sessions_cmd", required=True)

    list_p = sub.add_parser("list", help="list local sessions")
    list_p.add_argument("--json", action="store_true")
    list_p.set_defaults(func=cmd_list)

    show_p = sub.add_parser("show", help="show one session")
    show_p.add_argument("session_id")
    show_p.set_defaults(func=cmd_show)

    rm_p = sub.add_parser("rm", help="delete a session directory")
    rm_p.add_argument("session_id")
    rm_p.add_argument("--yes", "-y", action="store_true")
    rm_p.set_defaults(func=cmd_rm)

    q = sub.add_parser("quarantine", help="quarantine management")
    qsub = q.add_subparsers(dest="q_cmd", required=True)
    ql = qsub.add_parser("list", help="list quarantined checkpoints")
    ql.add_argument("--json", action="store_true")
    ql.set_defaults(func=cmd_quarantine_list)

    return p


def run_sessions_cli(argv: list[str] | None = None) -> int:
    parser = build_sessions_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
