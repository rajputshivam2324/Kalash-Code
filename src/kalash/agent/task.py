"""The ``task`` tool: delegate a focused job to a fresh subagent with its own context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from kalash.llm.types import Usage
from kalash.tools.base import Params, Tool, ToolContext, ToolResult
from kalash.tools.output import limit_output

KINDS = {
    "general": "Full tool access (except delegating again). Use for self-contained implementation "
    "or multi-step investigation.",
    "explore": "Read-only. Use to search the codebase and answer questions without changing "
    "anything.",
}


@dataclass
class SubagentOutcome:
    text: str
    reason: str
    usage: Usage = field(default_factory=Usage)
    cost: float | None = None
    error: str | None = None


Spawner = Callable[[str, str, str, Callable[[str], None]], Awaitable[SubagentOutcome]]


class TaskParams(Params):
    description: str = Field(description="A short (3-6 word) label for the job.")
    prompt: str = Field(
        description="The complete task. The subagent sees nothing of this conversation, so "
        "include every path, constraint and detail it needs, and say what to report back."
    )
    agent: Literal["general", "explore"] = Field(
        default="general",
        description="; ".join(f"{k}: {v}" for k, v in KINDS.items()),
    )


class TaskTool(Tool):
    name = "task"
    description = (
        "Delegate a self-contained job to a subagent that works in its own fresh context and "
        "returns one final report. Use it for research or exploration that would otherwise "
        "fill your context, and start independent tasks together in one message. The subagent "
        "cannot ask you questions and shares your permissions."
    )
    params = TaskParams
    concurrent_safe = True

    def __init__(self, spawn: Spawner) -> None:
        self._spawn = spawn

    def permission_key(self, params: Any, ctx: ToolContext) -> str:
        return f"task {params.agent} {params.description}"

    async def run(self, params: TaskParams, ctx: ToolContext) -> ToolResult:
        outcome = await self._spawn(
            params.agent, params.prompt, params.description, ctx.emit_output
        )
        sink = ctx.extras.get("usage_sink")
        if sink is not None:
            sink(outcome.usage, outcome.cost, f"subagent: {params.description}")
        if outcome.reason != "completed":
            detail = outcome.error or outcome.reason
            partial = f"\nPartial report:\n{outcome.text}" if outcome.text else ""
            return ToolResult(
                f"The subagent did not finish ({outcome.reason}: {detail}).{partial}",
                is_error=True,
            )
        body = outcome.text or "(the subagent returned no text)"
        return ToolResult(limit_output(body, ctx.spill_dir, "task"))
