"""MCP server multiplexing and lifecycle management.

Manages multiple MCP server connections with namespace collision detection,
health monitoring, reconnection, and config precedence.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any

from kalash.core.errors import KalashError
from kalash.core.events import Event, EventType, get_event_bus
from kalash.mcp.client import MCPClient, MCPServerConfig, MCPToolSchema
from kalash.storage.engine import StorageEngine


class ServerState(StrEnum):
    """MCP server connection state machine.

    CONFIGURED → CONNECTING → READY/FAILED/DEGRADED
    """

    CONFIGURED = "CONFIGURED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"  # connected but some tools unavailable


class NamespaceCollisionError(KalashError):
    """Two servers expose tools that collide after namespacing."""

    code = "KALASH_MCP_NAMESPACE_COLLISION"


@dataclass
class ServerStatus:
    """Runtime status of a managed MCP server."""

    name: str
    state: ServerState = ServerState.CONFIGURED
    tool_count: int = 0
    error: str | None = None
    last_health_check: str | None = None
    reconnect_attempts: int = 0
    max_reconnect_attempts: int = 3


class MCPManager:
    """Manages multiple MCP server connections.

    Config precedence: user → workspace → CLI flags.
    Handles namespace collision detection, health monitoring, and reconnection.
    """

    def __init__(self, engine: StorageEngine | None = None) -> None:
        self._engine = engine
        self._clients: dict[str, MCPClient] = {}
        self._status: dict[str, ServerStatus] = {}
        self._tools: dict[str, MCPToolSchema] = {}  # namespaced_name → schema
        self._health_task: asyncio.Task[None] | None = None
        self._health_interval_s: float = 60.0

    @property
    def servers(self) -> dict[str, MCPClient]:
        """All registered server clients."""
        return dict(self._clients)

    @property
    def all_tools(self) -> dict[str, MCPToolSchema]:
        """All available tools across all connected servers."""
        return dict(self._tools)

    def get_status(self, server_name: str) -> ServerStatus | None:
        """Get the status of a specific server."""
        return self._status.get(server_name)

    async def configure(self, configs: list[MCPServerConfig]) -> None:
        """Configure servers from merged config (user → workspace → CLI).

        Args:
            configs: List of server configurations, already merged by precedence.
        """
        for config in configs:
            if config.name in self._clients:
                # Reconfigure existing
                await self.disconnect_server(config.name)

            self._clients[config.name] = MCPClient(config)
            self._status[config.name] = ServerStatus(name=config.name)

    async def connect_all(self) -> dict[str, ServerState]:
        """Connect all configured servers. Returns final states."""
        tasks = [self._connect_server(name) for name in self._clients]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Check for namespace collisions
        self._detect_collisions()

        # Start health monitoring
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_loop())

        return {name: status.state for name, status in self._status.items()}

    async def connect_server(self, name: str) -> ServerState:
        """Connect a specific server."""
        await self._connect_server(name)
        self._detect_collisions()
        return self._status[name].state

    async def disconnect_server(self, name: str) -> None:
        """Disconnect a specific server."""
        client = self._clients.get(name)
        if client:
            await client.disconnect()
            # Remove its tools from the global registry
            to_remove = [k for k, v in self._tools.items() if v.server_name == name]
            for key in to_remove:
                del self._tools[key]
            self._status[name].state = ServerState.CONFIGURED

    async def disconnect_all(self) -> None:
        """Disconnect all servers and stop health monitoring."""
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        for name in list(self._clients.keys()):
            await self.disconnect_server(name)

    async def call_tool(
        self,
        namespaced_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Call a tool by its namespaced name (mcp__<server>__<tool>).

        Resolves the server and delegates to the appropriate client.
        """
        schema = self._tools.get(namespaced_name)
        if schema is None:
            raise KalashError(
                f"Tool '{namespaced_name}' not found in any connected MCP server",
                recoverable=True,
            )

        client = self._clients.get(schema.server_name)
        if client is None:
            raise KalashError(
                f"Server '{schema.server_name}' not registered",
                recoverable=True,
            )

        return await client.call_tool(schema.name, arguments)

    def resolve_server(self, namespaced_name: str) -> str | None:
        """Resolve which server owns a namespaced tool."""
        schema = self._tools.get(namespaced_name)
        return schema.server_name if schema else None

    async def _connect_server(self, name: str) -> None:
        """Connect a single server with error handling."""
        client = self._clients.get(name)
        status = self._status.get(name)
        if not client or not status:
            return

        status.state = ServerState.CONNECTING
        try:
            await client.connect()
            status.state = ServerState.READY
            status.tool_count = len(client.tools)
            status.error = None
            status.reconnect_attempts = 0

            # Register tools in global registry
            self._tools.update(client.tools)

        except Exception as e:
            await client.disconnect()
            status.state = ServerState.FAILED
            status.error = str(e)
            import logging as _log

            _log.getLogger(__name__).warning(
                "mcp_server_connect_failed: server=%s error=%s",
                name,
                str(e),
            )

    def _detect_collisions(self) -> None:
        """Detect namespace collisions across all servers.

        Raises NamespaceCollisionError if two different servers
        produce the same namespaced tool name.
        """
        seen: dict[str, str] = {}  # tool_name → server_name
        for name, schema in self._tools.items():
            if name in seen and seen[name] != schema.server_name:
                raise NamespaceCollisionError(
                    f"Tool '{name}' collision between servers "
                    f"'{seen[name]}' and '{schema.server_name}'"
                )
            seen[name] = schema.server_name

    async def _health_loop(self) -> None:
        """Periodic health check and reconnection loop."""
        while True:
            try:
                await asyncio.sleep(self._health_interval_s)
                await self._check_health()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    async def _check_health(self) -> None:
        """Check health of all servers and attempt reconnection for failed ones."""
        from datetime import datetime

        now = datetime.now(UTC).isoformat()

        for name, status in self._status.items():
            status.last_health_check = now

            if status.state == ServerState.READY:
                # Verify still connected
                client = self._clients[name]
                if not client.is_connected:
                    status.state = ServerState.FAILED
                    status.error = "Connection lost"

            if status.state == ServerState.FAILED:
                # Attempt reconnection
                if status.reconnect_attempts < status.max_reconnect_attempts:
                    status.reconnect_attempts += 1
                    await self._connect_server(name)

                    if self._clients[name].is_connected:
                        bus = get_event_bus()
                        await bus.emit(
                            Event(
                                type=EventType.NOTIFICATION,
                                data={
                                    "level": "info",
                                    "message": f"MCP server '{name}' reconnected",
                                },
                            )
                        )
