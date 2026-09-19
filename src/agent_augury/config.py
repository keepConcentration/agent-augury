"""YAML/CLI config loading & validation (§4.1)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .bot_token_env import validate_token_env_name
from .core.server import RESERVED_NAMES

_VALID_BACKEND_TYPES = {"openai", "nous", "nous_oauth"}

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# ---------------------------------------------------------------------------
# tools: 섹션 검증 (AGENT_TOOLS_EXPANSION_DESIGN.md §4.7, agent-2 담당)
# ---------------------------------------------------------------------------

# 허용된 tools: 최상위 키
_TOOLS_TOP_KEYS = frozenset({"shell", "web", "file", "approval"})

# tools.approval 허용 키 / 모드 (TOOL_HUMAN_APPROVAL_DESIGN)
_TOOLS_APPROVAL_KEYS = frozenset(
    {"shell", "file_write", "web", "bypass", "ttl_seconds"}
)
_VALID_APPROVAL_MODES = frozenset({"require", "off", "dangerous"})

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

# ---------------------------------------------------------------------------
# attention: 섹션 검증 (AGENT_RELEVANCE_BUDGET_DESIGN.md §6, agent-2 담당)
# ---------------------------------------------------------------------------

# attention: 최상위 허용 키
_ATTENTION_TOP_KEYS = frozenset(
    {"enabled", "mode", "floors", "tiers", "features", "schedule", "context"}
)

# attention.floors 허용 키
_ATTENTION_FLOORS_KEYS = frozenset({"default", "near_gate", "p1_ready_pending"})

# attention.tiers 허용 키
_ATTENTION_TIERS_KEYS = frozenset({"ignore", "skim", "engage"})

# attention.features 허용 키
_ATTENTION_FEATURES_KEYS = frozenset(
    {"mention_boost", "thread_participant", "recent_interact"}
)

# attention.schedule 허용 키
_ATTENTION_SCHEDULE_KEYS = frozenset({"skip_t0_llm"})

# attention.context 허용 키
_ATTENTION_CONTEXT_KEYS = frozenset({"skim_max_chars", "t0_digest"})

# per-agent attention override 허용 키 (간소화: floor만)
_ATTENTION_AGENT_KEYS = frozenset({"floor"})

# attention.mode 허용 값
_VALID_ATTENTION_MODES = frozenset({"heuristic"})

# 기본 attention 설정 (설계문서 §6)
_ATTENTION_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "heuristic",
    "floors": {
        "default": 0.0,
        "near_gate": 0.5,
        "p1_ready_pending": 0.15,
    },
    "tiers": {
        "ignore": 0.15,
        "skim": 0.45,
        "engage": 0.80,
    },
    "features": {
        "mention_boost": 0.5,
        "thread_participant": 0.25,
        "recent_interact": 0.2,
    },
    "schedule": {
        "skip_t0_llm": True,
    },
    "context": {
        "skim_max_chars": 400,
        "t0_digest": False,
    },
}


def _validate_attention_section(
    attention: Any, *, where: str = "attention"
) -> None:
    """Validate and normalize an ``attention:`` mapping.

    - Unknown keys → ConfigError
    - Missing sub-sections → filled from _ATTENTION_DEFAULTS
    - Scalar value type checks
    - Invariant: features.mention_boost >= tiers.skim
      (``tiers.skim`` is the lower bound of T2 engage; ``tiers.engage`` is T3)
    - Per-agent overrides: agent['attention'] only allows {'floor': float}

    Mutates ``attention`` in-place (fills defaults).
    Returns a dict suitable for ``RelevancePolicy.from_config()``.
    """
    if not isinstance(attention, dict):
        raise ConfigError(f"'{where}' must be a mapping")

    # 1. 최상위 키 검증
    for key in attention:
        if key not in _ATTENTION_TOP_KEYS:
            raise ConfigError(
                f"'{where}' contains unknown key {key!r} — "
                f"only {sorted(_ATTENTION_TOP_KEYS)} are allowed"
            )

    # 2. enabled
    enabled = attention.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ConfigError(f"'{where}.enabled' must be a boolean, got {type(enabled).__name__}")

    # 3. mode
    mode = attention.get("mode")
    if mode is not None and mode not in _VALID_ATTENTION_MODES:
        raise ConfigError(
            f"'{where}.mode' must be one of {sorted(_VALID_ATTENTION_MODES)}, "
            f"got {mode!r}"
        )

    # 4. floors
    floors = attention.get("floors")
    if floors is not None:
        if not isinstance(floors, dict):
            raise ConfigError(f"'{where}.floors' must be a mapping")
        for key in floors:
            if key not in _ATTENTION_FLOORS_KEYS:
                raise ConfigError(
                    f"'{where}.floors' contains unknown key {key!r} — "
                    f"only {sorted(_ATTENTION_FLOORS_KEYS)} are allowed"
                )
            val = floors[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(
                    f"'{where}.floors.{key}' must be a float in [0.0, 1.0], "
                    f"got {val!r}"
                )
            if not (0.0 <= val <= 1.0):
                raise ConfigError(
                    f"'{where}.floors.{key}' must be in [0.0, 1.0], got {val}"
                )

    # 5. tiers
    tiers = attention.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, dict):
            raise ConfigError(f"'{where}.tiers' must be a mapping")
        for key in tiers:
            if key not in _ATTENTION_TIERS_KEYS:
                raise ConfigError(
                    f"'{where}.tiers' contains unknown key {key!r} — "
                    f"only {sorted(_ATTENTION_TIERS_KEYS)} are allowed"
                )
            val = tiers[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(
                    f"'{where}.tiers.{key}' must be a float in [0.0, 1.0], "
                    f"got {val!r}"
                )
            if not (0.0 <= val <= 1.0):
                raise ConfigError(
                    f"'{where}.tiers.{key}' must be in [0.0, 1.0], got {val}"
                )
        # 불변식: ignore < skim < engage (단조증가) — 설계문서 §6.2
        ignore_v = tiers.get("ignore")
        skim_v = tiers.get("skim")
        engage_v = tiers.get("engage")
        if ignore_v is not None and skim_v is not None and ignore_v > skim_v:
            raise ConfigError(
                f"'{where}.tiers.ignore' ({ignore_v}) must be <= "
                f"tiers.skim ({skim_v})"
            )
        if skim_v is not None and engage_v is not None and skim_v > engage_v:
            raise ConfigError(
                f"'{where}.tiers.skim' ({skim_v}) must be <= "
                f"tiers.engage ({engage_v})"
            )

    # 6. features
    features = attention.get("features")
    if features is not None:
        if not isinstance(features, dict):
            raise ConfigError(f"'{where}.features' must be a mapping")
        for key in features:
            if key not in _ATTENTION_FEATURES_KEYS:
                raise ConfigError(
                    f"'{where}.features' contains unknown key {key!r} — "
                    f"only {sorted(_ATTENTION_FEATURES_KEYS)} are allowed"
                )
            val = features[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(
                    f"'{where}.features.{key}' must be a float in [0.0, 1.0], "
                    f"got {val!r}"
                )
            if not (0.0 <= val <= 1.0):
                raise ConfigError(
                    f"'{where}.features.{key}' must be in [0.0, 1.0], got {val}"
                )

    # 7. schedule
    schedule = attention.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            raise ConfigError(f"'{where}.schedule' must be a mapping")
        for key in schedule:
            if key not in _ATTENTION_SCHEDULE_KEYS:
                raise ConfigError(
                    f"'{where}.schedule' contains unknown key {key!r} — "
                    f"only {sorted(_ATTENTION_SCHEDULE_KEYS)} are allowed"
                )
        if "skip_t0_llm" in schedule and not isinstance(schedule["skip_t0_llm"], bool):
            raise ConfigError(
                f"'{where}.schedule.skip_t0_llm' must be a boolean"
            )

    # 8. context
    context = attention.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ConfigError(f"'{where}.context' must be a mapping")
        for key in context:
            if key not in _ATTENTION_CONTEXT_KEYS:
                raise ConfigError(
                    f"'{where}.context' contains unknown key {key!r} — "
                    f"only {sorted(_ATTENTION_CONTEXT_KEYS)} are allowed"
                )
        if "skim_max_chars" in context:
            v = context["skim_max_chars"]
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise ConfigError(
                    f"'{where}.context.skim_max_chars' must be a positive integer, "
                    f"got {v!r}"
                )
        if "t0_digest" in context and not isinstance(context["t0_digest"], bool):
            raise ConfigError(
                f"'{where}.context.t0_digest' must be a boolean"
            )


def _normalize_attention(attention: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``attention`` mapping with ``_ATTENTION_DEFAULTS``.

    Returns a *new* dict — does not mutate the input.
    The caller has already validated keys/types via ``_validate_attention_section``.
    """
    import copy

    merged: dict[str, Any] = copy.deepcopy(_ATTENTION_DEFAULTS)

    for top_key in ("enabled", "mode"):
        if top_key in attention:
            merged[top_key] = attention[top_key]

    for section_key in ("floors", "tiers", "features", "schedule", "context"):
        if section_key in attention and isinstance(attention[section_key], dict):
            merged[section_key] = {
                **merged[section_key],
                **attention[section_key],
            }

    # 불변식: mention_boost >= tiers.skim (설계 §4.2 — 멘션 = 최소 T2 engage)
    # tiers.skim 은 T2 하한, tiers.engage 는 T3 하한이다.
    mention_boost = merged["features"]["mention_boost"]
    tiers_skim = merged["tiers"]["skim"]
    if mention_boost < tiers_skim:
        raise ConfigError(
            f"attention.features.mention_boost ({mention_boost}) "
            f"must be >= attention.tiers.skim ({tiers_skim}) — "
            f"otherwise mentions alone can never reach ENGAGE (T2) tier"
        )

    return merged


