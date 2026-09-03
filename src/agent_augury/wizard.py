"""Interactive setup wizard for agent-augury.

Runs when ``agent-augury`` is invoked without ``--config``.  Collects
model settings (mode, max_steps, agent/backend configuration) and
persists them to ``~/.agent-augury/model_config.json`` so subsequent
runs can skip this phase.

After provider selection and authentication, the wizard attempts to fetch
the available model list from the provider's ``/models`` endpoint.  If
succeeds, the user can pick from the list; otherwise they fall back to
manual model-ID entry.

Secrets are never requested or stored — only the environment-variable name
(``api_key_env``) is saved.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .model_config import (
    load_model_config,
    model_config_exists,
    save_model_config,
)

# -- constants ---------------------------------------------------------------

BACKEND_OPTIONS: dict[str, tuple[str, str]] = {
    "1": ("fake", "Fake (offline, scripted)"),
    "2": ("openai", "OpenAI-compatible API"),
    "3": ("nous", "Nous Portal (API key)"),
    "4": ("nous_oauth", "Nous Portal (OAuth device code)"),
}

NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


# -- TTY detection -----------------------------------------------------------


def _get_kernel32():
    """Return the kernel32 WinDLL instance (separated for testability)."""
    import ctypes

    return ctypes.windll.kernel32


def _try_attach_parent_console() -> bool:
    """On Windows, try to attach to the parent process's console.

    When agent-augury is launched from a CMD/PowerShell terminal via a
    pip-installed console_scripts wrapper (which uses a GUI subsystem EXE),
    the process is detached from the parent console. This causes isatty()
    to return False even though the user is at an interactive terminal.

    Returns True if the attach succeeded and stdin/stdout are now TTYs.
    """
    if sys.platform != "win32":
        return False

    try:
        kernel32 = _get_kernel32()
        ATTACH_PARENT_PROCESS = -1

        # Try to attach to the parent console.
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False

        # Reopen stdin/stdout to the console.
        sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")

        return sys.stdin.isatty() and sys.stdout.isatty()
    except (OSError, AttributeError, ValueError, ImportError):
        return False


def check_tty() -> bool:
    """Return True when both stdin and stdout are interactive terminals.

    On Windows, if isatty() returns False (e.g. when launched via a
    pip-installed console_scripts wrapper), attempt to attach to the
    parent console and reopen stdin/stdout.
    """
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return True
    except (AttributeError, ValueError):
        pass

    # On Windows, try to attach to the parent console.
    return _try_attach_parent_console()


# -- input helpers -----------------------------------------------------------


class WizardCancelled(Exception):
    """Raised when the user cancels the wizard (Ctrl+C / EOF)."""


def _input(prompt: str, default: str | None = None) -> str:
    """Read a line of input, falling back to *default* on empty input."""
    if default is not None:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "

    try:
        value = input(full_prompt).strip()
    except EOFError as exc:
        raise WizardCancelled("stdin closed") from exc
    except KeyboardInterrupt as exc:
        raise WizardCancelled("interrupted") from exc

    if not value and default is not None:
        return default
    return value


def _input_required(prompt: str) -> str:
    """Re-prompt until a non-empty value is entered."""
    while True:
        value = _input(prompt)
        if value:
            return value
        print("  (required — cannot be empty)")


def _input_int(prompt: str, default: int) -> int:
    """Read an integer, falling back to *default* on blank/invalid input."""
    raw = _input(prompt, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"  (invalid number {raw!r} — using default {default})")
        return default


# -- model listing -----------------------------------------------------------


def _try_list_models(
    backend_type: str,
    base_url: str,
    api_key_env: str | None,
) -> list[str] | None:
    """Attempt to fetch model IDs from the provider.

    Returns a list of model IDs on success, None on failure (network
    error, unsupported endpoint, missing credentials, etc.).
    """
    from .backends_factory import (
        list_models_nous_oauth,
        list_models_nous_portal,
        list_models_openai_compat,
    )

    try:
        if backend_type == "openai":
            if not api_key_env:
                return None
            api_key = os.environ.get(api_key_env, "")
            if not api_key:
                return None
            return list_models_openai_compat(base_url, api_key)
        if backend_type == "nous":
            if not api_key_env:
                return None
            api_key = os.environ.get(api_key_env, "")
            if not api_key:
                return None
            return list_models_nous_portal(base_url, api_key)
        if backend_type == "nous_oauth":
            return list_models_nous_oauth(base_url)
    except Exception:  # noqa: BLE001 — best-effort listing
        return None
    return None


def _has_valid_oauth_token() -> bool:
    """Check if a valid (non-expiring) OAuth token exists for Nous Portal."""
    from .auth.oauth import NOUS_PORTAL_CONFIG
    from .auth.token_store import TokenStore, is_token_expiring

    tokens = TokenStore().get_provider_tokens(NOUS_PORTAL_CONFIG.id)
    if not tokens or not tokens.get("access_token"):
        return False
    return not is_token_expiring(tokens.get("expires_at"))


def _run_nous_oauth_device_code(force_reconfigure: bool = False) -> str | None:
    """Run Nous Portal OAuth device code flow, store token, return access token.

    Args:
        force_reconfigure: If True, always run authentication even if a valid
            token exists.

    Returns None on failure or cancellation so the caller can fall back
    to manual model entry.
    """
    from .auth.oauth import NOUS_PORTAL_CONFIG, DeviceCodeFlow
    from .auth.token_store import TokenStore, compute_expires_at, is_token_expiring
    from datetime import datetime, timezone

    # Check for existing valid token first (unless force reconfigure).
    if not force_reconfigure:
        store = TokenStore()
        tokens = store.get_provider_tokens(NOUS_PORTAL_CONFIG.id)
        access_token = tokens.get("access_token")
        expires_at = tokens.get("expires_at")
        if access_token and not is_token_expiring(expires_at):
            print("\n  Using existing authentication (valid token found).")
            return access_token

    try:
        flow = DeviceCodeFlow(NOUS_PORTAL_CONFIG)
        print("\n  Opening browser for Nous Portal authentication...")
        token = flow.authenticate(
            on_user_code=lambda code, uri: (
                print(f"\n  To authenticate, enter code: {code}")
                or print(f"  Verification URL: {uri}")
            ),
            open_browser=True,
        )
        # Persist token for subsequent /models calls.
        expires_at = compute_expires_at(token.expires_in)
        TokenStore().set_provider_tokens(
            NOUS_PORTAL_CONFIG.id,
            {
                "access_token": token.access_token,
                "token_type": token.token_type,
                "expires_in": token.expires_in,
                "expires_at": expires_at,
                "refresh_token": token.refresh_token,
                "scope": token.scope,
                "obtained_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print("  Authentication successful!")
        return token.access_token
    except (KeyboardInterrupt, EOFError):
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort auth
        print(f"  Authentication failed: {exc}")
        return None


def _find_existing_provider_config(agents: list[dict[str, Any]], backend_type: str) -> dict[str, Any] | None:
    """Find an existing backend config for the same provider type.

    Returns the backend dict of the most recent matching agent, or None.
    """
    for agent in reversed(agents):
        backend = agent.get("backend", {})
        if backend.get("type") == backend_type:
            return backend
    return None


def _try_refresh_oauth_token() -> bool:
    """Attempt to refresh an expiring OAuth token.

    Returns True if the token was refreshed or is still valid.
    """
    from .auth.oauth import NOUS_PORTAL_CONFIG, DeviceCodeFlow
    from .auth.token_store import TokenStore, compute_expires_at
    from datetime import datetime, timezone

    tokens = TokenStore().get_provider_tokens(NOUS_PORTAL_CONFIG.id)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return False

    try:
        flow = DeviceCodeFlow(NOUS_PORTAL_CONFIG)
        new_token = flow.refresh_access_token(refresh_token)
        expires_at = compute_expires_at(new_token.expires_in)
        TokenStore().set_provider_tokens(
            NOUS_PORTAL_CONFIG.id,
            {
                "access_token": new_token.access_token,
                "token_type": new_token.token_type,
                "expires_in": new_token.expires_in,
                "expires_at": expires_at,
                "refresh_token": new_token.refresh_token or refresh_token,
                "scope": new_token.scope,
                "obtained_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return True
    except Exception:
        return False


def _select_model_interactive(models: list[str]) -> str | None:
    """Show a numbered model list; return selected model ID or None for manual."""
    print("\n  Available models:")
    for i, m in enumerate(models, 1):
        print(f"    {i}) {m}")
    manual_idx = len(models) + 1
    print(f"    {manual_idx}) Enter model ID manually")
    choice = _input_int("  Select model", manual_idx)
    if 1 <= choice <= len(models):
        return models[choice - 1]
    return None


# -- wizard steps ------------------------------------------------------------


def _select_backend() -> str:
    """Prompt the user to pick a backend type. Returns the type key."""
    print("\nSelect backend type:")
    for key, (_btype, desc) in BACKEND_OPTIONS.items():
        print(f"  {key}) {desc}")

    while True:
        choice = _input("Choice", "1")
        if choice in BACKEND_OPTIONS:
            return BACKEND_OPTIONS[choice][0]
        print(f"  (invalid choice: {choice!r} — enter 1, 2, 3, or 4)")


def _collect_model_for_backend(
    backend_type: str,
    base_url: str,
    api_key_env: str | None,
) -> str:
    """Collect a model ID — try listing from provider first, fall back to manual.

    The manual fallback does NOT block on empty input: if the user leaves
    it blank, a warning is shown and the value is returned as-is so the
    user can reconfigure later with ``--reconfigure``.
    """
    models = _try_list_models(backend_type, base_url, api_key_env)
    if models:
        selected = _select_model_interactive(models)
        if selected is not None:
            return selected
    # Fallback: manual entry (non-blocking — empty is allowed).
    model = _input("Model name")
    if not model:
        print(
            "  (no model selected — you can set it later with --reconfigure)"
        )
    return model


def _build_agent(
    agent_index: int,
    existing_agents: list[dict[str, Any]] | None = None,
    force_reconfigure: bool = False,
) -> dict[str, Any]:
    """Interactively build one agent entry.

    Args:
        agent_index: Zero-based index of this agent.
        existing_agents: Previously configured agents — used to offer
            reusing credentials when the same provider is selected.
        force_reconfigure: If True, always run OAuth authentication even
            if a valid token exists.
    """
    print(f"\n--- Agent {agent_index + 1} ---")

    agent_id = _input("Agent ID", f"agent-{agent_index + 1}")
    backend_type = _select_backend()

    existing = (
        _find_existing_provider_config(existing_agents, backend_type)
        if existing_agents
        else None
    )

    backend: dict[str, Any] = {"type": backend_type}

    if backend_type == "fake":
        # Fake backend — collect scripted text responses.
        print("  (fake backend — enter scripted text responses, empty line to finish)")
        script: list[str] = []
        while True:
            entry = _input("  script entry", "")
            if not entry:
                break
            script.append(entry)
        backend["script"] = script if script else ["done"]
    elif backend_type == "nous_oauth":
        # OAuth — use default Base URL internally, no prompt.
        # Authentication starts immediately (or reuses valid token).
        base_url = NOUS_DEFAULT_BASE_URL
        backend["base_url"] = base_url
        print("  (authentication via browser — device code flow)")
        # Check for valid token first (unless force_reconfigure)
        if not force_reconfigure and _has_valid_oauth_token():
            print("  (existing credentials detected — reusing authentication)")
            token = "existing"
        else:
            token = _run_nous_oauth_device_code(force_reconfigure=force_reconfigure)
        if token:
            backend["model"] = _collect_model_for_backend(
                backend_type, base_url, None
            )
        else:
            # Auth failed or cancelled — fall back to manual entry.
            print("  (authentication cancelled — manual model entry)")
            backend["model"] = _input("Model name")
    else:
        # Real backends (openai, nous) — reuse existing env var if available.
        default_url = (
            NOUS_DEFAULT_BASE_URL if backend_type == "nous" else OPENAI_DEFAULT_BASE_URL
        )
        if existing:
            default_url = existing.get("base_url", default_url)
        base_url = _input("Base URL", default_url)
        backend["base_url"] = base_url

        if existing and existing.get("api_key_env"):
            existing_env = existing["api_key_env"]
            reuse = _input(
                f"Re-use existing API key env var '{existing_env}'? (y/n)", "y"
            )
            if reuse.lower() in ("y", "yes"):
                backend["api_key_env"] = existing_env
            else:
                api_key_env = _input_required("API key env var name")
                backend["api_key_env"] = api_key_env
        else:
            api_key_env = _input_required("API key env var name")
            backend["api_key_env"] = api_key_env

        backend["model"] = _collect_model_for_backend(backend_type, base_url, backend["api_key_env"])

    return {"id": agent_id, "backend": backend}


def _collect_model_settings(force_reconfigure: bool = False) -> tuple[str, int, list[dict[str, Any]]]:
    """Phase 1: collect mode, max_steps, and agent/backend settings."""
    print("\n--- Model Configuration ---")
    mode = _input("Session mode (L2/L3)", "L3")
    max_steps = _input_int("Max steps", 20)

    # Agents.
    agents: list[dict[str, Any]] = []
    agent_index = 0
    while True:
        # Only the first agent honors force_reconfigure; subsequent agents
        # should reuse the shared TokenStore so they don't re-open the
        # browser when a valid token already exists.
        agent_force_reconfigure = force_reconfigure if agent_index == 0 else False
        agent = _build_agent(agent_index, existing_agents=agents, force_reconfigure=agent_force_reconfigure)
        agents.append(agent)
        agent_index += 1

        add_more = _input("\nAdd another agent? (y/n)", "n")
        if add_more.lower() not in ("y", "yes"):
            break

    return mode, max_steps, agents


def run_wizard(
    existing_model_config: dict[str, Any] | None = None,
    force_reconfigure: bool = False,
) -> dict[str, Any]:
    """Run the interactive wizard and return a config dict.

    Collects model settings from the user (or loads from
    *existing_model_config* when provided), then persists them to disk.

    Args:
        existing_model_config: If provided, the model-settings phase is
            skipped and these values are reused.
        force_reconfigure: If True, always run OAuth authentication even
            if a valid token exists.
    """
    print("=" * 50)
    print("  agent-augury setup wizard")
    print("=" * 50)
    print()
    print("This wizard generates a YAML config file for agent-augury.")
    print("No API keys are stored — only environment variable names.")

    if existing_model_config is not None:
        # Reuse saved model settings — skip directly to config generation.
        mode = existing_model_config.get("mode", "L3")
        max_steps = existing_model_config.get("max_steps", 20)
        agents = existing_model_config["agents"]
        print(
            f"\nUsing saved model config: mode={mode}, max_steps={max_steps}, "
            f"{len(agents)} agent(s)."
        )
    else:
        # Phase 1: collect model settings from user.
        mode, max_steps, agents = _collect_model_settings(force_reconfigure=force_reconfigure)
        # Persist model settings immediately (path is internal to save_model_config).
        save_model_config(mode, max_steps, agents)

    return {
        "mode": mode,
        "max_steps": max_steps,
        "agents": agents,
    }
