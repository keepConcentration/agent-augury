"""Model config persistence — unit tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_augury.model_config import (
    DEFAULT_MODEL_CONFIG_PATH,
    clear_model_config,
    load_model_config,
    model_config_exists,
    save_model_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


SAMPLE_AGENTS = [
    {
        "id": "a1",
        "backend": {
            "type": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
    },
]


# ---------------------------------------------------------------------------
# save_model_config
# ---------------------------------------------------------------------------


def test_save_model_config_writes_json(tmp_path):
    """save_model_config writes a JSON file with mode, max_steps, agents."""
    path = tmp_path / "config.json"
    result = save_model_config("L3", 25, SAMPLE_AGENTS, path=path)

    assert result == path
    assert path.exists()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["mode"] == "L3"
    assert raw["max_steps"] == 25
    assert raw["agents"] == SAMPLE_AGENTS


def test_save_model_config_creates_parent_dir(tmp_path):
    """save_model_config creates missing parent directories."""
    path = tmp_path / "nested" / "dir" / "config.json"
    save_model_config("L3", 20, SAMPLE_AGENTS, path=path)

    assert path.exists()


def test_save_model_config_defaults_to_home_dir():
    """save_model_config uses DEFAULT_MODEL_CONFIG_PATH when no path given."""
    with patch(
        "agent_augury.model_config.DEFAULT_MODEL_CONFIG_PATH",
        Path("/tmp/test_augury_config.json"),
    ):
        result = save_model_config("L3", 20, SAMPLE_AGENTS)
        assert result == Path("/tmp/test_augury_config.json")


def test_save_model_config_unicode_agent_id(tmp_path):
    """Unicode agent IDs are preserved as-is (ensure_ascii=False)."""
    agents = [{"id": "에이전트-1", "backend": {"type": "fake", "script": ["hi"]}}]
    path = tmp_path / "config.json"
    save_model_config("L3", 20, agents, path=path)

    raw = path.read_text(encoding="utf-8")
    assert "에이전트-1" in raw  # not escaped as \uc5d0\uc774\uc804\ud2b8


# ---------------------------------------------------------------------------
# load_model_config
# ---------------------------------------------------------------------------


def test_load_model_config_reads_saved_data(tmp_path):
    """load_model_config reads back what save_model_config wrote."""
    path = tmp_path / "config.json"
    save_model_config("L3", 30, SAMPLE_AGENTS, path=path)

    result = load_model_config(path=path)
    assert result is not None
    assert result["mode"] == "L3"
    assert result["max_steps"] == 30
    assert result["agents"] == SAMPLE_AGENTS


def test_load_model_config_returns_none_when_missing(tmp_path):
    """load_model_config returns None if file does not exist."""
    assert load_model_config(path=tmp_path / "nonexistent.json") is None


def test_load_model_config_returns_none_on_invalid_json(tmp_path):
    """load_model_config returns None on malformed JSON."""
    path = tmp_path / "bad.json"
    path.write_text("{invalid json", encoding="utf-8")

    assert load_model_config(path=path) is None


def test_load_model_config_returns_none_on_missing_agents_key(tmp_path):
    """load_model_config returns None when 'agents' key is missing."""
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps({"mode": "L3", "max_steps": 10}), encoding="utf-8")

    assert load_model_config(path=path) is None


def test_load_model_config_returns_none_on_non_dict(tmp_path):
    """load_model_config returns None when root is not a dict."""
    path = tmp_path / "list.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert load_model_config(path=path) is None


# ---------------------------------------------------------------------------
# model_config_exists
# ---------------------------------------------------------------------------


def test_model_config_exists_returns_true_when_present(tmp_path):
    path = tmp_path / "config.json"
    save_model_config("L3", 20, SAMPLE_AGENTS, path=path)
    assert model_config_exists(path=path) is True


def test_model_config_exists_returns_false_when_missing(tmp_path):
    assert model_config_exists(path=tmp_path / "missing.json") is False


# ---------------------------------------------------------------------------
# clear_model_config
# ---------------------------------------------------------------------------


def test_clear_model_config_removes_file(tmp_path):
    path = tmp_path / "config.json"
    save_model_config("L3", 20, SAMPLE_AGENTS, path=path)
    assert path.exists()

    result = clear_model_config(path=path)
    assert result is True
    assert not path.exists()


def test_clear_model_config_returns_false_when_missing(tmp_path):
    assert clear_model_config(path=tmp_path / "missing.json") is False