def _merge_agent_attention(
    global_attention: dict[str, Any],
    agent_override: Any,
    agent_index: int,
) -> dict[str, Any]:
    """Per-agent attention override: only ``floor`` key is allowed.

    Returns a new merged attention dict for the agent.
    """
    import copy

    merged = copy.deepcopy(global_attention)

    if agent_override is None:
        return merged

    if not isinstance(agent_override, dict):
        raise ConfigError(
            f"agents[{agent_index}].attention must be a mapping"
        )

    for key in agent_override:
        if key not in _ATTENTION_AGENT_KEYS:
            raise ConfigError(
                f"agents[{agent_index}].attention contains unknown key {key!r} — "
                f"only {sorted(_ATTENTION_AGENT_KEYS)} are allowed "
                f"(per-agent overrides are minimal by design)"
            )

    if "floor" in agent_override:
        floor_val = agent_override["floor"]
        if not isinstance(floor_val, (int, float)) or isinstance(floor_val, bool):
            raise ConfigError(
                f"agents[{agent_index}].attention.floor must be a float "
                f"in [0.0, 1.0], got {floor_val!r}"
            )
        if not (0.0 <= floor_val <= 1.0):
            raise ConfigError(
                f"agents[{agent_index}].attention.floor must be in "
                f"[0.0, 1.0], got {floor_val}"
            )
        merged["floors"]["default"] = floor_val

    return merged


