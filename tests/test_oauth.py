"""OAuth authentication tests."""

from __future__ import annotations

import time
from datetime import UTC
from pathlib import Path

import httpx
import pytest

from agent_augury.auth.oauth import (
    NOUS_PORTAL_CONFIG,
    DeviceCodeFlow,
    OAuthProviderConfig,
    _generate_pkce_pair,
)
from agent_augury.auth.token_store import (
    TokenStore,
    compute_expires_at,
    is_token_expiring,
)

# ---------------------------------------------------------------------------
# Token store tests
# ---------------------------------------------------------------------------


class TestTokenStore:
    def test_save_and_load(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.set_provider_tokens("nous", {"access_token": "abc123"})

        loaded = store.get_provider_tokens("nous")
        assert loaded["access_token"] == "abc123"

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        assert store.load() == {}

    def test_clear(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.set_provider_tokens("nous", {"access_token": "abc123"})
        store.clear()
        assert store.load() == {}

    def test_overwrite_provider(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path / "tokens.json")
        store.set_provider_tokens("nous", {"access_token": "old"})
        store.set_provider_tokens("nous", {"access_token": "new"})

        loaded = store.get_provider_tokens("nous")
        assert loaded["access_token"] == "new"

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        store1 = TokenStore(path)
        store1.set_provider_tokens("nous", {"access_token": "persisted"})

        store2 = TokenStore(path)
        assert store2.get_provider_tokens("nous")["access_token"] == "persisted"


# ---------------------------------------------------------------------------
# Token expiry tests
# ---------------------------------------------------------------------------


class TestTokenExpiry:
    def test_expired_token_returns_true(self) -> None:
        past = "2020-01-01T00:00:00+00:00"
        assert is_token_expiring(past) is True

    def test_valid_token_returns_false(self) -> None:
        future = "2099-01-01T00:00:00+00:00"
        assert is_token_expiring(future) is False

    def test_expiring_within_skew_returns_true(self) -> None:
        # Token expiring in 60s with default 120s skew => expiring
        near_future = time.time() + 60
        from datetime import datetime
        expires_at = datetime.fromtimestamp(near_future, tz=UTC).isoformat()
        assert is_token_expiring(expires_at, skew_seconds=120) is True

    def test_invalid_expires_at_returns_true(self) -> None:
        assert is_token_expiring("not-a-date") is True

    def test_none_expires_at_returns_false(self) -> None:
        # No expiry info — treat as valid (e.g. tokens without expires_in)
        assert is_token_expiring(None) is False

    def test_compute_expires_at_valid(self) -> None:
        result = compute_expires_at(3600)
        assert result is not None
        assert "2099" not in result  # Should be ~1hr from now

    def test_compute_expires_at_invalid(self) -> None:
        assert compute_expires_at(0) is None
        assert compute_expires_at(-1) is None


# ---------------------------------------------------------------------------
# PKCE helper tests
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce_pair_format(self) -> None:
        verifier, challenge = _generate_pkce_pair()
        assert len(verifier) >= 43
        assert len(challenge) >= 43
        # URL-safe base64, no padding
        assert "=" not in verifier
        assert "=" not in challenge

    def test_challenge_is_sha256_of_verifier(self) -> None:
        import base64
        import hashlib
        verifier, challenge = _generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected


# ---------------------------------------------------------------------------
# Device Code Flow tests
# ---------------------------------------------------------------------------


class TestDeviceCodeFlow:
    """Tests using mocked HTTP transport."""

    def test_request_device_code_success(self) -> None:
        config = OAuthProviderConfig(
            id="test",
            name="Test",
            device_code_url="https://example.com/device",
            token_url="https://example.com/token",
            client_id="test-client",
        )

        def mock_client_factory():
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "device_code": "dev-123",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://example.com/verify",
                    "verification_uri_complete": "https://example.com/verify?code=ABCD",
                    "expires_in": 300,
                    "interval": 5,
                })
            return httpx.Client(transport=httpx.MockTransport(handler))

        flow = DeviceCodeFlow(config, http_client_factory=mock_client_factory)
        device = flow.request_device_code()

        assert device.device_code == "dev-123"
        assert device.user_code == "ABCD-EFGH"
        assert device.expires_in == 300

    def test_request_device_code_missing_fields(self) -> None:
        config = OAuthProviderConfig(
            id="test",
            name="Test",
            device_code_url="https://example.com/device",
            token_url="https://example.com/token",
            client_id="test-client",
        )

        def mock_client_factory():
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "device_code": "dev-123",
                    # Missing other fields
                })
            return httpx.Client(transport=httpx.MockTransport(handler))

        flow = DeviceCodeFlow(config, http_client_factory=mock_client_factory)
        with pytest.raises(ValueError, match="missing"):
            flow.request_device_code()

    def test_poll_for_token_immediate_success(self) -> None:
        config = OAuthProviderConfig(
            id="test",
            name="Test",
            device_code_url="https://example.com/device",
            token_url="https://example.com/token",
            client_id="test-client",
        )

        def mock_client_factory():
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={
                    "access_token": "token-abc",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-xyz",
                })
            return httpx.Client(transport=httpx.MockTransport(handler))

        flow = DeviceCodeFlow(config, http_client_factory=mock_client_factory)
        token = flow.poll_for_token("dev-123", expires_in=300, poll_interval=1)

        assert token.access_token == "token-abc"
        assert token.refresh_token == "refresh-xyz"

    def test_poll_for_token_timeout(self) -> None:
        config = OAuthProviderConfig(
            id="test",
            name="Test",
            device_code_url="https://example.com/device",
            token_url="https://example.com/token",
            client_id="test-client",
        )

        def mock_client_factory():
            call_count = 0
            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(400, json={
                    "error": "authorization_pending",
                    "error_description": "Still waiting",
                })
            return httpx.Client(transport=httpx.MockTransport(handler))

        flow = DeviceCodeFlow(config, http_client_factory=mock_client_factory)
        with pytest.raises(TimeoutError):
            flow.poll_for_token("dev-123", expires_in=1, poll_interval=1)


