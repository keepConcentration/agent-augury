"""Secure token storage for OAuth credentials.

Stores tokens in a JSON file with restrictive file permissions.
Thread-safe for concurrent access within a single process.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Token refresh: refresh when less than this many seconds remain
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


class TokenStore:
    """Persistent store for OAuth tokens.

    Stores tokens in a JSON file at ``~/.agent-augury/tokens.json``.
    File is created with 0o600 permissions (owner read/write only).
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self._store_path = store_path or self._default_path()
        self._lock = threading.Lock()

    @staticmethod
    def _default_path() -> Path:
        """Return default token store path."""
        return Path.home() / ".agent-augury" / "tokens.json"

    def load(self) -> Dict[str, Any]:
        """Load tokens from disk. Returns empty dict if not found."""
        with self._lock:
            if not self._store_path.exists():
                return {}
            try:
                raw = self._store_path.read_text(encoding="utf-8")
                return json.loads(raw)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load token store: %s", exc)
                return {}

    def save(self, tokens: Dict[str, Any]) -> None:
        """Atomically persist tokens to disk."""
        with self._lock:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            # Write to temp file first, then atomically replace
            tmp_path = self._store_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(tokens, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # Set restrictive permissions (owner read/write only)
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows may not support this fully
            # Atomic replace
            tmp_path.replace(self._store_path)

    def clear(self) -> None:
        """Remove all stored tokens."""
        with self._lock:
            if self._store_path.exists():
                self._store_path.unlink()

    def get_provider_tokens(self, provider_id: str) -> Dict[str, Any]:
        """Get tokens for a specific provider."""
        return self.load().get(provider_id, {})

    def set_provider_tokens(self, provider_id: str, tokens: Dict[str, Any]) -> None:
        """Set tokens for a specific provider."""
        all_tokens = self.load()
        all_tokens[provider_id] = tokens
        self.save(all_tokens)


def is_token_expiring(expires_at: Optional[str], skew_seconds: int = ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    """Check if a token is expired or about to expire.

    Args:
        expires_at: ISO-format datetime string.
        skew_seconds: Refresh this many seconds before actual expiry.

    Returns:
        True if the token is expired or expiring within skew_seconds.
        False if there is no expiry information (treat as valid/unknown).
    """
    if not expires_at:
        return False  # No expiry info — treat as valid
    try:
        expiry = datetime.fromisoformat(expires_at)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return now.timestamp() + skew_seconds >= expiry.timestamp()
    except (ValueError, TypeError):
        return True  # Malformed date — treat as expired


def compute_expires_at(expires_in: Optional[int]) -> Optional[str]:
    """Compute absolute expiry timestamp from relative seconds.

    Args:
        expires_in: Seconds until token expires.

    Returns:
        ISO-format datetime string in UTC, or None if expires_in is invalid.
    """
    if not expires_in or expires_in <= 0:
        return None
    expiry = datetime.now(timezone.utc).timestamp() + expires_in
    return datetime.fromtimestamp(expiry, tz=timezone.utc).isoformat()


