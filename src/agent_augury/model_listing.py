"""Shared helpers for provider model listing and wizard display order."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Colon suffixes kept in the wizard list (everything else, e.g. :batch / :US, is dropped).
_ALLOWED_COLON_SUFFIXES = frozenset({"free"})

# Specialty / non-stable name markers (case-insensitive).
_SPECIALTY_NAME = re.compile(
    r"(^|[-/])(preview|experimental|alpha|beta|nightly|internal|latest)($|[-/:]|\d)"
)
_EXP_SUFFIX = re.compile(r"-exp($|[-:]|\d)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_COMPACT_DATE = re.compile(r"-\d{8}($|:)")
_SHORT_DATE_PIN = re.compile(r"-\d{4}($|:)")  # -0902, -0613, -0125, …
_IMAGE_MODEL = re.compile(r"(^|[-/])image($|[-/])")


@dataclass(frozen=True)
class ModelInfo:
    """One selectable model for the wizard list."""

    id: str
    prompt_per_token: float | None = None
    completion_per_token: float | None = None

    @property
    def is_free(self) -> bool:
        """True when pricing is zero, or id ends with ``:free`` when pricing unknown."""
        if self.prompt_per_token is None and self.completion_per_token is None:
            return self.id.endswith(":free")
        return (self.prompt_per_token or 0.0) == 0.0 and (self.completion_per_token or 0.0) == 0.0

    def price_suffix(self) -> str | None:
        """Pricing column text, or ``None`` when pricing is unavailable."""
        if self.prompt_per_token is None and self.completion_per_token is None:
            return None
        if self.is_free:
            return "[free]"
        p_m = (self.prompt_per_token or 0.0) * 1_000_000
        c_m = (self.completion_per_token or 0.0) * 1_000_000
        return f"[${p_m:.4g}/${c_m:.4g} per 1M tok]"

    def label(self, *, id_width: int | None = None) -> str:
        """Human-readable line for the wizard numbered list."""
        name = self.id if id_width is None else f"{self.id:<{id_width}}"
        suffix = self.price_suffix()
        if suffix is None:
            return name.rstrip() if id_width is not None else name
        return f"{name}  {suffix}"


def is_general_purpose_model_id(model_id: str) -> bool:
    """Keep general chat/completion ids; drop batch, region, date, preview, aliases."""
    if not model_id or model_id.startswith("~"):
        return False
    if ":" in model_id:
        suffix = model_id.rsplit(":", 1)[1]
        if suffix.casefold() not in _ALLOWED_COLON_SUFFIXES:
            return False
    low = model_id.casefold()
    if _ISO_DATE.search(low) or _COMPACT_DATE.search(low) or _SHORT_DATE_PIN.search(low):
        return False
    if _SPECIALTY_NAME.search(low) or _EXP_SUFFIX.search(low) or _IMAGE_MODEL.search(low):
        return False
    return True


def _is_text_output_model(item: dict[str, Any]) -> bool:
    """Drop image/audio generators when architecture metadata is present."""
    arch = item.get("architecture")
    if not isinstance(arch, dict):
        return True
    outs = arch.get("output_modalities")
    if not isinstance(outs, list) or not outs:
        return True
    return outs == ["text"] or set(outs) == {"text"}


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


def sort_model_infos(
    models: list[ModelInfo],
    *,
    free_last: bool = False,
) -> list[ModelInfo]:
    """Sort model infos for the wizard.

    * ``free_last=False`` (default): provider group, then name (legacy).
    * ``free_last=True`` (OpenRouter / Nous): paid first, free last; then name.
    """
    if free_last:
        return sorted(
            models,
            key=lambda m: (m.is_free, m.id.casefold()),
        )
    return sorted(
        models,
        key=lambda m: (
            _provider_and_name(m.id)[0].casefold(),
            _provider_and_name(m.id)[1].casefold(),
        ),
    )


def format_aligned_labels(models: list[str] | list[ModelInfo]) -> list[str]:
    """Column-align model ids when pricing suffixes are present."""
    if not models:
        return []
    if isinstance(models[0], str):
        return list(models)  # type: ignore[arg-type]
    infos: list[ModelInfo] = models  # type: ignore[assignment]
    width = max(len(m.id) for m in infos)
    has_pricing = any(m.price_suffix() is not None for m in infos)
    if not has_pricing:
        return [m.id for m in infos]
    return [m.label(id_width=width) for m in infos]


def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def extract_model_ids(items: Any) -> list[str]:
    """Parse model ids from an OpenAI-style /models payload and sort them."""
    return [m.id for m in extract_model_infos(items, include_pricing=False)]


def extract_model_infos(
    items: Any,
    *,
    include_pricing: bool = False,
    free_last: bool = False,
    general_purpose_only: bool = False,
) -> list[ModelInfo]:
    """Parse ``ModelInfo`` rows from an OpenAI-style /models ``data`` array."""
    if not isinstance(items, list):
        return []
    models: list[ModelInfo] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        model_id = str(item["id"])
        if general_purpose_only:
            if not is_general_purpose_model_id(model_id):
                continue
            if item.get("expiration_date"):
                continue
            if not _is_text_output_model(item):
                continue
        if include_pricing:
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            models.append(
                ModelInfo(
                    id=model_id,
                    prompt_per_token=_parse_price(pricing.get("prompt")),
                    completion_per_token=_parse_price(pricing.get("completion")),
                )
            )
        else:
            models.append(ModelInfo(id=model_id))
    return sort_model_infos(models, free_last=free_last)
