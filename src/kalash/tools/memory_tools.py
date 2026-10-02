"""Memory tools: recall, remember, forget — wired to the local provider."""

from __future__ import annotations

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
)


class RecallParams(BaseModel):
    query: str = Field(description="Natural language query to search memories")
    limit: int = Field(default=10, ge=1, le=50)
    scope: str = Field(default="all", description="all, session, project, or user")


class RecallTool:
    """Query stored memories via keyword search."""

    @property
    def name(self) -> str:
        return "recall"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Search stored memories using natural language."

    @property
    def params(self) -> type[BaseModel]:
        return RecallParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.READ

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory.read"})

    @property
    def timeout_s(self) -> float:
        return 10.0

    @property
    def max_output_bytes(self) -> int:
        return 65536

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, RecallParams)
        from kalash.memory.session import get_session_memory

        mem = get_session_memory(ctx.session_id, ctx.cwd, config=ctx.config)
        hits = await mem.recall_tool(args.query, limit=args.limit, scope=args.scope)
        if not hits:
            return ToolEnvelope.success(content="No matching memories found.")
        lines = [f"- [{h['id']}] (score {h['score']:.2f}) {h['content'][:300]}" for h in hits]
        return ToolEnvelope.success(content="\n".join(lines), metadata={"count": len(hits)})


class RememberParams(BaseModel):
    content: str = Field(description="Content to remember")
    scope: str = Field(default="project", description="session, project, or user")
    tags: list[str] = Field(default_factory=list)


class RememberTool:
    """Store a memory for future recall."""

    @property
    def name(self) -> str:
        return "remember"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Store information in memory for future recall."

    @property
    def params(self) -> type[BaseModel]:
        return RememberParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.WRITE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory.write"})

    @property
    def timeout_s(self) -> float:
        return 5.0

    @property
    def max_output_bytes(self) -> int:
        return 1024

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, RememberParams)
        if not args.content.strip():
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message="Content cannot be empty.",
                recoverable=True,
            )
        from kalash.memory.session import get_session_memory

        mem = get_session_memory(ctx.session_id, ctx.cwd, config=ctx.config)
        mem_id = await mem.remember(
            args.content.strip(),
            scope=args.scope,
            tags=args.tags,
        )
        return ToolEnvelope.success(
            content=f"Stored memory {mem_id}",
            metadata={"memory_id": mem_id},
        )


class ForgetParams(BaseModel):
    memory_id: str = Field(default="", description="Specific memory ID to delete")
    content_match: str = Field(default="", description="Delete memories matching this text")
    scope: str = Field(default="project")


class ForgetTool:
    """Delete memories by ID or content match."""

    @property
    def name(self) -> str:
        return "forget"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Delete specific memories by ID or content match."

    @property
    def params(self) -> type[BaseModel]:
        return ForgetParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.WRITE

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory.write"})

    @property
    def timeout_s(self) -> float:
        return 10.0

    @property
    def max_output_bytes(self) -> int:
        return 1024

    @property
    def idempotent(self) -> bool:
        return True

    @property
    def cancellable(self) -> bool:
        return False

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        return frozenset()

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, ForgetParams)
        if not args.memory_id and not args.content_match:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_INVALID_ARGS",
                message="Provide memory_id or content_match.",
                recoverable=True,
            )
        from kalash.memory.session import get_session_memory

        mem = get_session_memory(ctx.session_id, ctx.cwd, config=ctx.config)
        count = await mem.forget(
            memory_id=args.memory_id,
            content_match=args.content_match,
        )
        if count == 0:
            return ToolEnvelope.success(content="No memories matched.")
        return ToolEnvelope.success(content=f"Forgot {count} memory record(s).")
