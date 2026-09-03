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
# run_wizard — fake backend (full flow: model settings + task)
# ---------------------------------------------------------------------------


def test_wizard_fake_backend_produces_valid_config(tmp_path):
    """Wizard with a fake backend must produce a loadable YAML config."""
    # Simulate user inputs for: max_steps, agent-1 (fake),
    # script entries, no more agents, output path.
    inputs = iter([
        "15",           # max_steps
        "agent-1",      # agent id
        "1",            # backend choice = fake
        "hello",        # script entry 1
        "world",        # script entry 2
        "",             # empty → finish script
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config") as mock_save:
        cfg = run_wizard()

    assert cfg["mode"] == "L3"
    assert cfg["max_steps"] == 15
    assert len(cfg["agents"]) == 1
    assert cfg["agents"][0]["id"] == "agent-1"
    assert cfg["agents"][0]["backend"]["type"] == "fake"
    assert cfg["agents"][0]["backend"]["script"] == ["hello", "world"]

    # Model config should have been saved.
    mock_save.assert_called_once()
    call_args = mock_save.call_args
    assert call_args[0][0] == 15    # max_steps
    assert len(call_args[0][1]) == 1  # agents

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
        "20",           # max_steps
        "agent-1",      # agent id
        "2",            # backend choice = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o-mini",  # model (manual entry when listing fails)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
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
        "20",           # max_steps
        "agent-1",      # agent id
        "3",            # backend choice = nous
        "",             # base_url → default
        "NOUS_API_KEY", # api_key_env
        "Hermes-4",     # model (manual entry when listing fails)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_nous_portal", return_value=None):
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
        "30",           # max_steps
        "agent-1",      # agent-1 id
        "1",            # fake
        "step1", "",    # script
        "y",            # add another
        "agent-2",      # agent-2 id
        "1",            # fake
        "step2", "",    # script
        "n",            # no more
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"):
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
            return "15"
        raise EOFError()

    with patch("builtins.input", side_effect=side_effect):
        with pytest.raises(WizardCancelled):
            run_wizard()


# ---------------------------------------------------------------------------
# run_wizard — reuse existing model config (skip to task)
# ---------------------------------------------------------------------------


def test_wizard_reuses_existing_model_config_skips_model_settings(tmp_path):
    """When existing_model_config is provided, model settings are reused."""
    existing = {
        "mode": "L3",
        "max_steps": 50,
        "agents": [
            {"id": "a1", "backend": {"type": "fake", "script": ["done"]}},
        ],
    }
    inputs = iter([])  # No inputs needed — model settings are reused.
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config") as mock_save:
        cfg = run_wizard(existing_model_config=existing)

    # Model settings come from existing config.
    assert cfg["mode"] == "L3"
    assert cfg["max_steps"] == 50
    assert len(cfg["agents"]) == 1
    assert cfg["agents"][0]["id"] == "a1"
    # save_model_config should NOT be called (we reused existing).
    mock_save.assert_not_called()


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
    assert "💭 a1:" in out


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
        "10",                # max_steps
        "a1", "1", "hello", "",  # agent-1 (fake)
        "n",                 # no more agents
        str(output),         # output path
        "e2e task",          # initial task
    ])
    # Patch check_tty in the module that imported it (cli), not the origin.
    # Also ensure no existing model config is loaded.
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.cli.model_config_exists", return_value=False):
        rc = main([])

    assert rc == 0
    assert output.exists()

    # Verify the generated YAML is valid.
    loaded = load_config(output)
    assert loaded["mode"] == "L3"
    assert loaded["agents"][0]["backend"]["script"] == ["hello"]


