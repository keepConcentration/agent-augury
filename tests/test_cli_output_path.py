"""CLI save-path validation tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agent_augury.cli import (
    _DEFAULT_OUTPUT_PATH,
    _output_path_problem,
    _prompt_output_path,
    _resolve_output_path,
)


def test_output_path_problem_empty():
    assert _output_path_problem("") == "empty"


def test_output_path_problem_whitespace_only():
    assert _output_path_problem("   ") == "whitespace only"


def test_output_path_problem_hangul_filler():
    assert _output_path_problem("\u3164") == "invisible character U+3164"


def test_output_path_problem_windows_invalid_char():
    assert _output_path_problem("bad|name.yaml") == "invalid path character '|'"


def test_output_path_problem_none_for_valid():
    assert _output_path_problem("session.yaml") is None


def test_resolve_output_path_blank_uses_default():
    assert _resolve_output_path("") == _DEFAULT_OUTPUT_PATH
    assert _resolve_output_path("   ") == _DEFAULT_OUTPUT_PATH


def test_resolve_output_path_invalid_falls_back_to_default():
    with patch("builtins.print"):
        assert _resolve_output_path("\u3164") == _DEFAULT_OUTPUT_PATH


def test_resolve_output_path_valid():
    assert _resolve_output_path("out/session.yaml") == Path("out/session.yaml")


def test_prompt_output_path_enter_uses_default():
    with patch("builtins.input", return_value=""):
        assert _prompt_output_path(Path("default.yaml")) == Path("default.yaml")


def test_prompt_output_path_valid_input():
    with patch("builtins.input", return_value="custom.yaml"):
        assert _prompt_output_path(Path("default.yaml")) == Path("custom.yaml")


def test_prompt_output_path_reprompts_then_accepts():
    responses = iter(["\u3164", "good.yaml"])
    with patch("builtins.input", side_effect=lambda _: next(responses)), patch("builtins.print"):
        assert _prompt_output_path(Path("default.yaml")) == Path("good.yaml")
