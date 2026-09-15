"""Chat surface display density (A6 V1 — SURFACE_DISPLAY_DESIGN.md).

Core publishes full Wire; Discord/Slack observe applies ``ChatDisplayPolicy``
before format and outbound enqueue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent_augury.gateway.types import WireEvent

from .chat_surface_format import format_wire_for_chat_surface

ChatDisplayMode = Literal["full", "summary", "quiet"]
CHAT_DISPLAY_MODES: frozenset[str] = frozenset({"full", "summary", "quiet"})

_SUMMARY_BODY_MAX = 280

_SUMMARY_TYPES = frozenset({
    "message",
    "agent.step",
    "human.question",
    "thread.created",
    "tool.denied",
    "approval.request",
    "approval.resolved",
    "approval.granted",
    "approval.expired",
})

def _parse_mode(value: Any, *, where: str) -> ChatDisplayMode:
    if not isinstance(value, str):
        raise TypeError(f"{where} must be 'full', 'summary', or 'quiet'")
    mode = value.strip().lower()
    if mode not in CHAT_DISPLAY_MODES:
        raise ValueError(
            f"{where} must be one of {sorted(CHAT_DISPLAY_MODES)}, got {value!r}"
        )
    return mode  # type: ignore[return-value]


def _default_chat_mode(cfg: dict[str, Any]) -> ChatDisplayMode:
    display = cfg.get("display")
    if isinstance(display, dict) and display.get("chat") is not None:
        return _parse_mode(display["chat"], where="display.chat")
    surfaces = cfg.get("surfaces")
    if isinstance(surfaces, dict):
        surf_display = surfaces.get("display")
        if isinstance(surf_display, dict) and surf_display.get("chat") is not None:
            return _parse_mode(surf_display["chat"], where="surfaces.display.chat")
    return "full"


def resolve_chat_display_policy(
    cfg: dict[str, Any],
    platform: Literal["discord", "slack"],
) -> ChatDisplayPolicy:
    """Resolve display mode for a chat platform (inherit ``display.chat``)."""
    base = _default_chat_mode(cfg)
    surfaces = cfg.get("surfaces")
    if isinstance(surfaces, dict):
        plat = surfaces.get(platform)
        if isinstance(plat, dict) and plat.get("display") is not None:
            return ChatDisplayPolicy(
                _parse_mode(
                    plat["display"],
                    where=f"surfaces.{platform}.display",
                )
            )
    return ChatDisplayPolicy(base)


def validate_display_config(data: dict[str, Any], *, config_error: type[Exception]) -> None:
    """Validate optional ``display`` / ``surfaces.display`` / per-platform override."""
    display = data.get("display")
    if display is not None:
        if not isinstance(display, dict):
            raise config_error("'display' must be a mapping")
        extra = set(display) - {"chat"}
        if extra:
            raise config_error(
                f"'display' contains unknown key(s) {sorted(extra)} — only 'chat' is allowed"
            )
        if display.get("chat") is not None:
            try:
                _parse_mode(display["chat"], where="display.chat")
            except (TypeError, ValueError) as exc:
                raise config_error(str(exc)) from exc

    surfaces = data.get("surfaces")
    if not isinstance(surfaces, dict):
        return
    surf_display = surfaces.get("display")
    if surf_display is not None:
        if not isinstance(surf_display, dict):
            raise config_error("surfaces.display must be a mapping")
        extra = set(surf_display) - {"chat"}
        if extra:
            raise config_error(
                "surfaces.display contains unknown key(s) "
                f"{sorted(extra)} — only 'chat' is allowed"
            )
        if surf_display.get("chat") is not None:
            try:
                _parse_mode(surf_display["chat"], where="surfaces.display.chat")
            except (TypeError, ValueError) as exc:
                raise config_error(str(exc)) from exc
    for plat in ("discord", "slack"):
        plat_cfg = surfaces.get(plat)
        if isinstance(plat_cfg, dict) and plat_cfg.get("display") is not None:
            try:
                _parse_mode(plat_cfg["display"], where=f"surfaces.{plat}.display")
            except (TypeError, ValueError) as exc:
                raise config_error(str(exc)) from exc


def _truncate_summary(text: str, *, max_len: int = _SUMMARY_BODY_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _is_human_message(event: WireEvent) -> bool:
    author = str(event.get("author") or event.get("agent_id") or "").lower()
    return author == "human"


@dataclass(frozen=True)
class ChatDisplayPolicy:
    mode: ChatDisplayMode = "full"

    def allow(self, event: WireEvent) -> bool:
        """Whether this Wire event should be shown on chat surfaces."""
        if self.mode == "full":
            return True
        etype = str(event.get("type") or "")
        if self.mode == "summary":
            if etype in ("log", "tool", "read_resource"):
                return False
            return etype.startswith("approval.") or etype in _SUMMARY_TYPES
        # quiet
        if etype == "message":
            return _is_human_message(event)
        if etype.startswith("approval."):
            return True
        return etype in ("human.question", "tool.denied")

    def format_wire(
        self,
        event: WireEvent,
        *,
        recipient_agent_id: str | None = None,
    ) -> str | None:
        if not self.allow(event):
            return None
        text = format_wire_for_chat_surface(
            event,
            recipient_agent_id=recipient_agent_id,
        )
        if text is None:
            return None
        if self.mode == "summary" and event.get("type") == "agent.step":
            return _truncate_summary(text)
        return text
