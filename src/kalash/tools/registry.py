"""Tool registry and dispatch.

Name resolution priority: built-in > plugin > MCP.
The registry is the single point of tool discovery and invocation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kalash.tools.base import (
    Tool,
    ToolContext,
    ToolEnvelope,
    tool_json_schema,
)

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry for all available tools.

    Tools are registered with a source tag (builtin, plugin, mcp)
    which determines priority during name resolution.
    """

    _SOURCE_PRIORITY = {"builtin": 0, "plugin": 1, "mcp": 2}

    def __init__(self) -> None:
        # Maps tool name -> list of (source, tool) ordered by registration time.
        self._tools: dict[str, list[tuple[str, Tool]]] = {}

    def register(self, tool: Tool, *, source: str = "builtin") -> None:
        """Register a tool with a source tag.

        Args:
            tool: A Tool protocol implementor.
            source: One of 'builtin', 'plugin', 'mcp'.

        Raises:
            ValueError: If source is unrecognized.
        """
        if source not in self._SOURCE_PRIORITY:
            raise ValueError(
                f"Unknown source '{source}'; expected one of {list(self._SOURCE_PRIORITY)}"
            )

        entries = self._tools.setdefault(tool.name, [])
        entries.append((source, tool))
        # Keep sorted by priority (lowest number = highest priority)
        entries.sort(key=lambda e: self._SOURCE_PRIORITY[e[0]])
        logger.debug("Registered tool '%s' from source '%s'", tool.name, source)

    def get(self, name: str) -> Tool:
        """Resolve a tool by name.

        Resolution order: built-in > plugin > MCP.

        Args:
            name: The tool name to look up.

        Returns:
            The highest-priority tool registered under that name.

        Raises:
            KeyError: If no tool is registered with that name.
        """
        entries = self._tools.get(name)
        if not entries:
            raise KeyError(f"Unknown tool: '{name}'")
        # First entry has highest priority due to sorting
        return entries[0][1]

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def list_tools(self, *, source: str | None = None) -> list[Tool]:
        """List all registered tools, optionally filtered by source.

        Args:
            source: If provided, only return tools from this source.

        Returns:
            List of resolved Tool instances.
        """
        results: list[Tool] = []
        for entries in self._tools.values():
            for src, tool in entries:
                if source is None or src == source:
                    results.append(tool)
                    break  # Only include highest-priority per name
        return results

    def list_schemas(self, *, source: str | None = None) -> list[dict[str, Any]]:
        """Generate JSON schemas for all tools (for LLM function calling).

        Args:
            source: If provided, only include tools from this source.
        """
        return [tool_json_schema(t) for t in self.list_tools(source=source)]

    def unregister(self, name: str, *, source: str | None = None) -> bool:
        """Remove a tool registration.

        Args:
            name: Tool name.
            source: If provided, only remove from this source.

        Returns:
            True if a tool was removed, False otherwise.
        """
        entries = self._tools.get(name)
        if not entries:
            return False

        if source is None:
            del self._tools[name]
            return True

        before = len(entries)
        self._tools[name] = [(s, t) for s, t in entries if s != source]
        if not self._tools[name]:
            del self._tools[name]
        return len(self._tools.get(name, [])) < before

    async def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolEnvelope:
        """Resolve and execute a tool by name.

        Never raises — returns a ToolEnvelope with error on failure.

        Args:
            name: Tool name.
            args: Raw arguments dict (will be validated against tool's params model).
            ctx: Execution context.
        """
        try:
            tool = self.get(name)
        except KeyError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_UNKNOWN",
                message=f"Unknown tool: '{name}'",
                recoverable=True,
                remediation="Check available tools with list_tools.",
            )

        # Validate arguments
        try:
            validated = tool.params.model_validate(args)
        except Exception as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message=f"Invalid arguments for tool '{name}': {exc}",
                recoverable=True,
                remediation="Check the tool's parameter schema.",
            )

        missing = tool.capabilities - ctx.capabilities
        if missing:
            return ToolEnvelope.fail(
                code="KALASH_CAPABILITY_DENIED",
                message=f"{name} requires unavailable capabilities: {', '.join(sorted(missing))}",
                recoverable=True,
            )

        # Execute
        try:
            return await asyncio.wait_for(tool.execute(validated, ctx), timeout=tool.timeout_s)
        except TimeoutError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_TIMEOUT",
                message=f"{name} exceeded {tool.timeout_s}s",
                recoverable=True,
            )
        except Exception as exc:
            logger.exception("Unhandled exception in tool '%s'", name)
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Tool '{name}' raised unexpectedly: {exc}",
                recoverable=False,
            )
