"""Synthetic Discord-shaped tokens for tests (not real credentials)."""

from __future__ import annotations


def fake_discord_bot_token(*, prefix: str = "aa") -> str:
    """Build a string that passes ``looks_like_discord_bot_token``.

    Assembled from short segments so secret-scanners / redactors do not
    replace the fixture with a placeholder.
    """
    a = (prefix + ("A" * 30))[:24]
    b = "Bb" * 3  # length 6
    c = ("Cc" * 20)[:27]
    return f"{a}.{b}.{c}"
