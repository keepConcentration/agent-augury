"""CLI entrypoint: wizard + Ink Interactive Surface.

Modes:
  - ``agent-augury --config PATH`` — Ink session (requires Node + fronts/ink).
  - ``agent-augury`` (no args) — interactive wizard, then Ink session.
  - ``agent-augury --ink-hello`` — Ink hello Gateway demo (M2/M3).

Flags:
  - ``--reconfigure`` — discard saved model settings and re-run the wizard.
  - ``--quiet`` — suppress live event output in the Surface.
  - ``--demo`` — allow ``type: fake`` backends.

Design: ``docs/architecture/MULTI_FRONT_DESIGN.md`` (M7) — Ink is the only
Interactive Surface. Headless automation uses Gateway ``session_stdio``.

v0.7 (AGENT_TOOLS_EXPANSION_DESIGN.md v4.1, P9): ``allowed_roots`` 배선은
Gateway session child (``session_stdio``)에서 프로젝트 루트를 전달한다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from .display import mask_sensitive, render_event
from .model_config import (
    load_model_config,
    model_config_exists,
)
from .protocol.defaults import DEFAULT_PROTOCOL
from .wizard import WizardCancelled, check_tty, run_wizard

_DEFAULT_OUTPUT_PATH = Path.home() / ".agent-augury" / "agent-augury-session.yaml"
_INVALID_PATH_CHARS = set('<>"|?*')
_INVISIBLE_CODEPOINTS = frozenset({0x3164, 0x200B, 0x200C, 0x200D, 0xFEFF, 0x00A0})

# P9: 프로젝트 루트 — cli.py → 프로젝트 루트 (agent_augury/ 하위의 cli.py 기준 parents[2])
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _mask_sensitive(text: str) -> str:
    """Backward-compat alias used by tests / older call sites."""
    return mask_sensitive(text)


def _log_tool_event(event: dict[str, Any]) -> None:
    """Print a tool/broadcast event (compat wrapper around render_event)."""
    ansi = render_event(event)
    if ansi:
        print(ansi)


def _output_path_problem(raw: str) -> str | None:
    """Return a short reason when *raw* is not a usable save path, else None."""
    if not raw:
        return "empty"
    if not raw.strip():
        return "whitespace only"
    for i, char in enumerate(raw):
        code = ord(char)
        if code in _INVISIBLE_CODEPOINTS:
            return f"invisible character U+{code:04X}"
        if char == ":":
            if i != 1:
                return f"invalid path character {char!r}"
            continue
        if char in _INVALID_PATH_CHARS:
            return f"invalid path character {char!r}"
        if code < 32:
            return "control character"
    return None


def _resolve_output_path(raw: str | None, default: Path = _DEFAULT_OUTPUT_PATH) -> Path:
    """Map wizard save-path input to a concrete path (blank → default)."""
    if raw is None or not raw.strip():
        return default
    problem = _output_path_problem(raw)
    if problem is not None:
        print(f"  Warning: invalid save path ({problem}). Using default: {default}")
        return default
    return Path(raw)


def _prompt_output_path(default: Path = _DEFAULT_OUTPUT_PATH) -> Path:
    """Prompt for a YAML output path; Enter uses *default*, invalid input warns."""
    while True:
        raw = input(f"\nSave config to [{default}]: ").strip()
        if not raw:
            return default
        problem = _output_path_problem(raw)
        if problem is None:
            return Path(raw)
        print(
            f"  Warning: invalid save path ({problem}). "
            "Press Enter for default or type a valid path."
        )


def _launch_session(
    cfg_path: str,
    *,
    quiet: bool = False,
    allow_fake: bool = False,
    force_ink: bool = False,
) -> int:
    """Start the Ink Interactive Surface for a session config."""
    del force_ink  # always Ink; flag kept for CLI compat
    return _run_ink_surface(
        mode="session",
        config=cfg_path,
        demo=allow_fake,
        quiet=quiet,
    )


def _save_config(cfg: dict[str, Any], output_path: Path) -> None:
    """Write a config dict to a YAML file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def _missing_api_key_envs(cfg: dict[str, Any]) -> list[str]:
    """Return api_key_env names required by the config but unset in this process."""
    missing: list[str] = []
    seen: set[str] = set()
    for agent in cfg.get("agents") or []:
        backend = agent.get("backend") or {}
        btype = backend.get("type")
        if btype not in ("openai", "nous"):
            continue
        env_name = backend.get("api_key_env")
        if not env_name or env_name in seen:
            continue
        seen.add(env_name)
        if not os.environ.get(env_name):
            missing.append(env_name)
    return missing