# YAML 1.1 parses a bare ``off``/``no`` as the boolean False, so a config
# written as ``mode: off`` never reaches us as the string "off".
_PROTOCOL_OFF = frozenset({"off", "false", "no", "none"})
_PROTOCOL_MODES = frozenset({"full", "light"})


def normalize_protocol_mode(data: dict[str, Any], *, config_error: type) -> Any:
    """Resolve ``protocol:`` / ``protocol.mode`` into a spec dict or None.

    ``protocol: false``, ``protocol: null`` and ``mode: off`` all mean "no
    collaboration protocol" and normalize to ``None`` (key removed), which is
    what Session already treats as protocol-less.
    """
    protocol = data.get("protocol")
    if protocol is None or protocol is False:
        data.pop("protocol", None)
        return None
    if not isinstance(protocol, dict):
        raise config_error(
            "'protocol' must be a mapping, or false to disable the protocol"
        )

    raw_mode = protocol.get("mode", "full")
    if raw_mode is False or (
        isinstance(raw_mode, str) and raw_mode.strip().lower() in _PROTOCOL_OFF
    ):
        data.pop("protocol", None)
        return None
    if raw_mode is True:
        raise config_error(
            "protocol.mode: 'on' is not a mode — use full, light or off"
        )
    mode = str(raw_mode).strip().lower()
    if mode not in _PROTOCOL_MODES:
        raise config_error(
            f"unknown protocol.mode: {raw_mode!r} (expected full, light or off)"
        )
    protocol["mode"] = mode

    if mode == "light":
        # Light visits P1 then the final gate only; P2-P4 gate names are noise.
        gates = protocol.get("gates") or {}
        if not isinstance(gates, dict):
            raise config_error("protocol.gates must be a mapping")
        dropped = sorted(k for k in gates if k != "P5_SUBMIT")
        if dropped:
            print(
                f"  [config] protocol.mode: light ignores gates {dropped} "
                "(P2-P4 are not visited)",
                flush=True,
            )
        protocol["gates"] = {"P5_SUBMIT": gates.get("P5_SUBMIT", "submission")}
    return protocol


