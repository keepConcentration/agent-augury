"""Shared pytest fixtures and opt-in integration markers."""

from __future__ import annotations

import os

import pytest

OPENAI_TESTS_ENABLED = os.environ.get("AUGURY_RUN_OPENAI_TESTS") == "1"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

requires_openai = pytest.mark.skipif(
    not (OPENAI_TESTS_ENABLED and OPENAI_API_KEY),
    reason=(
        "OpenAI integration tests are opt-in: set AUGURY_RUN_OPENAI_TESTS=1 "
        "and OPENAI_API_KEY to run (incurs API cost)."
    ),
)


def build_cfg(**overrides):
    """Build a valid config dict for Session.from_config, bypassing load_config.

    Use this for tests that need fake backends (which are not valid in
    production configs after the fake-backend removal from _VALID_BACKEND_TYPES).
    """
    cfg: dict = {
        "mode": "L3",
        "max_steps": 20,
        "task": "test",
        "agents": [],
    }
    cfg.update(overrides)
    return cfg
