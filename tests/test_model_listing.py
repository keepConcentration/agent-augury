"""Model listing — backend / factory / wizard tests."""

from __future__ import annotations

from datetime import UTC
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


def test_model_info_label_shows_pricing_and_free():
    from agent_augury.model_listing import ModelInfo

    paid = ModelInfo(id="anthropic/claude-sonnet-4", prompt_per_token=3e-6, completion_per_token=1.5e-5)
    free = ModelInfo(id="meta/llama:free", prompt_per_token=0.0, completion_per_token=0.0)
    assert "[free]" in free.label()
    assert "per 1M tok" in paid.label()
    assert paid.id in paid.label()


def test_format_aligned_labels_pads_ids():
    from agent_augury.model_listing import ModelInfo, format_aligned_labels

    labels = format_aligned_labels(
        [
            ModelInfo(id="a/short", prompt_per_token=1e-6, completion_per_token=2e-6),
            ModelInfo(id="provider/much-longer-id", prompt_per_token=0.0, completion_per_token=0.0),
        ]
    )
    assert labels[0].startswith("a/short")
    assert labels[1].startswith("provider/much-longer-id")
    # id columns share the same width before the pricing suffix
    assert labels[0].index("[") == labels[1].index("[")
    assert "[free]" in labels[1]


def test_is_general_purpose_model_id_filters_specialty():
    from agent_augury.model_listing import is_general_purpose_model_id

    assert is_general_purpose_model_id("anthropic/claude-sonnet-4")
    assert is_general_purpose_model_id("meta/llama:free")
    assert not is_general_purpose_model_id("openai/gpt-4o:batch")
    assert not is_general_purpose_model_id("openai/gpt-4o-2024-11-20")
    assert not is_general_purpose_model_id("qwen/qwen3.8-max-0902")
    assert not is_general_purpose_model_id("tencent/hy4-preview")
    assert not is_general_purpose_model_id("~z-ai/glm-latest")
    assert not is_general_purpose_model_id("openai/gpt-4o:US")
    assert not is_general_purpose_model_id("google/gemini-3-pro-image")


def test_sort_model_infos_free_last_then_name():
    from agent_augury.model_listing import ModelInfo, sort_model_infos

    models = [
        ModelInfo(id="zoo/free-a", prompt_per_token=0.0, completion_per_token=0.0),
        ModelInfo(id="aaa/paid", prompt_per_token=1e-6, completion_per_token=2e-6),
        ModelInfo(id="mmm/free-b", prompt_per_token=0.0, completion_per_token=0.0),
        ModelInfo(id="bbb/paid", prompt_per_token=1e-6, completion_per_token=2e-6),
    ]
    sorted_ids = [m.id for m in sort_model_infos(models, free_last=True)]
    assert sorted_ids == ["aaa/paid", "bbb/paid", "mmm/free-b", "zoo/free-a"]


def test_extract_model_infos_openrouter_pricing_sort():
    from agent_augury.model_listing import extract_model_infos

    items = [
        {"id": "z/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "a/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"id": "m/free", "pricing": {"prompt": "0", "completion": "0"}},
    ]
    infos = extract_model_infos(items, include_pricing=True, free_last=True)
    assert [m.id for m in infos] == ["a/paid", "m/free", "z/free"]
    assert infos[0].is_free is False
    assert infos[1].is_free is True


