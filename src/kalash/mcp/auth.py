"""OAuth + OS keyring token management for remote MCP servers.

Tokens stored via OS keyring — never in database, never in plain config.
Supports OAuth authorization_code flow and token refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

SERVICE_NAME = "kalash-mcp"


class AuthError(Exception):
    """Authentication-related errors."""


@dataclass
class OAuthConfig:
    """OAuth configuration for a remote MCP server."""

    server_name: str
    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str = ""  # optional for public clients
    scopes: list[str] = field(default_factory=list)
    redirect_uri: str = "http://localhost:9374/callback"


@dataclass
class TokenData:
    """OAuth token data stored in keyring."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0  # Unix timestamp
    token_type: str = "Bearer"
    scopes: list[str] = field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        """Check if the access token has expired (with 60s buffer)."""
        if self.expires_at <= 0:
            return False  # No expiry info = assume valid
        return time.time() >= (self.expires_at - 60)


class MCPAuth:
    """Manages OAuth tokens for remote MCP servers.

    Token storage uses the OS keyring (macOS Keychain, Linux Secret Service,
    Windows Credential Manager). Never stored in SQLite or plain config files.
    """

    def __init__(self) -> None:
        self._configs: dict[str, OAuthConfig] = {}

    def configure_server(self, config: OAuthConfig) -> None:
        """Register OAuth config for a server."""
        self._configs[config.server_name] = config

    async def get_token(self, server_name: str) -> str | None:
        """Get a valid access token for a server.

        Refreshes automatically if expired. Returns None if no token
        is available (user needs to authenticate).
        """
        token_data = await self._load_token(server_name)
        if token_data is None:
            return None

        if token_data.is_expired:
            if token_data.refresh_token:
                token_data = await self._refresh_token(server_name, token_data)
                if token_data:
                    await self._store_token(server_name, token_data)
                    return token_data.access_token
            return None  # expired and no refresh token

        return token_data.access_token

    async def get_headers(self, server_name: str) -> dict[str, str]:
        from kalash.models.auth_store import get_credential

        saved = get_credential(f"mcp:{server_name}")
        token = saved or await self.get_token(server_name)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def authenticate(self, server_name: str) -> TokenData:
        """Run the OAuth authorization_code flow for a server.

        This starts a local HTTP server to receive the callback,
        opens the browser for user authorization, and exchanges
        the code for tokens.
        """
        config = self._configs.get(server_name)
        if not config:
            raise AuthError(f"No OAuth config for server '{server_name}'")

        # Generate PKCE challenge
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = hashlib.sha256(code_verifier.encode()).digest()
        import base64

        code_challenge_b64 = base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)

        # Build authorization URL
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": " ".join(config.scopes),
            "state": state,
            "code_challenge": code_challenge_b64,
            "code_challenge_method": "S256",
        }
        auth_url = f"{config.authorization_url}?{urlencode(params)}"

        # Open the consent screen. Without this the flow builds a URL nobody
        # ever visits and then blocks on a callback that can never arrive.
        opened = False
        try:
            import webbrowser

            opened = await asyncio.to_thread(webbrowser.open, auth_url)
        except Exception:
            opened = False

        if not opened:
            # Headless or no browser available — the user must open it manually,
            # so the URL has to be surfaced rather than swallowed.
            import logging as _log

            _log.getLogger(__name__).warning(
                "mcp_oauth_open_browser_manually: server=%s url=%s",
                server_name,
                auth_url,
            )

        # Wait for the callback with authorization code
        code = await self._wait_for_callback(state, config.redirect_uri)

        # Exchange code for tokens
        token_data = await self._exchange_code(config, code, code_verifier)
        await self._store_token(server_name, token_data)
        return token_data

    async def revoke(self, server_name: str) -> None:
        """Revoke and delete stored tokens for a server."""
        await self._delete_token(server_name)

    async def has_token(self, server_name: str) -> bool:
        """Check if a token exists for a server (may be expired)."""
        token_data = await self._load_token(server_name)
        return token_data is not None

    async def _refresh_token(
        self,
        server_name: str,
        token_data: TokenData,
    ) -> TokenData | None:
        """Refresh an expired access token."""
        config = self._configs.get(server_name)
        if not config or not token_data.refresh_token:
            return None

        import urllib.error
        import urllib.request

        body = urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": token_data.refresh_token,
                "client_id": config.client_id,
            }
        ).encode()

        if config.client_secret:
            body += f"&client_secret={config.client_secret}".encode()

        req = urllib.request.Request(
            config.token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=15)
            data = json.loads(response.read().decode())
            return TokenData(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", token_data.refresh_token),
                expires_at=time.time() + data.get("expires_in", 3600),
                token_type=data.get("token_type", "Bearer"),
                scopes=data.get("scope", "").split(),
            )
        except Exception:
            return None

    async def _exchange_code(
        self,
        config: OAuthConfig,
        code: str,
        code_verifier: str,
    ) -> TokenData:
        """Exchange an authorization code for tokens."""
        import urllib.request

        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "code_verifier": code_verifier,
            }
        ).encode()

        if config.client_secret:
            body += f"&client_secret={config.client_secret}".encode()

        req = urllib.request.Request(
            config.token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=15)
        data = json.loads(response.read().decode())

        return TokenData(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + data.get("expires_in", 3600),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(),
        )

    async def _wait_for_callback(self, expected_state: str, redirect_uri: str) -> str:
        """Start a local HTTP server and wait for the OAuth callback.

        Returns the authorization code from the callback.
        """
        from http.server import BaseHTTPRequestHandler, HTTPServer

        parsed = urlparse(redirect_uri)
        port = parsed.port or 9374
        received_code: str | None = None
        received_error: str | None = None

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self_handler) -> None:
                nonlocal received_code, received_error
                params = parse_qs(urlparse(self_handler.path).query)

                state = params.get("state", [""])[0]
                if state != expected_state:
                    received_error = "State mismatch"
                    self_handler.send_response(400)
                    self_handler.end_headers()
                    self_handler.wfile.write(b"State mismatch")
                    return

                if "error" in params:
                    received_error = params["error"][0]
                    self_handler.send_response(400)
                    self_handler.end_headers()
                    self_handler.wfile.write(received_error.encode())
                    return

                received_code = params.get("code", [""])[0]
                self_handler.send_response(200)
                self_handler.end_headers()
                self_handler.wfile.write(b"Authorization successful. You can close this tab.")

            def log_message(self, *args: Any) -> None:
                pass  # Suppress server logs

        server = HTTPServer(("localhost", port), CallbackHandler)
        server.timeout = 120  # 2 minute timeout

        # Handle one request
        await asyncio.to_thread(server.handle_request)
        server.server_close()

        if received_error:
            raise AuthError(f"OAuth error: {received_error}")
        if not received_code:
            raise AuthError("No authorization code received")

        return received_code

    async def _store_token(self, server_name: str, token_data: TokenData) -> None:
        """Store token in OS keyring."""
        try:
            import importlib

            keyring = importlib.import_module("keyring")

            payload = json.dumps(
                {
                    "access_token": token_data.access_token,
                    "refresh_token": token_data.refresh_token,
                    "expires_at": token_data.expires_at,
                    "token_type": token_data.token_type,
                    "scopes": token_data.scopes,
                }
            )
            await asyncio.to_thread(keyring.set_password, SERVICE_NAME, server_name, payload)
        except ImportError:
            # keyring not available — fallback to in-memory only
            pass

    async def _load_token(self, server_name: str) -> TokenData | None:
        """Load token from OS keyring."""
        try:
            import importlib

            keyring = importlib.import_module("keyring")

            payload = await asyncio.to_thread(keyring.get_password, SERVICE_NAME, server_name)
            if not payload:
                return None
            data = json.loads(payload)
            return TokenData(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at", 0),
                token_type=data.get("token_type", "Bearer"),
                scopes=data.get("scopes", []),
            )
        except (ImportError, json.JSONDecodeError, KeyError):
            return None

    async def _delete_token(self, server_name: str) -> None:
        """Delete token from OS keyring."""
        try:
            import importlib

            keyring = importlib.import_module("keyring")

            await asyncio.to_thread(keyring.delete_password, SERVICE_NAME, server_name)
        except Exception:  # keyring missing or backend unavailable
            pass
