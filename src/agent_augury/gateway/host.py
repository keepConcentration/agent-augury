"""Gateway host bootstrap — SSOT for Ink/headless/future runners.

Design: docs/architecture/GATEWAY_SURFACE_BINDING_DESIGN.md (G2).
"""

from __future__ import annotations

from typing import Any

from .bridge import BridgeBindOptions, SessionBridge


def bootstrap_gateway_host(
    session: Any,
    bridge: SessionBridge,
    *,
    bind: BridgeBindOptions | None = None,
) -> None:
    """Wire Core step observers and install command routing on the gateway."""
    bridge.bind_session(session, options=bind)
    bridge.install()
