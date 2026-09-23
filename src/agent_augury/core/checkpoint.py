"""Session checkpoint & resume (SESSION_RESUME_DESIGN + M4).

Disk layout under ``~/.agent-augury/sessions/<session_id>/``:

- ``meta.json`` — id, schema, fingerprint, phase, timestamps
- ``conversations.json`` — per-agent conversation / created_threads / language
- ``protocol.json`` — phase + gate snapshots
- ``inbox.json`` — undrained message ids per agent
- ``approvals.json`` — pending tool approvals (M4a)
- ``bindings.json`` — platform↔thread + bridge HITL queue (A5)
- ``server.sqlite`` — MessageServer D5
- ``../LATEST`` — last session pointer
- ``../quarantine/`` — corrupt checkpoints (M4d)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
DEFAULT_SESSIONS_DIR = Path.home() / ".agent-augury" / "sessions"
LATEST_NAME = "LATEST"
QUARANTINE_DIRNAME = "quarantine"
DEFAULT_QUARANTINE_ON = frozenset(
    {"corrupt_json", "unsupported_schema", "sqlite_error"}
)


class CheckpointError(Exception):
    """Checkpoint load/save failure (caller may fall back to fresh)."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or "checkpoint_error"


@dataclass
class CompactOptions:
    enabled: bool = True
    soft_limit_chars: int = 200_000
    keep_tail_chars: int = 80_000
    keep_tail_messages: int = 40
    llm_summary: bool = False


@dataclass
class CheckpointConfig:
    """Runtime options derived from YAML ``session.checkpoint`` + CLI."""

    enabled: bool = True
    dir: Path = field(default_factory=lambda: DEFAULT_SESSIONS_DIR)
    flush_debounce_ms: int = 1000
    flush_interval_s: float = 30.0
    resume: str = "auto"  # auto | ask | never
    session_id: str | None = None
    new_session: bool = False
    cli_session_id: str | None = None
    approvals_persist: bool = True
    compact: CompactOptions = field(default_factory=CompactOptions)
    quarantine_on: frozenset[str] = field(
        default_factory=lambda: DEFAULT_QUARANTINE_ON
    )


@dataclass
class SessionBootstrap:
    """Resolved paths for opening a Session."""

    session_id: str
    session_dir: Path
    db_path: Path
    enabled: bool
    resumed: bool
    resume_failed: str | None = None
    meta: dict[str, Any] | None = None
    conversations: dict[str, Any] | None = None
    protocol: dict[str, Any] | None = None
    inbox: dict[str, list[str]] | None = None
    approvals: list[dict[str, Any]] | None = None
    approvals_corrupt: bool = False


