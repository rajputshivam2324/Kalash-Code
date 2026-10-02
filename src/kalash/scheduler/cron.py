"""Cron expression parsing with DST-aware scheduling (I-038).

next_fire() computed in local wall time, stored as UTC.
Uses zoneinfo for timezone handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class ScheduleType(StrEnum):
    """Types of schedule specifications."""

    CRON = "cron"
    INTERVAL = "interval"
    ONCE = "once"
    EVENT = "event"


@dataclass
class CronField:
    """A single field in a cron expression."""

    values: set[int]  # resolved set of valid values
    raw: str  # original expression fragment

    @classmethod
    def parse(cls, expr: str, min_val: int, max_val: int) -> CronField:
        """Parse a cron field expression.

        Supports: *, N, N-M, N/step, */step, N-M/step, comma-separated.
        """
        values: set[int] = set()

        for part in expr.split(","):
            part = part.strip()
            if part == "*":
                values.update(range(min_val, max_val + 1))
            elif "/" in part:
                range_part, step_str = part.split("/", 1)
                step = int(step_str)
                if range_part == "*":
                    start, end = min_val, max_val
                elif "-" in range_part:
                    start, end = (int(x) for x in range_part.split("-", 1))
                else:
                    start = int(range_part)
                    end = max_val
                values.update(range(start, end + 1, step))
            elif "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
                values.update(range(start, end + 1))
            else:
                values.add(int(part))

        # Clamp to valid range
        values = {v for v in values if min_val <= v <= max_val}
        return cls(values=values, raw=expr)

    def matches(self, value: int) -> bool:
        """Check if a value matches this field."""
        return value in self.values


@dataclass
class CronExpression:
    """5-field cron expression: minute hour day_of_month month day_of_week.

    Supports standard cron syntax including ranges, steps, and lists.
    Day matching uses OR semantics (matches if either dom or dow matches).
    """

    minute: CronField
    hour: CronField
    day_of_month: CronField
    month: CronField
    day_of_week: CronField
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    raw: str = ""

    @classmethod
    def parse(cls, expression: str, tz: str | ZoneInfo = "UTC") -> CronExpression:
        """Parse a 5-field cron expression.

        Args:
            expression: Standard 5-field cron (e.g. "30 2 * * 1-5").
            tz: Timezone for wall-time interpretation.
        """
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Cron expression must have exactly 5 fields, got {len(parts)}: '{expression}'"
            )

        zone = ZoneInfo(tz) if isinstance(tz, str) else tz

        return cls(
            minute=CronField.parse(parts[0], 0, 59),
            hour=CronField.parse(parts[1], 0, 23),
            day_of_month=CronField.parse(parts[2], 1, 31),
            month=CronField.parse(parts[3], 1, 12),
            day_of_week=CronField.parse(parts[4], 0, 6),  # 0=Sunday
            tz=zone,
            raw=expression,
        )

    def matches(self, dt: datetime) -> bool:
        """Check if a datetime matches this cron expression."""
        local_dt = dt.astimezone(self.tz)
        return (
            self.minute.matches(local_dt.minute)
            and self.hour.matches(local_dt.hour)
            and self.month.matches(local_dt.month)
            and (
                self.day_of_month.matches(local_dt.day)
                or self.day_of_week.matches(local_dt.weekday())
            )
        )

    def next_fire(self, after: datetime | None = None) -> datetime:
        """Compute the next fire time after the given datetime.

        Computed in local wall time (DST-aware), returned as UTC.
        """
        if after is None:
            after = datetime.now(UTC)

        # Work in local time for wall-clock correctness
        local = after.astimezone(self.tz)
        # Start from the next minute
        candidate = local.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Search forward (bounded to prevent infinite loops)
        max_iterations = 366 * 24 * 60  # ~1 year of minutes
        for _ in range(max_iterations):
            if self._matches_local(candidate):
                # Convert back to UTC for storage (I-038)
                return candidate.astimezone(UTC)
            candidate += timedelta(minutes=1)

        raise ValueError(f"Could not find next fire time for '{self.raw}' within search window")

    def _matches_local(self, local_dt: datetime) -> bool:
        """Check if a local datetime matches (avoiding repeated timezone conversion)."""
        return (
            self.minute.matches(local_dt.minute)
            and self.hour.matches(local_dt.hour)
            and self.month.matches(local_dt.month)
            and (
                self.day_of_month.matches(local_dt.day)
                or self.day_of_week.matches(
                    (local_dt.weekday() + 1) % 7  # Convert Mon=0 to Sun=0
                )
            )
        )


@dataclass
class IntervalSpec:
    """Fixed interval schedule (e.g. every 30 minutes)."""

    seconds: int
    start_after: datetime | None = None  # first fire time; if None, start now
    jitter_seconds: int = 0  # random jitter to avoid thundering herd

    def next_fire(self, last_fire: datetime | None = None) -> datetime:
        """Compute next fire time based on interval from last fire."""
        base = last_fire or self.start_after or datetime.now(UTC)
        return base + timedelta(seconds=self.seconds)


@dataclass
class OnceSpec:
    """One-shot schedule at a specific time."""

    fire_at: datetime  # UTC

    def next_fire(self, after: datetime | None = None) -> datetime | None:
        """Returns fire_at if it's still in the future, else None."""
        now = after or datetime.now(UTC)
        if self.fire_at > now:
            return self.fire_at
        return None


@dataclass
class EventSpec:
    """Event-triggered schedule. Fires when a matching event occurs."""

    event_type: str  # matches EventType values
    filter: dict[str, str] | None = None  # optional data field filters
    debounce_seconds: int = 0  # min gap between fires

    def matches_event(self, event_type: str, event_data: dict[str, str]) -> bool:
        """Check if an event matches this spec."""
        if event_type != self.event_type:
            return False
        if self.filter:
            for key, expected in self.filter.items():
                if event_data.get(key) != expected:
                    return False
        return True
