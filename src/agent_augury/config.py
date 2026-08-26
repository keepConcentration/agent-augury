"""YAML/CLI config loading & validation (§4.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_VALID_MODES = ("L2", "L3")


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    mode = data.get("mode", "L3")
    if mode not in _VALID_MODES:
        raise ConfigError(f"mode must be one of {_VALID_MODES}, got {mode!r}")

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError("'agents' must be a non-empty list")
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict) or "id" not in agent:
            raise ConfigError(f"agents[{i}] must be a mapping with an 'id'")
        if not isinstance(agent.get("backend"), dict):
            raise ConfigError(f"agents[{i}].backend must be a mapping")

    data["mode"] = mode
    data.setdefault("task", None)
    data.setdefault("max_steps", 20)
    return data
