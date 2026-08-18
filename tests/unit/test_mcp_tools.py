"""Tests for MCP tool wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kalash.mcp.client import MCPToolSchema
from kalash.mcp.tools import MCPToolAdapter, _render_mcp_result
from kalash.tools.registry import ToolRegistry
from kalash.tools.schema import tool_schema


def test_mcp_tool_uses_external_schema() -> None:
    schema = MCPToolSchema(
        name="search",
        namespaced_name="mcp__docs__search",
        description="Search docs",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        server_name="docs",
    )
    manager = MagicMock()
    tool = MCPToolAdapter(schema, manager)

    exported = tool_schema(tool)
    assert exported["name"] == "mcp__docs__search"
    assert "query" in exported["input_schema"]["properties"]


def test_render_mcp_result_wraps_untrusted_data() -> None:
    rendered = _render_mcp_result({"answer": 1})
    assert "untrusted external data" in rendered
    assert '"answer": 1' in rendered


@pytest.mark.asyncio
async def test_mcp_tool_execute_delegates_to_manager() -> None:
    schema = MCPToolSchema(
        name="ping",
        namespaced_name="mcp__srv__ping",
        description="",
        input_schema={"type": "object", "properties": {}},
        server_name="srv",
    )
    manager = MagicMock()
    manager.call_tool = AsyncMock(return_value={"ok": True})
    tool = MCPToolAdapter(schema, manager)

    from kalash.tools.base import ToolContext

    ctx = ToolContext(
        session_id="ses_test",
        run_id="ses_test",
        cwd=__import__("pathlib").Path("."),
        capabilities=frozenset({"network.fetch"}),
        allow_network=True,
    )
    envelope = await tool.execute(tool.params(), ctx)
    assert envelope.ok is True
    assert "untrusted external data" in envelope.content
    manager.call_tool.assert_awaited_once_with("mcp__srv__ping", {})


def test_mcp_side_effect_inferred_from_name() -> None:
    from kalash.tools.base import SideEffect

    read_tool = MCPToolAdapter(
        MCPToolSchema(
            name="search_docs",
            namespaced_name="mcp__docs__search_docs",
            description="Search documentation",
            server_name="docs",
        ),
        MagicMock(),
    )
    write_tool = MCPToolAdapter(
        MCPToolSchema(
            name="create_issue",
            namespaced_name="mcp__gh__create_issue",
            description="Create a GitHub issue",
            server_name="gh",
        ),
        MagicMock(),
    )
    assert read_tool.side_effect is SideEffect.READ
    assert write_tool.side_effect is SideEffect.WRITE


@pytest.mark.asyncio
async def test_wire_mcp_tools_registers_connected_tools(monkeypatch) -> None:
    from kalash.mcp import load as mcp_load

    schema = MCPToolSchema(
        name="ping",
        namespaced_name="mcp__srv__ping",
        description="",
        input_schema={"type": "object", "properties": {}},
        server_name="srv",
    )

    class FakeManager:
        async def configure(self, configs) -> None:
            self.configs = configs

        async def connect_all(self) -> dict[str, str]:
            return {"srv": "READY"}

        @property
        def all_tools(self) -> dict[str, MCPToolSchema]:
            return {schema.namespaced_name: schema}

    fake_entry = MagicMock()
    monkeypatch.setattr(
        "kalash.mcp.load.MCPRegistry.list_servers",
        lambda self: [fake_entry],
    )
    monkeypatch.setattr(
        "kalash.mcp.load._entry_to_config",
        lambda entry: MagicMock(name="cfg"),
    )
    monkeypatch.setattr(mcp_load, "MCPManager", FakeManager)

    registry = ToolRegistry()
    manager = await mcp_load.wire_mcp_tools(registry)
    assert manager is not None
    assert registry.has("mcp__srv__ping")
