"""Load MCP server configs and register tools with the agent registry."""

from __future__ import annotations

import logging
from pathlib import Path

from kalash.mcp.manager import MCPManager
from kalash.mcp.registry import MCPRegistry
from kalash.mcp.tools import MCPToolAdapter
from kalash.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


async def wire_mcp_tools(
    registry: ToolRegistry,
    *,
    cwd: Path | None = None,
) -> MCPManager | None:
    """Connect configured MCP servers and register their tools.

    Failures are non-fatal: a dead server is skipped and the agent still runs
    with built-in tools only.
    """
    mcp_registry = MCPRegistry(cwd)
    entries = mcp_registry.list_servers()
    if not entries:
        return None

    configs = []
    for entry in entries:
        if entry.scope == "project":
            from kalash.core.trust import is_trusted

            project_config = mcp_registry.cwd / ".kalash" / "settings" / "mcp.json"
            if not is_trusted(project_config):
                logger.warning(
                    "Skipping untrusted project MCP configuration; run kalash mcp trust after reviewing it"
                )
                continue
        try:
            configs.append(mcp_registry._to_config(entry))
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping MCP server %s: %s", entry.name, exc)
    manager = MCPManager()
    await manager.configure(configs)

    try:
        await manager.connect_all()
    except BaseException:
        await manager.disconnect_all()
        raise

    registered = 0
    for schema in manager.all_tools.values():
        try:
            registry.register(MCPToolAdapter(schema, manager), source="mcp")
            registered += 1
        except ValueError:
            logger.debug("skipped duplicate MCP tool %s", schema.namespaced_name)

    if registered:
        logger.info("registered %d MCP tool(s)", registered)
    return manager
