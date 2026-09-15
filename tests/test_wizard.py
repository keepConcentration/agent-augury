"""Interactive setup wizard — unit tests."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

from agent_augury.config import load_config
from agent_augury.wizard import (
    NOUS_DEFAULT_BASE_URL,
    OPENAI_DEFAULT_BASE_URL,
    WizardCancelled,
    _default_bot_token_env,
    _input,
    _input_int,
    _input_required,
    check_tty,
    run_wizard,
)


def _feed(responses):
    """Drive wizard prompts; exhausted inputs → '' so optional defaults apply."""
    it = iter(responses)
    def fake(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            return ""
    return fake

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
    with patch("builtins.input", side_effect=EOFError), pytest.raises(WizardCancelled):
        _input("prompt")


def test_input_raises_on_keyboard_interrupt():
    with patch("builtins.input", side_effect=KeyboardInterrupt), pytest.raises(WizardCancelled):
        _input("prompt")


def test_input_required_reprompts_until_nonempty():
    responses = iter(["", "  ", "finally"])
    with (
        patch("builtins.input", side_effect=lambda _: next(responses)),
        patch("builtins.print"),
    ):
        assert _input_required("prompt") == "finally"


def test_input_int_returns_default_on_invalid():
    with patch("builtins.input", return_value="abc"), patch("builtins.print"):
        assert _input_int("prompt", 42) == 42


def test_input_int_returns_parsed_value():
    with patch("builtins.input", return_value=" 10 "):
        assert _input_int("prompt", 42) == 10


# ---------------------------------------------------------------------------
# run_wizard — fake backend (full flow: model settings + task)
# ---------------------------------------------------------------------------


def test_wizard_openai_backend_produces_valid_config(tmp_path):
    """Wizard with an openai backend must produce a loadable YAML config."""
    # Simulate user inputs for: max_steps, agent-1 (openai),
    # api_key_env, model, no more agents, output path.
    inputs = iter([
        "agent-1",      # agent id
        "1",            # backend choice = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o-mini",  # model (manual entry when listing fails)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config") as mock_save, \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert cfg["max_steps"] == 0
    assert cfg["protocol"]["gates"] == {
        "P2_SPLIT": "plan",
        "P3_EXECUTE": "execution",
        "P4_REVIEW": "review",
        "P5_SUBMIT": "submission",
    }
    assert "assembler_id" not in cfg["protocol"]
    assert len(cfg["agents"]) == 1
    assert cfg["agents"][0]["id"] == "agent-1"
    assert cfg["agents"][0]["backend"]["type"] == "openai"
    assert cfg["agents"][0]["backend"]["model"] == "gpt-4o-mini"

    # Model config should have been saved.
    mock_save.assert_called_once()
    call_args = mock_save.call_args
    assert call_args[0][0] == 0      # max_steps
    assert len(call_args[0][1]) == 1  # agents

    # Must be loadable by the real config loader.
    cfg_path = tmp_path / "check.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    loaded = load_config(cfg_path)
    assert loaded["max_steps"] == 0


# ---------------------------------------------------------------------------
# run_wizard — openai backend
# ---------------------------------------------------------------------------


def test_wizard_openai_backend_uses_default_base_url(tmp_path):
    inputs = iter([
        "agent-1",      # agent id
        "1",            # backend choice = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o-mini",  # model (manual entry when listing fails)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
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
        "agent-1",      # agent id
        "3",            # backend choice = nous
        "",             # base_url → default
        "NOUS_API_KEY", # api_key_env
        "Hermes-4",     # model (manual entry when listing fails)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_nous_portal", return_value=None):
        cfg = run_wizard()

    backend = cfg["agents"][0]["backend"]
    assert backend["type"] == "nous"
    assert backend["model"] == "Hermes-4"
    assert backend["base_url"] == NOUS_DEFAULT_BASE_URL
    assert backend["api_key_env"] == "NOUS_API_KEY"


def test_wizard_openrouter_emits_openai_with_defaults(tmp_path):
    """OpenRouter wizard preset: no URL/env prompts; stores openai + OpenRouter defaults."""
    from agent_augury.wizard import (
        OPENROUTER_DEFAULT_API_KEY_ENV,
        OPENROUTER_DEFAULT_BASE_URL,
    )

    inputs = iter([
        "agent-1",
        "2",            # openrouter
        "anthropic/claude-sonnet-4",  # model (listing mocked None → manual)
        "n",
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_openrouter_models", return_value=None):
        cfg = run_wizard()

    backend = cfg["agents"][0]["backend"]
    assert backend["type"] == "openai"
    assert backend["base_url"] == OPENROUTER_DEFAULT_BASE_URL
    assert backend["api_key_env"] == OPENROUTER_DEFAULT_API_KEY_ENV
    assert backend["model"] == "anthropic/claude-sonnet-4"


def test_wizard_openrouter_lists_models_without_env_key(tmp_path):
    """OpenRouter /models works without OPENROUTER_API_KEY; user can pick from list."""
    from agent_augury.model_listing import ModelInfo

    inputs = iter([
        "agent-1",
        "2",            # openrouter
        "1",            # pick first listed model
        "n",
    ])
    infos = [
        ModelInfo(id="anthropic/claude-sonnet-4", prompt_per_token=3e-6, completion_per_token=1.5e-5),
        ModelInfo(id="openai/gpt-4o-mini", prompt_per_token=0.0, completion_per_token=0.0),
    ]
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch(
             "agent_augury.backends_factory.list_openrouter_models",
             return_value=infos,
         ) as mock_list:
        cfg = run_wizard()

    mock_list.assert_called()
    assert cfg["agents"][0]["backend"]["model"] == "anthropic/claude-sonnet-4"



# ---------------------------------------------------------------------------
# run_wizard — multi-agent
# ---------------------------------------------------------------------------


def test_wizard_multiple_agents(tmp_path):
    inputs = iter([
        "agent-1",      # agent-1 id
        "1",            # openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o-mini",  # model
        "y",            # add another
        "agent-2",      # agent-2 id
        "1",            # openai
        "",             # base_url → default (from existing)
        "y",            # reuse env var
        "gpt-4o-mini",  # model
        "n",            # no more
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
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
            return "agent-1"
        raise EOFError()

    with patch("builtins.input", side_effect=side_effect), pytest.raises(WizardCancelled):
        run_wizard()


# ---------------------------------------------------------------------------
# run_wizard — reuse existing model config (skip to task)
# ---------------------------------------------------------------------------


def test_wizard_reuses_existing_model_config_skips_model_settings(tmp_path):
    """When existing_model_config is provided, model settings are reused."""
    existing = {
        "max_steps": 50,
        "agents": [
            {"id": "a1", "backend": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"}},
        ],
    }
    inputs = iter([])  # No inputs needed — model settings are reused.
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config") as mock_save:
        cfg = run_wizard(existing_model_config=existing)

    # Model settings come from existing config.
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


def test_cli_with_config_still_works(tmp_path, monkeypatch):
    """--config launches Ink session (Surface owns the TTY)."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agent_augury.cli import main

    cfg = {
        "max_steps": 5,
        "task": "smoke test",
        "agents": [
            {"id": "a1", "backend": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"}},
        ],
    }
    cfg_path = tmp_path / "test.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    with patch("agent_augury.cli._run_ink_surface", return_value=0) as ink:
        rc = main(["--config", str(cfg_path)])
    assert rc == 0
    ink.assert_called_once()
    assert ink.call_args.kwargs["mode"] == "session"
    assert ink.call_args.kwargs["config"] == str(cfg_path)


def test_cli_output_without_config_errors(tmp_path, capsys):
    """--output WITH --config must be rejected (output is wizard-only)."""
    from agent_augury.cli import main

    rc = main(["--config", "x.yaml", "--output", str(tmp_path / "x.yaml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--output" in err


def test_cli_wizard_generates_valid_yaml(tmp_path, monkeypatch):
    """End-to-end: wizard output must be loadable by load_config."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from agent_augury.cli import main

    output = tmp_path / "wizard_out.yaml"
    inputs = iter([
        "a1", "1", "", "OPENAI_API_KEY", "gpt-4o-mini",  # agent-1 (openai)
        "n",                 # no more agents
    ])
    # Patch check_tty in the module that imported it (cli), not the origin.
    # Also ensure no existing model config is loaded.
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.cli.model_config_exists", return_value=False), \
         patch("agent_augury.cli._run_ink_surface", return_value=0):
        rc = main(["--output", str(output)])

    assert rc == 0
    assert output.exists()

    # Verify the generated YAML is valid.
    loaded = load_config(output)
    assert loaded["agents"][0]["backend"]["type"] == "openai"
    assert loaded["agents"][0]["backend"]["model"] == "gpt-4o-mini"


def test_cli_wizard_reuses_model_config_skips_save_prompt(tmp_path, monkeypatch):
    """When model config exists, 'Save config to' prompt is skipped."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import os

    from agent_augury import cli
    from agent_augury.cli import main

    # _DEFAULT_OUTPUT_PATH is a module-level constant (evaluated at import time),
    # so patch it directly rather than monkeypatching Path.home().
    monkeypatch.setattr(
        cli, "_DEFAULT_OUTPUT_PATH", tmp_path / ".agent-augury" / "agent-augury-session.yaml"
    )

    # Use tmp_path as working directory so the default output path lands there.
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        # Only task input needed — model settings reused, no save prompt.
        existing = {
            "mode": "L3",
            "max_steps": 10,
            "agents": [
                {"id": "a1", "backend": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"}},
            ],
        }
        with patch("agent_augury.cli.check_tty", return_value=True), \
             patch("agent_augury.cli.model_config_exists", return_value=True), \
             patch("agent_augury.cli.load_model_config", return_value=existing), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("builtins.input", side_effect=_feed([])), \
             patch("agent_augury.cli._run_ink_surface", return_value=0):
            rc = main([])

        assert rc == 0
        output = tmp_path / ".agent-augury" / "agent-augury-session.yaml"
        assert output.exists()

        loaded = load_config(output)
        assert loaded["agents"][0]["backend"]["type"] == "openai"
        assert loaded["agents"][0]["backend"]["model"] == "gpt-4o-mini"
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
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
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
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
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
        "agent-1",      # agent-1 id
        "4",            # backend = nous_oauth
        "Hermes-4",     # model for agent-1 (manual entry)
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "4",            # backend = nous_oauth (same provider)
        "Hermes-4",     # model for agent-2 (manual entry)
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
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
        return {"id": f"agent-{agent_index + 1}", "backend": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"}}

    def fake_input(prompt, default=None):
        nonlocal another_count
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
        return {"id": f"agent-{agent_index + 1}", "backend": {"type": "openai", "base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4o-mini"}}

    def fake_input(prompt, default=None):
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
        "agent-1",      # agent-1 id
        "1",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "1",            # backend = openai (same provider)
        "",             # base_url → default (from existing)
        "y",            # reuse env var
        "gpt-4o-mini",  # model for agent-2
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
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
        "agent-1",      # agent-1 id
        "1",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "1",            # backend = openai (same provider)
        "",             # base_url → default (from existing)
        "n",            # DON'T reuse env var
        "OTHER_API_KEY",  # new env var
        "gpt-4o-mini",  # model for agent-2
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["api_key_env"] == "OPENAI_API_KEY"
    assert cfg["agents"][1]["backend"]["api_key_env"] == "OTHER_API_KEY"


def test_wizard_different_provider_triggers_new_auth():
    """Agent 2 with different provider (openrouter after openai) uses OpenRouter defaults."""
    inputs = iter([
        "agent-1",      # agent-1 id
        "1",            # backend = openai
        "",             # base_url → default
        "OPENAI_API_KEY",  # api_key_env
        "gpt-4o",       # model for agent-1
        "y",            # add another agent
        "agent-2",      # agent-2 id
        "2",            # backend = openrouter (no URL/env prompts)
        "anthropic/claude-sonnet-4",
        "n",            # no more agents
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None), \
         patch("agent_augury.backends_factory.list_openrouter_models", return_value=None):
        cfg = run_wizard()

    assert len(cfg["agents"]) == 2
    assert cfg["agents"][0]["backend"]["type"] == "openai"
    assert cfg["agents"][0]["backend"]["api_key_env"] == "OPENAI_API_KEY"
    assert cfg["agents"][1]["backend"]["type"] == "openai"
    assert "openrouter.ai" in cfg["agents"][1]["backend"]["base_url"]
    assert cfg["agents"][1]["backend"]["api_key_env"] == "OPENROUTER_API_KEY"


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


def test_find_existing_provider_config_distinguishes_openrouter():
    """OpenRouter (openai+openrouter URL) must not reuse plain OpenAI creds."""
    from agent_augury.wizard import _find_existing_provider_config
    agents = [
        {
            "id": "a1",
            "backend": {
                "type": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            },
        },
        {
            "id": "a2",
            "backend": {
                "type": "openai",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
    ]
    assert _find_existing_provider_config(agents, "openai")["api_key_env"] == "OPENAI_API_KEY"
    assert (
        _find_existing_provider_config(agents, "openrouter")["api_key_env"]
        == "OPENROUTER_API_KEY"
    )


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


def test_wizard_rejects_duplicate_agent_ids(tmp_path):
    """Second agent cannot reuse the first agent's id (case-insensitive)."""
    inputs = iter([
        "agent-1",       # agent 1 id
        "1",             # openai
        "",              # base_url default
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        "y",             # add another
        "Agent-1",       # duplicate (different case) → re-prompt
        "agent-2",       # unique
        "1",
        "",
        "y",             # reuse api key env
        "gpt-4o-mini",
        "n",
    ])
    with (
        patch("builtins.input", side_effect=_feed(inputs)),
        patch("builtins.print"),
        patch("agent_augury.wizard.save_model_config"),
        patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None),
    ):
        cfg = run_wizard()

    assert [a["id"] for a in cfg["agents"]] == ["agent-1", "agent-2"]


def test_load_config_rejects_duplicate_agent_ids(tmp_path):
    from agent_augury.config import ConfigError, load_config

    path = tmp_path / "dup.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "id": "agent-1",
                        "backend": {
                            "type": "openai",
                            "base_url": "http://x",
                            "api_key_env": "K",
                            "model": "m",
                        },
                    },
                    {
                        "id": "Agent-1",
                        "backend": {
                            "type": "openai",
                            "base_url": "http://x",
                            "api_key_env": "K",
                            "model": "m",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicated"):
        load_config(path)


# ---------------------------------------------------------------------------
# Messaging apps (Discord)
# ---------------------------------------------------------------------------


def test_default_bot_token_env():
    assert _default_bot_token_env("agent-1") == "BOT_TOKEN_AGENT_1"
    assert _default_bot_token_env("coder") == "BOT_TOKEN_CODER"


def test_wizard_skips_messaging_by_default(tmp_path):
    inputs = iter([
        "agent-1",
        "1",
        "",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        "n",  # no more agents
        # messaging: empty → default n
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()
    assert "bots" not in cfg


def test_wizard_discord_bots_per_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN_AGENT_1", raising=False)
    monkeypatch.delenv("BOT_TOKEN_CUSTOM", raising=False)
    inputs = iter([
        "agent-1",
        "1",
        "",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        "y",            # another agent
        "agent-2",
        "1",
        "",
        "y",            # reuse api key
        "gpt-4o-mini",
        "n",            # no more agents
        "y",            # add messaging app
        "1",            # Discord
        "y",            # connect agent-1
        "",             # default token env
        "111111111111111111",
        "y",            # inbound
        "y",            # connect agent-2
        "BOT_TOKEN_CUSTOM",
        "222222222222222222",
        "n",            # no inbound
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.wizard.getpass.getpass", return_value=""), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert "bots" in cfg
    assert len(cfg["bots"]) == 2
    assert cfg["bots"][0] == {
        "agent_id": "agent-1",
        "token_env": "BOT_TOKEN_AGENT_1",
        "channel_id": 111111111111111111,
        "inbound": True,
    }
    assert cfg["bots"][1] == {
        "agent_id": "agent-2",
        "token_env": "BOT_TOKEN_CUSTOM",
        "channel_id": 222222222222222222,
    }

    path = tmp_path / "with_bots.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    loaded = load_config(path)
    assert loaded["bots"][0]["inbound"] is True
    assert loaded["bots"][1]["channel_id"] == 222222222222222222


def test_wizard_rejects_pasted_bot_token_as_env_name(monkeypatch):
    from tests.token_fakes import fake_discord_bot_token

    monkeypatch.delenv("BOT_TOKEN_SOLO", raising=False)
    pasted = fake_discord_bot_token()
    inputs = iter([
        "solo",
        "1",
        "",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        "n",
        "y",
        "1",
        "y",
        pasted,
        "",
        "555555555555555555",
        "n",
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.wizard.getpass.getpass", return_value=""), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()
    assert cfg["bots"][0]["token_env"] == "BOT_TOKEN_SOLO"


def test_wizard_stores_discord_bot_token_in_dotenv(tmp_path, monkeypatch):
    from tests.token_fakes import fake_discord_bot_token

    secrets = tmp_path / ".env"
    token = fake_discord_bot_token(prefix="st")
    monkeypatch.delenv("BOT_TOKEN_SOLO", raising=False)
    inputs = iter([
        "solo",
        "1",
        "",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
        "n",
        "y",
        "1",
        "y",
        "",
        "555555555555555555",
        "n",
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config"), \
         patch("agent_augury.wizard.getpass.getpass", return_value=token), \
         patch("agent_augury.wizard.DEFAULT_SECRETS_ENV_PATH", secrets), \
         patch("agent_augury.backends_factory.list_models_openai_compat", return_value=None):
        cfg = run_wizard()

    assert cfg["bots"][0]["token_env"] == "BOT_TOKEN_SOLO"
    assert token not in yaml.safe_dump(cfg)
    assert secrets.exists()
    assert "BOT_TOKEN_SOLO=" in secrets.read_text(encoding="utf-8")
    assert os.environ.get("BOT_TOKEN_SOLO") == token


def test_wizard_messaging_reuse_model_config_still_asks(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN_A1", raising=False)
    existing = {
        "max_steps": 10,
        "agents": [
            {
                "id": "a1",
                "backend": {
                    "type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key_env": "OPENAI_API_KEY",
                    "model": "gpt-4o-mini",
                },
            },
        ],
    }
    inputs = iter([
        "y",  # add messaging
        "1",  # Discord
        "y",  # connect a1
        "BOT_TOKEN_A1",
        "999",
        "y",  # inbound
    ])
    with patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard.save_model_config") as mock_save, \
         patch("agent_augury.wizard.getpass.getpass", return_value=""):
        cfg = run_wizard(existing_model_config=existing)

    mock_save.assert_not_called()
    assert cfg["bots"][0]["agent_id"] == "a1"
    assert cfg["bots"][0]["token_env"] == "BOT_TOKEN_A1"
    assert cfg["bots"][0]["inbound"] is True