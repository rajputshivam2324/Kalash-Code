"""Scheduler manager for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from kalash.core.ids import generate_id
from kalash.scheduler.cron import CronExpression
from kalash.storage.engine import get_engine


@dataclass
class ScheduleJob:
    id: str
    name: str
    schedule: str
    prompt: str
    enabled: bool = True
    next_run: datetime | None = None
    last_run: datetime | None = None


@dataclass
class TriggerResult:
    session_id: str


@dataclass
class LogEntry:
    timestamp: datetime
    job_name: str
    success: bool
    duration_seconds: float
    session_id: str | None = None


class SchedulerManager:
    """SQLite-backed schedule CRUD for ``kalash cron``."""

    def __init__(self) -> None:
        self._engine = get_engine()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        from kalash.storage.migrations import run_migrations_sync

        run_migrations_sync(self._engine)

    def add(
        self,
        *,
        name: str,
        schedule: str,
        prompt: str,
        model: str | None = None,
        max_turns: int = 10,
    ) -> ScheduleJob:
        job_id = generate_id("sch_")
        now = datetime.now(UTC)
        try:
            cron = CronExpression.parse(schedule)
            next_run = cron.next_fire(now)
        except Exception:
            next_run = None
        self._engine.execute_write_sync(
            """INSERT INTO schedules
               (id, name, spec, spec_kind, prompt, enabled, next_run_at, created_at, updated_at)
               VALUES (?, ?, ?, 'cron', ?, 1, ?, ?, ?)""",
            (
                job_id,
                name,
                schedule,
                prompt,
                next_run.isoformat() if next_run else None,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return ScheduleJob(
            id=job_id,
            name=name,
            schedule=schedule,
            prompt=prompt,
            enabled=True,
            next_run=next_run,
        )

    def list_jobs(self, *, include_disabled: bool = False) -> list[ScheduleJob]:
        query = "SELECT * FROM schedules"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY name"
        rows = self._engine.execute_read(query)
        jobs: list[ScheduleJob] = []
        for row in rows:
            data = dict(row)
            next_run = (
                datetime.fromisoformat(data["next_run_at"]) if data.get("next_run_at") else None
            )
            jobs.append(
                ScheduleJob(
                    id=data["id"],
                    name=data["name"],
                    schedule=data["spec"],
                    prompt=data["prompt"],
                    enabled=bool(data.get("enabled", 1)),
                    next_run=next_run,
                )
            )
        return jobs

    def get(self, job_id: str) -> ScheduleJob | None:
        rows = self._engine.execute_read(
            "SELECT * FROM schedules WHERE id = ?",
            (job_id,),
        )
        row = rows[0] if rows else None
        if row is None:
            rows = self._engine.execute_read(
                "SELECT * FROM schedules WHERE id LIKE ?",
                (f"{job_id}%",),
            )
            row = rows[0] if rows else None
        if row is None:
            return None
        data = dict(row)
        return ScheduleJob(
            id=data["id"],
            name=data["name"],
            schedule=data["spec"],
            prompt=data["prompt"],
            enabled=bool(data.get("enabled", 1)),
        )

    def delete(self, job_id: str) -> bool:
        self._engine.execute_write_sync(
            "DELETE FROM schedules WHERE id = ? OR id LIKE ?",
            (job_id, f"{job_id}%"),
        )
        return True

    def set_enabled(self, job_id: str, *, enabled: bool) -> bool:
        self._engine.execute_write_sync(
            "UPDATE schedules SET enabled = ? WHERE id = ? OR id LIKE ?",
            (1 if enabled else 0, job_id, f"{job_id}%"),
        )
        return True

    def trigger(self, job_id: str) -> TriggerResult | None:
        job = self.get(job_id)
        if job is None:
            return None
        session_id = generate_id("ses_")
        now = datetime.now(UTC).isoformat()
        self._engine.execute_write_sync(
            """INSERT INTO schedule_runs (id, schedule_id, status, started_at, trigger_source)
               VALUES (?, ?, 'running', ?, 'manual')""",
            (generate_id("sr_"), job.id, now),
        )
        return TriggerResult(session_id=session_id)

    def get_logs(self, *, job_id: str | None = None, limit: int = 20) -> list[LogEntry]:
        if job_id:
            rows = self._engine.execute_read(
                """SELECT r.*, s.name AS job_name FROM schedule_runs r
                   JOIN schedules s ON s.id = r.schedule_id
                   WHERE r.schedule_id = ? OR r.schedule_id LIKE ?
                   ORDER BY r.started_at DESC LIMIT ?""",
                (job_id, f"{job_id}%", limit),
            )
        else:
            rows = self._engine.execute_read(
                """SELECT r.*, s.name AS job_name FROM schedule_runs r
                   JOIN schedules s ON s.id = r.schedule_id
                   ORDER BY r.started_at DESC LIMIT ?""",
                (limit,),
            )
        entries: list[LogEntry] = []
        for row in rows:
            data = dict(row)
            started = data.get("started_at")
            finished = data.get("finished_at")
            duration = 0.0
            if started and finished:
                duration = (
                    datetime.fromisoformat(finished) - datetime.fromisoformat(started)
                ).total_seconds()
            entries.append(
                LogEntry(
                    timestamp=datetime.fromisoformat(started) if started else datetime.now(UTC),
                    job_name=str(data.get("job_name", "unknown")),
                    success=data.get("status") == "succeeded",
                    duration_seconds=duration,
                )
            )
        return entries
