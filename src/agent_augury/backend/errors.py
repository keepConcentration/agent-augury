"""Backend failure classification (BACKEND_ERROR_CLASSIFICATION_DESIGN).

An API failure is NOT model output. Turning it into ``Completion(text=...)``
loses the one fact the runtime needs: that the call failed. A live session
stalled at a closed gate because a 429 looked exactly like an agent choosing
to stay quiet, and gate-wait park keeps the turn open until a human quits.

``kind`` names the cause (for logs, and so new values can be added without
breaking callers); the two booleans are what the retry loop actually branches
on. Hermes needs 20+ reasons because it has 20+ recovery actions (credential
rotation, model fallback, context compression); we have two — retry or stop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# A busy upstream is not a throttled key: a different model may serve it.
_UPSTREAM_BUSY = (
    "capacity upstream",
    "temporarily at capacity",
    "not your api key's rate limit",
    "provider returned error",
    "overloaded",
    "server is busy",
)
# 400s that mean "the conversation got too big", not "the request is malformed".
_CONTEXT_OVERFLOW = (
    "context length",
    "context size",
    "maximum context",
    "too many tokens",
    "reduce the length",
)


@dataclass
class BackendError:
    """Why a model call failed, and what the loop may do about it."""

    kind: str
    retryable: bool
    should_fallback: bool = False
    model: str | None = None
    status: int | None = None
    message: str = ""
    detail: str = ""

    def __str__(self) -> str:  # log/UI one-liner
        where = f" [{self.model}]" if self.model else ""
        return f"{self.kind}{where}: {self.message}"


def classify_http(
    status: int | None, body: str, *, model: str | None = None
) -> BackendError:
    """Map an HTTP failure to a recovery decision."""
    text = (body or "").lower()
    detail = (body or "")[:500]

    def err(kind: str, retryable: bool, message: str, *, fallback: bool = False):
        return BackendError(
            kind=kind, retryable=retryable, should_fallback=fallback,
            model=model, status=status, message=message, detail=detail,
        )

    if status in (401, 403):
        return err(
            "auth", False,
            f"Authentication failed (HTTP {status}). Check that the API key env "
            "var holds the real key, not the variable name.",
        )
    if status == 404:
        # A missing model is fatal today, but a fallback model would fix it.
        return err("model_not_found", False,
                   f"Model {model!r} not found (HTTP 404).", fallback=True)
    if status == 429:
        if any(p in text for p in _UPSTREAM_BUSY):
            return err("upstream_busy", True,
                       "Upstream model is at capacity (HTTP 429). The API key is "
                       "fine; another model would likely serve.", fallback=True)
        return err("rate_limit", True, "Rate limited (HTTP 429).")
    if status == 400:
        if any(p in text for p in _CONTEXT_OVERFLOW):
            # Retrying unchanged will fail again, but calling it fatal kills a
            # session that compaction could save. Keep it recoverable-ish and
            # let the detail reach the log. (design 4.1)
            return err("unknown", True, "Request rejected (HTTP 400) — the "
                                        "conversation may be too long.")
        return err("bad_request", False, "Malformed request (HTTP 400).")
    if status is not None and 500 <= status < 600:
        return err("server_error", True, f"Provider error (HTTP {status}).")
    return err("unknown", True, f"Unexpected response (HTTP {status}).")


def network_error(message: str, *, model: str | None = None) -> BackendError:
    return BackendError(
        kind="network", retryable=True, model=model,
        message=f"Network error: {message}", detail=message,
    )


def auth_error(message: str, *, model: str | None = None) -> BackendError:
    return BackendError(
        kind="auth", retryable=False, model=model, message=message, detail=message,
    )


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Exponential backoff with jitter (ported from Hermes ``retry_utils``).

    ``attempt`` is 1-based. Jitter spreads simultaneous agents so they do not
    all retry into the same busy upstream at once.
    """
    delay = min(base_delay * (2 ** max(attempt - 1, 0)), max_delay)
    return delay + random.uniform(0, jitter_ratio * delay)
