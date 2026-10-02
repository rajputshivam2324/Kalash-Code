"""Per-server MCP client lifecycle.

Connects to MCP servers (stdio, streamable HTTP, legacy SSE).
Lazy connect on first use. Schema discovery and tool namespacing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.errors import KalashError, ToolTimeoutError
from kalash.mcp.auth import MCPAuth

logger = logging.getLogger(__name__)


class TransportType(StrEnum):
    """Supported MCP transport types."""

    STDIO = "stdio"
    HTTP = "streamable_http"
    SSE = "sse"  # legacy SSE transport


class MCPConnectionError(KalashError):
    """Failed to connect to an MCP server."""

    code = "KALASH_MCP_CONNECTION_ERROR"


class MCPSchemaError(KalashError):
    """Schema discovery or validation failed."""

    code = "KALASH_MCP_SCHEMA_ERROR"


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    transport: TransportType = TransportType.STDIO
    command: str = ""  # For stdio
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # For HTTP/SSE
    headers: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
    capabilities: list[str] = field(default_factory=list)


@dataclass
class MCPToolSchema:
    """Schema for a single MCP tool."""

    name: str  # Raw tool name from server
    namespaced_name: str  # mcp__<server>__<tool>
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""


class MCPClient:
    """Client for a single MCP server with lazy connection.

    Tool namespacing: mcp__<server>__<tool>
    Connects on first use (lazy) and maintains the connection.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._auth = MCPAuth()
        self._connected = False
        self._tools: dict[str, MCPToolSchema] = {}  # namespaced_name → schema
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._http: Any = None
        self._sse_client: Any = None
        self._sse_endpoint: str = ""
        self._post_endpoint: str = ""
        self._endpoint_ready = asyncio.Event()

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tools(self) -> dict[str, MCPToolSchema]:
        return dict(self._tools)

    async def connect(self) -> None:
        """Establish connection to the MCP server."""
        if self._connected:
            return

        if self._config.transport == TransportType.STDIO:
            await self._connect_stdio()
        elif self._config.transport == TransportType.HTTP:
            await self._connect_http()
        elif self._config.transport == TransportType.SSE:
            await self._connect_sse()
        else:
            raise MCPConnectionError(
                f"Unsupported transport: {self._config.transport}",
                recoverable=True,
            )

        # Discover tools
        await self._discover_schema()
        self._connected = True

    async def ensure_connected(self) -> None:
        """Lazy connect: connect on first use."""
        if not self._connected:
            await self.connect()

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Call a tool on the MCP server.

        Args:
            tool_name: The raw (non-namespaced) tool name.
            arguments: Tool arguments validated against schema.

        Returns:
            Tool result from the server.
        """
        await self.ensure_connected()

        # Validate tool exists
        namespaced = self._namespace(tool_name)
        if namespaced not in self._tools:
            raise MCPSchemaError(
                f"Tool '{tool_name}' not found on server '{self._config.name}'",
                recoverable=True,
            )

        # Send JSON-RPC request
        result = await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        return result

    async def disconnect(self) -> None:
        """Close the connection to the MCP server."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process and self._process.returncode is None:
            import os
            import signal

            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self._process.wait()
        if self._http is not None:
            headers = {**self._config.headers, **await self._auth.get_headers(self._config.name)}
            await self._http.close(headers)
            self._http = None
        if self._sse_client:
            await self._sse_client.aclose()
            self._sse_client = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPConnectionError("MCP disconnected", recoverable=True))
        self._pending.clear()
        self._connected = False
        self._tools.clear()
        self._process = None

    def get_tool_schema(self, tool_name: str) -> MCPToolSchema | None:
        """Get schema for a specific tool (by raw or namespaced name)."""
        # Check namespaced first
        if tool_name in self._tools:
            return self._tools[tool_name]
        # Check by raw name
        namespaced = self._namespace(tool_name)
        return self._tools.get(namespaced)

    def _namespace(self, tool_name: str) -> str:
        """Apply namespacing: mcp__<server>__<tool>."""
        return f"mcp__{self._config.name}__{tool_name}"

    async def _connect_stdio(self) -> None:
        """Connect via stdio transport (subprocess with JSON-RPC on stdin/stdout)."""
        from kalash.sandbox.environment import safe_environment

        env = {**safe_environment(), **self._config.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._config.command,
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
            self._stdin = self._process.stdin
            self._stdout = self._process.stdout

            # Start reader task
            self._reader_task = asyncio.create_task(self._read_loop())

            # Send initialize
            await self._send_request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "kalash", "version": "0.1.0"},
                },
            )

            # Send initialized notification
            await self._send_notification("notifications/initialized", {})

        except (OSError, FileNotFoundError) as e:
            raise MCPConnectionError(
                f"Failed to start MCP server '{self._config.name}': {e}",
                recoverable=True,
            )

    async def _connect_http(self) -> None:
        """Connect via streamable HTTP transport."""
        if not self._config.url:
            raise MCPConnectionError(
                f"HTTP transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        from kalash.mcp.http import HTTPTransport

        self._http = HTTPTransport(self._config.url, self._config.timeout_s)
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "kalash", "version": "0.1.0"},
            },
        )
        if not isinstance(result, dict) or "protocolVersion" not in result:
            raise MCPSchemaError("Invalid MCP initialize response", recoverable=True)
        self._http.protocol = result["protocolVersion"]
        await self._send_notification("notifications/initialized", {})

    async def _connect_sse(self) -> None:
        """Connect via legacy SSE transport."""
        if not self._config.url:
            raise MCPConnectionError(
                f"SSE transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        import httpx

        self._sse_client = httpx.AsyncClient(timeout=self._config.timeout_s)
        self._sse_endpoint = self._config.url
        self._endpoint_ready.clear()
        self._reader_task = asyncio.create_task(self._sse_read_loop())
        await asyncio.wait_for(self._endpoint_ready.wait(), self._config.timeout_s)
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kalash", "version": "0.1.0"},
            },
        )
        await self._send_notification("notifications/initialized", {})

    async def _sse_read_loop(self) -> None:
        import json

        while True:
            try:
                headers = {"Accept": "text/event-stream"}
                auth_headers = await self._auth.get_headers(self._config.name)
                headers.update(self._config.headers)
                if auth_headers:
                    headers.update(auth_headers)

                async with self._sse_client.stream(
                    "GET", self._sse_endpoint, headers=headers
                ) as response:
                    response.raise_for_status()
                    event_type = "message"
                    buffer: list[str] = []

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            if buffer:
                                data_str = "\n".join(buffer)
                                buffer = []

                                if event_type == "endpoint":
                                    from urllib.parse import urljoin, urlsplit

                                    # Post endpoint is relative to sse_endpoint or absolute
                                    endpoint = urljoin(self._sse_endpoint, data_str)
                                    origin = urlsplit(self._sse_endpoint)
                                    target = urlsplit(endpoint)
                                    if (origin.scheme, origin.netloc) != (
                                        target.scheme,
                                        target.netloc,
                                    ):
                                        raise MCPConnectionError(
                                            "SSE endpoint changed origin", recoverable=False
                                        )
                                    self._post_endpoint = endpoint
                                    self._endpoint_ready.set()
                                elif event_type == "message":
                                    try:
                                        msg = json.loads(data_str)
                                        req_id = msg.get("id")
                                        if req_id and req_id in self._pending:
                                            future = self._pending.pop(req_id)
                                            if "error" in msg:
                                                future.set_exception(
                                                    MCPSchemaError(
                                                        str(msg["error"]), recoverable=True
                                                    )
                                                )
                                            else:
                                                future.set_result(msg.get("result"))
                                    except json.JSONDecodeError:
                                        pass
                            event_type = "message"
                            continue

                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            buffer.append(line[5:].strip())
                            if sum(len(part) for part in buffer) > 2 * 1024 * 1024:
                                raise MCPSchemaError("MCP SSE event too large", recoverable=True)

                self._connection_lost()
                return
            except asyncio.CancelledError:
                self._connection_lost()
                return
            except Exception:
                logger.warning("MCP SSE stream disconnected", exc_info=True)
                self._connection_lost()
                return

    async def _send_sse(self, message: dict[str, Any], req_id: int) -> Any:

        if not self._sse_client:
            raise MCPConnectionError("Not connected via SSE", recoverable=True)

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        headers = {"Content-Type": "application/json"}
        auth_headers = await self._auth.get_headers(self._config.name)
        headers.update(self._config.headers)
        if auth_headers:
            headers.update(auth_headers)

        try:
            response = await self._sse_client.post(
                self._post_endpoint, json=message, headers=headers
            )
            response.raise_for_status()

            result = await asyncio.wait_for(future, timeout=self._config.timeout_s)
            return result
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise ToolTimeoutError(
                f"MCP request to '{self._config.name}' timed out after {self._config.timeout_s}s",
                recoverable=True,
            )
        except Exception as e:
            self._pending.pop(req_id, None)
            raise MCPConnectionError(f"Failed to send SSE POST: {e}", recoverable=True)
        finally:
            self._pending.pop(req_id, None)

    async def _discover_schema(self) -> None:
        """Discover available tools from the server."""
        result = await self._send_request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []

        for tool_def in tools:
            name = tool_def.get("name", "")
            namespaced = self._namespace(name)
            self._tools[namespaced] = MCPToolSchema(
                name=name,
                namespaced_name=namespaced,
                description=tool_def.get("description", ""),
                input_schema=tool_def.get("inputSchema", {}),
                server_name=self._config.name,
            )

    async def _send_request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and wait for response."""
        self._request_id += 1
        req_id = self._request_id

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        if self._config.transport == TransportType.STDIO:
            return await self._send_stdio(message, req_id)
        if self._config.transport == TransportType.HTTP:
            return await self._send_http(message)
        return await self._send_sse(message, req_id)

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if self._config.transport == TransportType.HTTP:
            await self._send_http(message)
        elif self._config.transport == TransportType.SSE and self._sse_client:
            headers = {**self._config.headers, **await self._auth.get_headers(self._config.name)}
            response = await self._sse_client.post(
                self._post_endpoint, json=message, headers=headers
            )
            response.raise_for_status()
        elif self._stdin:
            data = json.dumps(message) + "\n"
            self._stdin.write(data.encode())
            await self._stdin.drain()

    async def _send_stdio(self, message: dict[str, Any], req_id: int) -> Any:
        """Send via stdio and wait for response."""
        if not self._stdin:
            raise MCPConnectionError("Not connected via stdio", recoverable=True)

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        data = json.dumps(message) + "\n"
        self._stdin.write(data.encode())
        await self._stdin.drain()

        try:
            result = await asyncio.wait_for(future, timeout=self._config.timeout_s)
            return result
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise ToolTimeoutError(
                f"MCP request to '{self._config.name}' timed out after {self._config.timeout_s}s",
                recoverable=True,
            )
        finally:
            self._pending.pop(req_id, None)

    async def _send_http(self, message: dict[str, Any]) -> Any:
        if self._http is None:
            raise MCPConnectionError("HTTP transport not connected", recoverable=True)
        headers = {**self._config.headers, **await self._auth.get_headers(self._config.name)}
        try:
            return await self._http.send(message, headers)
        except Exception as exc:
            raise MCPConnectionError(f"MCP HTTP request failed: {exc}", recoverable=True) from exc

    async def _read_loop(self) -> None:
        """Read responses from stdout for stdio transport."""
        if not self._stdout:
            return

        while True:
            try:
                line = await self._stdout.readline()
                if not line:
                    self._connection_lost()
                    break  # EOF

                data = json.loads(line.decode())
                req_id = data.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending.pop(req_id)
                    if future.done():
                        continue
                    if "error" in data:
                        future.set_exception(MCPSchemaError(str(data["error"]), recoverable=True))
                    else:
                        future.set_result(data.get("result"))
            except json.JSONDecodeError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in MCP read loop", exc_info=e)
                self._connection_lost()
                break

    def _connection_lost(self) -> None:
        self._connected = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPConnectionError("MCP stream ended", recoverable=True))
        self._pending.clear()