# ---------------------------------------------------------------------------
# Nous Portal config tests
# ---------------------------------------------------------------------------


class TestNousPortalConfig:
    def test_default_config_values(self) -> None:
        assert NOUS_PORTAL_CONFIG.id == "nous"
        assert NOUS_PORTAL_CONFIG.client_id == "hermes-cli"
        assert "portal.nousresearch.com" in NOUS_PORTAL_CONFIG.device_code_url

    def test_token_urls_match(self) -> None:
        assert "/api/oauth/token" in NOUS_PORTAL_CONFIG.token_url


# ---------------------------------------------------------------------------
# Backend factory tests
# ---------------------------------------------------------------------------


class TestBackendFactory:
    def test_build_nous_oauth_backend(self) -> None:
        from agent_augury.backends_factory import build_backend
        backend = build_backend({
            "type": "nous_oauth",
            "model": "nous-hermes-2",
        })
        assert backend.model == "nous-hermes-2"

    def test_nous_oauth_backend_reuses_client_across_completions(self) -> None:
        """Regression: complete() must not close the client after every request."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        import httpx

        from agent_augury.backend.nous_portal_oauth import NousPortalOAuthBackend

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": f"ok-{calls['n']}", "tool_calls": []}}]
                    },
                )
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        backend = NousPortalOAuthBackend(
            model="test-model",
            client=client,
            token_store=MagicMock(),
        )
        backend.get_access_token = AsyncMock(return_value="tok")

        async def run_twice() -> None:
            first = await backend.complete([{"role": "user", "content": "hi"}], [])
            second = await backend.complete([{"role": "user", "content": "again"}], [])
            assert first.text == "ok-1"
            assert second.text == "ok-2"

        asyncio.run(run_twice())

    def test_unknown_type_raises(self) -> None:
        from agent_augury.backends_factory import build_backend
        with pytest.raises(ValueError, match="unknown backend type"):
            build_backend({"type": "unknown", "model": "x"})


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_nous_oauth_accepted(self, tmp_path: Path) -> None:
        from agent_augury.config import load_config
        cfg_path = tmp_path / "test.yaml"
        cfg_path.write_text("""