def test_cli_wizard_reuses_model_config_skips_save_prompt(tmp_path):
    """When model config exists, 'Save config to' prompt is skipped."""
    from agent_augury.cli import main
    import os

    # Use tmp_path as working directory so the default output path lands there.
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Only task input needed — model settings reused, no save prompt.
        inputs = iter([
            "e2e task",          # initial task
        ])
        existing = {
            "mode": "L3",
            "max_steps": 10,
            "agents": [
                {"id": "a1", "backend": {"type": "fake", "script": ["hello"]}},
            ],
        }
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.cli.check_tty", return_value=True), \
             patch("agent_augury.cli.model_config_exists", return_value=True), \
             patch("agent_augury.cli.load_model_config", return_value=existing), \
             patch("agent_augury.wizard.save_model_config"):
            rc = main([])

        assert rc == 0
        output = tmp_path / "agent-augury-session.yaml"
        assert output.exists()

        loaded = load_config(output)
        assert loaded["mode"] == "L3"
        assert loaded["agents"][0]["backend"]["script"] == ["hello"]
    finally:
        os.chdir(old_cwd)


def test_cli_reconfigure_flag_with_config_errors(tmp_path, capsys):
    """--reconfigure WITH --config must be rejected."""
    from agent_augury.cli import main

    rc = main(["--config", "x.yaml", "--reconfigure"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--reconfigure" in err


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


# ---------------------------------------------------------------------------
# Same-provider credential reuse
# ---------------------------------------------------------------------------


def test_wizard_second_agent_reuses_oauth_no_reauthentication():
    """Second agent with nous_oauth should reuse token, not re-authenticate."""
    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_nous_oauth", return_value=None), \
         patch("agent_augury.wizard._has_valid_oauth_token", return_value=True):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"
    assert cfg["agents"][1]["backend"]["type"] == "nous_oauth"
    # Both use default base URL.
    assert cfg["agents"][0]["backend"]["base_url"] == NOUS_DEFAULT_BASE_URL
    assert cfg["agents"][1]["backend"]["base_url"] == NOUS_DEFAULT_BASE_URL


def test_wizard_second_agent_reuses_oauth_real_token_store(tmp_path):
    """Integration: real TokenStore with no-expiry token is reused by Agent 2."""
    from agent_augury.auth.token_store import TokenStore

    # Use a temp token store, simulate Agent 1 having completed OAuth
    store = TokenStore(store_path=tmp_path / "tokens.json")
    store.set_provider_tokens("nous", {
        "access_token": "test-access-token",
        "token_type": "Bearer",
        "expires_in": None,
        "expires_at": None,  # No expiry — should still be treated as valid
    })

    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_nous_oauth", return_value=None), \
         patch("agent_augury.auth.token_store.TokenStore", return_value=store):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"
    assert cfg["agents"][1]["backend"]["type"] == "nous_oauth"
    # Agent 2 should NOT have triggered a new auth (no browser open).
    # Both agents should have a model set (from stored token → model listing).
    assert cfg["agents"][0]["backend"]["model"] == "Hermes-4"
    assert cfg["agents"][1]["backend"]["model"] == "Hermes-4"


def test_wizard_second_agent_oauth_no_token_triggers_auth():
    """Second agent with nous_oauth and no token must authenticate."""
    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_nous_oauth", return_value=None), \
         patch("agent_augury.wizard._has_valid_oauth_token", return_value=False), \
         patch("agent_augury.wizard._run_nous_oauth_device_code", return_value="tok-123") as mock_auth:
        cfg = run_wizard()

    # Auth flow was called (at least once across agents; no valid token).
    mock_auth.assert_called()
    assert len(cfg["agents"]) == 2
    assert cfg["agents"][1]["backend"]["model"] == "Hermes-4"





def test_wizard_force_reconfigure_only_for_first_agent():
    """--reconfigure must only force re-auth on the first agent, not subsequent ones."""
    from agent_augury.wizard import _collect_model_settings

    call_args = []
    another_count = 0

    def fake_build_agent(agent_index, existing_agents=None, force_reconfigure=False):
        call_args.append(force_reconfigure)
        return {"id": f"agent-{agent_index + 1}", "backend": {"type": "fake"}}

    def fake_input(prompt, default=None):
        nonlocal another_count
        if "steps" in prompt:
            return "20"
        if "ID" in prompt:
            return f"agent-{len(call_args) + 1}"
        if "another" in prompt.lower():
            another_count += 1
            return "y" if another_count == 1 else "n"
        return default or ""

    with patch("agent_augury.wizard._build_agent", side_effect=fake_build_agent), \
         patch("builtins.input", side_effect=fake_input):
        _collect_model_settings(force_reconfigure=True)

    # Only the first agent should receive force_reconfigure=True
    assert len(call_args) == 2, f"Expected 2 agents, got {len(call_args)}"
    assert call_args == [True, False], f"Expected [True, False], got {call_args}"


def test_wizard_force_reconfigure_single_agent():
    """With a single agent, force_reconfigure is still honored."""
    from agent_augury.wizard import _collect_model_settings

    call_args = []

    def fake_build_agent(agent_index, existing_agents=None, force_reconfigure=False):
        call_args.append(force_reconfigure)
        return {"id": f"agent-{agent_index + 1}", "backend": {"type": "fake"}}

    def fake_input(prompt, default=None):
        if "steps" in prompt:
            return "20"
        if "ID" in prompt:
            return f"agent-{len(call_args) + 1}"
        if "another" in prompt.lower():
            return "n"
        return default or ""

    with patch("agent_augury.wizard._build_agent", side_effect=fake_build_agent), \
         patch("builtins.input", side_effect=fake_input):
        _collect_model_settings(force_reconfigure=False)

    assert call_args == [False], f"Expected [False], got {call_args}"


def test_wizard_second_agent_reuses_api_key_env_var():
    """Second agent with same API key provider should offer env var reuse."""
    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "2",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "2",            # backend = openai (same provider)
        "",             # base_url → default (from existing)
        "y",            # reuse env var
        "gpt-4o-mini",  # model for agent-2
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    # Both agents reuse the same env var.
    assert cfg["agents"][0]["backend"]["api_key_env"] == "OPENAI_API_KEY"
    assert cfg["agents"][1]["backend"]["api_key_env"] == "OPENAI_API_KEY"


def test_wizard_second_agent_chooses_different_api_key_env():
    """User can override env var reuse and enter a new one."""
    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "2",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "2",            # backend = openai (same provider)
        "",             # base_url → default (from existing)
        "n",            # DON'T reuse env var
        "OTHER_API_KEY",  # new env var
        "gpt-4o-mini",  # model for agent-2
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["api_key_env"] == "OPENAI_API_KEY"
    assert cfg["agents"][1]["backend"]["api_key_env"] == "OTHER_API_KEY"


def test_wizard_different_provider_triggers_new_auth():
    """Agent 2 with different provider (nous after openai) should ask for new credentials."""
    inputs = iter([
        "20",           # max_steps
        "agent-1",      # agent-1 id
        "2",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "3",            # backend = nous (different provider)
        "",             # base_url → default
        "NOUS_API_KEY", # new api_key_env
        "Hermes-4",     # model for agent-2
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=lambda _: next(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None), \
         patch("agent_augury.backends_factory.list_models_nous_portal", return_value=None):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["type"] == "openai"
    assert cfg["agents"][0]["backend"]["api_key_env"] == "OPENAI_API_KEY"
    assert cfg["agents"][1]["backend"]["type"] == "nous"
    assert cfg["agents"][1]["backend"]["api_key_env"] == "NOUS_API_KEY"


def test_find_existing_provider_config_returns_latest_match():
    """_find_existing_provider_config returns the most recent matching agent."""
    from agent_augury.wizard import _find_existing_provider_config
    agents = [
        {"id": "a1", "backend": {"type": "openai", "api_key_env": "FIRST"}},
        {"id": "a2", "backend": {"type": "fake"}},
        {"id": "a3", "backend": {"type": "openai", "api_key_env": "SECOND"}},
    ]
    result = _find_existing_provider_config(agents, "openai")
    assert result["api_key_env"] == "SECOND"


def test_find_existing_provider_config_no_match():
    """_find_existing_provider_config returns None when no match."""
    from agent_augury.wizard import _find_existing_provider_config
    agents = [
        {"id": "a1", "backend": {"type": "fake"}},
    ]
    assert _find_existing_provider_config(agents, "openai") is None


def test_find_existing_provider_config_empty_list():
    """_find_existing_provider_config with empty list returns None."""
    from agent_augury.wizard import _find_existing_provider_config
    assert _find_existing_provider_config([], "openai") is None
