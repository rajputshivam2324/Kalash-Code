"""Task tool: spawn isolated-context subagent.

Creates a child run with its own context window, executes the delegated
task, and returns only the structured result to the parent.
"""

from __future__ import annotations

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

    agent: str | None = Field(default=None, description="Optional named .kalash/agents definition")
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
        return 1800.0

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
            if not p.resolve().is_relative_to(ctx.cwd.resolve()) or not p.is_file():
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message=f"Context file not found: {file_path}",
                    recoverable=True,
                )

        # Depth is carried in the context and checked here rather than in a
        # global, so a child that spawns a grandchild is bounded on the way down.
        if not ctx.can_spawn:
            return ToolEnvelope.fail(
                code="KALASH_SUBAGENT_DEPTH_EXCEEDED",
                message=(
                    f"Spawn depth limit reached ({ctx.max_spawn_depth}). "
                    f"Do this work directly instead of delegating further."
                ),
                recoverable=True,
            )

        import asyncio

        if ctx.delegate is None:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR", message="Delegation is unavailable in this tool context"
            )
        requested_caps = requested_caps or ctx.capabilities
        child_instructions = None
        allowed_tools = None
        child_turns = args.max_turns
        child_mode = "build"
        if args.agent:
            from kalash.agents.loader import load_agent_definition

            definition = load_agent_definition(args.agent, ctx.cwd)
            if definition is None:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR", message=f"Unknown agent {args.agent!r}"
                )
            if definition.model:
                return ToolEnvelope.fail(
                    code="KALASH_TOOL_ERROR",
                    message="A delegated agent must inherit its parent's provider; use agents run for a model override",
                )
            child_instructions = definition.body
            allowed_tools = frozenset(definition.tools) if definition.tools else None
            child_turns = min(child_turns, definition.max_turns)
            child_mode = definition.mode

        try:
            outcome = await asyncio.wait_for(
                ctx.delegate(
                    prompt=args.prompt,
                    max_turns=child_turns,
                    timeout_s=args.timeout_s,
                    capabilities=requested_caps,
                    context_files=tuple(args.context_files),
                    agent_instructions=child_instructions,
                    allowed_tools=allowed_tools,
                    mode=child_mode,
                ),
                timeout=args.timeout_s,
            )
        except TimeoutError:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_TIMEOUT",
                message=f"Subagent exceeded its {args.timeout_s}s budget.",
                recoverable=True,
                remediation="Narrow the brief, or raise timeout_s.",
            )
        except Exception as exc:
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Subagent failed: {exc}",
                recoverable=True,
            )

        if outcome.get("status") != "completed":
            return ToolEnvelope.fail(
                code="KALASH_TOOL_ERROR",
                message=f"Subagent did not complete: {outcome.get('error') or 'unknown'}",
                recoverable=True,
            )

        output = str(outcome.get("output") or "").strip()
        usage = outcome.get("usage")
        child_tokens = getattr(usage, "total_tokens", 0)

        return ToolEnvelope.success(
            content=output or "(the subagent returned no findings)",
            metadata={
                "task_id": task_id,
                "turns": outcome.get("turns", 0),
                "child_tokens": child_tokens,
                "termination": outcome.get("termination", ""),
                "status": "completed",
            },
            side_effects=(SideEffectRecord(kind="executed", path=f"task:{task_id}"),),
        )
