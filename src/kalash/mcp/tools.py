"""MCP tool adapters for the agent tool registry."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, create_model, model_validator

from kalash.mcp.schema import validate_arguments, validator_for
from kalash.tools.base import SideEffect, ToolContext, ToolEnvelope

if TYPE_CHECKING:
    from kalash.mcp.client import MCPToolSchema
    from kalash.mcp.manager import MCPManager


def _params_model(schema: dict[str, Any]) -> type[BaseModel]:
    """Validate complete arguments before Pydantic can discard or coerce fields."""
    validator = validator_for(schema or {"type": "object"})

    def validate(value: Any) -> Any:
        return validate_arguments(validator, value)

    return create_model(
        "MCPParams",
        __config__=ConfigDict(extra="allow"),
        __validators__={"external_schema": cast("Any", model_validator(mode="before")(validate))},
    )


def _infer_side_effect(_schema: MCPToolSchema) -> SideEffect:
    """Guess side-effect class from tool name and description."""
    # Names/descriptions are untrusted observations, not authority metadata.
    return SideEffect.EXEC


def _render_mcp_result(result: Any) -> str:
    """Format MCP output as clearly-labelled untrusted data."""
    if isinstance(result, str):
        body = result
    else:
        try:
            body = json.dumps(result, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(result)

    return f"--- MCP tool result (untrusted external data) ---\n{body}\n--- end MCP result ---"


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

    def dynamic_capabilities(self, _args: BaseModel) -> frozenset[str]:
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
                args.model_dump(exclude_unset=True),
            )
        except Exception as exc:
            return ToolEnvelope.fail(
                code="KALASH_MCP_TOOL_ERROR",
                message=str(exc),
                recoverable=True,
            )

        if isinstance(result, dict) and result.get("isError") is True:
            return ToolEnvelope.fail(
                code="KALASH_MCP_TOOL_ERROR", message=_render_mcp_result(result), recoverable=True
            )
        return ToolEnvelope.success(
            content=_render_mcp_result(result),
            metadata={"server": self._schema.server_name, "tool": self._schema.name},
        )
