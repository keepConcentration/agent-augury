"""Model backend adapters (OpenAI-compatible 1st, Nous Portal 2nd, Fake for tests)."""

from .base import Completion, ModelBackend, ToolCall

__all__ = ["Completion", "ModelBackend", "ToolCall"]
