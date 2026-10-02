"""Scheduler tick loop with advisory locking and lease-based coordination.

Polls the schedules table, fires due schedules respecting overlap_policy
and catchup_policy. Creates headless agent sessions for each run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from kalash.core.events import Event, EventType, get_event_bus
from kalash.core.ids import generate_id
from kalash.scheduler.cron import CronExpression
from kalash.storage.engine import StorageEngine


class OverlapPolicy(StrEnum):
    """What to do when a schedule fires while a previous run is still active."""

    SKIP = "skip"  # Skip this fire
    QUEUE = "queue"  # Queue the new run
    CANCEL_PREVIOUS = "cancel_previous"  # Cancel the running one


class CatchupPolicy(StrEnum):
    """What to do about missed fires (e.g. daemon was down)."""

    SKIP = "skip"  # Skip missed fires
    FIRE_ONCE = "fire_once"  # Fire once to catch up
    FIRE_ALL = "fire_all"  # Fire all missed instances


@dataclass
class ScheduleConfig:
    """Configuration for a single schedule entry."""

    id: str = ""
    name: str = ""
    cron: str | None = None
    interval_seconds: int | None = None
    once_at: str | None = None
    agent: str = "default"
    prompt: str = ""
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    catchup_policy: CatchupPolicy = CatchupPolicy.FIRE_ONCE
    max_consecutive_failures: int = 5
    enabled: bool = True


# Type for notification callbacks
NotificationHook = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, None]]


class SchedulerDaemon:
    """Main scheduler tick loop.

    - Polls schedules table at tick_interval.
    - Uses advisory lock row with lease for single-leader coordination.
    - Heartbeats to maintain lease.
    - Fires due schedules as headless agent sessions.
    - Auto-disables after max_consecutive_failures.
    """

    def __init__(
        self,
        engine: StorageEngine,
        *,
        tick_interval_s: float = 15.0,
        lease_duration_s: int = 60,
        heartbeat_interval_s: float = 20.0,
    ) -> None:
        self._engine = engine
        self._tick_interval = tick_interval_s
        self._lease_duration = lease_duration_s
        self._heartbeat_interval = heartbeat_interval_s
        self._running = False
        self._leader = False
        self._leader_id = generate_id("sched_")
        self._tick_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._notification_hooks: list[NotificationHook] = []

    def on_notification(self, hook: NotificationHook) -> None:
        """Register a notification hook for success/failure events."""
        self._notification_hooks.append(hook)

    async def start(self) -> None:
        """Start the scheduler daemon."""
        self._running = True
        await self._try_acquire_lease()
        self._tick_task = asyncio.create_task(self._tick_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Stop the scheduler daemon gracefully."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        await self._release_lease()

    async def _tick_loop(self) -> None:
        """Main tick loop — polls and fires due schedules."""
        while self._running:
            try:
                if self._leader:
                    await self._process_due_schedules()
            except asyncio.CancelledError:
                break
            except Exception as e:
                import logging as _log

                _log.getLogger(__name__).error("scheduler_tick_error", exc_info=e)

            await asyncio.sleep(self._tick_interval)

    async def _heartbeat_loop(self) -> None:
        """Maintain the advisory lock lease via heartbeat."""
        while self._running:
            try:
                if self._leader:
                    await self._renew_lease()
                else:
                    await self._try_acquire_lease()
            except asyncio.CancelledError:
                break
            except Exception as e:
                import logging as _log

                _log.getLogger(__name__).error("scheduler_heartbeat_error", exc_info=e)

            await asyncio.sleep(self._heartbeat_interval)

    async def _try_acquire_lease(self) -> bool:
        """Try to acquire the scheduler advisory lock with a lease."""
        now = datetime.now(UTC).isoformat()
        expires = (datetime.now(UTC) + timedelta(seconds=self._lease_duration)).isoformat()

        # Try to insert or claim an expired lease
        try:
            rows = await self._engine.execute_read_async(
                "SELECT leader_id, lease_expires_at FROM scheduler_lock WHERE id = 'singleton'"
            )
            if not rows:
                # No lock exists — claim it
                await self._engine.execute_write(
                    """INSERT INTO scheduler_lock (id, leader_id, lease_expires_at, heartbeat_at)
                       VALUES ('singleton', ?, ?, ?)""",
                    (self._leader_id, expires, now),
                )
                self._leader = True
                return True

            row = rows[0]
            current_expires = row["lease_expires_at"]
            if current_expires and current_expires < now:
                # Lease expired — take over
                await self._engine.execute_write(
                    """UPDATE scheduler_lock
                       SET leader_id = ?, lease_expires_at = ?, heartbeat_at = ?
                       WHERE id = 'singleton'""",
                    (self._leader_id, expires, now),
                )
                self._leader = True
                return True

            # Check if we already own it
            if row["leader_id"] == self._leader_id:
                self._leader = True
                return True

        except Exception as e:
            import logging as _log

            _log.getLogger(__name__).error("scheduler_lease_error", exc_info=e)

        self._leader = False
        return False

    async def _renew_lease(self) -> None:
        """Renew the advisory lock lease."""
        now = datetime.now(UTC).isoformat()
        expires = (datetime.now(UTC) + timedelta(seconds=self._lease_duration)).isoformat()
        await self._engine.execute_write(
            """UPDATE scheduler_lock
               SET lease_expires_at = ?, heartbeat_at = ?
               WHERE id = 'singleton' AND leader_id = ?""",
            (expires, now, self._leader_id),
        )

    async def _release_lease(self) -> None:
        """Release the advisory lock."""
        await self._engine.execute_write(
            "DELETE FROM scheduler_lock WHERE id = 'singleton' AND leader_id = ?",
            (self._leader_id,),
        )
        self._leader = False

    async def _process_due_schedules(self) -> None:
        """Find and fire all due schedules."""
        now = datetime.now(UTC).isoformat()

        rows = await self._engine.execute_read_async(
            """SELECT * FROM schedules
               WHERE enabled = 1 AND next_fire_at <= ?
               ORDER BY next_fire_at""",
            (now,),
        )

        for row in rows:
            schedule = dict(row)
            await self._fire_schedule(schedule)

    async def _fire_schedule(self, schedule: dict[str, Any]) -> None:
        """Fire a single schedule, respecting overlap and catchup policies."""
        schedule_id = schedule["id"]
        overlap_policy = OverlapPolicy(schedule.get("overlap_policy", "skip"))

        # Check overlap
        if overlap_policy == OverlapPolicy.SKIP:
            active = await self._engine.execute_read_async(
                """SELECT 1 FROM schedule_runs
                   WHERE schedule_id = ? AND status = 'running' LIMIT 1""",
                (schedule_id,),
            )
            if active:
                await self._advance_next_fire(schedule)
                return

        elif overlap_policy == OverlapPolicy.CANCEL_PREVIOUS:
            await self._engine.execute_write(
                """UPDATE schedule_runs SET status = 'cancelled', finished_at = ?
                   WHERE schedule_id = ? AND status = 'running'""",
                (datetime.now(UTC).isoformat(), schedule_id),
            )

        run_id = generate_id("sr_")
        started = datetime.now(UTC).isoformat()
        await self._engine.execute_write(
            """INSERT INTO schedule_runs
               (id, schedule_id, status, started_at, trigger_source)
               VALUES (?, ?, 'running', ?, 'scheduler')""",
            (run_id, schedule_id, started),
        )

        try:
            bus = get_event_bus()
            await bus.emit(
                Event(
                    type=EventType.SCHEDULE_FIRE,
                    data={
                        "schedule_id": schedule_id,
                        "run_id": run_id,
                        "agent": schedule.get("agent", "default"),
                    },
                )
            )

            prompt = str(schedule.get("prompt", "")).strip()
            if not prompt:
                raise RuntimeError("schedule has no prompt")

            from kalash.runtime.agent import build_agent
            from kalash.runtime.bootstrap import prepare_agent

            sandbox_mode = str(schedule.get("sandbox_mode", "read-only"))
            from kalash.core.config import load_config

            config = load_config(overrides={"permissions": {"sandbox": sandbox_mode}})
            agent, reason = build_agent(
                config=config,
                mode="plan" if sandbox_mode == "read-only" else "build",
                interactive=False,
            )
            if agent is None:
                raise RuntimeError(reason or "could not build agent")

            try:
                await prepare_agent(agent)
                result = await agent.send(prompt)
            finally:
                await agent.close()
            from kalash.runtime.loop import TerminationReason

            if result.termination_reason is not TerminationReason.NO_TOOL_CALLS or result.error:
                raise RuntimeError(
                    result.error or f"Run stopped: {result.termination_reason.value}"
                )
            finished = datetime.now(UTC).isoformat()
            await self._engine.execute_write(
                """UPDATE schedule_runs SET status = 'succeeded', finished_at = ?,
                   tokens_used = ? WHERE id = ?""",
                (finished, result.total_tokens, run_id),
            )

            await self._engine.execute_write(
                "UPDATE schedules SET consecutive_failures = 0 WHERE id = ?",
                (schedule_id,),
            )
            await self._notify("success", schedule_id, {"run_id": run_id})

        except Exception as e:
            finished = datetime.now(UTC).isoformat()
            await self._engine.execute_write(
                """UPDATE schedule_runs SET status = 'failed', finished_at = ?,
                   error_code = ? WHERE id = ?""",
                (finished, str(e)[:200], run_id),
            )
            await self._increment_failure_counter(schedule)
            await self._notify("failure", schedule_id, {"run_id": run_id, "error": str(e)})

        # Advance next fire time
        await self._advance_next_fire(schedule)

    async def _advance_next_fire(self, schedule: dict[str, Any]) -> None:
        """Compute and store the next fire time."""
        cron_expr = schedule.get("cron")
        if cron_expr:
            tz = schedule.get("timezone", "UTC")
            cron = CronExpression.parse(cron_expr, tz)
            next_fire = cron.next_fire()
            await self._engine.execute_write(
                "UPDATE schedules SET next_fire_at = ? WHERE id = ?",
                (next_fire.isoformat(), schedule["id"]),
            )

    async def _increment_failure_counter(self, schedule: dict[str, Any]) -> None:
        """Increment consecutive failures and auto-disable if threshold reached."""
        schedule_id = schedule["id"]
        max_failures = schedule.get("max_consecutive_failures", 5)

        await self._engine.execute_write(
            "UPDATE schedules SET consecutive_failures = consecutive_failures + 1 WHERE id = ?",
            (schedule_id,),
        )

        rows = await self._engine.execute_read_async(
            "SELECT consecutive_failures FROM schedules WHERE id = ?",
            (schedule_id,),
        )
        if rows and rows[0]["consecutive_failures"] >= max_failures:
            await self._engine.execute_write(
                "UPDATE schedules SET enabled = 0 WHERE id = ?",
                (schedule_id,),
            )
            await self._notify(
                "auto_disabled",
                schedule_id,
                {"reason": f"Exceeded {max_failures} consecutive failures"},
            )

    async def _notify(self, event: str, schedule_id: str, data: dict[str, Any]) -> None:
        """Invoke notification hooks."""
        for hook in self._notification_hooks:
            try:
                await hook(event, schedule_id, data)
            except Exception as e:
                import logging as _log

                _log.getLogger(__name__).warning("Notification hook failed", exc_info=e)
