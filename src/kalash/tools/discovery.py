"""Provider-independent discovery of extension tools without executing them."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalash.tools.base import SideEffect, ToolContext, ToolEnvelope


class ToolSearchParams(BaseModel):
    query: str = Field(default="", max_length=500, description="Words in a tool name or purpose")
    names: list[str] = Field(default_factory=list, max_length=5, description="Exact names to load")
    limit: int = Field(default=5, ge=1, le=5)
    offset: int = Field(default=0, ge=0, description="Catalog offset when browsing without a query")


class ToolSearchTool:
    name = "tool_search"
    version = "1.0.0"
    description = (
        "Discover permitted plugin/MCP tools. A query or exact names loads up to five schemas "
        "for the next request; this never executes the tools. Omit both to browse metadata. "
        "Use when core tools cannot access an external app or service."
    )
    params = ToolSearchParams
    side_effect = SideEffect.READ
    capabilities: frozenset[str] = frozenset()
    timeout_s = 10.0
    max_output_bytes = 32_768
    idempotent = True
    cancellable = False

    def dynamic_capabilities(self, _args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ToolSearchParams)
        if ctx.discover_tools is None:
            return ToolEnvelope.fail("KALASH_TOOL_ERROR", "No extension catalog in this context")
        return ctx.discover_tools(**args.model_dump())
