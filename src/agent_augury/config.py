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

# ---------------------------------------------------------------------------
# tools: 섹션 검증 (AGENT_TOOLS_EXPANSION_DESIGN.md §4.7, agent-2 담당)
# ---------------------------------------------------------------------------

# 허용된 tools: 최상위 키
_TOOLS_TOP_KEYS = frozenset({"shell", "web", "file"})

# tools.shell 허용 키
_TOOLS_SHELL_KEYS = frozenset(
    {
        "enabled",
        "allowed",
        "blocked",
        "timeout_seconds",
        "max_output_chars",
        "cwd",
    }
)

# tools.web 허용 키
_TOOLS_WEB_KEYS = frozenset(
    {
        "enabled",
        "search_provider",
        "allow_domains",
        "deny_domains",
        "block_private_ips",
        "timeout_seconds",
        "max_bytes",
        "max_results",
    }
)

# tools.file 허용 키
_TOOLS_FILE_KEYS = frozenset({"edit_enabled", "allowed_roots"})

# 유효한 web search provider (v0.9에서 searxng 추가 예정)
_VALID_SEARCH_PROVIDERS = frozenset({"duckduckgo", "serper", "tavily", "searxng"})

# IP 리터럴 / localhost — allow_domains에 넣으면 경고
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _validate_tools_section(tools: Any, *, where: str) -> None:
    """Validate a ``tools:`` mapping (global or per-agent).

    ``where`` is used for error messages (e.g. "tools", "agents[0].tools").
    """
    if not isinstance(tools, dict):
        raise ConfigError(f"'{where}' must be a mapping")
    for key in tools:
        if key not in _TOOLS_TOP_KEYS:
            raise ConfigError(
                f"'{where}' contains unknown key {key!r} — "
                f"only {sorted(_TOOLS_TOP_KEYS)} are allowed"
            )

    shell = tools.get("shell")
    if shell is not None:
        if not isinstance(shell, dict):
            raise ConfigError(f"'{where}.shell' must be a mapping")
        for key in shell:
            if key not in _TOOLS_SHELL_KEYS:
                raise ConfigError(
                    f"'{where}.shell' contains unknown key {key!r} — "
                    f"only {sorted(_TOOLS_SHELL_KEYS)} are allowed"
                )
        # enabled:true + 빈 allowed → 경고 (agent-1 피드백 5). load_config는
        # 경고를 출력하고 진행한다 (치명 오류 아님 — 기본 활성화 정책).
        if shell.get("enabled", True) is not False:
            allowed = shell.get("allowed")
            if allowed is not None and not isinstance(allowed, list):
                raise ConfigError(f"'{where}.shell.allowed' must be a list")
            if allowed == []:
                print(
                    f"  [config] warning: '{where}.shell.allowed' is empty — "
                    "ALL commands allowed (only the built-in blocklist applies). "
                    "See docs/TOOLS_OPERATION_GUIDE.md for hardening.",
                    flush=True,
                )

    web = tools.get("web")
    if web is not None:
        if not isinstance(web, dict):
            raise ConfigError(f"'{where}.web' must be a mapping")
        for key in web:
            if key not in _TOOLS_WEB_KEYS:
                raise ConfigError(
                    f"'{where}.web' contains unknown key {key!r} — "
                    f"only {sorted(_TOOLS_WEB_KEYS)} are allowed"
                )
        provider = web.get("search_provider")
        if provider is not None and (not isinstance(provider, str) or provider.lower() not in _VALID_SEARCH_PROVIDERS):
            raise ConfigError(
                f"'{where}.web.search_provider' must be one of "
                f"{sorted(_VALID_SEARCH_PROVIDERS)}, got {provider!r}"
            )
        allow_domains = web.get("allow_domains")
        if allow_domains is not None:
            if not isinstance(allow_domains, list):
                raise ConfigError(f"'{where}.web.allow_domains' must be a list")
            for d in allow_domains:
                if not isinstance(d, str):
                    raise ConfigError(f"'{where}.web.allow_domains' entries must be strings")
                if d.lower().strip() in _LOCALHOST_NAMES or re.match(
                    r"^\d{1,3}(\.\d{1,3}){3}$", d.strip()
                ):
                    print(
                        f"  [config] warning: '{where}.web.allow_domains' contains "
                        f"{d!r} (IP literal / localhost) — SSRF deny still applies.",
                        flush=True,
                    )

    file_ = tools.get("file")
    if file_ is not None:
        if not isinstance(file_, dict):
            raise ConfigError(f"'{where}.file' must be a mapping")
        for key in file_:
            if key not in _TOOLS_FILE_KEYS:
                raise ConfigError(
                    f"'{where}.file' contains unknown key {key!r} — "
                    f"only {sorted(_TOOLS_FILE_KEYS)} are allowed"
                )
        roots = file_.get("allowed_roots")
        if roots is not None and (not isinstance(roots, list) or not all(isinstance(r, str) for r in roots)):
            raise ConfigError(f"'{where}.file.allowed_roots' must be a list of strings")


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

    # mode 키는 v1.0부터 코드에 내장 (항상 L3, config에서 무시)
    data.pop("mode", None)

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

        # tools: 섹션 (에이전트별 오버라이드) 검증
        agent_tools = agent.get("tools")
        if agent_tools is not None:
            _validate_tools_section(agent_tools, where=f"agents[{i}].tools")

    # mirror.url_env 검증
    mirror = data.get("mirror")
    if mirror is not None and isinstance(mirror, dict) and "url_env" not in mirror:
        raise ConfigError("mirror requires 'url_env' key")

    # human 섹션은 v1.0+ 코드에 내장 — config 키는 무시 (옵트인 폐기).
    # REPL/TUI always-on: human 참가 + 상시 입력은 Session.from_config에서 항상 활성.
    # (SESSION_TUI_REDESIGN v2.5 이후 human.tui / human.interface 키 검증 없음)
    human = data.get("human")
    if human is not None and isinstance(human, dict):
        pass  # accepted but ignored — no warning

    # tools: 섹션 (전역) 검증 — AGENT_TOOLS_EXPANSION_DESIGN.md §4.7
    tools = data.get("tools")
    if tools is not None:
        _validate_tools_section(tools, where="tools")

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
            if "inbound" in bot and not isinstance(bot["inbound"], bool):
                raise ConfigError(
                    f"bots[{i}].inbound must be a boolean, got {bot['inbound']!r}"
                )

    # M6: slack observe (Incoming Webhook)
    slack = data.get("slack")
    if slack is not None:
        if not isinstance(slack, dict):
            raise ConfigError("'slack' must be a mapping")
        if slack.get("enabled") is False:
            pass
        else:
            mode = slack.get("mode", "observe")
            if mode not in ("observe",):
                raise ConfigError(
                    f"slack.mode must be 'observe' in M6, got {mode!r}"
                )
            url_env = slack.get("url_env")
            webhook = slack.get("webhook")
            if url_env is None and isinstance(webhook, dict):
                url_env = webhook.get("url_env")
            if not url_env:
                raise ConfigError("slack requires 'url_env' (or webhook.url_env)")

    data.setdefault("task", None)
    data.setdefault("max_steps", 0)
    return data
