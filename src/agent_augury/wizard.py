"""Interactive setup wizard for agent-augury.

Runs when ``agent-augury`` is invoked without ``--config``. Walks the user
through backend selection, model/URL/env-var entry, and writes a YAML file
that is compatible with :func:`agent_augury.config.load_config`.

Secrets are never requested or stored — only the environment-variable name
(``api_key_env``) is saved.
"""

from __future__ import annotations

import sys
from typing import Any

# -- constants ---------------------------------------------------------------

BACKEND_OPTIONS: dict[str, tuple[str, str]] = {
    "1": ("fake", "Fake (offline, scripted)"),
    "2": ("openai", "OpenAI-compatible API"),
    "3": ("nous", "Nous Portal"),
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
        print(f"  (invalid choice: {choice!r} — enter 1, 2, or 3)")


def _build_agent(agent_index: int) -> dict[str, Any]:
    """Interactively build one agent entry."""
    print(f"\n--- Agent {agent_index + 1} ---")

    agent_id = _input("Agent ID", f"agent-{agent_index + 1}")
    backend_type = _select_backend()

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
    else:
        # Real backends — model, base URL, env-var name.
        model = _input_required("Model name")
        backend["model"] = model

        default_url = (
            NOUS_DEFAULT_BASE_URL if backend_type == "nous" else OPENAI_DEFAULT_BASE_URL
        )
        base_url = _input("Base URL", default_url)
        backend["base_url"] = base_url

        api_key_env = _input_required("API key env var name")
        backend["api_key_env"] = api_key_env

    return {"id": agent_id, "backend": backend}


def run_wizard() -> dict[str, Any]:
    """Run the interactive wizard and return a config dict."""
    print("=" * 50)
    print("  agent-augury setup wizard")
    print("=" * 50)
    print()
    print("This wizard generates a YAML config file for agent-augury.")
    print("No API keys are stored — only environment variable names.")

    # Session-level settings.
    mode = _input("Session mode (L2/L3)", "L3")
    task = _input("Task description", "Multi-agent collaboration")
    max_steps = _input_int("Max steps", 20)

    # Agents.
    agents: list[dict[str, Any]] = []
    agent_index = 0
    while True:
        agent = _build_agent(agent_index)
        agents.append(agent)
        agent_index += 1

        add_more = _input("\nAdd another agent? (y/n)", "n")
        if add_more.lower() not in ("y", "yes"):
            break

    return {
        "mode": mode,
        "max_steps": max_steps,
        "task": task,
        "agents": agents,
    }
