"""ToolPolicy — config-derived tool enablement + security bounds.

DESIGN ref: docs/AGENT_TOOLS_EXPANSION_DESIGN.md §4.1 (v4.1 확정).

Policy drives which tools ``ToolBox`` exposes and with what security
limits. Defaults = all new tools ENABLED with built-in safety bounds
(D3 — user decision "기본 활성화"). Per-agent ``tools:`` sections
deep-merge over the global section (§4.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default destructive-command blocklist (POSIX-focused; 2nd line of defense).
_DEFAULT_SHELL_BLOCKED = (
    "rm -rf",
    "mkfs",
    "dd if=",
    ":(){",
    "sudo",
    "shutdown",
    "reboot",
    "halt",
    "chmod -R 777 /",
    "> /dev/sd",
    "git push --force",
    "pip uninstall",
)

# Cloud metadata + obviously-internal hosts. Always blocked regardless of
# private-IP toggle (enforced again in web.py).
_DEFAULT_WEB_DENY_DOMAINS = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "169.254.169.254",
    "169.254.170.2",
    "metadata.google.internal",
    "metadata.azure.internal",
    "100.100.100.200",
)


@dataclass(frozen=True)
class ToolPolicy:
    """Immutable tool enablement + security bounds."""

    # shell
    shell_enabled: bool = True
    shell_allowed: tuple[str, ...] = ()
    shell_blocked: tuple[str, ...] = _DEFAULT_SHELL_BLOCKED
    shell_timeout: float = 30.0
    shell_max_output: int = 16_384  # chars (16KB — agent-1 feedback #3)
    shell_cwd: str | None = None
    # web
    web_enabled: bool = True
    web_allow_domains: tuple[str, ...] = ()
    web_deny_domains: tuple[str, ...] = _DEFAULT_WEB_DENY_DOMAINS
    web_block_private_ips: bool = True
    web_timeout: float = 15.0
    web_max_bytes: int = 65_536  # 64KB — agent-1 feedback #3
    web_max_results: int = 5
    web_search_provider: str = "duckduckgo"
    # file
    allowed_roots: tuple[str, ...] = ()
    edit_enabled: bool = True  # edit_file / append_file
    # approval (TOOL_HUMAN_APPROVAL_DESIGN — T5: shell/file_write default require)
    approval_shell: str = "require"  # require | off
    approval_file_write: str = "require"
    approval_web: str = "off"
    approval_bypass: bool = False
    approval_ttl_seconds: float = 600.0

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        tools_cfg: dict[str, Any] | None,
        *,
        allowed_roots: list[str] | None = None,
    ) -> ToolPolicy:
        """Build the global policy from the ``tools:`` YAML section.

        Missing keys fall back to defaults (all enabled + built-in safety).
        ``allowed_roots`` (CLI/session param) is merged as the base file roots.
        """
        tools_cfg = tools_cfg or {}
        shell = tools_cfg.get("shell") or {}
        web = tools_cfg.get("web") or {}
        file_cfg = tools_cfg.get("file") or {}
        approval = tools_cfg.get("approval") or {}

        roots = tuple(allowed_roots or ()) + tuple(file_cfg.get("allowed_roots") or ())

        policy = cls(
            shell_enabled=_as_bool(shell.get("enabled"), True),
            shell_allowed=tuple(shell.get("allowed") or ()),
            shell_blocked=_DEFAULT_SHELL_BLOCKED + tuple(shell.get("blocked") or ()),
            shell_timeout=_as_float(shell.get("timeout_seconds"), 30.0),
            shell_max_output=_as_int(shell.get("max_output_chars"), 16_384),
            shell_cwd=_as_str(shell.get("cwd")),
            web_enabled=_as_bool(web.get("enabled"), True),
            web_allow_domains=tuple(web.get("allow_domains") or ()),
            web_deny_domains=_DEFAULT_WEB_DENY_DOMAINS + tuple(web.get("deny_domains") or ()),
            web_block_private_ips=_as_bool(web.get("block_private_ips"), True),
            web_timeout=_as_float(web.get("timeout_seconds"), 15.0),
            web_max_bytes=_as_int(web.get("max_bytes"), 65_536),
            web_max_results=_as_int(web.get("max_results"), 5),
            web_search_provider=_as_str(web.get("search_provider"), "duckduckgo"),
            allowed_roots=roots,
            edit_enabled=_as_bool(file_cfg.get("edit_enabled"), True),
            approval_shell=_as_approval_mode(approval.get("shell"), "require"),
            approval_file_write=_as_approval_mode(approval.get("file_write"), "require"),
            approval_web=_as_approval_mode(approval.get("web"), "off"),
            approval_bypass=_as_bool(approval.get("bypass"), False),
            approval_ttl_seconds=_as_float(approval.get("ttl_seconds"), 600.0),
        )

        # agent-1 feedback #5: enabled shell with empty allowlist → warn.
        if policy.shell_enabled and not policy.shell_allowed:
            logger.warning(
                "tools.shell.enabled=true with empty 'allowed' list — ALL commands "
                "except the built-in blocklist are permitted. Consider setting "
                "tools.shell.allowed for defense in depth."
            )
        return policy

    def merge(self, agent_tools: dict[str, Any] | None) -> ToolPolicy:
        """Deep-merge a per-agent ``tools:`` section over this policy.

        Agent-provided keys override; unspecified keys inherit (deep merge per
        subsection — shell/web/file/approval each merge independently).
        """
        if not agent_tools:
            return self
        shell = agent_tools.get("shell") or {}
        web = agent_tools.get("web") or {}
        file_cfg = agent_tools.get("file") or {}
        approval = agent_tools.get("approval") or {}

        return ToolPolicy(
            shell_enabled=_as_bool(shell.get("enabled"), self.shell_enabled),
            shell_allowed=tuple(shell.get("allowed") or self.shell_allowed),
            shell_blocked=tuple(shell.get("blocked") or self.shell_blocked),
            shell_timeout=_as_float(shell.get("timeout_seconds"), self.shell_timeout),
            shell_max_output=_as_int(shell.get("max_output_chars"), self.shell_max_output),
            shell_cwd=_as_str(shell.get("cwd")) if "cwd" in shell else self.shell_cwd,
            web_enabled=_as_bool(web.get("enabled"), self.web_enabled),
            web_allow_domains=tuple(web.get("allow_domains") or self.web_allow_domains),
            web_deny_domains=tuple(web.get("deny_domains") or self.web_deny_domains),
            web_block_private_ips=_as_bool(
                web.get("block_private_ips"), self.web_block_private_ips
            ),
            web_timeout=_as_float(web.get("timeout_seconds"), self.web_timeout),
            web_max_bytes=_as_int(web.get("max_bytes"), self.web_max_bytes),
            web_max_results=_as_int(web.get("max_results"), self.web_max_results),
            web_search_provider=_as_str(
                web.get("search_provider"), self.web_search_provider
            ),
            allowed_roots=tuple(file_cfg.get("allowed_roots") or self.allowed_roots),
            edit_enabled=_as_bool(file_cfg.get("edit_enabled"), self.edit_enabled),
            approval_shell=_as_approval_mode(approval.get("shell"), self.approval_shell),
            approval_file_write=_as_approval_mode(
                approval.get("file_write"), self.approval_file_write
            ),
            approval_web=_as_approval_mode(approval.get("web"), self.approval_web),
            approval_bypass=_as_bool(approval.get("bypass"), self.approval_bypass),
            approval_ttl_seconds=_as_float(
                approval.get("ttl_seconds"), self.approval_ttl_seconds
            ),
        )

    # -- helpers for callers ---------------------------------------------------

    def resolve_shell_cwd(self) -> str | None:
        """cwd 결정 규칙 (§4.2): shell_cwd → allowed_roots[0] → None(세션 cwd)."""
        if self.shell_cwd:
            return self.shell_cwd
        if self.allowed_roots:
            return self.allowed_roots[0]
        return None

    def approval_mode_for(self, tool: str) -> str:
        """Return ``require`` / ``off`` for *tool* (ungated tools → ``off``)."""
        from .approval import tool_approval_class

        kind = tool_approval_class(tool)
        if kind == "shell":
            return self.approval_shell
        if kind == "file_write":
            return self.approval_file_write
        if kind == "web":
            return self.approval_web
        return "off"

    def requires_approval(self, tool: str) -> bool:
        return self.approval_mode_for(tool) == "require" and not self.approval_bypass


# ---------------------------------------------------------------------------
# coercion helpers (defensive — malformed YAML never crashes policy build)
# ---------------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_approval_mode(value: Any, default: str) -> str:
    if value is None:
        return default
    if value is False:  # YAML unquoted `off`
        return "off"
    if value is True:
        return default
    text = str(value).strip().lower()
    if text in ("require", "off"):
        return text
    return default