def normalize_protocol_roster(
    protocol: dict[str, Any],
    *,
    pool_size: int,
    config_error: type,
) -> None:
    """Fill ``protocol.roster`` defaults (DYNAMIC_ROSTER_DESIGN §4.7).

    Missing ``roster`` → ``start=min(2, pool)``, ``max=pool`` (intentional
    behaviour change vs pre-roster full participation). ``start: -1`` keeps
    the whole pool active from R0.
    """
    raw = protocol.get("roster")
    if raw is None:
        roster: dict[str, Any] = {}
    elif isinstance(raw, dict):
        roster = dict(raw)
    else:
        raise config_error("protocol.roster must be a mapping")

    if "start" not in roster:
        roster["start"] = min(2, pool_size) if pool_size else 0
    else:
        try:
            roster["start"] = int(roster["start"])
        except (TypeError, ValueError) as exc:
            raise config_error(
                f"protocol.roster.start must be an int (got {roster['start']!r})"
            ) from exc
        if roster["start"] < -1 or roster["start"] == 0:
            raise config_error(
                "protocol.roster.start must be -1 (all) or a positive int"
            )

    if "max" not in roster:
        roster["max"] = pool_size
    else:
        try:
            roster["max"] = int(roster["max"])
        except (TypeError, ValueError) as exc:
            raise config_error(
                f"protocol.roster.max must be an int (got {roster['max']!r})"
            ) from exc
        if roster["max"] < 1:
            raise config_error("protocol.roster.max must be >= 1")

    protocol["roster"] = roster


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

    approval = tools.get("approval")
    if approval is not None:
        if not isinstance(approval, dict):
            raise ConfigError(f"'{where}.approval' must be a mapping")
        for key in approval:
            if key not in _TOOLS_APPROVAL_KEYS:
                raise ConfigError(
                    f"'{where}.approval' contains unknown key {key!r} — "
                    f"only {sorted(_TOOLS_APPROVAL_KEYS)} are allowed"
                )
        for mode_key in ("shell", "file_write", "web"):
            if mode_key not in approval:
                continue
            mode = approval[mode_key]
            # Unquoted YAML `off` becomes bool False — treat as the mode string.
            if mode is False:
                mode = "off"
                approval[mode_key] = "off"
            if mode is True:
                raise ConfigError(
                    f"'{where}.approval.{mode_key}' must be one of "
                    f"{sorted(_VALID_APPROVAL_MODES)} (quote strings in YAML), "
                    f"got boolean true"
                )
            if mode not in _VALID_APPROVAL_MODES:
                raise ConfigError(
                    f"'{where}.approval.{mode_key}' must be one of "
                    f"{sorted(_VALID_APPROVAL_MODES)}, got {mode!r}"
                )
        if "bypass" in approval and not isinstance(approval["bypass"], bool):
            raise ConfigError(f"'{where}.approval.bypass' must be a boolean")
        if "ttl_seconds" in approval:
            ttl = approval["ttl_seconds"]
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
                raise ConfigError(
                    f"'{where}.approval.ttl_seconds' must be a positive number"
                )


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