def config_fingerprint(cfg: dict[str, Any], *, config_path: str | None = None) -> str:
    """Stable fingerprint: agent ids + protocol presence (+ optional config path)."""
    agents = cfg.get("agents") or []
    ids = sorted(str(a.get("id", "")) for a in agents if isinstance(a, dict))
    proto = cfg.get("protocol") if isinstance(cfg.get("protocol"), dict) else {}
    ha = proto.get("human_approval") if isinstance(proto.get("human_approval"), dict) else {}
    ha_norm = {
        "P2_SPLIT": bool(ha.get("P2_SPLIT", False)),
        "P3_EXECUTE": bool(ha.get("P3_EXECUTE", False)),
        "P4_REVIEW": bool(ha.get("P4_REVIEW", False)),
        "P5_SUBMIT": bool(ha.get("P5_SUBMIT", False)),
    }
    payload = {
        "agents": ids,
        "protocol": bool(cfg.get("protocol")),
        "human_approval": ha_norm,
        "gate": bool(cfg.get("gate")),
        "config_path": str(Path(config_path).resolve()) if config_path else "",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def parse_checkpoint_config(
    cfg: dict[str, Any],
    *,
    demo: bool = False,
    new_session: bool = False,
    cli_session_id: str | None = None,
) -> CheckpointConfig:
    """Build CheckpointConfig from YAML + CLI. ``--demo`` forces disabled."""
    if demo:
        return CheckpointConfig(enabled=False, new_session=True)

    sess = cfg.get("session") if isinstance(cfg.get("session"), dict) else {}
    cp = sess.get("checkpoint") if isinstance(sess.get("checkpoint"), dict) else {}

    enabled = cp.get("enabled", True)
    if enabled is None:
        enabled = True
    enabled = bool(enabled)

    resume = str(cp.get("resume", "auto")).strip().lower()
    if resume not in ("auto", "ask", "never"):
        resume = "auto"

    dir_raw = cp.get("dir")
    base = Path(str(dir_raw)).expanduser() if dir_raw else DEFAULT_SESSIONS_DIR

    debounce = int(cp.get("flush_debounce_ms", 1000) or 1000)
    interval = float(cp.get("flush_interval_s", 30) or 30)

    yaml_id = sess.get("id")
    yaml_id_s = str(yaml_id).strip() if yaml_id else None

    env_id = os.environ.get("AGENT_AUGURY_SESSION", "").strip() or None
    env_new = os.environ.get("AGENT_AUGURY_NEW_SESSION", "").strip() in (
        "1",
        "true",
        "True",
        "yes",
    )

    approvals_persist = cp.get("approvals_persist", True)
    if approvals_persist is None:
        approvals_persist = True

    compact_raw = cp.get("compact") if isinstance(cp.get("compact"), dict) else {}
    compact = CompactOptions(
        enabled=bool(compact_raw.get("enabled", True)),
        soft_limit_chars=int(compact_raw.get("soft_limit_chars", 200_000) or 200_000),
        keep_tail_chars=int(compact_raw.get("keep_tail_chars", 80_000) or 80_000),
        keep_tail_messages=int(compact_raw.get("keep_tail_messages", 40) or 40),
        llm_summary=bool(compact_raw.get("llm_summary", False)),
    )

    q_raw = cp.get("quarantine_on")
    if isinstance(q_raw, list) and q_raw:
        quarantine_on = frozenset(str(x) for x in q_raw)
    else:
        quarantine_on = DEFAULT_QUARANTINE_ON

    return CheckpointConfig(
        enabled=enabled,
        dir=base,
        flush_debounce_ms=max(0, debounce),
        flush_interval_s=max(0.0, interval),
        resume="never" if (new_session or env_new) else resume,
        session_id=yaml_id_s,
        new_session=bool(new_session or env_new),
        cli_session_id=(cli_session_id or env_id),
        approvals_persist=bool(approvals_persist),
        compact=compact,
        quarantine_on=quarantine_on,
    )


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class CheckpointStore:
    """Filesystem checkpoint store for one session directory."""

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = session_id
        self.meta_path = self.session_dir / "meta.json"
        self.conversations_path = self.session_dir / "conversations.json"
        self.protocol_path = self.session_dir / "protocol.json"
        self.inbox_path = self.session_dir / "inbox.json"
        self.approvals_path = self.session_dir / "approvals.json"
        self.db_path = self.session_dir / "server.sqlite"

    def ensure_dir(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.session_dir, 0o700)
        except OSError:
            pass

    def exists(self) -> bool:
        return self.meta_path.is_file()

    def save(
        self,
        *,
        fingerprint: str,
        conversations: dict[str, Any],
        protocol: dict[str, Any] | None,
        inbox: dict[str, list[str]],
        exit_reason: str | None = None,
        phase: str | None = None,
        created_at: float | None = None,
        approvals: list[dict[str, Any]] | None = None,
        compactions: list[dict[str, Any]] | None = None,
        pending_approvals: int | None = None,
        task: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_dir()
        now = time.time()
        prev: dict[str, Any] = {}
        if self.meta_path.is_file():
            try:
                prev = _read_json(self.meta_path)
            except (OSError, json.JSONDecodeError):
                prev = {}
        prev_compactions = list(prev.get("compactions") or [])
        if compactions:
            prev_compactions.extend(compactions)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "config_fingerprint": fingerprint,
            "created_at": created_at or prev.get("created_at") or now,
            "updated_at": now,
            "checkpoint_seq": int(prev.get("checkpoint_seq") or 0) + 1,
            "exit_reason": exit_reason,
            "phase": phase,
            # The request the round is serving. P2/P5 hold the team to it, so
            # losing it on resume would quietly disarm both checks.
            "task": task or prev.get("task"),
            "agent_ids": sorted(conversations.keys()),
            "compactions": prev_compactions[-50:],
            "pending_approvals": (
                pending_approvals
                if pending_approvals is not None
                else len(approvals or [])
            ),
        }
        _atomic_write_json(self.conversations_path, conversations)
        _atomic_write_json(self.protocol_path, protocol or {})
        _atomic_write_json(self.inbox_path, inbox)
        _atomic_write_json(
            self.approvals_path,
            {"schema_version": 1, "records": list(approvals or [])},
        )
        _atomic_write_json(self.meta_path, meta)
        for p in (
            self.meta_path,
            self.conversations_path,
            self.protocol_path,
            self.inbox_path,
            self.approvals_path,
        ):
            _chmod_private(p)
        return meta

    def load_approvals(self) -> tuple[list[dict[str, Any]], bool]:
        """Return (records, corrupt). Missing file → ([], False)."""
        if not self.approvals_path.is_file():
            return [], False
        try:
            data = _read_json(self.approvals_path)
        except (OSError, json.JSONDecodeError):
            return [], True
        if not isinstance(data, dict):
            return [], True
        records = data.get("records")
        if records is None:
            return [], False
        if not isinstance(records, list):
            return [], True
        return [r for r in records if isinstance(r, dict)], False

    def load(self) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, list[str]],
        list[dict[str, Any]],
        bool,
    ]:
        if not self.meta_path.is_file():
            raise CheckpointError(
                f"no meta.json in {self.session_dir}", code="corrupt_json"
            )
        try:
            meta = _read_json(self.meta_path)
            conversations = _read_json(self.conversations_path)
            protocol = (
                _read_json(self.protocol_path) if self.protocol_path.is_file() else {}
            )
            inbox = _read_json(self.inbox_path) if self.inbox_path.is_file() else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"corrupt checkpoint: {exc}", code="corrupt_json"
            ) from exc
        ver = int(meta.get("schema_version") or 0)
        if ver not in SUPPORTED_SCHEMA_VERSIONS:
            raise CheckpointError(
                f"unsupported schema_version {meta.get('schema_version')!r}",
                code="unsupported_schema",
            )
        if not isinstance(conversations, dict):
            raise CheckpointError(
                "conversations.json must be a mapping", code="corrupt_json"
            )
        if not isinstance(inbox, dict):
            inbox = {}
        approvals, approvals_corrupt = self.load_approvals()
        return (
            meta,
            conversations,
            protocol if isinstance(protocol, dict) else {},
            {
                str(k): [str(x) for x in (v or [])]
                for k, v in inbox.items()
                if isinstance(v, list)
            },
            approvals,
            approvals_corrupt,
        )


