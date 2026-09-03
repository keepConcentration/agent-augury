"""OAuth authentication flows for agent-augury.

Implements browser-based OAuth flows:
- Device Code Flow (RFC 8628): for Nous Portal and similar providers
- PKCE Flow (RFC 7636): for providers with loopback callback

Reuses patterns proven in Hermes Agent auth system.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Configuration for an OAuth provider."""
    id: str
    name: str
    device_code_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scope: str = ""
    authorization_url: str = ""
    redirect_uri: str = "http://127.0.0.1:0/callback"
    extra_params: Dict[str, str] = field(default_factory=dict)


NOUS_PORTAL_CONFIG = OAuthProviderConfig(
    id="nous",
    name="Nous Portal",
    device_code_url="https://portal.nousresearch.com/api/oauth/device/code",
    token_url="https://portal.nousresearch.com/api/oauth/token",
    client_id="hermes-cli",
    scope="inference:invoke",
)


@dataclass
class DeviceCodeResponse:
    """Response from device code endpoint."""
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass
class TokenResponse:
    """Response from token endpoint."""
    access_token: str
    token_type: str = "Bearer"
    expires_in: Optional[int] = None
    expires_at: Optional[str] = None
    refresh_token: Optional[str] = None
    scope: Optional[str] = None


class DeviceCodeFlow:
    """OAuth 2.0 Device Code Flow (RFC 8628)."""

    def __init__(self, config: OAuthProviderConfig, http_client_factory: Optional[Callable] = None) -> None:
        self._config = config
        self._http_factory = http_client_factory

    def _get_http_client(self):
        if self._http_factory:
            return self._http_factory()
        import httpx
        return httpx.Client(timeout=30.0, headers={"Accept": "application/json"})

    def request_device_code(self) -> DeviceCodeResponse:
        """Request a device code from the provider."""
        client = self._get_http_client()
        try:
            response = client.post(
                self._config.device_code_url,
                data={
                    "client_id": self._config.client_id,
                    **({"scope": self._config.scope} if self._config.scope else {}),
                    **self._config.extra_params,
                },
            )
            response.raise_for_status()
            data = response.json()
            required = ["device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in", "interval"]
            missing = [f for f in required if f not in data]
            if missing:
                raise ValueError(f"Device code response missing: {', '.join(missing)}")
            return DeviceCodeResponse(
                device_code=data["device_code"],
                user_code=data["user_code"],
                verification_uri=data["verification_uri"],
                verification_uri_complete=data["verification_uri_complete"],
                expires_in=int(data["expires_in"]),
                interval=int(data["interval"]),
            )
        finally:
            if hasattr(client, "close"):
                client.close()

    def poll_for_token(self, device_code: str, expires_in: int, poll_interval: int) -> TokenResponse:
        """Poll the token endpoint until user approves or code expires."""
        import httpx
        deadline = time.monotonic() + max(1, expires_in)
        current_interval = max(1, min(poll_interval, 5))
        client = self._get_http_client()
        try:
            while time.monotonic() < deadline:
                response = client.post(
                    self._config.token_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": self._config.client_id,
                        "device_code": device_code,
                    },
                )
                if response.status_code == 200:
                    payload = response.json()
                    if "access_token" not in payload:
                        raise ValueError("Token response missing access_token")
                    return TokenResponse(
                        access_token=payload["access_token"],
                        token_type=payload.get("token_type", "Bearer"),
                        expires_in=payload.get("expires_in"),
                        refresh_token=payload.get("refresh_token"),
                        scope=payload.get("scope"),
                    )
                try:
                    error_payload = response.json()
                except Exception:
                    response.raise_for_status()
                    raise RuntimeError("Non-JSON error response from token endpoint")
                error_code = error_payload.get("error", "")
                if error_code == "authorization_pending":
                    time.sleep(current_interval)
                    continue
                if error_code == "slow_down":
                    current_interval = min(current_interval + 1, 30)
                    time.sleep(current_interval)
                    continue
                description = error_payload.get("error_description", "Unknown error")
                raise RuntimeError(f"{error_code}: {description}")
            raise TimeoutError("Timed out waiting for device authorization. Please complete sign-in in your browser and try again.")
        finally:
            if hasattr(client, "close"):
                client.close()

    def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        """Refresh an access token using a refresh token."""
        import httpx
        client = self._get_http_client()
        try:
            response = client.post(
                self._config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._config.client_id,
                    "refresh_token": refresh_token,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return TokenResponse(
                access_token=payload["access_token"],
                token_type=payload.get("token_type", "Bearer"),
                expires_in=payload.get("expires_in"),
                refresh_token=payload.get("refresh_token", refresh_token),
                scope=payload.get("scope"),
            )
        finally:
            if hasattr(client, "close"):
                client.close()

    def authenticate(self, on_user_code: Optional[Callable[[str, str], None]] = None, open_browser: bool = True) -> TokenResponse:
        """Run the full device code flow."""
        device = self.request_device_code()
        if on_user_code:
            on_user_code(device.user_code, device.verification_uri)
        else:
            print(f"\nTo authenticate, enter code: {device.user_code}")
            print(f"Verification URL: {device.verification_uri}")
        if open_browser:
            try:
                webbrowser.open(device.verification_uri_complete)
            except Exception:
                pass
        return self.poll_for_token(device.device_code, device.expires_in, device.interval)


def _generate_pkce_pair() -> Tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback."""
    auth_code: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def reset(cls) -> None:
        cls.auth_code = None
        cls.error = None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Authorization successful!</h1><p>You can close this tab.</p></body></html>")
        elif "error" in params:
            _CallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<html><body><h1>Authorization failed: {_CallbackHandler.error}</h1></body></html>".encode())
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class PKCEFlow:
    """OAuth 2.0 Authorization Code Flow with PKCE (RFC 7636)."""

    def __init__(self, config: OAuthProviderConfig) -> None:
        self._config = config

    def _start_callback_server(self) -> Tuple[HTTPServer, int]:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
        return server, port

    def authenticate(self, on_auth_url: Optional[Callable[[str], None]] = None, open_browser: bool = True) -> TokenResponse:
        """Run the full PKCE flow."""
        import httpx
        verifier, challenge = _generate_pkce_pair()
        server, port = self._start_callback_server()
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        auth_params = {
            "client_id": self._config.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **({"scope": self._config.scope} if self._config.scope else {}),
        }
        auth_url = f"{self._config.authorization_url}?{urlencode(auth_params)}"
        if on_auth_url:
            on_auth_url(auth_url)
        else:
            print(f"\nAuthorize at: {auth_url}")
        if open_browser:
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass
        server.timeout = 300
        _CallbackHandler.reset()
        while not _CallbackHandler.auth_code and not _CallbackHandler.error:
            server.handle_request()
        if _CallbackHandler.error:
            raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
        if not _CallbackHandler.auth_code:
            raise TimeoutError("Timed out waiting for authorization callback")
        client = httpx.Client(timeout=30.0, headers={"Accept": "application/json"})
        try:
            response = client.post(
                self._config.token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._config.client_id,
                    "code": _CallbackHandler.auth_code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return TokenResponse(
                access_token=payload["access_token"],
                token_type=payload.get("token_type", "Bearer"),
                expires_in=payload.get("expires_in"),
                refresh_token=payload.get("refresh_token"),
                scope=payload.get("scope"),
            )
        finally:
            server.server_close()
            client.close()
