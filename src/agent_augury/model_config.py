"""Model config persistence — stores agent/backend settings between runs.

Model settings (max_steps, agent IDs, backend types, model names,
base URLs, env-var names) and optional messaging ``bots`` bindings are
separated from the per-run session YAML and persisted to
``~/.agent-augury/model_config.json``.  Subsequent wizard runs reuse these
and skip both the model-configuration and messaging-app prompts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Default persistence path — kept independent of any session YAML so the
# model config survives across sessions.
DEFAULT_MODEL_CONFIG_PATH = Path.home() / ".agent-augury" / "model_config.json"


def _resolve_path(path: Path | None = None) -> Path:
    """Return *path* if given, else the default model-config path."""
    return path if path is not None else DEFAULT_MODEL_CONFIG_PATH


def save_model_config(
    max_steps: int,
    agents: list[dict[str, Any]],
    path: Path | None = None,
    *,
    bots: list[dict[str, Any]] | None = None,
) -> Path:
    """Persist model settings (and optional Discord ``bots``) to disk.

    Returns the path written.  The file is written as JSON with
    ``ensure_ascii=False`` so Unicode agent IDs are human-readable.

    ``bots`` is always written (default ``[]``) so later wizard runs know
    messaging was already decided and can skip the prompt.
    """
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "max_steps": max_steps,
        "agents": agents,
        "bots": list(bots) if bots is not None else [],
    }
    target.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return target


def load_model_config(path: Path | None = None) -> dict[str, Any] | None:
    """Load saved model config.

    Returns ``None`` if the file does not exist, cannot be parsed, or is
    missing required keys.  A saved config whose ``agents`` is not a
    non-empty list is treated as invalid (None) so callers re-collect
    model settings instead of failing late in ``load_config`` (N2-A).

    Missing ``bots`` (older files) is treated as ``[]``.
    """
    target = _resolve_path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None
    # Validate minimum structure.
    if not isinstance(data, dict) or "agents" not in data:
        return None
    agents = data["agents"]
    if not isinstance(agents, list) or not agents:
        return None
    bots = data.get("bots", [])
    if not isinstance(bots, list):
        bots = []
    out = dict(data)
    out["bots"] = bots
    return out


def model_config_exists(path: Path | None = None) -> bool:
    """Return True if a saved model config exists."""
    return _resolve_path(path).exists()


def clear_model_config(path: Path | None = None) -> bool:
    """Remove saved model config.  Returns True if a file was removed."""
    target = _resolve_path(path)
    if target.exists():
        target.unlink()
        return True
    return False
