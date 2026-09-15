"""Fail-closed tool approval tokens (P0 — TOOL_HUMAN_APPROVAL_DESIGN).

Static class policy lives on ``ToolPolicy``; per-call tokens live here.
``_execute_tool`` wiring (pending return / grant-time execute) is M1+.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

ApprovalState = Literal["pending", "granted", "denied", "expired", "executed"]
ApprovalDecision = Literal["granted", "denied"]
ApprovalClass = Literal["shell", "file_write", "web"]
GateDecision = Literal["execute", "bypass", "deny_no_channel", "require_approval"]

_SHELL_TOOLS = frozenset({"run_command"})
_FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file", "append_file"})
_WEB_TOOLS = frozenset({"web_search", "fetch_url"})

# Hermes-adjacent: only escalate these shell patterns (not every run_command).
# Case-insensitive; matched against a lightly normalized command string.
_DANGEROUS_SHELL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*|--force)\b",
        r"\brmdir\s+/s\b",
        r"\b(del|erase)\s+/[sq]\b",
        r"\bformat\s+[a-z]:",
        r"\bmkfs\b",
        r"\bdd\s+.*\bof=/dev/",
        r">\s*/dev/sd",
        r"\b(shutdown|reboot|poweroff|halt)\b",
        r":\(\)\s*\{\s*:\|:&\s*\}\s*;?",  # fork bomb
        r"\bcurl\b.*\|\s*(ba)?sh\b",
        r"\bwget\b.*\|\s*(ba)?sh\b",
        r"\|\s*(ba)?sh\s*$",
        r"\bchmod\s+-R\s+777\s+/",
        r"\bchown\s+-R\b.*/",
        r"\b(drop|truncate)\s+(database|table)\b",
        r"\bgit\s+push\s+.*--force",
        r"\bInvoke-Expression\b",
        r"\biex\s*\(",
        r"\bRemove-Item\s+.*-Recurse\b.*-Force\b.*[C-Z]:\\",
    )
)


def detect_dangerous_shell_command(command: str) -> str | None:
    """Return a short reason if *command* looks destructive; else None."""
    text = " ".join((command or "").split())
    if not text:
        return None
    for pat in _DANGEROUS_SHELL_PATTERNS:
        if pat.search(text):
            return f"matched dangerous pattern: {pat.pattern!r}"
    return None


def tool_approval_class(tool: str) -> ApprovalClass | None:
    """Map a tool name to an approval class, or None if ungated."""
    if tool in _SHELL_TOOLS:
        return "shell"
    if tool in _FILE_WRITE_TOOLS:
        return "file_write"
    if tool in _WEB_TOOLS:
        return "web"
    return None


def normalize_args(args: dict[str, Any]) -> Any:
    """Stable JSON-serializable form for digests (sorted keys, path-ish normalize)."""

    def _norm(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): _norm(value[k]) for k in sorted(value, key=str)}
        if isinstance(value, (list, tuple)):
            return [_norm(v) for v in value]
        if isinstance(value, str):
            # Light path normalize: backslash → slash (Windows), strip trailing slash
            # except drive roots; keep command strings otherwise unchanged.
            if "/" in value or "\\" in value:
                cleaned = value.replace("\\", "/")
                if len(cleaned) > 1 and cleaned.endswith("/"):
                    cleaned = cleaned.rstrip("/")
                return cleaned
            return value
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)

    return _norm(args)


def args_digest(tool: str, args: dict[str, Any]) -> str:
    """SHA-256 hex digest of tool name + normalized args."""
    payload = {"tool": tool, "args": normalize_args(args)}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pending_result(approval_id: str, tool: str, *, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tool-result payload when execution is deferred pending human approval."""
    out: dict[str, Any] = {
        "status": "pending_approval",
        "approval_id": approval_id,
        "tool": tool,
    }
    if args is not None:
        out["args_preview"] = normalize_args(args)
    return out


def denied_result(reason: str, *, approval_id: str | None = None, tool: str | None = None) -> dict[str, Any]:
    """Tool-result payload for fail-closed denial (no side effect ran)."""
    out: dict[str, Any] = {"status": "denied", "reason": reason}
    if approval_id is not None:
        out["approval_id"] = approval_id
    if tool is not None:
        out["tool"] = tool
    return out


def radio_line(
    *,
    approval_id: str,
    decision: str,
    tool: str,
    reason: str | None = None,
    result_summary: str | None = None,
) -> str:
    """Full display line (includes ``[radio] from human:`` prefix)."""
    return f"[radio] from human: {approval_notice_body(approval_id=approval_id, decision=decision, tool=tool, reason=reason, result_summary=result_summary)}"


def approval_notice_body(
    *,
    approval_id: str,
    decision: str,
    tool: str,
    reason: str | None = None,
    result_summary: str | None = None,
) -> str:
    """Inbox message body (``format_radio_block`` adds ``[radio]`` / ``from``)."""
    base = f"approval_id={approval_id} {decision.upper()} tool={tool}"
    if reason:
        base += f" reason={reason}"
    if result_summary:
        base += f" {result_summary}"
    return base


@dataclass
class ApprovalRecord:
    approval_id: str
    agent_id: str
    tool: str
    args_digest: str
    args_snapshot: dict[str, Any]
    created_at: float
    expires_at: float
    state: ApprovalState = "pending"
    reason: str | None = None

    @property
    def approval_class(self) -> ApprovalClass | None:
        return tool_approval_class(self.tool)


