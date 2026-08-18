"""MCP tool adapters for the agent tool registry."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, create_model

from kalash.mcp.client import MCPToolSchema
from kalash.mcp.manager import MCPManager
from kalash.tools.base import SideEffect, ToolContext, ToolEnvelope

_WRITE_HINTS = re.compile(
    r"\b(create|delete|update|write|post|put|patch|send|remove|insert|set|save|publish|upload|commit|push|deploy)\b",
    re.I,
)
_READ_HINTS = re.compile(
    r"\b(read|get|list|search|fetch|query|find|describe|show|lookup|download|pull|inspect)\b",
    re.I,
)


def _params_model(schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic params model from an MCP inputSchema."""
    if not schema or schema.get("type") not in (None, "object"):
        return create_model("MCPParams", __base__=BaseModel)

    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return create_model("MCPParams", __base__=BaseModel)

    required = set(schema.get("required", []))
    fields: dict[str, tuple[Any, Any]] = {}
    for name, prop in properties.items():
        default = ... if name in required else None
        fields[name] = (Any, default)
    return create_model("MCPParams", **fields)


def _infer_side_effect(schema: MCPToolSchema) -> SideEffect:
    """Guess side-effect class from tool name and description."""
    blob = f"{schema.name} {schema.description}"
    if _WRITE_HINTS.search(blob):
        return SideEffect.WRITE
    if _READ_HINTS.search(blob):
        return SideEffect.READ
    annotations = schema.input_schema.get("x-kalash-side-effect")
    if isinstance(annotations, str):
        try:
            return SideEffect(annotations.lower())
        except ValueError:
            pass
    return SideEffect.READ


def _render_mcp_result(result: Any) -> str:
    """Format MCP output as clearly-labelled untrusted data."""
    if isinstance(result, str):
        body = result
    else:
        try:
            body = json.dumps(result, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(result)

    return (
        "--- MCP tool result (untrusted external data) ---\n"
        f"{body}\n"
        "--- end MCP result ---"
    )


class MCPToolAdapter:
    """Wrap one MCP tool so the registry and permission gate can execute it."""

    def __init__(self, schema: MCPToolSchema, manager: MCPManager) -> None:
        self._schema = schema
        self._manager = manager
        self._params = _params_model(schema.input_schema)
        self._side_effect = _infer_side_effect(schema)

    @property
    def name(self) -> str:
        return self._schema.namespaced_name

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        server = self._schema.server_name
        detail = self._schema.description.strip()
        prefix = f"[MCP:{server}] "
        if detail:
            return f"{prefix}{detail}"
        return f"{prefix}Tool from MCP server {server!r}."

    @property
    def external_input_schema(self) -> dict[str, Any]:
        """Provider-facing schema from the MCP server."""
        return dict(self._schema.input_schema or {"type": "object", "properties": {}})

    @property
    def params(self) -> type[BaseModel]:
        return self._params

    @property
    def side_effect(self) -> SideEffect:
        return self._side_effect

    @property
    def capabilities(self) -> frozenset[str]:
        if self._side_effect is SideEffect.READ:
            return frozenset({"network.fetch"})
        return frozenset({"network.fetch", "network.write"})

    @property
    def timeout_s(self) -> float:
        return 60.0

    @property
    def max_output_bytes(self) -> int:
        return 256_000

    @property
    def idempotent(self) -> bool:
        return self._side_effect is SideEffect.READ

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return self.capabilities

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        if "network.fetch" not in ctx.capabilities and not ctx.allow_network:
            return ToolEnvelope.fail(
                code="KALASH_CAPABILITY_DENIED",
                message=f"{self.name} requires network access",
                recoverable=True,
                remediation="Grant network access or switch sandbox mode.",
            )

        try:
            result = await self._manager.call_tool(
                self._schema.namespaced_name,
                args.model_dump(exclude_none=True),
            )
        except Exception as exc:
            return ToolEnvelope.fail(
                code="KALASH_MCP_TOOL_ERROR",
                message=str(exc),
                recoverable=True,
            )

        return ToolEnvelope.success(
            content=_render_mcp_result(result),
            metadata={"server": self._schema.server_name, "tool": self._schema.name},
        )