def write_latest(base_dir: Path, session_id: str, fingerprint: str) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / LATEST_NAME
    _atomic_write_json(
        path,
        {"session_id": session_id, "config_fingerprint": fingerprint},
    )


def read_latest(base_dir: Path) -> dict[str, Any] | None:
    path = base_dir / LATEST_NAME
    if not path.is_file():
        return None
    try:
        data = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_latest_if(base_dir: Path, session_id: str) -> None:
    latest = read_latest(base_dir)
    if latest and str(latest.get("session_id")) == session_id:
        path = base_dir / LATEST_NAME
        try:
            path.unlink()
        except OSError:
            pass


def quarantine_session(
    base_dir: Path,
    session_id: str,
    *,
    reason: str,
    code: str = "corrupt_json",
) -> Path | None:
    """Move ``sessions/<id>`` → ``sessions/quarantine/<id>-<utc>/``. Return dest or None."""
    src = base_dir / session_id
    if not src.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest_root = base_dir / QUARANTINE_DIRNAME
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / f"{session_id}-{stamp}"
    if dest.exists():
        dest = dest_root / f"{session_id}-{stamp}-{uuid.uuid4().hex[:6]}"
    shutil.move(str(src), str(dest))
    reason_path = dest / "REASON.txt"
    reason_path.write_text(
        f"code={code}\nreason={reason}\n",
        encoding="utf-8",
    )
    clear_latest_if(base_dir, session_id)
    return dest