def _prompt_and_set_api_keys(env_names: list[str]) -> list[str]:
    """Prompt for missing API keys and set them in the current process env.

    Keys are never written to the YAML config — only into ``os.environ`` for
    this run. Returns names that are still unset after prompting.
    """
    import getpass

    still_missing: list[str] = []
    for name in env_names:
        print(
            f"\n{name} is not set in this shell.\n"
            "Enter the API key for this session "
            "(not saved to config — only the env var name is):"
        )
        try:
            key = getpass.getpass(f"  {name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            still_missing.append(name)
            continue
        if not key:
            still_missing.append(name)
            continue
        os.environ[name] = key
        print(f"  ({name} set for this process)")
    return still_missing


def _run_wizard_flow(
    output_path: Path | None = None,
    force_reconfigure: bool = False,
    quiet: bool = False,
    *,
    force_ink: bool = False,
    allow_fake: bool = False,
) -> int:
    """Run the interactive wizard, save the YAML, then start Ink."""
    del force_ink  # always Ink
    if not check_tty():
        print(
            "error: interactive wizard requires a TTY. "
            "Use --config PATH to run a pre-built config, "
            "or run from an interactive terminal.",
            file=sys.stderr,
        )
        return 1

    try:
        existing = None
        if not force_reconfigure and model_config_exists():
            existing = load_model_config()
            if existing is None:
                existing = None

        if existing is not None and not force_reconfigure:
            cfg = {
                "max_steps": existing.get("max_steps", 0),
                "human": {"id": "human"},
                "protocol": dict(DEFAULT_PROTOCOL),
                "agents": existing["agents"],
            }
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nUsing saved model config. Config saved to: {output_path}")
        else:
            cfg = run_wizard(
                existing_model_config=existing, force_reconfigure=force_reconfigure
            )
            if output_path is None:
                output_path = _DEFAULT_OUTPUT_PATH
            else:
                output_path = _resolve_output_path(str(output_path))
            _save_config(cfg, output_path)
            print(f"\nConfig saved to: {output_path}")
    except WizardCancelled:
        print("\nWizard cancelled.")
        return 130

    missing = _missing_api_key_envs(cfg)
    if missing:
        missing = _prompt_and_set_api_keys(missing)
    if missing:
        print(
            "\nerror: required API key environment variable(s) are not set:",
            file=sys.stderr,
        )
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "Set them in this shell, then re-run agent-augury "
            "(config is already saved).\n"
            "Note: the YAML stores the env var *name* (e.g. OPENROUTER_API_KEY), "
            "not the secret itself.",
            file=sys.stderr,
        )
        if sys.platform == "win32":
            print(
                f'  PowerShell: $env:{missing[0]}="sk-or-..."',
                file=sys.stderr,
            )
        else:
            print(
                f'  export {missing[0]}="sk-or-..."',
                file=sys.stderr,
            )
        return 1

    return _run_ink_surface(
        mode="session",
        config=str(output_path),
        demo=allow_fake,
        quiet=quiet,
    )


def _run_ink_surface(
    *,
    mode: str = "hello",
    config: str | None = None,
    demo: bool = False,
    quiet: bool = False,
) -> int:
    """Spawn ``npm start`` in fronts/ink (M2 hello / M7 real session).

    Ink owns the TTY and spawns a Python Gateway child over JSONL stdio.
    """
    import shutil
    import subprocess

    ink_dir = PROJECT_ROOT / "fronts" / "ink"
    if not ink_dir.is_dir():
        print(
            f"error: Ink Surface required — missing front at {ink_dir}",
            file=sys.stderr,
        )
        return 1
    npm = shutil.which("npm")
    if npm is None:
        print(
            "error: Ink Surface required — npm not found. "
            "Install Node.js >= 22, then re-run.",
            file=sys.stderr,
        )
        return 1
    if not (ink_dir / "node_modules").is_dir():
        print("Installing fronts/ink dependencies…", flush=True)
        install = subprocess.run([npm, "install"], cwd=ink_dir, check=False)
        if install.returncode != 0:
            return install.returncode
    from .gateway.secrets import scrub_env_for_ink

    # D1: do not pass API tokens / bot secrets into the Node Ink process.
    # Secrets ride a short-lived file that only the Python Gateway child loads.
    env = scrub_env_for_ink()
    env.setdefault("AUGURY_PYTHON", sys.executable)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["AUGURY_GATEWAY_MODE"] = mode
    if mode == "session":
        if not config:
            print("error: session mode requires --config PATH", file=sys.stderr)
            return 1
        cfg_path = Path(config).expanduser().resolve()
        if not cfg_path.is_file():
            print(f"error: config not found: {cfg_path}", file=sys.stderr)
            return 1
        env["AUGURY_CONFIG"] = str(cfg_path)
        if demo:
            env["AUGURY_DEMO"] = "1"
        if quiet:
            env["AUGURY_QUIET"] = "1"
    return subprocess.call([npm, "start"], cwd=ink_dir, env=env)


def _run_ink_hello() -> int:
    """M2: Ink hello Surface against ``gateway.hello_demo``."""
    return _run_ink_surface(mode="hello")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-augury")
    parser.add_argument(
        "--config",
        required=False,
        help="path to session YAML (omit to launch interactive wizard)",
    )
    parser.add_argument(
        "--output",
        required=False,
        default=None,
        help="wizard output path (only valid without --config)",
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        default=False,
        help="discard saved model settings and re-run the wizard from scratch",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="suppress broadcast event output (only show final summary)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="allow type:fake backends (offline examples / tests)",
    )
    parser.add_argument(
        "--ink-hello",
        action="store_true",
        default=False,
        help="Ink hello against the Gateway (no full Core session)",
    )
    parser.add_argument(
        "--ink",
        action="store_true",
        default=False,
        help="Ink Surface (default; kept for explicit scripts)",
    )
    args = parser.parse_args(argv)

    if args.ink_hello and args.ink:
        print("error: --ink-hello cannot be combined with --ink", file=sys.stderr)
        return 1

    if args.ink_hello:
        return _run_ink_hello()

    if args.output is not None and args.config is not None:
        print("error: --output is only valid without --config", file=sys.stderr)
        return 1

    if args.config is not None:
        if args.reconfigure:
            print(
                "error: --reconfigure is only valid without --config",
                file=sys.stderr,
            )
            return 1
        return _launch_session(
            args.config,
            quiet=args.quiet,
            allow_fake=args.demo,
            force_ink=args.ink,
        )

    output_path = Path(args.output) if args.output else None
    try:
        return _run_wizard_flow(
            output_path,
            force_reconfigure=args.reconfigure,
            quiet=args.quiet,
            force_ink=args.ink,
            allow_fake=args.demo,
        )
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
