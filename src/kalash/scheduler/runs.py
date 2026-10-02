"""Schedule execution records with state machine and retry logic.

State machine: pending → running → succeeded/failed/cancelled.
Records stored in schedule_runs table (see storage migrations).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from kalash.core.errors import StateError
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine


class RunState(StrEnum):
    """Schedule run states stored in the ``status`` column."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


# Valid state transitions
_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.PENDING: {RunState.RUNNING, RunState.SKIPPED, RunState.CANCELLED},
    RunState.RUNNING: {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED},
    RunState.SUCCEEDED: set(),
    RunState.FAILED: {RunState.RUNNING},
    RunState.SKIPPED: set(),
    RunState.CANCELLED: set(),
}


@dataclass
class RetryConfig:
    """Configuration for exponential backoff retry."""

    max_retries: int = 3
    base_delay_s: float = 5.0
    max_delay_s: float = 300.0
    multiplier: float = 2.0
    jitter: bool = True

    def compute_delay(self, attempt: int) -> float:
        """Compute delay for a given attempt number (0-indexed)."""
        delay = self.base_delay_s * (self.multiplier**attempt)
        delay = min(delay, self.max_delay_s)
        if self.jitter:
            delay *= 0.5 + random.random()
        return delay


@dataclass
class ScheduleRun:
    """A single execution instance of a schedule."""

    id: str = ""
    schedule_id: str = ""
    status: RunState = RunState.PENDING
    agent_run_id: str | None = None
    attempt: int = 0
    max_retries: int = 3

    started_at: datetime | None = None
    finished_at: datetime | None = None
    trigger_source: str | None = None
    error_code: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    next_retry_at: datetime | None = None
    retry_config: RetryConfig = field(default_factory=RetryConfig)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = generate_id("sr_")

    def transition(self, new_state: RunState) -> None:
        """Transition to a new state with validation."""
        valid = _TRANSITIONS.get(self.status, set())
        if new_state not in valid:
            raise StateError(
                f"Invalid transition: {self.status} → {new_state}. Valid targets: {valid}",
                recoverable=True,
            )

        self.status = new_state
        if new_state == RunState.RUNNING:
            self.started_at = datetime.now(UTC)
        elif new_state in (
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.SKIPPED,
            RunState.CANCELLED,
        ):
            self.finished_at = datetime.now(UTC)

    def should_retry(self) -> bool:
        return self.status == RunState.FAILED and self.attempt < self.retry_config.max_retries

    def schedule_retry(self) -> None:
        if not self.should_retry():
            raise StateError(
                f"Cannot retry: status={self.status}, attempt={self.attempt}/{self.retry_config.max_retries}",
                recoverable=True,
            )
        delay = self.retry_config.compute_delay(self.attempt)
        self.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
        self.attempt += 1
        self.transition(RunState.RUNNING)

    async def save(self, engine: StorageEngine) -> None:
        """Persist this run to the schedule_runs table."""
        await engine.execute_write(
            """INSERT OR REPLACE INTO schedule_runs
               (id, schedule_id, agent_run_id, status, started_at, finished_at,
                trigger_source, error_code, tokens_used, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.id,
                self.schedule_id,
                self.agent_run_id,
                self.status.value,
                self.started_at.isoformat() if self.started_at else None,
                self.finished_at.isoformat() if self.finished_at else None,
                self.trigger_source,
                self.error_code,
                self.tokens_used,
                self.cost_usd,
            ),
        )

    @classmethod
    async def load(cls, engine: StorageEngine, run_id: str) -> ScheduleRun | None:
        rows = await engine.execute_read_async(
            "SELECT * FROM schedule_runs WHERE id = ?", (run_id,)
        )
        if not rows:
            return None

        row = dict(rows[0])
        return cls(
            id=row["id"],
            schedule_id=row["schedule_id"],
            agent_run_id=row.get("agent_run_id"),
            status=RunState(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row.get("started_at") else None,
            finished_at=datetime.fromisoformat(row["finished_at"])
            if row.get("finished_at")
            else None,
            trigger_source=row.get("trigger_source"),
            error_code=row.get("error_code"),
            tokens_used=row.get("tokens_used"),
            cost_usd=row.get("cost_usd"),
        )

    @classmethod
    async def list_for_schedule(
        cls,
        engine: StorageEngine,
        schedule_id: str,
        *,
        limit: int = 50,
        status: RunState | None = None,
    ) -> list[ScheduleRun]:
        sql = "SELECT * FROM schedule_runs WHERE schedule_id = ?"
        params: list[Any] = [schedule_id]

        if status:
            sql += " AND status = ?"
            params.append(status.value)

        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = await engine.execute_read_async(sql, tuple(params))
        runs: list[ScheduleRun] = []
        for row in rows:
            r = dict(row)
            runs.append(
                cls(
                    id=r["id"],
                    schedule_id=r["schedule_id"],
                    agent_run_id=r.get("agent_run_id"),
                    status=RunState(r["status"]),
                    started_at=datetime.fromisoformat(r["started_at"])
                    if r.get("started_at")
                    else None,
                    finished_at=datetime.fromisoformat(r["finished_at"])
                    if r.get("finished_at")
                    else None,
                    trigger_source=r.get("trigger_source"),
                    error_code=r.get("error_code"),
                    tokens_used=r.get("tokens_used"),
                    cost_usd=r.get("cost_usd"),
                )
            )
        return runs
