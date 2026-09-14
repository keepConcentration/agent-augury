"""Tests for Ink front path resolution and cache materialization."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _write_fake_ink(root: Path) -> Path:
    ink = root / "fronts" / "ink"
    ink.mkdir(parents=True)
    (ink / "package.json").write_text(
        '{"name":"@agent-augury/ink","version":"0.0.0-test"}',
        encoding="utf-8",
    )
    (ink / "src").mkdir()
    (ink / "src" / "cli.tsx").write_text("// test", encoding="utf-8")
    return ink


def test_resolve_project_root_env(tmp_path, monkeypatch):
    from agent_augury import ink_front

    monkeypatch.setenv("AUGURY_PROJECT_ROOT", str(tmp_path))
    assert ink_front.resolve_project_root() == tmp_path.resolve()


def test_resolve_ink_dir_env_override(tmp_path, monkeypatch):
    from agent_augury import ink_front

    ink = _write_fake_ink(tmp_path)
    monkeypatch.setenv("AUGURY_INK_DIR", str(ink))
    assert ink_front.resolve_ink_dir() == ink.resolve()


def test_resolve_ink_dir_env_invalid(tmp_path, monkeypatch):
    from agent_augury import ink_front

    monkeypatch.setenv("AUGURY_INK_DIR", str(tmp_path / "missing"))
    assert ink_front.resolve_ink_dir() is None


def test_materialize_packaged_ink_to_cache(tmp_path, monkeypatch):
    from agent_augury import ink_front

    packaged = _write_fake_ink(tmp_path / "pkg")
    cache_base = tmp_path / "cache"
    monkeypatch.setenv("AUGURY_CACHE_DIR", str(cache_base))
    monkeypatch.delenv("AUGURY_INK_DIR", raising=False)

    with (
        patch.object(ink_front, "repo_ink_dir", return_value=None),
        patch.object(ink_front, "packaged_ink_source", return_value=packaged),
        patch.object(ink_front, "_package_version", return_value="9.9.9-test"),
    ):
        resolved = ink_front.resolve_ink_dir()

    assert resolved is not None
    assert resolved == (cache_base / "ink" / "9.9.9-test").resolve()
    assert (resolved / "package.json").is_file()
    assert (resolved / "src" / "cli.tsx").is_file()
    assert (resolved / ".augury-ink-stamp").is_file()

    # Second call keeps the same tree (stamp match).
    with (
        patch.object(ink_front, "repo_ink_dir", return_value=None),
        patch.object(ink_front, "packaged_ink_source", return_value=packaged),
        patch.object(ink_front, "_package_version", return_value="9.9.9-test"),
    ):
        again = ink_front.resolve_ink_dir()
    assert again == resolved


def test_ensure_ink_front_missing_message(monkeypatch):
    from agent_augury import ink_front

    monkeypatch.delenv("AUGURY_INK_DIR", raising=False)
    with (
        patch.object(ink_front, "resolve_ink_dir", return_value=None),
    ):
        path, err = ink_front.ensure_ink_front()
    assert path is None
    assert err is not None
    assert "Node.js >= 22" in err
    assert "AUGURY_INK_DIR" in err


def test_ensure_npm_dependencies_skips_when_present(tmp_path):
    from agent_augury import ink_front

    ink = _write_fake_ink(tmp_path)
    (ink / "node_modules").mkdir()
    assert ink_front.ensure_npm_dependencies(ink) is None


def test_ensure_npm_dependencies_reports_missing_npm(tmp_path, monkeypatch):
    from agent_augury import ink_front

    ink = _write_fake_ink(tmp_path)
    monkeypatch.setattr(ink_front.shutil, "which", lambda _name: None)
    err = ink_front.ensure_npm_dependencies(ink)
    assert err is not None
    assert "npm not found" in err


def test_cli_ink_missing_prints_helpful_error(capsys, monkeypatch):
    from agent_augury.cli import _run_ink_surface

    monkeypatch.setattr(
        "agent_augury.cli.ensure_ink_front",
        lambda: (None, "Ink Surface front not found.\nInstall Node.js >= 22"),
    )
    rc = _run_ink_surface(mode="hello")
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Node.js >= 22" in err


def test_pythonpath_src_entry_none_without_repo(monkeypatch):
    from agent_augury import ink_front

    monkeypatch.delenv("AUGURY_PROJECT_ROOT", raising=False)
    with patch.object(ink_front, "resolve_project_root", return_value=None):
        assert ink_front.pythonpath_src_entry() is None
