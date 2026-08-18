"""Schedule execution records with state machine and retry logic.

State machine: SCHEDULED → DUE → QUEUED → RUNNING → SUCCEEDED/FAILED/SKIPPED.
Records stored in schedule_runs table.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from kalash.core.errors import StateError
from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine


class RunState(StrEnum):
    """Schedule run state machine states."""

    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


# Valid state transitions
_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.SCHEDULED: {RunState.DUE, RunState.SKIPPED, RunState.CANCELLED},
    RunState.DUE: {RunState.QUEUED, RunState.SKIPPED, RunState.CANCELLED},
    RunState.QUEUED: {RunState.RUNNING, RunState.SKIPPED, RunState.CANCELLED},
    RunState.RUNNING: {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED},
    RunState.SUCCEEDED: set(),  # terminal
    RunState.FAILED: {RunState.QUEUED},  # retry goes back to QUEUED
    RunState.SKIPPED: set(),  # terminal
    RunState.CANCELLED: set(),  # terminal
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
        delay = self.base_delay_s * (self.multiplier ** attempt)
        delay = min(delay, self.max_delay_s)
        if self.jitter:
            delay *= (0.5 + random.random())  # jitter between 50-150% of delay
        return delay


@dataclass
class ScheduleRun:
    """A single execution instance of a schedule.

    Tracks state, timing, retries, and results.
    """

    id: str = ""
    schedule_id: str = ""
    state: RunState = RunState.SCHEDULED
    attempt: int = 0
    max_retries: int = 3

    # Timing
    scheduled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_retry_at: datetime | None = None

    # Results
    result: Any = None
    error: str | None = None
    duration_ms: int | None = None

    # Retry config
    retry_config: RetryConfig = field(default_factory=RetryConfig)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = generate_id("sr_")

    def transition(self, new_state: RunState) -> None:
        """Transition to a new state with validation."""
        valid = _TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            raise StateError(
                f"Invalid transition: {self.state} → {new_state}. "
                f"Valid targets: {valid}",
                recoverable=True,
            )

        self.state = new_state

        # Record timing
        if new_state == RunState.RUNNING:
            self.started_at = datetime.now(timezone.utc)
        elif new_state in (RunState.SUCCEEDED, RunState.FAILED, RunState.SKIPPED, RunState.CANCELLED):
            self.finished_at = datetime.now(timezone.utc)
            if self.started_at:
                delta = self.finished_at - self.started_at
                self.duration_ms = int(delta.total_seconds() * 1000)

    def should_retry(self) -> bool:
        """Check if this run should be retried after failure."""
        return (
            self.state == RunState.FAILED
            and self.attempt < self.retry_config.max_retries
        )

    def schedule_retry(self) -> None:
        """Schedule a retry with exponential backoff."""
        if not self.should_retry():
            raise StateError(
                f"Cannot retry: state={self.state}, attempt={self.attempt}/{self.retry_config.max_retries}",
                recoverable=True,
            )
        delay = self.retry_config.compute_delay(self.attempt)
        self.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.attempt += 1
        self.transition(RunState.QUEUED)

    async def save(self, engine: StorageEngine) -> None:
        """Persist this run to the schedule_runs table."""
        now = datetime.now(timezone.utc).isoformat()
        await engine.execute_write(
            """INSERT OR REPLACE INTO schedule_runs
               (id, schedule_id, state, attempt, scheduled_at, started_at,
                finished_at, next_retry_at, error, duration_ms, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.id,
                self.schedule_id,
                self.state.value,
                self.attempt,
                self.scheduled_at.isoformat(),
                self.started_at.isoformat() if self.started_at else None,
                self.finished_at.isoformat() if self.finished_at else None,
                self.next_retry_at.isoformat() if self.next_retry_at else None,
                self.error,
                self.duration_ms,
                now,
            ),
        )

    @classmethod
    async def load(cls, engine: StorageEngine, run_id: str) -> "ScheduleRun | None":
        """Load a run from the database."""
        rows = await engine.execute_read_async(
            "SELECT * FROM schedule_runs WHERE id = ?", (run_id,)
        )
        if not rows:
            return None

        row = dict(rows[0])
        return cls(
            id=row["id"],
            schedule_id=row["schedule_id"],
            state=RunState(row["state"]),
            attempt=row["attempt"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            next_retry_at=datetime.fromisoformat(row["next_retry_at"]) if row["next_retry_at"] else None,
            error=row["error"],
            duration_ms=row["duration_ms"],
        )

    @classmethod
    async def list_for_schedule(
        cls,
        engine: StorageEngine,
        schedule_id: str,
        *,
        limit: int = 50,
        state: RunState | None = None,
    ) -> list["ScheduleRun"]:
        """List runs for a schedule, optionally filtered by state."""
        sql = "SELECT * FROM schedule_runs WHERE schedule_id = ?"
        params: list[Any] = [schedule_id]

        if state:
            sql += " AND state = ?"
            params.append(state.value)

        sql += " ORDER BY scheduled_at DESC LIMIT ?"
        params.append(limit)

        rows = await engine.execute_read_async(sql, tuple(params))
        runs: list[ScheduleRun] = []
        for row in rows:
            r = dict(row)
            runs.append(cls(
                id=r["id"],
                schedule_id=r["schedule_id"],
                state=RunState(r["state"]),
                attempt=r["attempt"],
                scheduled_at=datetime.fromisoformat(r["scheduled_at"]),
                started_at=datetime.fromisoformat(r["started_at"]) if r["started_at"] else None,
                finished_at=datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None,
                error=r["error"],
                duration_ms=r["duration_ms"],
            ))
        return runs
