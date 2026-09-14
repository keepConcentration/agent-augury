"""Locate and prepare the Ink Interactive Surface (Node front).

Resolution order for the Ink directory:

1. ``AUGURY_INK_DIR`` — explicit override
2. Repo checkout ``fronts/ink`` (editable / ``src`` layout)
3. Bundled package data copied into a user cache, then ``npm install``

PyPI wheels ship Ink sources under ``agent_augury/fronts/ink`` (no
``node_modules``). First run materializes them into a writable cache because
site-packages is often read-only.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib import metadata
from pathlib import Path

INK_DIR_ENV = "AUGURY_INK_DIR"
PROJECT_ROOT_ENV = "AUGURY_PROJECT_ROOT"

_PACKAGE_DIR = Path(__file__).resolve().parent
_MARKER_NAME = ".augury-ink-stamp"


def _package_version() -> str:
    try:
        return metadata.version("agent-augury")
    except metadata.PackageNotFoundError:
        from agent_augury import __version__

        return __version__


def package_dir() -> Path:
    """Directory containing the installed ``agent_augury`` package."""
    return _PACKAGE_DIR


def packaged_ink_source() -> Path | None:
    """Ink sources shipped inside the wheel, or ``None`` if absent."""
    candidate = _PACKAGE_DIR / "fronts" / "ink"
    if (candidate / "package.json").is_file():
        return candidate
    return None


def resolve_project_root() -> Path | None:
    """Return the git/checkout project root when known.

    Order: ``AUGURY_PROJECT_ROOT`` → editable ``src/`` layout → ``None``
    (plain ``pip install`` has no checkout root).
    """
    raw = os.environ.get(PROJECT_ROOT_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            return path
        return None

    # Editable / monorepo: .../src/agent_augury/ink_front.py → parents[1] == repo
    if _PACKAGE_DIR.parent.name == "src":
        root = _PACKAGE_DIR.parents[1]
        if (root / "fronts" / "ink" / "package.json").is_file() or (
            root / "pyproject.toml"
        ).is_file():
            return root
    return None


def repo_ink_dir() -> Path | None:
    """``fronts/ink`` under a local checkout, if present."""
    root = resolve_project_root()
    if root is None:
        return None
    candidate = root / "fronts" / "ink"
    if (candidate / "package.json").is_file():
        return candidate
    return None


def ink_cache_dir() -> Path:
    """Writable cache root for materialized Ink + ``node_modules``."""
    ver = _package_version()
    override = os.environ.get("AUGURY_CACHE_DIR", "").strip()
    if override:
        base = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        base = base / "agent-augury"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "agent-augury"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".cache"
        base = base / "agent-augury"
    return base / "ink" / ver


def _stamp_payload(source: Path) -> str:
    pkg_json = (source / "package.json").read_text(encoding="utf-8")
    return f"{_package_version()}\n{len(pkg_json)}\n{pkg_json}"


def _materialize_ink(source: Path, dest: Path) -> None:
    """Copy bundled Ink sources into *dest* when stamp mismatches."""
    stamp_path = dest / _MARKER_NAME
    payload = _stamp_payload(source)
    if (
        dest.is_dir()
        and stamp_path.is_file()
        and stamp_path.read_text(encoding="utf-8") == payload
    ):
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("node_modules", ".augury-ink-stamp"),
    )
    stamp_path.write_text(payload, encoding="utf-8")


def _looks_like_ink(path: Path) -> bool:
    return (path / "package.json").is_file()


def resolve_ink_dir() -> Path | None:
    """Pick an Ink front directory without running ``npm install``."""
    raw = os.environ.get(INK_DIR_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
        return path if _looks_like_ink(path) else None

    repo = repo_ink_dir()
    if repo is not None:
        return repo

    packaged = packaged_ink_source()
    if packaged is not None:
        cache = ink_cache_dir()
        _materialize_ink(packaged, cache)
        return cache if _looks_like_ink(cache) else None

    return None


def ensure_npm_dependencies(ink_dir: Path) -> str | None:
    """Run ``npm install`` when ``node_modules`` is missing. Return error or None."""
    import subprocess

    if (ink_dir / "node_modules").is_dir():
        return None
    npm = shutil.which("npm")
    if npm is None:
        return (
            "npm not found on PATH. Install Node.js >= 22 from https://nodejs.org/ "
            "so the Ink Surface can install its dependencies."
        )
    print(f"Installing Ink dependencies in {ink_dir} …", flush=True)
    result = subprocess.run([npm, "install"], cwd=ink_dir, check=False)
    if result.returncode != 0:
        return (
            f"`npm install` failed in {ink_dir} (exit {result.returncode}). "
            "Fix Node/npm, or set AUGURY_INK_DIR to a prepared fronts/ink checkout."
        )
    return None


def ensure_ink_front() -> tuple[Path | None, str | None]:
    """Resolve Ink and ensure deps. Returns ``(path, error_message)``."""
    raw = os.environ.get(INK_DIR_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            return None, (
                f"AUGURY_INK_DIR={path} is not a directory.\n"
                "Point it at a fronts/ink checkout (must contain package.json)."
            )
        if not _looks_like_ink(path):
            return None, (
                f"AUGURY_INK_DIR={path} has no package.json.\n"
                "Expected an agent-augury Ink front (repo fronts/ink)."
            )
        err = ensure_npm_dependencies(path)
        return (path, err) if err else (path, None)

    ink = resolve_ink_dir()
    if ink is None:
        return None, (
            "Ink Surface front not found.\n"
            "\n"
            "agent-augury needs the Node Ink UI (bundled with the package, or a "
            "repo checkout).\n"
            "\n"
            "Fix:\n"
            "  1. Install Node.js >= 22 (npm on PATH): https://nodejs.org/\n"
            "  2. Re-run `agent-augury` — first launch copies the bundled front "
            "and runs `npm install`\n"
            "  3. Or clone https://github.com/keepConcentration/agent-augury and "
            "set:\n"
            "       AUGURY_INK_DIR=/path/to/agent-augury/fronts/ink\n"
            "  4. Developers: run from a checkout (`uv sync` / editable install) "
            "so fronts/ink resolves automatically\n"
            f"\n"
            f"Also accepted: set {PROJECT_ROOT_ENV} to the repo root."
        )

    err = ensure_npm_dependencies(ink)
    if err:
        return None, err
    return ink, None


def pythonpath_src_entry() -> Path | None:
    """``<repo>/src`` for editable installs so Gateway children find the package."""
    root = resolve_project_root()
    if root is None:
        return None
    src = root / "src"
    return src if src.is_dir() else None
