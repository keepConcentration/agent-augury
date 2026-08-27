"""Verify consensus_openai.yaml loads and validates correctly."""

import pytest
import yaml

from agent_augury.config import load_config


def test_consensus_openai_yaml_loads():
    """The real-backend example config must pass validation."""
    cfg = load_config("examples/consensus_openai.yaml")
    assert cfg["mode"] == "L3"
    assert cfg["max_steps"] == 30
    assert cfg["gate"]["thread_name"] == "plan"
    assert len(cfg["agents"]) == 2
    assert all(a["backend"]["type"] == "openai" for a in cfg["agents"])
    assert all(a["backend"]["api_key_env"] == "OPENAI_API_KEY" for a in cfg["agents"])
    assert cfg["mirror"]["type"] == "discord_webhook"
    assert cfg["mirror"]["url_env"] == "AUGURY_MIRROR_URL"


def test_consensus_openai_yaml_has_no_hardcoded_secrets():
    """Secrets must come from env vars only — never hardcoded in YAML."""
    with open("examples/consensus_openai.yaml", encoding="utf-8") as f:
        raw = f.read()
    # No sk- patterns or similar secrets
    assert "sk-" not in raw
    # Must use api_key_env (env var name), not api_key: <literal>
    import re
    # Match "api_key:" exactly (not "api_key_env:") followed by a value
    literal_key = re.search(r"^api_key:(?!\w)\s+\S+", raw, re.MULTILINE)
    assert literal_key is None, f"found hardcoded api_key: {literal_key.group()}"