def _dir_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def list_sessions(base_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return session summaries under *base_dir* (excludes quarantine)."""
    base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_DIR
    if not base.is_dir():
        return []
    latest = read_latest(base)
    latest_id = str(latest.get("session_id")) if latest else None
    rows: list[dict[str, Any]] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name == QUARANTINE_DIRNAME:
            continue
        meta_path = child / "meta.json"
        status = "ok"
        meta: dict[str, Any] = {}
        if not meta_path.is_file():
            status = "corrupt"
        else:
            try:
                meta = _read_json(meta_path)
                ver = int(meta.get("schema_version") or 0)
                if ver not in SUPPORTED_SCHEMA_VERSIONS:
                    status = "corrupt"
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                status = "corrupt"
                meta = {}
        sid = str(meta.get("session_id") or child.name)
        rows.append(
            {
                "session_id": sid,
                "short_id": sid[:8],
                "updated_at": meta.get("updated_at"),
                "phase": meta.get("phase"),
                "exit_reason": meta.get("exit_reason"),
                "agent_ids": meta.get("agent_ids") or [],
                "fingerprint": meta.get("config_fingerprint"),
                "status": status,
                "approx_bytes": _dir_size(child),
                "is_latest": sid == latest_id,
                "pending_approvals": meta.get("pending_approvals"),
                "path": str(child),
            }
        )
    rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
    return rows


def list_quarantine(base_dir: Path | None = None) -> list[dict[str, Any]]:
    base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_DIR
    qdir = base / QUARANTINE_DIRNAME
    if not qdir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(qdir.iterdir(), key=lambda p: p.name, reverse=True):
        if not child.is_dir():
            continue
        reason = ""
        rp = child / "REASON.txt"
        if rp.is_file():
            try:
                reason = rp.read_text(encoding="utf-8").strip()
            except OSError:
                reason = ""
        rows.append(
            {
                "name": child.name,
                "path": str(child),
                "reason": reason,
                "approx_bytes": _dir_size(child),
                "status": "quarantined",
            }
        )
    return rows


def show_session(session_id: str, *, base_dir: Path | None = None) -> dict[str, Any]:
    base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_DIR
    store = CheckpointStore(base / session_id, session_id)
    if not store.exists():
        raise CheckpointError(f"session not found: {session_id}", code="not_found")
    meta, conversations, protocol, inbox, approvals, approvals_corrupt = store.load()
    per_agent: dict[str, Any] = {}
    for aid, blob in conversations.items():
        conv = blob.get("conversation") if isinstance(blob, dict) else None
        n = len(conv) if isinstance(conv, list) else 0
        chars = 0
        if isinstance(conv, list):
            try:
                chars = len(json.dumps(conv, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                chars = 0
        per_agent[str(aid)] = {"messages": n, "chars": chars}
    return {
        "meta": meta,
        "protocol_phase": (protocol or {}).get("phase"),
        "inbox_sizes": {k: len(v) for k, v in inbox.items()},
        "pending_approvals": len(approvals),
        "approvals_corrupt": approvals_corrupt,
        "has_sqlite": store.db_path.is_file(),
        "agents": per_agent,
        "path": str(store.session_dir),
    }


def remove_session(session_id: str, *, base_dir: Path | None = None) -> None:
    base = Path(base_dir) if base_dir else DEFAULT_SESSIONS_DIR
    target = base / session_id
    if not target.exists():
        raise CheckpointError(f"session not found: {session_id}", code="not_found")
    shutil.rmtree(target)
    clear_latest_if(base, session_id)


def _fresh_boot(opts: CheckpointConfig, *, resume_failed: str | None = None) -> SessionBootstrap:
    sid = str(uuid.uuid4())
    sdir = opts.dir / sid
    return SessionBootstrap(
        session_id=sid,
        session_dir=sdir,
        db_path=sdir / "server.sqlite",
        enabled=True,
        resumed=False,
        resume_failed=resume_failed,
    )


def bootstrap_session(
    cfg: dict[str, Any],
    *,
    config_path: str | None = None,
    demo: bool = False,
    new_session: bool = False,
    cli_session_id: str | None = None,
) -> SessionBootstrap:
    """Resolve session id, optionally load checkpoint (fail → fresh)."""
    opts = parse_checkpoint_config(
        cfg,
        demo=demo,
        new_session=new_session,
        cli_session_id=cli_session_id,
    )
    fp = config_fingerprint(cfg, config_path=config_path)

    if not opts.enabled:
        sid = str(uuid.uuid4())
        sdir = opts.dir / sid
        return SessionBootstrap(
            session_id=sid,
            session_dir=sdir,
            db_path=sdir / "server.sqlite",
            enabled=False,
            resumed=False,
        )

    if opts.new_session or opts.resume == "never":
        sid = opts.cli_session_id or opts.session_id or str(uuid.uuid4())
        sdir = opts.dir / sid
        return SessionBootstrap(
            session_id=sid,
            session_dir=sdir,
            db_path=sdir / "server.sqlite",
            enabled=True,
            resumed=False,
        )

    candidate = opts.cli_session_id or opts.session_id
    if candidate is None:
        latest = read_latest(opts.dir)
        if latest and latest.get("config_fingerprint") == fp:
            candidate = str(latest.get("session_id") or "") or None

    if not candidate:
        return _fresh_boot(opts)

    store = CheckpointStore(opts.dir / candidate, candidate)
    if not store.exists():
        return SessionBootstrap(
            session_id=candidate,
            session_dir=store.session_dir,
            db_path=store.db_path,
            enabled=True,
            resumed=False,
        )

    try:
        meta, conversations, protocol, inbox, approvals, approvals_corrupt = store.load()
        if meta.get("config_fingerprint") != fp:
            raise CheckpointError(
                f"fingerprint mismatch: checkpoint={meta.get('config_fingerprint')!r} "
                f"current={fp!r}",
                code="fingerprint_mismatch",
            )
        agent_ids = sorted(
            str(a.get("id")) for a in (cfg.get("agents") or []) if isinstance(a, dict)
        )
        saved_ids = sorted(str(x) for x in (meta.get("agent_ids") or conversations.keys()))
        if agent_ids != saved_ids:
            raise CheckpointError(
                f"agent id set mismatch: checkpoint={saved_ids} current={agent_ids}",
                code="agent_mismatch",
            )
        return SessionBootstrap(
            session_id=candidate,
            session_dir=store.session_dir,
            db_path=store.db_path,
            enabled=True,
            resumed=True,
            meta=meta,
            conversations=conversations,
            protocol=protocol,
            inbox=inbox,
            approvals=approvals,
            approvals_corrupt=approvals_corrupt,
        )
    except CheckpointError as exc:
        code = exc.code
        # Hard corrupt → quarantine when configured
        if code in opts.quarantine_on:
            dest = quarantine_session(
                opts.dir, candidate, reason=str(exc), code=code
            )
            qmsg = f"quarantined:{code}:{exc}"
            if dest:
                qmsg += f" -> {dest}"
            return _fresh_boot(opts, resume_failed=qmsg)
        # fingerprint / agent mismatch: leave in place
        return _fresh_boot(opts, resume_failed=str(exc))
