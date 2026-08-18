"""Load MCP server configs and register tools with the agent registry."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kalash.mcp.client import MCPServerConfig
from kalash.mcp.manager import MCPManager
from kalash.mcp.registry import MCPRegistry
from kalash.mcp.tools import MCPToolAdapter
from kalash.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _entry_to_config(entry: object) -> MCPServerConfig:
    registry = MCPRegistry()
    return registry._to_config(entry)  # noqa: SLF001 — shared conversion logic


async def wire_mcp_tools(
    registry: ToolRegistry,
    *,
    cwd: Path | None = None,
) -> MCPManager | None:
    """Connect configured MCP servers and register their tools.

    Failures are non-fatal: a dead server is skipped and the agent still runs
    with built-in tools only.
    """
    _ = cwd  # reserved for future project-scoped config overrides
    mcp_registry = MCPRegistry()
    entries = mcp_registry.list_servers()
    if not entries:
        return None

    configs = [_entry_to_config(entry) for entry in entries]
    manager = MCPManager()
    await manager.configure(configs)

    try:
        await manager.connect_all()
    except Exception:
        logger.debug("mcp connect_all failed", exc_info=True)

    registered = 0
    for schema in manager.all_tools.values():
        try:
            registry.register(MCPToolAdapter(schema, manager), source="mcp")
            registered += 1
        except ValueError:
            logger.debug("skipped duplicate MCP tool %s", schema.namespaced_name)

    if registered:
        logger.info("registered %d MCP tool(s)", registered)
    return manager if registered else None


def wire_mcp_tools_sync(
    registry: ToolRegistry,
    *,
    cwd: Path | None = None,
) -> MCPManager | None:
    """Sync wrapper for callers that are not already inside an event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wire_mcp_tools(registry, cwd=cwd))

    # Inside a running loop: schedule wiring; caller should also await
    # wire_agent_tools() before the first turn when MCP tools are required.
    loop.create_task(wire_mcp_tools(registry, cwd=cwd))
    logger.debug("scheduled async MCP wiring inside running event loop")
    return None
