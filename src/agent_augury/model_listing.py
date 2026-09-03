"""Shared helpers for provider model listing and wizard display order."""

from __future__ import annotations

from typing import Any


def _provider_and_name(model_id: str) -> tuple[str, str]:
    """Split a model id into provider group and model name for sorting."""
    if "/" in model_id:
        provider, name = model_id.split("/", 1)
        return provider, name
    return "", model_id


def sort_model_ids(models: list[str]) -> list[str]:
    """Sort model IDs for the wizard selection UI.

    Keeps provider groups (prefix before the first ``/``) together, then
    orders by model name case-insensitively.  Python's stable sort preserves
    the original order for equal keys (duplicate names).
    """
    return sorted(
        models,
        key=lambda model_id: (
            _provider_and_name(model_id)[0].casefold(),
            _provider_and_name(model_id)[1].casefold(),
        ),
    )


def extract_model_ids(items: Any) -> list[str]:
    """Parse model ids from an OpenAI-style /models payload and sort them."""
    if not isinstance(items, list):
        return []
    ids = [m["id"] for m in items if isinstance(m, dict) and m.get("id")]
    return sort_model_ids(ids)
