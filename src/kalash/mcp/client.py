"""Per-server MCP client lifecycle.

Connects to MCP servers (stdio, streamable HTTP, legacy SSE).
Lazy connect on first use. Schema discovery and tool namespacing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kalash.core.errors import KalashError, ToolTimeoutError
from kalash.core.ids import generate_id
import logging

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
        self._connected = False
        self._tools: dict[str, MCPToolSchema] = {}  # namespaced_name → schema
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stdin: asyncio.StreamWriter | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._sse_client: Any = None
        self._sse_endpoint: str = ""
        self._post_endpoint: str = "" 

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
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._sse_client:
            await self._sse_client.aclose()
            self._sse_client = None
        self._connected = False
        self._tools.clear()

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
        env = {**self._config.env} if self._config.env else None
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._config.command,
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._stdin = self._process.stdin  # type: ignore
            self._stdout = self._process.stdout  # type: ignore

            # Start reader task
            self._reader_task = asyncio.create_task(self._read_loop())

            # Send initialize
            await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kalash", "version": "0.1.0"},
            })

            # Send initialized notification
            await self._send_notification("notifications/initialized", {})

        except (OSError, FileNotFoundError) as e:
            raise MCPConnectionError(
                f"Failed to start MCP server '{self._config.name}': {e}",
                recoverable=True,
            )

    async def _connect_http(self) -> None:
        """Connect via streamable HTTP transport."""
        # HTTP transport is stateless per-request; just validate the URL
        if not self._config.url:
            raise MCPConnectionError(
                f"HTTP transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        # Connection is established per-request in _send_request
        self._connected = True

    async def _connect_sse(self) -> None:
        """Connect via legacy SSE transport."""
        if not self._config.url:
            raise MCPConnectionError(
                f"SSE transport requires a URL for server '{self._config.name}'",
                recoverable=True,
            )
        import httpx
        from kalash.mcp.auth import MCPAuth
        self._sse_client = httpx.AsyncClient(timeout=self._config.timeout_s)
        self._sse_endpoint = self._config.url
        self._post_endpoint = self._config.url
        
        self._reader_task = asyncio.create_task(self._sse_read_loop())
        self._connected = True

    async def _sse_read_loop(self) -> None:
        import httpx
        import json
        from kalash.mcp.auth import MCPAuth
        
        while True:
            try:
                headers = {"Accept": "text/event-stream"}
                auth_headers = MCPAuth.get_headers(self._config.name)
                if auth_headers:
                    headers.update(auth_headers)
                    
                async with self._sse_client.stream("GET", self._sse_endpoint, headers=headers) as response:
                    response.raise_for_status()
                    event_type = "message"
                    buffer = []
                    
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line:
                            if buffer:
                                data_str = "\n".join(buffer)
                                buffer = []
                                
                                if event_type == "endpoint":
                                    from urllib.parse import urljoin
                                    # Post endpoint is relative to sse_endpoint or absolute
                                    self._post_endpoint = urljoin(self._sse_endpoint, data_str)
                                elif event_type == "message":
                                    try:
                                        msg = json.loads(data_str)
                                        req_id = msg.get("id")
                                        if req_id and req_id in self._pending:
                                            future = self._pending.pop(req_id)
                                            if "error" in msg:
                                                future.set_exception(MCPSchemaError(str(msg["error"]), recoverable=True))
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
                            
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    async def _send_sse(self, message: dict[str, Any], req_id: int) -> Any:
        import httpx
        import json
        from kalash.mcp.auth import MCPAuth
        
        if not self._sse_client:
            raise MCPConnectionError("Not connected via SSE", recoverable=True)
            
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        
        headers = {"Content-Type": "application/json"}
        auth_headers = MCPAuth.get_headers(self._config.name)
        if auth_headers:
            headers.update(auth_headers)
            
        try:
            response = await self._sse_client.post(self._post_endpoint, json=message, headers=headers)
            response.raise_for_status()
            
            result = await asyncio.wait_for(future, timeout=self._config.timeout_s)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ToolTimeoutError(
                f"MCP request to '{self._config.name}' timed out after {self._config.timeout_s}s",
                recoverable=True,
            )
        except Exception as e:
            self._pending.pop(req_id, None)
            raise MCPConnectionError(f"Failed to send SSE POST: {e}", recoverable=True)


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
        elif self._config.transport == TransportType.HTTP:
            return await self._send_http(message)
        else:
            return await self._send_sse(message, req_id)

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if self._stdin:
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
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ToolTimeoutError(
                f"MCP request to '{self._config.name}' timed out after {self._config.timeout_s}s",
                recoverable=True,
            )

    async def _send_http(self, message: dict[str, Any]) -> Any:
        """Send via HTTP transport."""
        import urllib.request
        import urllib.error

        body = json.dumps(message).encode()
        headers = {"Content-Type": "application/json"}

        # Add auth if available
        from kalash.mcp.auth import MCPAuth
        auth_headers = MCPAuth.get_headers(self._config.name)
        if auth_headers:
            headers.update(auth_headers)

        req = urllib.request.Request(
            self._config.url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            response = await asyncio.to_thread(
                urllib.request.urlopen, req, timeout=self._config.timeout_s
            )
            response_data = json.loads(response.read().decode())
            if "error" in response_data:
                raise MCPSchemaError(
                    f"MCP error: {response_data['error']}",
                    recoverable=True,
                )
            return response_data.get("result")
        except urllib.error.URLError as e:
            raise MCPConnectionError(
                f"HTTP request to '{self._config.name}' failed: {e}",
                recoverable=True,
            )

    async def _read_loop(self) -> None:
        """Read responses from stdout for stdio transport."""
        if not self._stdout:
            return

        while True:
            try:
                line = await self._stdout.readline()
                if not line:
                    break  # EOF

                data = json.loads(line.decode())
                req_id = data.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending.pop(req_id)
                    if "error" in data:
                        future.set_exception(
                            MCPSchemaError(str(data["error"]), recoverable=True)
                        )
                    else:
                        future.set_result(data.get("result"))
            except json.JSONDecodeError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in MCP read loop", exc_info=e)
                continue
