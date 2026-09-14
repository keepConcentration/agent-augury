"""Discord ``token_env`` validation and user ``.env`` secret helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DEFAULT_SECRETS_ENV_PATH = Path.home() / ".agent-augury" / ".env"


def looks_like_discord_bot_token(value: str) -> bool:
    """Heuristic: user pasted a Bot Token into ``token_env``."""
    value = value.strip()
    if len(value) < 50 or value.count(".") != 2:
        return False
    if _ENV_NAME_PATTERN.fullmatch(value) and value.isupper():
        return False
    a, b, c = value.split(".", 2)
    if not a or not b or not c:
        return False
    segment = re.compile(r"^[\w-]+$")
    return bool(segment.fullmatch(a) and segment.fullmatch(b) and segment.fullmatch(c))


def validate_token_env_name(name: str) -> str | None:
    """Return an error message if *name* is not a valid env var name."""
    name = name.strip()
    if not name:
        return "env var name cannot be empty."
    if looks_like_discord_bot_token(name):
        return (
            "that looks like a Discord bot token, not an env var name. "
            "Use e.g. BOT_TOKEN_AGENT_1 and put the token in `.env`."
        )
    if not _ENV_NAME_PATTERN.fullmatch(name):
        return "use letters, digits, underscore only (e.g. BOT_TOKEN_AGENT_1)."
    return None


def upsert_dotenv_value(
    key: str,
    value: str,
    *,
    path: Path | None = None,
) -> Path:
    """Write or update ``KEY=value`` in a dotenv file; return the path used."""
    from dotenv import set_key

    dest = path if path is not None else DEFAULT_SECRETS_ENV_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(dest), key, value, quote_mode="always")
    return dest


def normalize_discord_token(raw: str) -> str:
    """Strip whitespace/quotes accidentally copied into ``.env``."""
    token = (raw or "").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1].strip()
    return token


def discord_token_shape_hint(token: str) -> str | None:
    """Return a short hint when *token* is unlikely to be a Bot Token."""
    token = normalize_discord_token(token)
    if not token:
        return "token is empty after trimming — check the env var name and `.env` file."
    if " " in token:
        return "token contains spaces — paste one continuous line with no spaces."
    if token.count(".") != 2:
        return (
            "Bot Token is usually three dot-separated segments; this may be a "
            "Client Secret, Public Key, or a truncated copy."
        )
    if len(token) < 50:
        return "token looks too short — copy the full Bot Token from Portal → Bot."
    return None


def load_merged_dotenv_into_environ() -> None:
    """Fill unset env vars from dotenv files (shell exports always win).

    Later paths override earlier ones among files only:
    project ``.env`` < cwd ``.env`` < ``~/.agent-augury/.env``.
    """
    from dotenv import dotenv_values

    preexisting = frozenset(os.environ)
    paths = [
        Path(__file__).resolve().parents[2] / ".env",
        Path.cwd() / ".env",
        DEFAULT_SECRETS_ENV_PATH,
    ]
    merged: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        for key, value in (dotenv_values(path) or {}).items():
            if value is not None and value != "":
                merged[key] = value
    for key, value in merged.items():
        if key not in preexisting:
            os.environ[key] = value


def store_bot_token(
    env_name: str,
    token: str,
    *,
    path: Path | None = None,
) -> Path:
    """Persist *token* under *env_name* in ``.env`` and ``os.environ``."""
    token = normalize_discord_token(token)
    if not token:
        raise ValueError("token cannot be empty")
    err = validate_token_env_name(env_name)
    if err:
        raise ValueError(err)
    dest = upsert_dotenv_value(env_name, token, path=path)
    os.environ[env_name] = token
    return dest
