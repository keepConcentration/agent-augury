"""Model backend adapters (OpenAI-compatible, Anthropic Messages, Nous, Fake)."""

from .base import Completion, ModelBackend, ToolCall

__all__ = ["Completion", "ModelBackend", "ToolCall"]
