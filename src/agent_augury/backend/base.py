"""Model backend abstraction (DESIGN.md §3.2 'Model Backend')."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]
ToolSpec = dict[str, Any]


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Completion:
    """One model response: final text and/or requested tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None


class ModelBackend(ABC):
    """Adapter interface over a specific model API."""

    @abstractmethod
    async def complete(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Completion:
        """Run one model call over the conversation; may return text and/or tool calls."""

    async def list_models(self) -> list[str] | None:
        """Fetch available model IDs from the provider.

        Returns None if the provider does not support model listing.
        Subclasses may override this to provide real listings.
        """
        return None


class OAuthModelBackend(ModelBackend):
    """Backend that uses OAuth tokens for API access.

    Subclasses must implement `complete`; token management is handled
    by the subclass via its own auth flow.
    """

    @abstractmethod
    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""