mode: L3
max_steps: 10
task: test
agents:
  - id: agent-1
    backend:
      type: nous_oauth
      model: nous-hermes-2
""")
        cfg = load_config(cfg_path)
        assert cfg["agents"][0]["backend"]["type"] == "nous_oauth"

    def test_invalid_backend_type_rejected(self, tmp_path: Path) -> None:
        from agent_augury.config import ConfigError, load_config
        cfg_path = tmp_path / "test.yaml"
        cfg_path.write_text("""
mode: L3
agents:
  - id: agent-1
    backend:
      type: invalid_type
      model: x
""")
        with pytest.raises(ConfigError, match="backend.type"):
            load_config(cfg_path)


# ---------------------------------------------------------------------------
# OAuth duplicate auth prevention tests
# ---------------------------------------------------------------------------


class TestOAuthDuplicateAuthPrevention:
    """Tests that multiple backends sharing a provider authenticate only once."""

    def test_shared_token_store_prevents_duplicate_auth(self, tmp_path: Path) -> None:
        """Two backends with same provider + shared TokenStore → auth once."""
        import asyncio
        from unittest.mock import MagicMock

        from agent_augury.auth.token_store import TokenStore
        from agent_augury.backend.nous_portal_oauth import NousPortalOAuthBackend

        auth_count = {"n": 0}

        def fake_authenticate(self):
            auth_count["n"] += 1
            return asyncio.Future()

        store = TokenStore(store_path=tmp_path / "tokens.json")

        backend1 = NousPortalOAuthBackend(
            model="test", token_store=store, client=MagicMock()
        )
        backend2 = NousPortalOAuthBackend(
            model="test", token_store=store, client=MagicMock()
        )

        async def run():
            # Simulate: backend1 authenticates first, stores token
            # backend2 should reuse it without re-authenticating
            store.set_provider_tokens("nous", {
                "access_token": "shared-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "refresh_token": "refresh",
            })

            # Both should get the same token from the store
            token1 = await backend1.get_access_token()
            token2 = await backend2.get_access_token()
            assert token1 == "shared-token"
            assert token2 == "shared-token"

        asyncio.run(run())

    def test_concurrent_auth_serialized(self, tmp_path: Path) -> None:
        """Concurrent get_access_token calls → only one auth flow runs."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from agent_augury.auth.token_store import TokenStore
        from agent_augury.backend.nous_portal_oauth import NousPortalOAuthBackend

        auth_count = {"n": 0}

        store = TokenStore(store_path=tmp_path / "tokens.json")

        backend = NousPortalOAuthBackend(
            model="test", token_store=store, client=MagicMock()
        )

        async def fake_auth():
            auth_count["n"] += 1
            await asyncio.sleep(0.05)  # simulate slow auth
            backend._token = __import__(
                "agent_augury.auth.oauth", fromlist=["TokenResponse"]
            ).TokenResponse(access_token="tok", token_type="Bearer", expires_in=3600)
            store.set_provider_tokens("nous", {
                "access_token": "tok",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": "2099-01-01T00:00:00+00:00",
                "refresh_token": "refresh",
            })
            return "tok"

        async def run():
            with patch.object(backend, "_authenticate", side_effect=fake_auth):
                # Launch 3 concurrent get_access_token calls
                results = await asyncio.gather(
                    backend.get_access_token(),
                    backend.get_access_token(),
                    backend.get_access_token(),
                )
                assert all(r == "tok" for r in results)
                # Only one auth flow should have run
                assert auth_count["n"] == 1

        asyncio.run(run())

    def test_build_backend_passes_token_store(self) -> None:
        """build_backend forwards token_store to NousPortalOAuthBackend."""
        from unittest.mock import MagicMock

        from agent_augury.backends_factory import build_backend

        store = MagicMock()
        backend = build_backend(
            {"type": "nous_oauth", "model": "test"},
            token_store=store,
        )
        assert backend._token_store is store
