"""Expose Kalash built-in tools as an MCP server.

Binds to loopback by default. Requires auth token.
Refuses 0.0.0.0 without explicit flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kalash.core.errors import KalashError
from kalash.core.privacy import scrub_metadata, scrub_text
from kalash.mcp.schema import validate_arguments, validator_for

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine


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
    max_body_bytes: int = 1_000_000
    request_timeout_s: float = 15
    handler_timeout_s: float = 60

    def __post_init__(self) -> None:
        if not self.auth_token:
            self.auth_token = secrets.token_urlsafe(32)

        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and not self.allow_all_interfaces:
            raise ServerSecurityError(
                "Binding outside loopback requires explicit allow_all_interfaces=True. "
                "This exposes Kalash tools to the network.",
                recoverable=True,
            )
        if not 0 < self.max_body_bytes <= 16_000_000:
            raise ValueError("MCP body limit must be between 1 and 16000000 bytes")
        if not 0 < self.request_timeout_s <= 300 or not 0 < self.handler_timeout_s <= 3600:
            raise ValueError("MCP request/handler deadlines must be positive and bounded")


@dataclass
class ToolDefinition:
    """A tool exposed by the Kalash MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    handler: Callable[..., Coroutine[Any, Any, Any]] | None = None
    validator: Any = None


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
        self._connections: set[asyncio.Task[Any]] = set()

    @property
    def auth_token(self) -> str:
        """The auth token required for connections."""
        return self._config.auth_token

    @property
    def endpoint(self) -> str:
        """The server endpoint URL."""
        host = f"[{self._config.host}]" if ":" in self._config.host else self._config.host
        return f"http://{host}:{self._config.port}/mcp"

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
            validator=validator_for(input_schema or {"type": "object", "properties": {}}),
        )

    async def start(self) -> None:
        """Start the MCP server."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._config.host,
            self._config.port,
        )
        self._running = True
        if self._config.port == 0 and self._server.sockets:
            self._config.port = self._server.sockets[0].getsockname()[1]
        await self._server.start_serving()

    async def stop(self) -> None:
        """Stop the MCP server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        active = list(self._connections)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming TCP connection (HTTP over TCP)."""
        task = asyncio.current_task()
        if len(self._connections) >= 64:
            writer.close()
            return
        if task is not None:
            self._connections.add(task)
        try:
            async with asyncio.timeout(self._config.request_timeout_s):
                request_line = await reader.readline()
                if not request_line:
                    return
                if len(request_line) > 4096:
                    raise ValueError("Request line exceeds limit")
                method, path, version = request_line.decode("ascii").strip().split()
                headers: dict[str, str] = {}
                size = len(request_line)
                while True:
                    header_line = await reader.readline()
                    size += len(header_line)
                    if size > 16_384:
                        raise ValueError("Headers exceed limit")
                    if header_line == b"\r\n" or not header_line:
                        break
                    key, separator, value = header_line.decode("ascii").partition(":")
                    key = key.strip().lower()
                    if not separator or key in headers:
                        raise ValueError("Malformed or duplicate header")
                    headers[key] = value.strip()
                if not self._authenticate(headers.get("authorization", "")):
                    await self._respond(writer, 401, {"error": "Unauthorized"})
                    return
                if method != "POST" or path != "/mcp" or version != "HTTP/1.1":
                    await self._respond(writer, 405, {"error": "Use POST /mcp HTTP/1.1"})
                    return
                if "transfer-encoding" in headers:
                    raise ValueError("Transfer encoding is unsupported")
                if headers.get("content-type", "").split(";")[0] != "application/json":
                    await self._respond(writer, 415, {"error": "Use application/json"})
                    return
                # A stateless authenticated endpoint has no browser origins.
                if "origin" in headers:
                    await self._respond(writer, 403, {"error": "Browser origins are unsupported"})
                    return
                content_length = int(headers.get("content-length", "0"))
                if not 0 < content_length <= self._config.max_body_bytes:
                    await self._respond(writer, 413, {"error": "Body length exceeds limit"})
                    return
                body = await reader.readexactly(content_length)
            request = json.loads(body)
            async with asyncio.timeout(self._config.handler_timeout_s):
                result = await self._handle_jsonrpc(request)
            await self._respond(writer, 202 if result is None else 200, result)
        except (ValueError, UnicodeError):
            await self._respond(writer, 400, {"error": "Malformed request"})
        except TimeoutError:
            await self._respond(writer, 408, {"error": "Request timed out"})
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _respond(self, writer: Any, status: int, body: dict[str, Any] | None) -> None:
        writer.write(self._http_response(status, body))
        async with asyncio.timeout(self._config.request_timeout_s):
            await writer.drain()

    async def _handle_jsonrpc(self, request: Any) -> dict[str, Any] | None:
        """Handle a JSON-RPC request."""
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid request"},
            }
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")
        if "id" in request and (isinstance(req_id, bool) or not isinstance(req_id, (str, int))):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid request ID"},
            }
        if not isinstance(method, str) or not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid request"},
            }
        if "id" not in request:
            return None  # Notifications never execute tools or receive RPC replies.

        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kalash", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
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
            if not isinstance(tool_name, str) or not isinstance(arguments, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid tool arguments"},
                }
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
            validate_arguments(tool.validator, arguments)
            result = await tool.handler(**arguments)
            rendered = json.dumps(scrub_metadata({"result": result})["result"], default=str)
            if len(rendered.encode()) > self._config.max_body_bytes:
                raise ValueError("Tool response exceeds output limit")
            return {"content": [{"type": "text", "text": rendered}]}
        except Exception as e:
            return {
                "isError": True,
                "content": [{"type": "text", "text": scrub_text(str(e))[:4096]}],
            }

    def _authenticate(self, auth_header: str) -> bool:
        """Validate the Bearer token."""
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        return secrets.compare_digest(token, self._config.auth_token)

    def _http_response(self, status: int, body: dict[str, Any] | None) -> bytes:
        """Build a raw HTTP response."""
        body_bytes = json.dumps(body).encode() if body is not None else b""
        status_text = {
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            401: "Unauthorized",
            413: "Payload Too Large",
        }.get(status, "Error")
        lines = [
            f"HTTP/1.1 {status} {status_text}",
            "Content-Type: application/json",
            f"Content-Length: {len(body_bytes)}",
            "Connection: close",
            "",
            "",
        ]
        return ("\r\n".join(lines)).encode() + body_bytes
