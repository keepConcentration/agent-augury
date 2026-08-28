"""Interactive setup wizard — unit tests."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_augury.config import load_config
from agent_augury.wizard import (
    NOUS_DEFAULT_BASE_URL,
    OPENAI_DEFAULT_BASE_URL,
    WizardCancelled,
    _input,
    _input_int,
    _input_required,
    check_tty,
    run_wizard,
)


# ---------------------------------------------------------------------------
# TTY detection
# ---------------------------------------------------------------------------


def test_check_tty_returns_false_when_stdin_not_tty():
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        assert check_tty() is False


def test_check_tty_returns_false_when_stdout_not_tty():
    with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
        mock_stdin.isatty.return_value = True
        mock_stdout.isatty.return_value = False
        assert check_tty() is False


def test_check_tty_returns_true_when_both_tty():
    with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
        mock_stdin.isatty.return_value = True
        mock_stdout.isatty.return_value = True
        assert check_tty() is True


# ---------------------------------------------------------------------------
# _input helpers
# ---------------------------------------------------------------------------


def test_input_returns_default_on_empty():
    with patch("builtins.input", return_value=""):
        assert _input("prompt", "default_val") == "default_val"


def test_input_returns_user_value():
    with patch("builtins.input", return_value="  hello  "):
        assert _input("prompt", "default_val") == "hello"


def test_input_raises_on_eof():
    with patch("builtins.input", side_effect=EOFError):
        with pytest.raises(WizardCancelled):
            _input("prompt")


def test_input_raises_on_keyboard_interrupt():
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with pytest.raises(WizardCancelled):
            _input("prompt")


def test_input_required_reprompts_until_nonempty():
    responses = iter(["", "  ", "finally"])
    with patch("builtins.input", side_effect=lambda _: next(responses)):
        with patch("builtins.print"):
            assert _input_required("prompt") == "finally"


def test_input_int_returns_default_on_invalid():
    with patch("builtins.input", return_value="abc"):
        with patch("builtins.print"):
            assert _input_int("prompt", 42) == 42


def test_input_int_returns_parsed_value():
    with patch("builtins.input", return_value=" 10 "):
        assert _input_int("prompt", 42) == 10


# ---------------------------------------------------------------------------
# run_wizard — fake backend
# ---------------------------------------------------------------------------


def test_wizard_fake_backend_produces_valid_config(tmp_path):
    """Wizard with a fake backend must produce a loadable YAML config."""
    # Simulate user inputs for: mode, task, max_steps, agent-1 (fake),
    # script entries, no more agents, output path, don't run.
    inputs = iter([
        "L3",           # mode
        "test task",    # task
        "15",           # max_steps
        "agent-1",      # agent id
        "1",            # backend choice = fake
        "hello",        # script entry 1
        "world",        # script entry 2
        "",             # empty → finish script
        "n",            # no more agents
        str(tmp_path / "out.yaml"),  # output path
        "n",            # don't run now
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        cfg = run_wizard()

    assert cfg["mode"] == "L3"
    assert cfg["task"] == "test task"
    assert cfg["max_steps"] == 15
    assert len(cfg["agents"]) == 1
    assert cfg["agents"][0]["id"] == "agent-1"
    assert cfg["agents"][0]["backend"]["type"] == "fake"
    assert cfg["agents"][0]["backend"]["script"] == ["hello", "world"]

    # Must be loadable by the real config loader.
    cfg_path = tmp_path / "check.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    loaded = load_config(cfg_path)
    assert loaded["mode"] == "L3"


# ---------------------------------------------------------------------------
# run_wizard — openai backend
# ---------------------------------------------------------------------------


def test_wizard_openai_backend_uses_default_base_url(tmp_path):
    inputs = iter([
        "L3",           # mode
        "task",         # task
        "20",           # max_steps
        "agent-1",      # agent id
        "2",            # backend choice = openai
        "gpt-4o-mini",  # model
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "n",            # no more agents
        str(tmp_path / "out.yaml"),
        "n",
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        cfg = run_wizard()

    backend = cfg["agents"][0]["backend"]
    assert backend["type"] == "openai"
    assert backend["model"] == "gpt-4o-mini"
    assert backend["base_url"] == OPENAI_DEFAULT_BASE_URL
    assert backend["api_key_env"] == "OPENAI_API_KEY"

    # No api_key (literal secret) anywhere in the config.
    raw = yaml.safe_dump(cfg)
    assert "sk-" not in raw
    assert "api_key:" not in raw


def test_wizard_nous_backend_uses_default_base_url(tmp_path):
    inputs = iter([
        "L3",           # mode
        "task",         # task
        "20",           # max_steps
        "agent-1",      # agent id
        "3",            # backend choice = nous
        "Hermes-4",     # model
        "",             # base_url → default
        "NOUS_API_KEY", # api_key_env
        "n",            # no more agents
        str(tmp_path / "out.yaml"),
        "n",
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        cfg = run_wizard()

    backend = cfg["agents"][0]["backend"]
    assert backend["type"] == "nous"
    assert backend["model"] == "Hermes-4"
    assert backend["base_url"] == NOUS_DEFAULT_BASE_URL
    assert backend["api_key_env"] == "NOUS_API_KEY"


# ---------------------------------------------------------------------------
# run_wizard — multi-agent
# ---------------------------------------------------------------------------


def test_wizard_multiple_agents(tmp_path):
    inputs = iter([
        "L3",           # mode
        "collab task",  # task
        "30",           # max_steps
        "agent-1",      # agent-1 id
        "1",            # fake
        "step1", "",    # script
        "y",            # add another
        "agent-2",      # agent-2 id
        "1",            # fake
        "step2", "",    # script
        "n",            # no more
        str(tmp_path / "out.yaml"),
        "n",
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["id"] == "agent-1"
    assert cfg["agents"][1]["id"] == "agent-2"


# ---------------------------------------------------------------------------
# run_wizard — cancellation
# ---------------------------------------------------------------------------


def test_wizard_cancels_on_eof():
    """EOFError from input() must surface as WizardCancelled."""
    call_count = [0]

    def side_effect(_prompt):
        call_count[0] += 1
        if call_count[0] == 1:
            return "L3"
        raise EOFError()

    with patch("builtins.input", side_effect=side_effect):
        with pytest.raises(WizardCancelled):
            run_wizard()


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_without_config_non_tty_returns_error(capsys):
    """Without --config and no TTY, CLI must error clearly."""
    from agent_augury.cli import main

    with patch("agent_augury.cli.check_tty", return_value=False):
        rc = main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "TTY" in err or "tty" in err


def test_cli_with_config_still_works(tmp_path, capsys):
    """Existing --config mode must be unaffected by wizard changes."""
    from agent_augury.cli import main

    cfg = {
        "mode": "L3",
        "max_steps": 5,
        "task": "smoke test",
        "agents": [
            {"id": "a1", "backend": {"type": "fake", "script": ["done"]}},
        ],
    }
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    rc = main(["--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[a1]" in out


def test_cli_output_without_config_errors(tmp_path, capsys):
    """--output WITH --config must be rejected (output is wizard-only)."""
    from agent_augury.cli import main

    rc = main(["--config", "x.yaml", "--output", str(tmp_path / "x.yaml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--output" in err


def test_cli_wizard_generates_valid_yaml(tmp_path):
    """End-to-end: wizard output must be loadable by load_config."""
    from agent_augury.cli import main

    output = tmp_path / "wizard_out.yaml"
    inputs = iter([
        "L3", "e2e task", "10",
        "a1", "1", "hello", "",
        "n",
        str(output),
        "n",
    ])
    # Patch check_tty in the module that imported it (cli), not the origin.
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.cli.check_tty", return_value=True):
        rc = main([])

    assert rc == 0
    assert output.exists()

    # Verify the generated YAML is valid.
    loaded = load_config(output)
    assert loaded["mode"] == "L3"
    assert loaded["task"] == "e2e task"
    assert loaded["agents"][0]["backend"]["script"] == ["hello"]


# ---------------------------------------------------------------------------
# Windows AttachConsole scenarios
# ---------------------------------------------------------------------------


def test_try_attach_parent_console_non_windows():
    """On non-Windows, _try_attach_parent_console returns False immediately."""
    with patch("sys.platform", "linux"):
        from agent_augury.wizard import _try_attach_parent_console
        assert _try_attach_parent_console() is False


def test_try_attach_parent_console_success():
    """On Windows, AttachConsole succeeds and stdin/stdout become TTYs."""
    mock_kernel = MagicMock()
    mock_kernel.AttachConsole.return_value = 1  # success

    with patch("sys.platform", "win32"), \
         patch("agent_augury.wizard._get_kernel32", return_value=mock_kernel), \
         patch("builtins.open", MagicMock(return_value=MagicMock(isatty=MagicMock(return_value=True)))):
        from agent_augury.wizard import _try_attach_parent_console
        assert _try_attach_parent_console() is True
        mock_kernel.AttachConsole.assert_called_once_with(-1)


def test_try_attach_parent_console_kernel_fails():
    """When AttachConsole returns 0 (failure), return False."""
    mock_kernel = MagicMock()
    mock_kernel.AttachConsole.return_value = 0  # failure

    with patch("sys.platform", "win32"), \
         patch("agent_augury.wizard._get_kernel32", return_value=mock_kernel):
        from agent_augury.wizard import _try_attach_parent_console
        assert _try_attach_parent_console() is False


def test_try_attach_parent_console_exception():
    """If any exception occurs, return False gracefully."""
    with patch("sys.platform", "win32"), \
         patch("agent_augury.wizard._get_kernel32", side_effect=OSError("no console")):
        from agent_augury.wizard import _try_attach_parent_console
        assert _try_attach_parent_console() is False


def test_check_tty_falls_back_to_attach_console():
    """When isatty() returns False, check_tty tries AttachConsole on Windows."""
    with patch("sys.platform", "win32"), \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patch("sys.stdout", MagicMock(isatty=MagicMock(return_value=False))), \
         patch("agent_augury.wizard._try_attach_parent_console", return_value=True) as mock_attach:
        from agent_augury.wizard import check_tty
        assert check_tty() is True
        mock_attach.assert_called_once()


def test_check_tty_no_attach_on_non_windows():
    """On non-Windows, check_tty does not try AttachConsole."""
    with patch("sys.platform", "linux"), \
         patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=False))), \
         patch("sys.stdout", MagicMock(isatty=MagicMock(return_value=False))):
        from agent_augury.wizard import check_tty
        assert check_tty() is False
