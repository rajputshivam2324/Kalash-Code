"""Shared agent startup hooks used by CLI, TUI, SDK, and scheduler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kalash.runtime.agent import Agent

logger = logging.getLogger(__name__)


async def wire_agent_tools(agent: Agent) -> None:
    """Connect MCP servers and register their tools. Failures are non-fatal."""
    try:
        from kalash.mcp.load import wire_mcp_tools

        await wire_mcp_tools(agent.host.registry, cwd=agent.cwd)
    except Exception:
        logger.debug("MCP tool wiring skipped", exc_info=True)


async def prepare_agent(agent: Agent) -> None:
    """Post-build setup every entry point should run before the first turn."""
    from kalash.core.logging import configure_logging

    configure_logging()
    await wire_agent_tools(agent)
