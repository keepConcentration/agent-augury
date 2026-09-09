"""YAML/CLI config loading & validation (§4.1)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .server import RESERVED_NAMES

_VALID_BACKEND_TYPES = {"openai", "nous", "nous_oauth"}

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(data: Any) -> Any:
    """Replace ``${VAR_NAME}`` placeholders with os.environ values.

    Works recursively on nested dicts/lists. Unresolved variables are left
    as-is (the caller will surface a clearer error at validation time).
    """
    if isinstance(data, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), data)
    if isinstance(data, dict):
        return {k: _expand_env_refs(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_expand_env_refs(item) for item in data]
    return data


class ConfigError(Exception):
    pass


def load_config(path: str | Path, allow_fake: bool = False) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    # Expand ${ENV_VAR} references from os.environ
    data = _expand_env_refs(data)

    # mode key is accepted for backward compatibility but ignored.
    # agent-augury is L3-only as of v0.3.
    mode = data.get("mode", "L3")
    if mode not in ("L2", "L3"):
        raise ConfigError(f"mode must be 'L3' (or omitted), got {mode!r}")
    if mode == "L2":
        raise ConfigError(
            "L2 contrast mode removed — agent-augury is L3-only as of v0.3. "
            "Use 'mode: L3' or omit the key."
        )

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError("'agents' must be a non-empty list")
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict) or "id" not in agent:
            raise ConfigError(f"agents[{i}] must be a mapping with an 'id'")
        agent_id = agent["id"]
        if not isinstance(agent_id, str):
            raise ConfigError(f"agents[{i}].id must be a string")
        if agent_id.lower() in RESERVED_NAMES:
            raise ConfigError(
                f"agents[{i}].id {agent_id!r} is reserved for the human participant; "
                f"rename the agent (e.g. 'human-relay')"
            )
        if not isinstance(agent.get("backend"), dict):
            raise ConfigError(f"agents[{i}].backend must be a mapping")
        backend = agent["backend"]
        btype = backend.get("type")
        # allow_fake=True → fake 백엔드 허용 (오프라인 데모/벤치마크 전용)
        if btype == "fake":
            if not allow_fake:
                raise ConfigError(
                    f"agents[{i}].backend.type 'fake' requires --demo flag "
                    f"(offline demo/benchmark only)"
                )
            continue
        if btype not in _VALID_BACKEND_TYPES:
            raise ConfigError(
                f"agents[{i}].backend.type must be one of {_VALID_BACKEND_TYPES}, got {btype!r}"
            )
        # D8: backend type별 필수 키를 빠르게 검증
        if btype == "openai":
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

    # agents 섹션 내 role/role_custom 검증
    roles = data.get("roles")
    if roles is not None:
        if not isinstance(roles, dict):
            raise ConfigError("'roles' must be a mapping")
        for role_name, role_def in roles.items():
            if not isinstance(role_def, dict):
                raise ConfigError(f"roles[{role_name!r}] must be a mapping")
            # 허용된 키만 검증 (description, prompt)
            for key in role_def:
                if key not in ("description", "prompt"):
                    raise ConfigError(
                        f"roles[{role_name!r}] contains unknown key {key!r} — "
                        f"only 'description' and 'prompt' are allowed"
                    )
    for i, agent in enumerate(agents):
        role = agent.get("role")
        role_custom = agent.get("role_custom")
        if role is not None and role_custom is not None:
            raise ConfigError(
                f"agents[{i}] cannot specify both 'role' and 'role_custom' — "
                f"choose one"
            )
        if role is not None:
            if not isinstance(role, str):
                raise ConfigError(f"agents[{i}].role must be a string")
            if roles is None or role not in roles:
                raise ConfigError(
                    f"agents[{i}].role {role!r} is not defined in 'roles' section"
                )
        if role_custom is not None and (not isinstance(role_custom, str) or not role_custom.strip()):
            raise ConfigError(
                f"agents[{i}].role_custom must be a non-empty string"
            )

    # mirror.url_env 검증
    mirror = data.get("mirror")
    if mirror is not None and isinstance(mirror, dict) and "url_env" not in mirror:
        raise ConfigError("mirror requires 'url_env' key")

# human 섹션 검증 (Human-in-the-loop, USER_INTERVENTION_DESIGN.md §5)
    human = data.get("human")
    if human is not None:
        if not isinstance(human, dict):
            raise ConfigError("'human' must be a mapping")
        human_id = human.get("id", "human")
        if human_id.lower() != "human":
            raise ConfigError(
                f"human.id must be 'human' in v1.0 (reserved namespace), got {human_id!r}"
            )
        interface = human.get("interface", "cli")
        if interface not in ("cli", "discord", "file", "tui"):
            raise ConfigError(
                f"human.interface must be one of 'cli', 'discord', 'file', 'tui', got {interface!r}"
            )
        # v1.0: cli/tui 지원 (discord/file는 후속 단계)
        if interface not in ("cli", "tui"):
            raise ConfigError(
                f"human.interface {interface!r} is not supported yet — only 'cli' and 'tui' in v1.0"
            )

        # v1.0: human.tui 섹션 검증 (interface: tui일 때만 사용)
        tui = human.get("tui")
        if tui is not None:
            if not isinstance(tui, dict):
                raise ConfigError("'human.tui' must be a mapping")
            allowed_tui_keys = {
                "response_format", "history_file", "input_prompt",
                "multiline", "full_screen", "choice_queue", "pin_options",
            }
            for key in tui:
                if key not in allowed_tui_keys:
                    raise ConfigError(
                        f"human.tui contains unknown key {key!r} — "
                        f"allowed: {sorted(allowed_tui_keys)}"
                    )
            # Validate response_format if present
            rf = tui.get("response_format", "text")
            if rf not in ("text", "number"):
                raise ConfigError(
                    f"human.tui.response_format must be 'text' or 'number', got {rf!r}"
                )

# bots 섹션 검증 (N개 봇 통합)
    bots = data.get("bots")
    if bots is not None:
        if not isinstance(bots, list):
            raise ConfigError("'bots' must be a list")
        for i, bot in enumerate(bots):
            if not isinstance(bot, dict):
                raise ConfigError(f"bots[{i}] must be a mapping")
            if "agent_id" not in bot:
                raise ConfigError(f"bots[{i}] requires 'agent_id'")
            if "token_env" not in bot:
                raise ConfigError(f"bots[{i}] requires 'token_env'")
            if "channel_id" not in bot:
                raise ConfigError(f"bots[{i}] requires 'channel_id'")
            # channel_id는 int 변환 가능해야 함
            try:
                int(bot["channel_id"])
            except (ValueError, TypeError) as exc:
                raise ConfigError(
                    f"bots[{i}].channel_id must be an integer, got {bot['channel_id']!r}"
                ) from exc

    data["mode"] = "L3"
    data.setdefault("task", None)
    data.setdefault("max_steps", 0)
    return data