@dataclass
class ApprovalStore:
    """In-process approval tokens owned by a Session (P0: memory only)."""

    _by_id: dict[str, ApprovalRecord] = field(default_factory=dict)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._by_id.get(approval_id)

    def find_pending(
        self,
        agent_id: str,
        tool: str,
        digest: str,
        *,
        now: float | None = None,
    ) -> ApprovalRecord | None:
        """Return an unexpired pending token for the same call fingerprint."""
        now = time.time() if now is None else now
        for rec in self._by_id.values():
            if (
                rec.state == "pending"
                and rec.agent_id == agent_id
                and rec.tool == tool
                and rec.args_digest == digest
                and rec.expires_at > now
            ):
                return rec
        return None

    def request_or_join(
        self,
        agent_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> tuple[ApprovalRecord, bool]:
        """Create a pending token or join an existing one.

        Returns ``(record, created)`` where ``created`` is False on join.
        """
        now = time.time() if now is None else now
        digest = args_digest(tool, args)
        existing = self.find_pending(agent_id, tool, digest, now=now)
        if existing is not None:
            return existing, False
        ttl = max(1.0, float(ttl_seconds))
        rec = ApprovalRecord(
            approval_id=str(uuid.uuid4()),
            agent_id=agent_id,
            tool=tool,
            args_digest=digest,
            args_snapshot=dict(normalize_args(args)),
            created_at=now,
            expires_at=now + ttl,
            state="pending",
        )
        self._by_id[rec.approval_id] = rec
        return rec, True

    def resolve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        *,
        reason: str | None = None,
        now: float | None = None,
    ) -> ApprovalRecord:
        """Apply grant/deny. Raises ``KeyError`` / ``ValueError`` on bad state."""
        now = time.time() if now is None else now
        rec = self._by_id.get(approval_id)
        if rec is None:
            raise KeyError(f"unknown approval_id: {approval_id}")
        if rec.state == "pending" and rec.expires_at <= now:
            rec.state = "expired"
            rec.reason = "expired"
            raise ValueError(f"approval {approval_id} expired")
        if rec.state != "pending":
            raise ValueError(f"approval {approval_id} is {rec.state}, not pending")
        rec.state = "granted" if decision == "granted" else "denied"
        rec.reason = reason or ("user" if decision == "denied" else None)
        return rec

    def expire_due(self, *, now: float | None = None) -> list[ApprovalRecord]:
        """Move overdue pending tokens to expired; return newly expired records."""
        now = time.time() if now is None else now
        expired: list[ApprovalRecord] = []
        for rec in self._by_id.values():
            if rec.state == "pending" and rec.expires_at <= now:
                rec.state = "expired"
                rec.reason = "expired"
                expired.append(rec)
        return expired

    def mark_executed(self, approval_id: str) -> ApprovalRecord:
        rec = self._by_id.get(approval_id)
        if rec is None:
            raise KeyError(f"unknown approval_id: {approval_id}")
        if rec.state != "granted":
            raise ValueError(f"approval {approval_id} is {rec.state}, not granted")
        rec.state = "executed"
        return rec

    def digest_matches(self, approval_id: str, tool: str, args: dict[str, Any]) -> bool:
        rec = self._by_id.get(approval_id)
        if rec is None:
            return False
        return rec.tool == tool and rec.args_digest == args_digest(tool, args)

    def export_pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Serialize unexpired pending records for checkpoint (M4a)."""
        now = time.time() if now is None else now
        out: list[dict[str, Any]] = []
        for rec in self._by_id.values():
            if rec.state != "pending":
                continue
            if rec.expires_at <= now:
                continue
            out.append(
                {
                    "approval_id": rec.approval_id,
                    "agent_id": rec.agent_id,
                    "tool": rec.tool,
                    "args_digest": rec.args_digest,
                    "args_snapshot": dict(rec.args_snapshot),
                    "created_at": rec.created_at,
                    "expires_at": rec.expires_at,
                    "state": "pending",
                    "reason": rec.reason,
                }
            )
        return out

    def import_pending(
        self,
        records: list[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> tuple[list[ApprovalRecord], list[ApprovalRecord]]:
        """Load pending records. Returns ``(alive, expired_on_import)``."""
        now = time.time() if now is None else now
        alive: list[ApprovalRecord] = []
        expired: list[ApprovalRecord] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            try:
                rec = ApprovalRecord(
                    approval_id=str(raw["approval_id"]),
                    agent_id=str(raw["agent_id"]),
                    tool=str(raw["tool"]),
                    args_digest=str(raw.get("args_digest") or ""),
                    args_snapshot=dict(raw.get("args_snapshot") or {}),
                    created_at=float(raw.get("created_at") or now),
                    expires_at=float(raw.get("expires_at") or now),
                    state="pending",
                    reason=raw.get("reason"),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if rec.expires_at <= now:
                rec.state = "expired"
                rec.reason = "expired"
                self._by_id[rec.approval_id] = rec
                expired.append(rec)
            else:
                self._by_id[rec.approval_id] = rec
                alive.append(rec)
        return alive, expired


def gate_decision(
    *,
    requires_approval: bool,
    bypass: bool,
    has_interact_surface: bool,
) -> GateDecision:
    """Fail-closed gate without Store I/O (pure policy)."""
    if not requires_approval:
        return "execute"
    if bypass:
        return "bypass"
    if not has_interact_surface:
        return "deny_no_channel"
    return "require_approval"
