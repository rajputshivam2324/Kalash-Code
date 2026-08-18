"""Task tool: spawn isolated-context subagent.

Creates a child run with its own context window, executes the delegated
task, and returns only the structured result to the parent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kalash.core.ids import generate_id
from kalash.tools.base import (
    SideEffect,
    SideEffectRecord,
    ToolContext,
    ToolEnvelope,
)


# ---------------------------------------------------------------------------
# TaskTool
# ---------------------------------------------------------------------------


class TaskParams(BaseModel):
    """Parameters for spawning a subagent task."""

    prompt: str = Field(description="Task instruction for the subagent")
    context_files: list[str] = Field(
        default_factory=list,
        description="File paths to include in the subagent's initial context",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Capabilities to grant the subagent (subset of parent's)",
    )
    timeout_s: float = Field(
        default=300.0,
        gt=0,
        le=1800,
        description="Maximum time for the subagent to complete",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum conversation turns for the subagent",
    )


class TaskResult(BaseModel):
    """Structured result from a subagent execution."""

    task_id: str = Field(description="Unique identifier for this task run")
    success: bool = Field(description="Whether the task completed successfully")
    response: str = Field(default="", description="Subagent's final response")
    files_modified: list[str] = Field(
        default_factory=list, description="Files modified by the subagent"
    )
    error: str = Field(default="", description="Error message if failed")
    turns_used: int = Field(default=0, description="Number of turns consumed")


class TaskTool:
    """Spawn an isolated-context child run (subagent).

    The subagent operates with:
    - Its own context window (no shared mutable state)
    - A subset of the parent's capabilities
    - Its own budget allocation
    - Only the structured TaskResult is returned to the parent
    """

    @property
    def name(self) -> str:
        return "task"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return (
            "Spawn an isolated subagent to handle a delegated task. "
            "Returns only the structured result — no context leakage."
        )

    @property
    def params(self) -> type[BaseModel]:
        return TaskParams

    @property
    def side_effect(self) -> SideEffect:
        return SideEffect.EXEC

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"task.spawn"})

    @property
    def timeout_s(self) -> float:
        return 300.0

    @property
    def max_output_bytes(self) -> int:
        return 262_144  # 256 KiB

    @property
    def idempotent(self) -> bool:
        return False

    @property
    def cancellable(self) -> bool:
        return True

    def dynamic_capabilities(self, args: BaseModel) -> frozenset[str]:
        """Subagent inherits subset of capabilities — computed at dispatch time."""
        assert isinstance(args, TaskParams)
        return frozenset(args.capabilities)

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolEnvelope:
        assert isinstance(args, TaskParams)

        task_id = generate_id("tsk_")

        # Validate requested capabilities are subset of parent's
        requested_caps = frozenset(args.capabilities)
        if not requested_caps.issubset(ctx.capabilities):
            denied = requested_caps - ctx.capabilities
            return ToolEnvelope.fail(
                code="KALASH_PERMISSION_DENIED",
                message=f"Subagent requested capabilities not available to parent: {denied}",
                recoverable=True,
                remediation="Only capabilities the parent holds can be delegated.",
            )

        # Validate context files exist
        from pathlib import Path

        for file_path in args.context_files:
            p = Path(file_path)
            if not p.is_absolute():
                p = ctx.cwd / p
            if not p.exists():
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message=f"Context file not found: {file_path}",
                    recoverable=True,
                )

        # The actual subagent execution is handled by the orchestration layer.
        # This tool validates inputs and returns a handle; the orchestrator
        # creates the child run, manages its lifecycle, and captures results.
        #
        # In production, this would:
        # 1. Create a new Run with isolated context
        # 2. Inject context_files into the child's context
        # 3. Set capability/budget boundaries
        # 4. Execute the child run asynchronously
        # 5. Await completion or timeout
        # 6. Return TaskResult

        # Placeholder response indicating the orchestration layer should handle this
        return ToolEnvelope.success(
            content=f"Task {task_id} queued for execution.",
            metadata={
                "task_id": task_id,
                "prompt_preview": args.prompt[:100],
                "context_files": args.context_files,
                "capabilities": list(requested_caps),
                "timeout_s": args.timeout_s,
                "max_turns": args.max_turns,
                "status": "queued",
            },
            side_effects=(
                SideEffectRecord(kind="executed", path=f"task:{task_id}"),
            ),
        )
