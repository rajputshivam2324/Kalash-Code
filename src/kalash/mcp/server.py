"""Expose Kalash built-in tools as an MCP server.

Binds to loopback by default. Requires auth token.
Refuses 0.0.0.0 without explicit flag.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from kalash.core.errors import KalashError


class ServerSecurityError(KalashError):
    """Security violation in MCP server configuration."""

    code = "KALASH_MCP_SERVER_SECURITY"


@dataclass
class ServerConfig:
    """Configuration for the Kalash MCP server."""

    host: str = "127.0.0.1"
    port: int = 9375
    auth_token: str = ""  # Auto-generated if empty
    allow_all_interfaces: bool = False  # Must be explicit for 0.0.0.0

    def __post_init__(self) -> None:
        if not self.auth_token:
            self.auth_token = secrets.token_urlsafe(32)

        # Security check: refuse 0.0.0.0 without explicit flag
        if self.host == "0.0.0.0" and not self.allow_all_interfaces:
            raise ServerSecurityError(
                "Binding to 0.0.0.0 requires explicit allow_all_interfaces=True. "
                "This exposes Kalash tools to the network.",
                recoverable=True,
            )


@dataclass
class ToolDefinition:
    """A tool exposed by the Kalash MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., Coroutine[Any, Any, Any]] | None = None


class KalashMCPServer:
    """Exposes Kalash built-in tools over MCP protocol.

    - Binds to loopback (127.0.0.1) by default.
    - Requires Bearer token authentication.
    - Supports JSON-RPC over HTTP transport.
    """

    def __init__(self, config: ServerConfig | None = None) -> None:
        self._config = config or ServerConfig()
        self._tools: dict[str, ToolDefinition] = {}
        self._server: asyncio.Server | None = None
        self._running = False

    @property
    def auth_token(self) -> str:
        """The auth token required for connections."""
        return self._config.auth_token

    @property
    def endpoint(self) -> str:
        """The server endpoint URL."""
        return f"http://{self._config.host}:{self._config.port}"

    def register_tool(
        self,
        name: str,
        handler: Callable[..., Coroutine[Any, Any, Any]],
        *,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool to expose via MCP."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema or {"type": "object", "properties": {}},
            handler=handler,
        )

    async def start(self) -> None:
        """Start the MCP server."""
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._config.host,
            self._config.port,
        )
        await self._server.start_serving()

    async def stop(self) -> None:
        """Stop the MCP server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming TCP connection (HTTP over TCP)."""
        try:
            while self._running:
                # Read HTTP request
                request_line = await reader.readline()
                if not request_line:
                    break

                # Parse headers
                headers: dict[str, str] = {}
                while True:
                    header_line = await reader.readline()
                    if header_line == b"\r\n" or not header_line:
                        break
                    key, _, value = header_line.decode().partition(":")
                    headers[key.strip().lower()] = value.strip()

                # Read body
                content_length = int(headers.get("content-length", "0"))
                body = b""
                if content_length > 0:
                    body = await reader.readexactly(content_length)

                # Authenticate
                auth_header = headers.get("authorization", "")
                if not self._authenticate(auth_header):
                    response = self._http_response(401, {"error": "Unauthorized"})
                    writer.write(response)
                    await writer.drain()
                    break

                # Process JSON-RPC
                try:
                    request = json.loads(body.decode())
                    result = await self._handle_jsonrpc(request)
                    response = self._http_response(200, result)
                except json.JSONDecodeError:
                    response = self._http_response(400, {"error": "Invalid JSON"})

                writer.write(response)
                await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any]:
        """Handle a JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kalash", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.input_schema,
                    }
                    for tool in self._tools.values()
                ]
            }
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self._call_tool(tool_name, arguments)
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a registered tool."""
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found"}

        if not tool.handler:
            return {"error": f"Tool '{name}' has no handler"}

        try:
            result = await tool.handler(**arguments)
            return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        except Exception as e:
            return {"isError": True, "content": [{"type": "text", "text": str(e)}]}

    def _authenticate(self, auth_header: str) -> bool:
        """Validate the Bearer token."""
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        return secrets.compare_digest(token, self._config.auth_token)

    def _http_response(self, status: int, body: dict[str, Any]) -> bytes:
        """Build a raw HTTP response."""
        body_bytes = json.dumps(body).encode()
        status_text = {200: "OK", 400: "Bad Request", 401: "Unauthorized"}.get(status, "Error")
        lines = [
            f"HTTP/1.1 {status} {status_text}",
            "Content-Type: application/json",
            f"Content-Length: {len(body_bytes)}",
            "Connection: keep-alive",
            "",
            "",
        ]
        return ("\r\n".join(lines)).encode() + body_bytes
