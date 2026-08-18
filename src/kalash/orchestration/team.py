"""Multi-agent team patterns.

All patterns enforce: global concurrency cap, per-run budget
ceilings, and cycle detection.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from kalash.core.budget import BudgetState
from kalash.core.errors import BudgetError, KalashError
from kalash.core.events import Event, EventType, get_event_bus
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine

from kalash.orchestration.subagent import SubagentConfig, SubagentRunner, ResultEnvelope


class ConcurrencyCapError(KalashError):
    """Global concurrency cap exceeded."""

    code = "KALASH_TEAM_CONCURRENCY_CAP"


@dataclass
class TeamConfig:
    """Base configuration for team patterns."""

    max_concurrency: int = 8
    budget: BudgetState = field(default_factory=BudgetState)
    parent_run_id: str = ""


@dataclass
class TeamResult:
    """Aggregated result from a team execution."""

    pattern: str
    results: list[ResultEnvelope] = field(default_factory=list)
    merged_output: Any = None
    total_duration_ms: int = 0
    status: str = "completed"  # "completed" | "partial" | "failed"


class TeamPattern(ABC):
    """Base class for multi-agent team patterns."""

    def __init__(self, engine: StorageEngine, config: TeamConfig) -> None:
        self._engine = engine
        self._config = config
        self._runner = SubagentRunner(engine)
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._active_count = 0

    @abstractmethod
    async def execute(self, agents: list[SubagentConfig]) -> TeamResult:
        """Execute the team pattern."""
        ...

    async def _spawn_with_cap(
        self,
        config: SubagentConfig,
    ) -> ResultEnvelope:
        """Spawn a subagent respecting the concurrency cap."""
        async with self._semaphore:
            self._active_count += 1
            try:
                return await self._runner.spawn(
                    parent_run_id=self._config.parent_run_id,
                    parent_budget=self._config.budget,
                    config=config,
                )
            finally:
                self._active_count -= 1

    def _detect_cycle(self, agents: list[SubagentConfig]) -> bool:
        """Check for duplicate agent+prompt combos within the batch."""
        seen: set[str] = set()
        for agent in agents:
            key = f"{agent.agent}:{agent.prompt[:128]}"
            if key in seen:
                return True
            seen.add(key)
        return False


class FanoutPattern(TeamPattern):
    """N independent agents, results merged.

    All agents run concurrently (up to cap). Results are collected
    and merged once all complete.
    """

    async def execute(self, agents: list[SubagentConfig]) -> TeamResult:
        if self._detect_cycle(agents):
            raise KalashError("Cycle detected in fanout agents", recoverable=True)

        start = asyncio.get_event_loop().time()
        tasks = [self._spawn_with_cap(agent) for agent in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        envelopes: list[ResultEnvelope] = []
        for r in results:
            if isinstance(r, ResultEnvelope):
                envelopes.append(r)
            elif isinstance(r, Exception):
                envelopes.append(ResultEnvelope(
                    run_id="",
                    status="failed",
                    error=str(r),
                ))

        elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)

        # Merge: collect all outputs into a list
        merged = [e.result for e in envelopes if e.status == "completed"]
        all_ok = all(e.status == "completed" for e in envelopes)

        return TeamResult(
            pattern="fanout",
            results=envelopes,
            merged_output=merged,
            total_duration_ms=elapsed_ms,
            status="completed" if all_ok else "partial",
        )


class PipelinePattern(TeamPattern):
    """Sequential agents with typed handoff.

    Each agent's output becomes the next agent's input context.
    Pipeline stops on first failure.
    """

    async def execute(self, agents: list[SubagentConfig]) -> TeamResult:
        if self._detect_cycle(agents):
            raise KalashError("Cycle detected in pipeline agents", recoverable=True)

        start = asyncio.get_event_loop().time()
        envelopes: list[ResultEnvelope] = []
        previous_output: Any = None

        for i, agent in enumerate(agents):
            # Inject previous output into prompt context
            if previous_output is not None:
                agent.prompt = f"{agent.prompt}\n\n---\nPrevious step output:\n{previous_output}"

            envelope = await self._spawn_with_cap(agent)
            envelopes.append(envelope)

            if envelope.status != "completed":
                elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                return TeamResult(
                    pattern="pipeline",
                    results=envelopes,
                    total_duration_ms=elapsed_ms,
                    status="failed",
                )

            previous_output = envelope.result

        elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        return TeamResult(
            pattern="pipeline",
            results=envelopes,
            merged_output=previous_output,
            total_duration_ms=elapsed_ms,
            status="completed",
        )


class CriticPattern(TeamPattern):
    """Proposer + reviewer loop until accept or max rounds.

    The proposer generates, the reviewer critiques. Loop continues
    until the reviewer accepts or max_rounds is hit.
    """

    def __init__(
        self,
        engine: StorageEngine,
        config: TeamConfig,
        max_rounds: int = 3,
    ) -> None:
        super().__init__(engine, config)
        self._max_rounds = max_rounds

    async def execute(self, agents: list[SubagentConfig]) -> TeamResult:
        """Execute critic pattern. agents[0] = proposer, agents[1] = reviewer."""
        if len(agents) < 2:
            raise KalashError("CriticPattern requires at least 2 agents (proposer, reviewer)")

        proposer_config = agents[0]
        reviewer_config = agents[1]
        start = asyncio.get_event_loop().time()
        envelopes: list[ResultEnvelope] = []

        proposal: Any = None
        for round_num in range(self._max_rounds):
            # Proposer generates (with feedback from previous round)
            if proposal is not None:
                proposer_config.prompt = (
                    f"{agents[0].prompt}\n\n---\n"
                    f"Round {round_num + 1} — incorporate reviewer feedback:\n{proposal}"
                )

            proposer_result = await self._spawn_with_cap(proposer_config)
            envelopes.append(proposer_result)

            if proposer_result.status != "completed":
                break

            # Reviewer critiques
            reviewer_config.prompt = (
                f"{agents[1].prompt}\n\n---\n"
                f"Proposal to review:\n{proposer_result.result}\n\n"
                f"Respond with ACCEPT if satisfactory, or provide critique."
            )

            reviewer_result = await self._spawn_with_cap(reviewer_config)
            envelopes.append(reviewer_result)

            if reviewer_result.status != "completed":
                break

            # Check if accepted
            review_output = str(reviewer_result.result or "")
            if "ACCEPT" in review_output.upper():
                elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
                return TeamResult(
                    pattern="critic",
                    results=envelopes,
                    merged_output=proposer_result.result,
                    total_duration_ms=elapsed_ms,
                    status="completed",
                )

            # Feed critique back for next round
            proposal = review_output

        elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        # Max rounds hit — return last proposal
        last_proposal = next(
            (e.result for e in reversed(envelopes) if e.status == "completed"),
            None,
        )
        return TeamResult(
            pattern="critic",
            results=envelopes,
            merged_output=last_proposal,
            total_duration_ms=elapsed_ms,
            status="partial",
        )


class SupervisorPattern(TeamPattern):
    """Lead agent dynamically delegates to workers.

    The supervisor decides which workers to invoke and with what
    prompts based on the task at hand.
    """

    def __init__(
        self,
        engine: StorageEngine,
        config: TeamConfig,
        max_delegations: int = 10,
    ) -> None:
        super().__init__(engine, config)
        self._max_delegations = max_delegations

    async def execute(self, agents: list[SubagentConfig]) -> TeamResult:
        """Execute supervisor pattern.

        agents[0] = supervisor, agents[1:] = available workers.
        The supervisor is invoked first and can dynamically dispatch workers.
        """
        if not agents:
            raise KalashError("SupervisorPattern requires at least a supervisor agent")

        supervisor_config = agents[0]
        worker_configs = {a.agent: a for a in agents[1:]}
        start = asyncio.get_event_loop().time()
        envelopes: list[ResultEnvelope] = []

        # Run supervisor — it produces delegation instructions
        supervisor_result = await self._spawn_with_cap(supervisor_config)
        envelopes.append(supervisor_result)

        if supervisor_result.status != "completed":
            elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
            return TeamResult(
                pattern="supervisor",
                results=envelopes,
                total_duration_ms=elapsed_ms,
                status="failed",
            )

        # Parse delegations from supervisor output
        delegations = self._parse_delegations(supervisor_result.result, worker_configs)

        # Execute delegations (up to cap)
        for delegation in delegations[: self._max_delegations]:
            worker_result = await self._spawn_with_cap(delegation)
            envelopes.append(worker_result)

        elapsed_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        worker_outputs = [e.result for e in envelopes[1:] if e.status == "completed"]

        return TeamResult(
            pattern="supervisor",
            results=envelopes,
            merged_output={
                "supervisor_output": supervisor_result.result,
                "worker_outputs": worker_outputs,
            },
            total_duration_ms=elapsed_ms,
            status="completed",
        )

    def _parse_delegations(
        self,
        supervisor_output: Any,
        workers: dict[str, SubagentConfig],
    ) -> list[SubagentConfig]:
        """Parse supervisor output to determine which workers to invoke.

        Expected format from supervisor: list of {agent, prompt} dicts.
        Falls back to empty list if output is malformed.
        """
        if not isinstance(supervisor_output, list):
            return []

        delegations: list[SubagentConfig] = []
        for item in supervisor_output:
            if not isinstance(item, dict):
                continue
            agent_name = item.get("agent", "")
            prompt = item.get("prompt", "")
            if agent_name in workers:
                config = SubagentConfig(
                    agent=agent_name,
                    prompt=prompt,
                    max_tokens=workers[agent_name].max_tokens,
                    max_turns=workers[agent_name].max_turns,
                )
                delegations.append(config)

        return delegations