_SURFACES_KEYS = frozenset({"ink", "discord", "slack", "display"})


def normalize_surfaces(data: dict[str, Any]) -> None:
    """Expand optional ``surfaces:`` into legacy ``mirror`` / ``bots`` / ``slack``.

    Design: MULTI_FRONT_DESIGN.md §4. Legacy top-level keys remain valid when
    ``surfaces:`` is omitted. Mixing the same family in both places raises
    ``ConfigError``.
    """
    surfaces = data.get("surfaces")
    if surfaces is None:
        return
    if not isinstance(surfaces, dict):
        raise ConfigError("'surfaces' must be a mapping")
    unknown = set(surfaces) - _SURFACES_KEYS
    if unknown:
        raise ConfigError(
            f"'surfaces' contains unknown key(s) {sorted(unknown)} — "
            f"allowed: {sorted(_SURFACES_KEYS)}"
        )

    ink = surfaces.get("ink")
    if ink is not None:
        if not isinstance(ink, dict):
            raise ConfigError("surfaces.ink must be a mapping")
        if "enabled" in ink and not isinstance(ink["enabled"], bool):
            raise ConfigError("surfaces.ink.enabled must be a boolean")

    discord = surfaces.get("discord")
    if discord is not None:
        _expand_discord_surface(data, discord)

    slack = surfaces.get("slack")
    if slack is not None:
        _expand_slack_surface(data, slack)


def _expand_discord_surface(data: dict[str, Any], discord: Any) -> None:
    if not isinstance(discord, dict):
        raise ConfigError("surfaces.discord must be a mapping")
    if discord.get("enabled") is False:
        return

    if data.get("mirror") is not None or data.get("bots") is not None:
        raise ConfigError(
            "surfaces.discord conflicts with top-level 'mirror'/'bots' — "
            "use surfaces.discord or the legacy keys, not both"
        )

    mode = discord.get("mode", "observe")
    if mode not in ("observe", "interact"):
        raise ConfigError(
            f"surfaces.discord.mode must be 'observe' or 'interact', got {mode!r}"
        )

    mirror = discord.get("mirror")
    if mirror is None and isinstance(discord.get("webhook"), dict):
        mirror = discord["webhook"]
    if mirror is not None:
        if not isinstance(mirror, dict):
            raise ConfigError("surfaces.discord.mirror must be a mapping")
        if "type" not in mirror and "url_env" in mirror:
            mirror = {"type": "discord_webhook", "url_env": mirror["url_env"]}
        data["mirror"] = mirror

    bots = discord.get("bots")
    if bots is None:
        return
    if not isinstance(bots, list):
        raise ConfigError("surfaces.discord.bots must be a list")

    agents_filter = discord.get("agents")
    if agents_filter is not None:
        if not isinstance(agents_filter, list) or not all(
            isinstance(a, str) for a in agents_filter
        ):
            raise ConfigError("surfaces.discord.agents must be a list of strings")
        allow = set(agents_filter)
        bots = [
            b
            for b in bots
            if isinstance(b, dict) and str(b.get("agent_id", "")) in allow
        ]

    expanded: list[dict[str, Any]] = []
    for bot in bots:
        if not isinstance(bot, dict):
            raise ConfigError("surfaces.discord.bots entries must be mappings")
        entry = dict(bot)
        if mode == "interact" and "inbound" not in entry:
            entry["inbound"] = True
        expanded.append(entry)
    data["bots"] = expanded


