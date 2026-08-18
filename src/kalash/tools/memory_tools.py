"""Memory tools: recall, remember, forget.

- RecallTool: explicit memory query (semantic search)
- RememberTool: explicit memory write (queued, non-blocking)
- ForgetTool: targeted deletion by ID or content
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kalash.tools.base import (
    SideEffect,
    ToolContext,
    ToolEnvelope,
)


# ---------------------------------------------------------------------------
# RecallTool
# ---------------------------------------------------------------------------


class RecallParams(BaseModel):
    """Parameters for memory recall."""

    query: str = Field(description="Natural language query to search memories")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum memories to return")
    scope: str = Field(
        default="all",
        description="Scope filter: 'all', 'session', 'project', 'user'",
    )


class RecallTool:
    """Query stored memories via semantic search."""

    @property
    def name(self) -> str:
        return "recall"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Search stored memories using natural language. Returns relevant past context."

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

        # Memory provider is injected at runtime via the orchestration layer.
        # This is the tool interface — actual recall delegates to memory.provider.
        try:
            from kalash.memory.protocol import RecallQuery, MemoryProvider
        except ImportError:
            pass

        # Placeholder: the orchestration layer injects the actual memory backend.
        # This tool exposes the interface; the registry wires it to the provider.
        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message="Memory provider not initialized.",
            recoverable=True,
            remediation="Memory system initializes during session setup.",
        )


# ---------------------------------------------------------------------------
# RememberTool
# ---------------------------------------------------------------------------


class RememberParams(BaseModel):
    """Parameters for storing a memory."""

    content: str = Field(description="Content to remember")
    scope: str = Field(
        default="project",
        description="Storage scope: 'session', 'project', or 'user'",
    )
    tags: list[str] = Field(default_factory=list, description="Optional tags for organization")


class RememberTool:
    """Store a memory (queued, non-blocking write)."""

    @property
    def name(self) -> str:
        return "remember"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Store information in memory for future recall. Queued and non-blocking."

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

        # The orchestration layer handles the actual write via memory provider.
        # This tool queues the write (non-blocking) and returns immediately.
        # In the real implementation, this would:
        # 1. Validate content
        # 2. Queue a MemoryWrite to the pipeline
        # 3. Return a receipt ID

        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message="Memory provider not initialized.",
            recoverable=True,
            remediation="Memory system initializes during session setup.",
        )


# ---------------------------------------------------------------------------
# ForgetTool
# ---------------------------------------------------------------------------


class ForgetParams(BaseModel):
    """Parameters for targeted memory deletion."""

    memory_id: str = Field(default="", description="Specific memory ID to delete")
    content_match: str = Field(
        default="", description="Delete memories matching this content (substring)"
    )
    scope: str = Field(
        default="project",
        description="Scope to delete from: 'session', 'project', or 'user'",
    )


class ForgetTool:
    """Targeted memory deletion by ID or content match."""

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
        return True  # Deleting same thing twice is safe

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
                message="Provide either memory_id or content_match.",
                recoverable=True,
            )

        # The orchestration layer handles actual deletion via memory provider.
        return ToolEnvelope.fail(
            code="KALASH_TOOL_ERROR",
            message="Memory provider not initialized.",
            recoverable=True,
            remediation="Memory system initializes during session setup.",
        )
