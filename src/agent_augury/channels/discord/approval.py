"""Discord button UI for per-approval grant/deny (Hermes-style HITL)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import discord

from agent_augury.gateway.bus import SessionGateway
from agent_augury.gateway.types import make_command

from ..chat_surface_format import format_wire_for_chat_surface

log = logging.getLogger(__name__)

# Keep in sync with discord_inbound.INBOUND_SURFACE (avoid circular import).
_APPROVAL_SURFACE = "discord-inbound"

# custom_id: augury:appr:<approval_id>:<g|d>  (Discord limit 100 chars)
_CUSTOM_PREFIX = "augury:appr:"


def approval_prompt_text(event: dict[str, Any], *, recipient_agent_id: str | None) -> str:
    """Body text for an approval.request (buttons carry the decision)."""
    # Reuse chat formatter but strip the old "Reply: 1/approve…" hint.
    text = format_wire_for_chat_surface(
        event,  # type: ignore[arg-type]
        recipient_agent_id=recipient_agent_id,
    ) or "🔐 Approval needed"
    text = text.replace("\nReply: 1/approve or 2/deny", "").rstrip()
    return f"{text}\nUse the buttons below (each request is separate)."


def make_approval_custom_id(approval_id: str, decision: str) -> str:
    flag = "g" if decision == "granted" else "d"
    return f"{_CUSTOM_PREFIX}{approval_id}:{flag}"


def parse_approval_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return ``(approval_id, granted|denied)`` or None."""
    raw = (custom_id or "").strip()
    if not raw.startswith(_CUSTOM_PREFIX):
        return None
    rest = raw[len(_CUSTOM_PREFIX) :]
    if ":" not in rest:
        return None
    aid, flag = rest.rsplit(":", 1)
    if not aid:
        return None
    if flag == "g":
        return aid, "granted"
    if flag == "d":
        return aid, "denied"
    return None


class ToolApprovalView(discord.ui.View):
    """Approve / Deny buttons bound to one ``approval_id`` (custom_id routed)."""

    def __init__(self, approval_id: str, *, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.approval_id = approval_id
        self.add_item(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=make_approval_custom_id(approval_id, "granted"),
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=make_approval_custom_id(approval_id, "denied"),
            )
        )


async def handle_approval_interaction(
    interaction: discord.Interaction,
    *,
    gateway: SessionGateway,
    approval_id: str,
    decision: str,
) -> None:
    """Dispatch ``approval.resolve`` and update the prompt message."""
    if _APPROVAL_SURFACE not in gateway.surfaces():
        await interaction.response.send_message(
            "No interact surface for approvals.", ephemeral=True
        )
        return

    cmd = make_command(
        "approval.resolve",
        id=str(uuid4()),
        approval_id=approval_id,
        decision=decision,
        reason="discord_button",
        source={
            "surface": "discord",
            "mode": "interact",
            "user": str(interaction.user.id),
            "channel": str(getattr(interaction.channel, "id", "")),
        },
    )
    result = gateway.dispatch(cmd, surface=_APPROVAL_SURFACE)
    label = "approved" if decision == "granted" else "denied"
    note = f"🔐 {label} by <@{interaction.user.id}>"
    if not result.get("ok", True) and result.get("error"):
        note = f"🔐 resolve failed: {result.get('error')}"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.edit_message(content=note, view=None)
            return
    except discord.HTTPException:
        log.warning("failed to update approval message %s", approval_id)

    # If we already deferred/responded another way, try edit on message.
    try:
        if interaction.message is not None:
            await interaction.message.edit(content=note, view=None)
    except discord.HTTPException:
        pass
