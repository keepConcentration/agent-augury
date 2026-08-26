"""Nous Portal adapter (2nd priority).

DESIGN.md D4: the Nous Portal API spec is unconfirmed, so no invented default
endpoint — callers must pass ``base_url`` explicitly.
"""

from __future__ import annotations

import httpx

from .openai_compat import OpenAICompatBackend


class NousPortalBackend(OpenAICompatBackend):
    """OpenAI-compatible surface over Nous Portal; base_url must be explicit."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        client: httpx.Client | httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not base_url:
            raise ValueError(
                "NousPortalBackend requires an explicit base_url "
                "(API spec unconfirmed — DESIGN.md D4)"
            )
        super().__init__(base_url=base_url, api_key=api_key, model=model, client=client, timeout=timeout)