def test_extract_model_infos_general_purpose_only():
    from agent_augury.model_listing import extract_model_infos

    items = [
        {"id": "keep/me", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"id": "drop/me:batch", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {
            "id": "drop/image-out",
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "architecture": {"output_modalities": ["image", "text"]},
        },
        {
            "id": "drop/expiring",
            "pricing": {"prompt": "0", "completion": "0"},
            "expiration_date": 123,
        },
    ]
    infos = extract_model_infos(
        items, include_pricing=True, free_last=True, general_purpose_only=True
    )
    assert [m.id for m in infos] == ["keep/me"]


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


def test_fetch_models_sync_omits_auth_header_when_key_empty():
    from unittest.mock import MagicMock, patch

    from agent_augury.backends_factory import _fetch_models_sync

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": [{"id": "anthropic/claude-sonnet-4"}]}
    with patch("httpx.get", return_value=resp) as mock_get:
        ids = _fetch_models_sync("https://openrouter.ai/api/v1", "")
    assert ids == ["anthropic/claude-sonnet-4"]
    headers = mock_get.call_args.kwargs["headers"]
    assert "Authorization" not in headers


def test_list_models_nous_portal_success():
    from unittest.mock import patch

    from agent_augury.backends_factory import list_models_nous_portal
    from agent_augury.model_listing import ModelInfo

    infos = [ModelInfo(id="Hermes-4", prompt_per_token=1e-6, completion_per_token=2e-6)]
    with patch("agent_augury.backends_factory._fetch_model_infos_sync", return_value=infos) as mock:
        assert list_models_nous_portal("https://inference-api.nousresearch.com/v1", "key") == infos
        mock.assert_called_once_with(
            "https://inference-api.nousresearch.com/v1",
            "key",
            include_pricing=True,
            free_last=True,
            general_purpose_only=True,
        )


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
    """With a stored token, calls list_models_nous_portal."""
    from unittest.mock import MagicMock, patch

    from agent_augury.auth.token_store import TokenStore
    from agent_augury.backends_factory import list_models_nous_oauth
    from agent_augury.model_listing import ModelInfo

    store = MagicMock(spec=TokenStore)
    store.get_provider_tokens.return_value = {"access_token": "tok-123"}
    infos = [ModelInfo(id="Hermes-4", prompt_per_token=0.0, completion_per_token=0.0)]

    with patch("agent_augury.backends_factory.TokenStore", return_value=store), \
         patch("agent_augury.backends_factory.list_models_nous_portal", return_value=infos) as mock_list:
        result = list_models_nous_oauth("https://inference-api.nousresearch.com/v1")
        assert result == infos
        mock_list.assert_called_once_with("https://inference-api.nousresearch.com/v1", "tok-123")


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


def test_try_list_models_openrouter_without_key_still_lists(monkeypatch):
    """OpenRouter listing must not require OPENROUTER_API_KEY to be set."""
    from unittest.mock import patch

    from agent_augury.model_listing import ModelInfo
    from agent_augury.wizard import _try_list_models

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    infos = [
        ModelInfo(id="anthropic/claude-sonnet-4", prompt_per_token=0.000003, completion_per_token=0.000015),
        ModelInfo(id="openai/gpt-4o", prompt_per_token=0.0, completion_per_token=0.0),
    ]
    with patch("agent_augury.backends_factory.list_openrouter_models", return_value=infos) as mock:
        result = _try_list_models(
            "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"
        )
        assert result == infos
        mock.assert_called_once_with("https://openrouter.ai/api/v1", "")


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


def test_select_model_interactive_modelinfo_returns_id():
    from agent_augury.model_listing import ModelInfo
    from agent_augury.wizard import _select_model_interactive

    models = [
        ModelInfo(id="a/paid", prompt_per_token=1e-6, completion_per_token=2e-6),
        ModelInfo(id="z/free", prompt_per_token=0.0, completion_per_token=0.0),
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.input", lambda _: "2")
        assert _select_model_interactive(models) == "z/free"


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

    async def fake_run(cfg_path, initial_prompt=None, *, quiet=False, **kwargs):
        calls.append({"cfg_path": cfg_path, "initial_prompt": initial_prompt, "quiet": quiet})
        return 0

    # Mock the listing function to return models
    with patch("agent_augury.backends_factory.list_models_openai_compat") as mock_list:
        mock_list.return_value = ["gpt-4o", "gpt-4o-mini"]

        inputs = iter([
            "agent-1",      # agent id
            "1",            # backend choice = openai
            "",             # base_url → default
            "OPENAI_API_KEY",  # api_key_env
            "1",            # select model #1 from list (gpt-4o)
            "n",            # no more agents
            "test task",    # task description (multi-line: first line)
            "",             # empty line terminates the task block
        ])
        with patch("builtins.input", side_effect=lambda *args: next(inputs)), \
             patch("agent_augury.cli.check_tty", return_value=True), \
             patch("agent_augury.cli.model_config_exists", return_value=False), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.cli._run_repl", fake_run), \
             patch("agent_augury.cli._prompt_multiline", return_value="test task"):
            rc = main(["--output", str(output_path)])

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
            "agent-1",      # agent id
            "4",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
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
            "agent-1",      # agent id
            "4",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
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
    from datetime import datetime, timedelta

    from agent_augury.auth.token_store import TokenStore
    from agent_augury.wizard import run_wizard

    # Store a valid token
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    store = TokenStore()
    store.set_provider_tokens("nous", {
        "access_token": "existing-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": future,
        "refresh_token": "refresh-token",
        "scope": "inference:invoke",
        "obtained_at": datetime.now(UTC).isoformat(),
    })

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "agent-1",      # agent id
            "4",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
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
    from datetime import datetime, timedelta

    from agent_augury.auth.token_store import TokenStore
    from agent_augury.wizard import run_wizard

    # Store a valid token
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    store = TokenStore()
    store.set_provider_tokens("nous", {
        "access_token": "existing-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": future,
        "refresh_token": "refresh-token",
        "scope": "inference:invoke",
        "obtained_at": datetime.now(UTC).isoformat(),
    })

    with patch("agent_augury.backends_factory.list_models_nous_oauth") as mock_list:
        mock_list.return_value = ["Hermes-4"]

        inputs = iter([
            "agent-1",      # agent id
            "4",            # backend choice = nous_oauth
            "1",            # select model #1
            "n",            # no more agents
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
            "agent-1",      # agent id
            "4",            # backend choice = nous_oauth
            "manual-model", # manual model entry after auth failure
            "n",            # no more agents
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
             patch("agent_augury.wizard.save_model_config"), \
             patch("agent_augury.wizard._run_nous_oauth_device_code", return_value=None):
            cfg = run_wizard()

    assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"
    assert cfg["agents"][0]["backend"]["model"] == "manual-model"
