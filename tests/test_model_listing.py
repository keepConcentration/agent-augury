"""Model listing — backend / factory / wizard tests."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.backend.openai_compat import OpenAICompatBackend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _models_response(ids: list[str]) -> dict:
    return {"data": [{"id": m, "object": "model"} for m in ids]}


def make_backend(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatBackend(
        base_url="http://fake.local/v1", api_key="secret-key", model="test-model", client=client
    )


# ---------------------------------------------------------------------------
# model_listing.sort_model_ids
# ---------------------------------------------------------------------------


def test_sort_model_ids_provider_groups():
    from agent_augury.model_listing import sort_model_ids

    models = [
        "openai/gpt-4",
        "anthropic/claude-3",
        "anthropic/claude-2",
        "openai/gpt-3.5",
    ]
    assert sort_model_ids(models) == [
        "anthropic/claude-2",
        "anthropic/claude-3",
        "openai/gpt-3.5",
        "openai/gpt-4",
    ]


def test_sort_model_ids_case_insensitive():
    from agent_augury.model_listing import sort_model_ids

    assert sort_model_ids(["Zeta", "alpha", "Beta"]) == ["alpha", "Beta", "Zeta"]


def test_sort_model_ids_stable_duplicates():
    from agent_augury.model_listing import sort_model_ids

    assert sort_model_ids(["dup", "other", "dup"]) == ["dup", "dup", "other"]


# ---------------------------------------------------------------------------
# OpenAICompatBackend.list_models
# ---------------------------------------------------------------------------


async def test_list_models_returns_sorted_ids():
    backend = make_backend(
        lambda req: httpx.Response(200, json=_models_response(["gpt-4o-mini", "gpt-4o"]))
    )
    result = await backend.list_models()
    assert result == ["gpt-4o", "gpt-4o-mini"]


async def test_list_models_returns_none_on_http_error():
    backend = make_backend(lambda req: httpx.Response(401, json={"error": "bad key"}))
    assert await backend.list_models() is None


async def test_list_models_returns_none_on_network_error():
    def handler(req):
        raise httpx.ConnectError("connection refused")

    backend = make_backend(handler)
    assert await backend.list_models() is None


async def test_list_models_returns_empty_list_when_no_data():
    backend = make_backend(lambda req: httpx.Response(200, json={"data": []}))
    assert await backend.list_models() == []


async def test_list_models_returns_none_on_non_json():
    backend = make_backend(lambda req: httpx.Response(200, text="not json"))
    assert await backend.list_models() is None


async def test_list_models_ignores_entries_without_id():
    backend = make_backend(
        lambda req: httpx.Response(200, json={"data": [{"id": "ok"}, {"no_id": "x"}]})
    )
    assert await backend.list_models() == ["ok"]


# ---------------------------------------------------------------------------
# FakeModelBackend.list_models (base default)
# ---------------------------------------------------------------------------


async def test_fake_backend_list_models_returns_none():
    fake = FakeModelBackend([Completion(text="x")])
    assert await fake.list_models() is None


async def test_base_backend_list_models_returns_none():
    """Base ModelBackend.list_models() default returns None."""

    class Dummy(ModelBackend):
        async def complete(self, messages, tools):
            return Completion(text="x")

    d = Dummy()
    assert await d.list_models() is None


# ---------------------------------------------------------------------------
# backends_factory helpers
# ---------------------------------------------------------------------------


def test_list_models_openai_compat_success():
    from unittest.mock import patch

    from agent_augury.backends_factory import list_models_openai_compat

    with patch("agent_augury.backends_factory._fetch_models_sync") as mock:
        mock.return_value = ["gpt-4o"]
        assert list_models_openai_compat("https://api.openai.com/v1", "key") == ["gpt-4o"]
        mock.assert_called_once_with("https://api.openai.com/v1", "key")


def test_list_models_nous_portal_success():
    from unittest.mock import patch

    from agent_augury.backends_factory import list_models_nous_portal

    with patch("agent_augury.backends_factory._fetch_models_sync") as mock:
        mock.return_value = ["Hermes-4"]
        assert list_models_nous_portal("https://inference-api.nousresearch.com/v1", "key") == [
            "Hermes-4"
        ]


def test_list_models_nous_oauth_no_token():
    """Without a stored token, returns None."""
    from unittest.mock import MagicMock, patch

    from agent_augury.auth.token_store import TokenStore
    from agent_augury.backends_factory import list_models_nous_oauth

    store = MagicMock(spec=TokenStore)
    store.get_provider_tokens.return_value = {}

    with patch("agent_augury.backends_factory.TokenStore", return_value=store):
        assert list_models_nous_oauth("https://inference-api.nousresearch.com/v1") is None


def test_list_models_nous_oauth_with_token():
    """With a stored token, calls _fetch_models_sync."""
    from unittest.mock import MagicMock, patch

    from agent_augury.auth.token_store import TokenStore
    from agent_augury.backends_factory import list_models_nous_oauth

    store = MagicMock(spec=TokenStore)
    store.get_provider_tokens.return_value = {"access_token": "tok-123"}

    with patch("agent_augury.backends_factory.TokenStore", return_value=store), \
         patch("agent_augury.backends_factory._fetch_models_sync") as mock_fetch:
        mock_fetch.return_value = ["Hermes-4"]
        result = list_models_nous_oauth("https://inference-api.nousresearch.com/v1")
        assert result == ["Hermes-4"]
        mock_fetch.assert_called_once_with("https://inference-api.nousresearch.com/v1", "tok-123")


# ---------------------------------------------------------------------------
# wizard._try_list_models
# ---------------------------------------------------------------------------


def test_try_list_models_fake_returns_none():
    from agent_augury.wizard import _try_list_models

    assert _try_list_models("fake", "", None) is None


def test_try_list_models_openai_no_env_returns_none():
    from agent_augury.wizard import _try_list_models

    assert _try_list_models("openai", "https://api.openai.com/v1", None) is None


def test_try_list_models_openai_no_key_returns_none(monkeypatch):
    from agent_augury.wizard import _try_list_models

    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert _try_list_models("openai", "https://api.openai.com/v1", "OPENAI_API_KEY") is None


def test_try_list_models_openai_success(monkeypatch):
    from unittest.mock import patch

    from agent_augury.wizard import _try_list_models

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock:
        mock.return_value = ["gpt-4o"]
        assert _try_list_models("openai", "https://api.openai.com/v1", "OPENAI_API_KEY") == [
            "gpt-4o"
        ]


def test_try_list_models_openai_failure_returns_none(monkeypatch):
    from unittest.mock import patch

    from agent_augury.wizard import _try_list_models

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock:
        mock.return_value = None
        assert _try_list_models("openai", "https://api.openai.com/v1", "OPENAI_API_KEY") is None


# ---------------------------------------------------------------------------
# wizard._select_model_interactive
# ---------------------------------------------------------------------------


def test_select_model_interactive_pick_by_number():
    from agent_augury.wizard import _select_model_interactive

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.input", lambda _: "1")
        assert _select_model_interactive(["gpt-4o", "gpt-4o-mini"]) == "gpt-4o"


def test_select_model_interactive_pick_last():
    from agent_augury.wizard import _select_model_interactive

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.input", lambda _: "2")
        assert _select_model_interactive(["gpt-4o", "gpt-4o-mini"]) == "gpt-4o-mini"


def test_select_model_interactive_manual_fallback():
    from agent_augury.wizard import _select_model_interactive

    with pytest.MonkeyPatch.context() as mp:
        # 3 = manual entry option
        mp.setattr("builtins.input", lambda _: "3")
        assert _select_model_interactive(["gpt-4o", "gpt-4o-mini"]) is None


# ---------------------------------------------------------------------------
# wizard.run_wizard — model listing integration
# ---------------------------------------------------------------------------


def test_wizard_openai_with_model_listing(tmp_path, monkeypatch):
    """CLI wizard E2E: model listing → pick → saved YAML → session launch (T3).

    Replaces the former ghost test (0 assertions, never captured the config):
    drives the full ``main([])`` wizard flow, asserts the picked model lands
    in the saved YAML, and that ``_run`` is launched with that path + task.
    """
    from agent_augury.cli import main
    from agent_augury.config import load_config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    output_path = tmp_path / "wizard_out.yaml"

    calls = []

    async def fake_run(cfg_path, initial_prompt=None, *, quiet=False):
        calls.append({"cfg_path": cfg_path, "initial_prompt": initial_prompt, "quiet": quiet})
        return 0

    # Mock the listing function to return models
    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock_list:
        mock_list.return_value = ["gpt-4o", "gpt-4o-mini"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "1",            # backend choice = openai
            "",             # base_url → default
            "OPENAI_API_KEY",  # api_key_env
            "1",            # select model #1 from list (gpt-4o)
            "n",            # no more agents
            str(output_path),  # save config path
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.cli.check_tty", return_value=True), \
             patch("agent_augury.cli.model_config_exists", return_value=False), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.cli._run", fake_run):
            rc = main([])

    assert rc == 0
    # The picked model landed in the saved YAML.
    assert output_path.exists()
    loaded = load_config(output_path)
    assert loaded["agents"][0]["backend"]["model"] == "gpt-4o"
    assert loaded["agents"][0]["backend"]["base_url"] == "https://api.openai.com/v1"
    # The session was launched against the saved config with the task.
    assert len(calls) == 1
    assert calls[0]["cfg_path"] == str(output_path)
    assert calls[0]["initial_prompt"] == "test task"
    assert calls[0]["quiet"] is False


def test_wizard_openai_model_listing_falls_back_to_manual(tmp_path, monkeypatch):
    """When model listing fails, user enters model ID manually."""
    from agent_augury.wizard import run_wizard

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock_list:
        mock_list.return_value = None  # listing failed

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "1",            # backend choice = openai
            "",             # base_url → default
            "OPENAI_API_KEY",  # api_key_env
            "my-custom-model",  # manual model entry (required)
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"):
            cfg = run_wizard()

    assert cfg["agents"][0]["backend"]["model"] == "my-custom-model"


def test_wizard_openai_model_listing_user_picks_model(tmp_path, monkeypatch):
    """User picks a model from the listed options."""
    from agent_augury.wizard import run_wizard

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock_list:
        mock_list.return_value = ["gpt-4o", "gpt-4o-mini"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "1",            # backend choice = openai
            "",             # base_url → default
            "OPENAI_API_KEY",  # api_key_env
            "2",            # select model #2 (gpt-4o-mini)
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"):
            cfg = run_wizard()

    assert cfg["agents"][0]["backend"]["model"] == "gpt-4o-mini"


def test_wizard_nous_oauth_with_model_listing(tmp_path):
    """OAuth backend: model listing uses stored token. No Base URL prompt."""
    from agent_augury.wizard import run_wizard

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4", "Hermes-3"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "3",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code", return_value="mock-token"):
            cfg = run_wizard()

    assert cfg["agents"][0]["backend"]["model"] == "Hermes-4"
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"


def test_wizard_nous_oauth_no_base_url_prompt(tmp_path):
    """OAuth selection must NOT show Base URL prompt."""
    from agent_augury.wizard import run_wizard

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "3",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)) as mock_input, \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code", return_value="mock-token"):
            cfg = run_wizard()

    # Verify Base URL prompt was never shown
    for call in mock_input.call_args_list:
        prompt = call[0][0] if call[0] else ""
        assert "Base URL" not in prompt, f"Base URL prompt should not appear: {prompt}"
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"
    assert cfg["agents"][0]["backend"]["base_url"] == "https://inference-api.nousresearch.com/v1"


def test_wizard_nous_oauth_reuses_valid_token(tmp_path):
    """OAuth with valid stored token skips authentication."""
    from agent_augury.wizard import run_wizard
    from agent_augury.auth.token_store import TokenStore
    from datetime import datetime, timezone, timedelta

    # Store a valid token
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store = TokenStore()
    store.set_provider_tokens("nous", {
        "access_token": "existing-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": future,
        "refresh_token": "refresh-token",
        "scope": "inference:invoke",
        "obtained_at": datetime.now(timezone.utc).isoformat(),
    })

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "3",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code") as mock_auth:
            cfg = run_wizard()

    # Authentication should NOT have been called
    mock_auth.assert_not_called()
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"

    # Cleanup
    store.clear()


def test_wizard_nous_oauth_force_reconfigure(tmp_path):
    """force_reconfigure=True must run auth even with valid token."""
    from agent_augury.wizard import run_wizard
    from agent_augury.auth.token_store import TokenStore
    from datetime import datetime, timezone, timedelta

    # Store a valid token
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store = TokenStore()
    store.set_provider_tokens("nous", {
        "access_token": "existing-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": future,
        "refresh_token": "refresh-token",
        "scope": "inference:invoke",
        "obtained_at": datetime.now(timezone.utc).isoformat(),
    })

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "3",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code", return_value="new-token") as mock_auth:
            cfg = run_wizard(force_reconfigure=True)

    # Authentication SHOULD have been called
    mock_auth.assert_called_once()
    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"

    # Cleanup
    store.clear()


def test_wizard_nous_oauth_auth_fallback_manual(tmp_path):
    """OAuth auth failure/cancellation falls back to manual model entry."""
    from agent_augury.wizard import run_wizard

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "L3",           # mode
            "10",           # max_steps
            "agent-1",      # agent id
            "3",            # backend choice = nous_oauth
            "manual-model", # manual model entry after auth failure
            "n",            # no more agents
            "test task",    # task description
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code", return_value=None):
            cfg = run_wizard()

    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"
    assert cfg["agents"][0]["backend"]["model"] == "manual-model"
