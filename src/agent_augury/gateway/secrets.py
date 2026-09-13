"""Pass API tokens to the Gateway child without exposing them to Ink (D1).

CLI writes a short-lived JSON secrets file, scrubs token-like env vars from the
env inherited by ``npm start`` (Node), and sets ``AUGURY_GATEWAY_SECRETS_FILE``.
The Python Gateway loads those values into ``os.environ`` then deletes the file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SECRETS_FILE_ENV = "AUGURY_GATEWAY_SECRETS_FILE"

# Names that look like credentials / webhook URLs (case-insensitive).
_SECRET_NAME_RE = re.compile(
    r"(?:^|.+_)("
    r"API_KEY|ACCESS_TOKEN|AUTH_TOKEN|BOT_TOKEN|SECRET|PASSWORD|"
    r"PRIVATE_KEY|WEBHOOK_URL|WEBHOOK"
    r")$",
    re.IGNORECASE,
)


def is_secret_env_name(name: str) -> bool:
    """Return True if *name* should not be inherited by the Ink Surface."""
    if name == SECRETS_FILE_ENV:
        return False
    return bool(_SECRET_NAME_RE.search(name))


def extract_secret_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``{name: value}`` for secret-like keys in *env* (default ``os.environ``)."""
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if is_secret_env_name(k) and v}


def write_secrets_file(secrets: dict[str, str]) -> Path | None:
    """Write *secrets* to a user-only tempfile. Returns None if empty."""
    if not secrets:
        return None
    fd, path_str = tempfile.mkstemp(prefix="augury-secrets-", suffix=".json")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(secrets, fh, ensure_ascii=False)
        try:
            os.chmod(path, 0o600)
        except OSError:  # noqa: S110 — best-effort on platforms without chmod
            pass
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def scrub_env_for_ink(env: dict[str, str] | None = None) -> dict[str, str]:
    """Copy *env*, drop secret values, and point Gateway at a secrets file.

    The secrets file path itself is not secret (contents are); Ink may pass the
    path through to the Python child without reading it.
    """
    base = dict(os.environ if env is None else env)
    secrets = extract_secret_env(base)
    for key in secrets:
        base.pop(key, None)
    path = write_secrets_file(secrets)
    if path is not None:
        base[SECRETS_FILE_ENV] = str(path)
    else:
        base.pop(SECRETS_FILE_ENV, None)
    return base


def load_gateway_secrets(*, unlink: bool = True) -> dict[str, Any]:
    """Load ``AUGURY_GATEWAY_SECRETS_FILE`` into ``os.environ`` (Gateway only).

    Existing env values win (do not overwrite). Returns the loaded mapping.
    """
    path_str = os.environ.pop(SECRETS_FILE_ENV, None)
    if not path_str:
        return {}
    path = Path(path_str)
    loaded: dict[str, Any] = {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                loaded[key] = value
                if key not in os.environ:
                    os.environ[key] = value
    except (OSError, json.JSONDecodeError, TypeError):
        return loaded
    finally:
        if unlink:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # noqa: S110
                pass
    return loaded
