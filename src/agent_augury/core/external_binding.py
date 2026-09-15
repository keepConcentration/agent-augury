"""Platform ↔ Core thread / HITL binding persistence (A5).

Disk: ``<session_dir>/bindings.json`` (checkpoint enabled sessions only).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BINDINGS_SCHEMA_VERSION = 1
BINDINGS_FILENAME = "bindings.json"


@dataclass
class BindingsSnapshot:
    schema_version: int = BINDINGS_SCHEMA_VERSION
    recent_thread: str | None = None
    platform_threads: dict[str, str] = field(default_factory=dict)
    pending_questions: list[dict[str, Any]] = field(default_factory=list)


def platform_ref_key(source: dict[str, Any] | None) -> str | None:
    """Stable key for a chat platform location (Discord channel/thread, …)."""
    if not source or not isinstance(source, dict):
        return None
    surface = str(source.get("surface") or "").strip()
    channel = source.get("channel")
    if not surface or channel is None:
        return None
    thread = source.get("thread")
    if thread is None:
        thread = source.get("thread_ts")
    thread_s = str(thread).strip() if thread is not None else ""
    return f"{surface}:ch:{channel}:th:{thread_s}"


def lookup_platform_thread(
    snap: BindingsSnapshot | None,
    source: dict[str, Any] | None,
) -> str | None:
    if snap is None:
        return None
    key = platform_ref_key(source)
    if not key:
        return None
    tid = snap.platform_threads.get(key)
    if tid:
        return tid
    if snap.recent_thread:
        return snap.recent_thread
    return None


def record_platform_thread(
    snap: BindingsSnapshot,
    source: dict[str, Any] | None,
    thread_id: str,
) -> None:
    tid = str(thread_id).strip()
    if not tid:
        return
    snap.recent_thread = tid
    key = platform_ref_key(source)
    if key:
        snap.platform_threads[key] = tid


def pending_question_to_dict(pq: Any) -> dict[str, Any]:
    return {
        "question_id": str(getattr(pq, "question_id", "") or ""),
        "thread_id": str(getattr(pq, "thread_id", "") or ""),
        "agent_id": str(getattr(pq, "agent_id", "") or ""),
        "question": str(getattr(pq, "question", "") or ""),
        "options": list(getattr(pq, "options", None) or []),
    }


def pending_from_dict(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "question_id": str(raw.get("question_id") or ""),
        "thread_id": str(raw.get("thread_id") or ""),
        "agent_id": str(raw.get("agent_id") or ""),
        "question": str(raw.get("question") or ""),
        "options": [str(x) for x in (raw.get("options") or [])],
    }


def capture_from_bridge(bridge: Any) -> BindingsSnapshot:
    """Snapshot bridge RAM state (pending deque + recent_thread)."""
    snap = BindingsSnapshot()
    recent = getattr(bridge, "_recent_thread", None)
    if recent:
        snap.recent_thread = str(recent)
    pending = getattr(bridge, "_pending", None)
    if pending is not None:
        for pq in list(pending):
            snap.pending_questions.append(pending_question_to_dict(pq))
    return snap


def merge_snapshots(
    base: BindingsSnapshot | None,
    bridge_capture: BindingsSnapshot,
) -> BindingsSnapshot:
    """Keep platform map from *base*; overlay bridge recent + pending."""
    out = BindingsSnapshot(
        platform_threads=dict(base.platform_threads) if base else {},
    )
    if base and base.recent_thread and not bridge_capture.recent_thread:
        out.recent_thread = base.recent_thread
    if bridge_capture.recent_thread:
        out.recent_thread = bridge_capture.recent_thread
    out.pending_questions = list(bridge_capture.pending_questions)
    return out


def apply_to_bridge(bridge: Any, snap: BindingsSnapshot | None) -> None:
    if snap is None:
        return
    if snap.recent_thread:
        bridge._recent_thread = snap.recent_thread
    from agent_augury.gateway.bridge import PendingQuestion

    bridge._pending = deque()
    for raw in snap.pending_questions:
        if not isinstance(raw, dict):
            continue
        d = pending_from_dict(raw)
        if not d["question_id"] and not d["question"]:
            continue
        bridge._pending.append(PendingQuestion(**d))


def load_bindings(path: Path) -> BindingsSnapshot | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ver = int(data.get("schema_version") or 0)
    if ver != BINDINGS_SCHEMA_VERSION:
        return None
    platform = data.get("platform_threads")
    pending = data.get("pending_questions")
    recent = data.get("recent_thread")
    snap = BindingsSnapshot(
        recent_thread=str(recent).strip() if recent else None,
        platform_threads={
            str(k): str(v)
            for k, v in (platform or {}).items()
            if v is not None and str(v).strip()
        },
        pending_questions=[
            pending_from_dict(x)
            for x in (pending or [])
            if isinstance(x, dict)
        ],
    )
    return snap


def save_bindings(path: Path, snap: BindingsSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": BINDINGS_SCHEMA_VERSION,
        "recent_thread": snap.recent_thread,
        "platform_threads": dict(snap.platform_threads),
        "pending_questions": list(snap.pending_questions),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    try:
        import os

        os.chmod(path, 0o600)
    except OSError:
        pass
