"""Isolated-context child runs (I-028).

Parent never sees child transcript. Only the structured result
envelope is returned. Ancestor chain validated for cycles (I-027).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kalash.core.budget import BudgetState, Usage, Pricing
from kalash.core.errors import BudgetError, KalashError, StateError
from kalash.core.events import Event, EventType, get_event_bus
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine


class CycleDetectedError(KalashError):
    """Ancestor chain contains a cycle (I-027)."""

    code = "KALASH_SUBAGENT_CYCLE"


class DepthLimitExceededError(KalashError):
    """Spawn depth exceeds the configured ceiling."""

    code = "KALASH_SUBAGENT_DEPTH_EXCEEDED"


@dataclass
class ResultEnvelope:
    """Structured result from a child run — the only data parent sees."""

    run_id: str
    status: str  # "completed" | "failed" | "budget_exceeded" | "cancelled"
    result: Any = None
    error: str | None = None
    usage: Usage | None = None
    duration_ms: int | None = None


@dataclass
class SubagentConfig:
    """Configuration for spawning a subagent."""

    agent: str = "default"
    prompt: str = ""
    context_files: list[str] = field(default_factory=list)
    max_tokens: int = 100_000
    max_turns: int = 25
    max_wallclock_s: int = 300
    tools: list[str] | None = None  # None = inherit parent's tools


class SubagentRunner:
    """Orchestrates isolated child agent runs."""

    def __init__(self, engine: StorageEngine, max_depth: int = 3) -> None:
        self._engine = engine
        self._max_depth = max_depth
        self._active_runs: dict[str, asyncio.Task[ResultEnvelope]] = {}

    async def spawn(
        self,
        parent_run_id: str,
        parent_budget: BudgetState,
        config: SubagentConfig,
    ) -> ResultEnvelope:
        """Spawn an isolated child run.

        - Validates ancestor chain for cycles (I-027).
        - Enforces depth limit.
        - Reserves budget from parent.
        - Returns only the structured result envelope.
        """
        # Validate depth
        depth = await self._get_depth(parent_run_id)
        if depth >= self._max_depth:
            raise DepthLimitExceededError(
                f"Spawn depth {depth + 1} exceeds limit {self._max_depth}",
                recoverable=True,
            )

        # Validate no cycles in ancestor chain (I-027)
        ancestors = await self._get_ancestor_chain(parent_run_id)
        # A cycle would mean the same agent+prompt combo already exists upstream
        agent_key = f"{config.agent}:{config.prompt[:128]}"
        for ancestor in ancestors:
            if ancestor.get("agent_key") == agent_key:
                raise CycleDetectedError(
                    f"Cycle detected: agent '{config.agent}' already in ancestor chain",
                    recoverable=True,
                )

        # Reserve budget from parent
        from decimal import Decimal

        reserve_cost = Decimal("0.50")  # default reservation
        if not parent_budget.reserve_for_child(config.max_tokens, reserve_cost):
            raise BudgetError(
                "Insufficient parent budget for child reservation",
                recoverable=True,
            )

        # Create child run record
        child_run_id = generate_id("run_")
        now = datetime.now(timezone.utc).isoformat()

        await self._engine.execute_write(
            """INSERT INTO agent_runs
               (id, parent_run_id, agent, state, depth, agent_key, created_at)
               VALUES (?, ?, ?, 'RUNNING', ?, ?, ?)""",
            (child_run_id, parent_run_id, config.agent, depth + 1, agent_key, now),
        )

        # Emit spawn event
        bus = get_event_bus()
        await bus.emit(Event(
            type=EventType.AGENT_SPAWN,
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_run_id,
                "agent": config.agent,
                "depth": depth + 1,
            },
            run_id=parent_run_id,
        ))

        # Execute child in isolation
        start_time = asyncio.get_event_loop().time()
        try:
            result = await self._execute_child(child_run_id, config, parent_budget)
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            envelope = ResultEnvelope(
                run_id=child_run_id,
                status="completed",
                result=result.get("output"),
                usage=result.get("usage"),
                duration_ms=elapsed_ms,
            )
        except BudgetError as e:
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            envelope = ResultEnvelope(
                run_id=child_run_id,
                status="budget_exceeded",
                error=str(e),
                duration_ms=elapsed_ms,
            )
        except asyncio.CancelledError:
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            envelope = ResultEnvelope(
                run_id=child_run_id,
                status="cancelled",
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            envelope = ResultEnvelope(
                run_id=child_run_id,
                status="failed",
                error=str(e),
                duration_ms=elapsed_ms,
            )
        finally:
            # Release reservation, debit actuals
            actual_tokens = envelope.usage.total_tokens if envelope.usage else 0
            actual_cost = Decimal("0")
            parent_budget.release_reservation(
                config.max_tokens, reserve_cost, actual_tokens, actual_cost
            )

        # Update run record
        await self._engine.execute_write(
            """UPDATE agent_runs SET state = ?, finished_at = ?, duration_ms = ?
               WHERE id = ?""",
            (envelope.status.upper(), datetime.now(timezone.utc).isoformat(),
             envelope.duration_ms, child_run_id),
        )

        # Emit completion event
        await bus.emit(Event(
            type=EventType.AGENT_COMPLETE,
            data={
                "child_run_id": child_run_id,
                "parent_run_id": parent_run_id,
                "status": envelope.status,
            },
            run_id=parent_run_id,
        ))

        return envelope

    async def cancel(self, run_id: str) -> None:
        """Cancel an active child run."""
        task = self._active_runs.get(run_id)
        if task and not task.done():
            task.cancel()

    async def _execute_child(
        self,
        child_run_id: str,
        config: SubagentConfig,
        parent_budget: BudgetState,
    ) -> dict[str, Any]:
        """Execute child run in isolated context.

        The child has its own context window — parent never sees
        the child's transcript (I-028).
        """
        # Child budget: min(requested, parent_remaining - outstanding)
        child_budget = BudgetState(
            max_tokens=min(config.max_tokens, parent_budget.remaining_tokens()),
            max_turns=config.max_turns,
            max_wallclock_s=config.max_wallclock_s,
        )

        # Subagent execution needs the agent loop, which is not wired yet.
        # Raising rather than returning an empty result is deliberate: a
        # success-shaped return made spawn() report status="completed" with a
        # None result, so callers could not tell a finished run from an
        # unimplemented one.
        raise NotImplementedError(
            "subagent execution requires the agent loop; "
            f"child budget would be {child_budget.max_tokens} tokens / "
            f"{child_budget.max_turns} turns"
        )

    async def _get_depth(self, run_id: str) -> int:
        """Get the current depth of a run in the ancestor chain."""
        rows = await self._engine.execute_read_async(
            "SELECT depth FROM agent_runs WHERE id = ?", (run_id,)
        )
        if rows:
            return int(rows[0]["depth"])
        return 0

    async def _get_ancestor_chain(self, run_id: str) -> list[dict[str, Any]]:
        """Walk up the parent chain to collect ancestors."""
        ancestors: list[dict[str, Any]] = []
        current_id: str | None = run_id

        while current_id:
            rows = await self._engine.execute_read_async(
                "SELECT id, parent_run_id, agent_key FROM agent_runs WHERE id = ?",
                (current_id,),
            )
            if not rows:
                break
            row = dict(rows[0])
            ancestors.append(row)
            current_id = row.get("parent_run_id")

        return ancestors
