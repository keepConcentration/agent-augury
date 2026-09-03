"""YAML/CLI config loading & validation (§4.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_VALID_MODES = ("L2", "L3")
_VALID_BACKEND_TYPES = {"fake", "openai", "nous", "nous_oauth"}


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
        backend = agent["backend"]
        btype = backend.get("type")
        if btype not in _VALID_BACKEND_TYPES:
            raise ConfigError(
                f"agents[{i}].backend.type must be one of {_VALID_BACKEND_TYPES}, got {btype!r}"
            )
        # D8: backend type별 필수 키를 빠르게 검증
        if btype == "fake":
            if "script" not in backend:
                raise ConfigError(f"agents[{i}] fake backend requires 'script' key")
        elif btype == "openai":
            if "base_url" not in backend:
                raise ConfigError(f"agents[{i}] openai backend requires 'base_url' key")
            if "api_key_env" not in backend:
                raise ConfigError(f"agents[{i}] openai backend requires 'api_key_env' key")
        elif btype == "nous":
            if "base_url" not in backend:
                raise ConfigError(f"agents[{i}] nous backend requires 'base_url' key")
            if "api_key_env" not in backend:
                raise ConfigError(f"agents[{i}] nous backend requires 'api_key_env' key")
        elif btype == "nous_oauth":
            if "model" not in backend:
                raise ConfigError(f"agents[{i}] nous_oauth backend requires 'model' key")

    # mirror.url_env 검증
    mirror = data.get("mirror")
    if mirror is not None and isinstance(mirror, dict):
        if "url_env" not in mirror:
            raise ConfigError("mirror requires 'url_env' key")

    data["mode"] = mode
    data.setdefault("task", None)
    data.setdefault("max_steps", 20)
    return data