def _expand_slack_surface(data: dict[str, Any], slack: Any) -> None:
    if not isinstance(slack, dict):
        raise ConfigError("surfaces.slack must be a mapping")
    if slack.get("enabled") is False:
        return

    if data.get("slack") is not None:
        raise ConfigError(
            "surfaces.slack conflicts with top-level 'slack' — "
            "use surfaces.slack or the legacy key, not both"
        )

    mode = slack.get("mode", "observe")
    if mode not in ("observe",):
        raise ConfigError(
            f"surfaces.slack.mode must be 'observe' (inbound not yet supported), "
            f"got {mode!r}"
        )

    spec = {k: v for k, v in slack.items() if k != "enabled"}
    data["slack"] = spec


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

    # attention: 섹션 (전역) 검증 + 정규화 — AGENT_RELEVANCE_BUDGET_DESIGN.md §6
    # agents 루프보다 먼저 수행되어야 per-agent merge에서 참조 가능
    attention = data.get("attention")
    if attention is not None:
        _validate_attention_section(attention, where="attention")
        data["_attention_normalized"] = _normalize_attention(attention)
    else:
        data["_attention_normalized"] = _normalize_attention({})

    seen_ids: set[str] = set()
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
        if agent_id.lower() in seen_ids:
            raise ConfigError(
                f"agents[{i}].id {agent_id!r} is duplicated — agent ids must be unique"
            )
        seen_ids.add(agent_id.lower())
        if not isinstance(agent.get("backend"), dict):
            raise ConfigError(f"agents[{i}].backend must be a mapping")
        backend = agent["backend"]
        btype = backend.get("type")
        # allow_fake=True → fake 백엔드 허용 (오프라인 데모/벤치마크 전용)
        if btype == "fake":
            if not allow_fake:
                raise ConfigError(
                    f"agents[{i}].backend.type 'fake' requires --demo flag "
                    f"(offline examples / tests only)"
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

        # attention: 섹션 (에이전트별 오버라이드) 검증 — per-agent minimal override
        agent_attn = agent.get("attention")
        if agent_attn is not None:
            # agent attention override는 global attention이 있을 때만 의미 있음
            if data.get("attention") is not None:
                agent["_attention"] = _merge_agent_attention(
                    data["_attention_normalized"], agent_attn, i
                )
            else:
                raise ConfigError(
                    f"agents[{i}].attention requires a global 'attention:' section"
                )

    # human 섹션은 v1.0+ 코드에 내장 — config 키는 무시 (옵트인 폐기).
    human = data.get("human")
    if human is not None and isinstance(human, dict):
        pass  # accepted but ignored — no warning

    # tools: 섹션 (전역) 검증 — AGENT_TOOLS_EXPANSION_DESIGN.md §4.7
    tools = data.get("tools")
    if tools is not None:
        _validate_tools_section(tools, where="tools")

    # protocol.mode + human_approval (HUMAN_APPROVAL_GATE_DESIGN)
    protocol = normalize_protocol_mode(data, config_error=ConfigError)
    if isinstance(protocol, dict):
        from .core.protocol.human_approval import normalize_human_approval

        # Normalize onto protocol for Session consumers (defaults all false).
        protocol["human_approval"] = normalize_human_approval(
            protocol, config_error=ConfigError
        )
        raw_pool = protocol.get("participants")
        if isinstance(raw_pool, list) and raw_pool:
            pool_size = len(raw_pool)
        else:
            pool_size = len(agents)
        normalize_protocol_roster(
            protocol, pool_size=pool_size, config_error=ConfigError
        )

    from .channels.display import validate_display_config

    validate_display_config(data, config_error=ConfigError)

    # A2: surfaces: → legacy mirror/bots/slack (before validating those keys)
    normalize_surfaces(data)

    # mirror.url_env 검증
    mirror = data.get("mirror")
    if mirror is not None and isinstance(mirror, dict) and "url_env" not in mirror:
        raise ConfigError("mirror requires 'url_env' key")

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
            te = bot["token_env"]
            if not isinstance(te, str):
                raise ConfigError(
                    f"bots[{i}].token_env must be a string, got {te!r}"
                )
            te_err = validate_token_env_name(te)
            if te_err:
                raise ConfigError(f"bots[{i}].token_env invalid: {te_err}")
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
