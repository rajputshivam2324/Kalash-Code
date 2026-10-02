"""One isolated child runtime, with inherited permissions and charged usage."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalash.core.budget import BudgetState, Usage
from kalash.core.config import BudgetConfig, KalashConfig, MemoryConfig, load_config
from kalash.runtime.loop import TerminationReason
from kalash.runtime.toolhost import ToolHost


async def run_isolated(
    prompt: str,
    *,
    cwd: Path | None = None,
    model_id: str | None = None,
    mode: str = "build",
    max_turns: int = 25,
    max_tokens: int | None = None,
    spawn_depth: int = 0,
    max_spawn_depth: int = 3,
    context_files: tuple[str, ...] = (),
    agent_instructions: str | None = None,
    allowed_tools: frozenset[str] | None = None,
    parent_budget: BudgetState | None = None,
    parent_host: ToolHost | None = None,
    config: KalashConfig | None = None,
    provider: Any | None = None,
    timeout_s: float = 300,
    capabilities: frozenset[str] | None = None,
    extensions: bool = True,
) -> dict[str, Any]:
    from kalash.runtime.agent import build_agent

    base = (cwd or Path.cwd()).resolve()
    settings = config or load_config(base)
    if spawn_depth >= max_spawn_depth:
        return {"status": "failed", "error": "Spawn depth exhausted", "output": ""}
    token_limit = max_tokens if max_tokens is not None else settings.budget.max_tokens
    tokens = min(token_limit, parent_budget.remaining_tokens()) if parent_budget else token_limit
    cost = parent_budget.remaining_cost() if parent_budget else settings.budget.max_cost_usd
    if tokens <= 0 or cost <= 0:
        return {"status": "failed", "error": "Insufficient parent budget", "output": ""}
    if parent_budget and not parent_budget.reserve_for_child(tokens, cost):
        return {"status": "failed", "error": "Insufficient parent budget", "output": ""}
    child = None
    try:
        if parent_host:
            inherited = frozenset(tool.name for tool in parent_host.registry.list_tools())
            allowed_tools = inherited if allowed_tools is None else allowed_tools & inherited
            mode = "plan" if parent_host.mode == "plan" or mode == "plan" else "build"
        child_config = replace(
            settings,
            project_dir=base,
            memory=MemoryConfig(enabled=False),
            budget=BudgetConfig(
                max_tokens=tokens,
                max_cost_usd=cost,
                max_wallclock_s=max(1, int(timeout_s)),
                max_turns=min(max_turns, parent_budget.max_turns - parent_budget.turns_used)
                if parent_budget
                else max_turns,
                max_tool_calls=parent_budget.max_tool_calls - parent_budget.tool_calls_used
                if parent_budget
                else settings.budget.max_tool_calls,
                max_spawn_depth=max_spawn_depth,
            ),
        )
        child, reason = build_agent(
            cwd=base,
            model_id=model_id,
            provider=provider,
            provider_id=parent_host.provider_id if parent_host else None,
            mode=mode,
            config=child_config,
            interactive=False,
            persist=False,
            max_iterations=max_turns,
            allowed_tools=allowed_tools,
            extensions=extensions,
        )
        if child is None:
            return {"status": "failed", "error": reason, "output": "", "usage": Usage()}
        child.host.spawn_depth = spawn_depth + 1
        child.host.max_spawn_depth = max_spawn_depth
        if parent_host:
            child.host.sandbox_mode = parent_host.sandbox_mode
            child.host.workspace_only = parent_host.workspace_only
            child.host.network_enabled = parent_host.network_enabled
            child.host.capability_limit = (
                parent_host.capabilities
                if capabilities is None
                else capabilities & parent_host.capabilities
            )
            child.host.approval = parent_host.approval
            child.host.policy = parent_host.policy
        if agent_instructions:
            child.loop.system_prompt += "\n\n# Delegated role\n" + agent_instructions
        if context_files:
            prompt += "\n\nRelevant paths (read before using):\n" + "\n".join(context_files)
        result = await child.send(prompt)
        completed = (
            result.termination_reason == TerminationReason.NO_TOOL_CALLS and not result.error
        )
        return {
            "output": result.final_response[-8000:],
            "status": "completed" if completed else "failed",
            "error": result.error or (None if completed else result.termination_reason.value),
            "usage": child.loop.gateway.total_usage,
            "cost": child.budget.cost_used,
            "turns": result.iterations,
            "termination": result.termination_reason.value,
        }
    finally:
        if parent_budget:
            if child:
                parent_budget.turns_used += child.budget.turns_used
                parent_budget.tool_calls_used += child.budget.tool_calls_used
            parent_budget.release_reservation(
                tokens,
                cost,
                child.budget.tokens_used if child else 0,
                child.budget.cost_used if child else Decimal(0),
            )
        if child:
            # The parent owns a shared provider; close only the child's resources.
            from kalash.tools.shell import terminate_session_backgrounds

            await terminate_session_backgrounds(child.session_id)
            if child._mcp_manager is not None:
                await child._mcp_manager.disconnect_all()
            if provider is None:
                close = getattr(child.loop.gateway.primary, "close", None)
                if close is not None:
                    await close()
