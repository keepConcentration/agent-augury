"""Shared pytest fixtures and opt-in integration markers."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

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
        "max_steps": 20,
        "task": "test",
        "agents": [],
    }
    cfg.update(overrides)
    return cfg


def popen_python_module(
    module_args: Sequence[str],
    *,
    env: Mapping[str, str],
) -> subprocess.Popen[str]:
    """Spawn ``sys.executable -m …`` preferring posix_spawn over fork+exec.

    After discord.py / aiohttp have been imported, macOS ``fork`` in
    ``subprocess.Popen`` can SIGSEGV the child (empty stdout → flaky JSONL
    tests). CPython only takes the posix_spawn path when ``close_fds`` is
    False and ``cwd`` is None — pass absolute paths in *module_args* instead.
    """
    return subprocess.Popen(
        [sys.executable, *module_args],
        cwd=None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=dict(env),
        close_fds=False,
    )