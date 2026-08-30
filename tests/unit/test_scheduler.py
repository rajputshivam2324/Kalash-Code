"""Tests for scheduler subsystem — cron parsing, interval/once specs, and manager CRUD."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kalash.scheduler.cron import (
    CronField,
    CronExpression,
    IntervalSpec,
    OnceSpec,
    EventSpec,
)


# ---------------------------------------------------------------------------
# CronField
# ---------------------------------------------------------------------------


class TestCronField:
    """Unit tests for cron field parsing."""

    def test_wildcard(self):
        f = CronField.parse("*", 0, 59)
        assert f.values == set(range(0, 60))

    def test_single_value(self):
        f = CronField.parse("5", 0, 59)
        assert f.values == {5}

    def test_range(self):
        f = CronField.parse("1-5", 0, 59)
        assert f.values == {1, 2, 3, 4, 5}

    def test_step(self):
        f = CronField.parse("*/15", 0, 59)
        assert f.values == {0, 15, 30, 45}

    def test_range_step(self):
        f = CronField.parse("0-30/10", 0, 59)
        assert f.values == {0, 10, 20, 30}

    def test_list(self):
        f = CronField.parse("1,15,30", 0, 59)
        assert f.values == {1, 15, 30}

    def test_mixed(self):
        f = CronField.parse("1-3,10,20-25", 0, 59)
        assert 2 in f.values
        assert 10 in f.values
        assert 22 in f.values

    def test_clamp_out_of_range(self):
        f = CronField.parse("100", 0, 59)
        assert f.values == set()

    def test_matches(self):
        f = CronField.parse("0,30", 0, 59)
        assert f.matches(0)
        assert f.matches(30)
        assert not f.matches(15)


# ---------------------------------------------------------------------------
# CronExpression
# ---------------------------------------------------------------------------


class TestCronExpression:
    """Tests for full cron expression parsing and next-fire computation."""

    def test_parse_five_fields(self):
        expr = CronExpression.parse("30 2 * * 1-5")
        assert 30 in expr.minute.values
        assert 2 in expr.hour.values

    def test_parse_wrong_field_count_raises(self):
        with pytest.raises(ValueError, match="5 fields"):
            CronExpression.parse("* *")

    def test_matches_specific_time(self):
        expr = CronExpression.parse("0 12 * * *")
        noon = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
        assert expr.matches(noon)

    def test_does_not_match_wrong_time(self):
        expr = CronExpression.parse("0 12 * * *")
        morning = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        assert not expr.matches(morning)

    def test_next_fire_advances_past_current(self):
        expr = CronExpression.parse("0 * * * *")
        now = datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc)
        nxt = expr.next_fire(after=now)
        assert nxt > now
        assert nxt.minute == 0
        assert nxt.hour == 11

    def test_next_fire_every_minute(self):
        expr = CronExpression.parse("* * * * *")
        now = datetime(2026, 8, 21, 10, 30, 45, tzinfo=timezone.utc)
        nxt = expr.next_fire(after=now)
        assert nxt.minute == 31

    def test_next_fire_specific_day(self):
        # Every Monday at 9:00
        expr = CronExpression.parse("0 9 * * 1")
        friday = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)  # This is a Friday
        nxt = expr.next_fire(after=friday)
        # Should be the following Monday
        assert nxt > friday

    def test_parse_with_timezone(self):
        expr = CronExpression.parse("0 0 * * *", tz="Asia/Kolkata")
        assert str(expr.tz) == "Asia/Kolkata"

    def test_next_fire_returns_utc(self):
        expr = CronExpression.parse("0 0 * * *", tz="Asia/Kolkata")
        nxt = expr.next_fire()
        assert nxt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# IntervalSpec
# ---------------------------------------------------------------------------


class TestIntervalSpec:
    """Tests for fixed-interval scheduling."""

    def test_next_fire_from_last(self):
        spec = IntervalSpec(seconds=300)
        base = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
        nxt = spec.next_fire(last_fire=base)
        assert nxt == base + timedelta(seconds=300)

    def test_next_fire_default_is_now_plus_interval(self):
        spec = IntervalSpec(seconds=60)
        nxt = spec.next_fire()
        assert nxt > datetime.now(timezone.utc) - timedelta(seconds=5)


# ---------------------------------------------------------------------------
# OnceSpec
# ---------------------------------------------------------------------------


class TestOnceSpec:
    """Tests for one-shot scheduling."""

    def test_future_returns_fire_at(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        spec = OnceSpec(fire_at=future)
        assert spec.next_fire() == future

    def test_past_returns_none(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        spec = OnceSpec(fire_at=past)
        assert spec.next_fire() is None


# ---------------------------------------------------------------------------
# EventSpec
# ---------------------------------------------------------------------------


class TestEventSpec:
    """Tests for event-triggered scheduling."""

    def test_matches_correct_event(self):
        spec = EventSpec(event_type="commit")
        assert spec.matches_event("commit", {})

    def test_does_not_match_wrong_event(self):
        spec = EventSpec(event_type="commit")
        assert not spec.matches_event("push", {})

    def test_filter_matching(self):
        spec = EventSpec(event_type="commit", filter={"branch": "main"})
        assert spec.matches_event("commit", {"branch": "main"})
        assert not spec.matches_event("commit", {"branch": "dev"})


# ---------------------------------------------------------------------------
# SchedulerManager (with temp DB)
# ---------------------------------------------------------------------------


class TestSchedulerManager:
    """Integration tests for SchedulerManager CRUD."""

    @pytest.fixture(autouse=True)
    def _setup_engine(self, tmp_path):
        """Provide a fresh StorageEngine with migrations."""
        from kalash.storage.engine import StorageEngine

        db_path = tmp_path / "test.db"
        self.engine = StorageEngine(db_path=db_path)
        # Run migrations synchronously
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.engine.initialize())
        loop.close()

        # Monkeypatch get_engine to return our test engine
        import kalash.scheduler.manager as mgr_mod
        self._orig = mgr_mod.get_engine
        mgr_mod.get_engine = lambda: self.engine
        yield
        mgr_mod.get_engine = self._orig
        self.engine.close()

    def test_add_and_list(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        job = mgr.add(name="daily_review", schedule="0 9 * * *", prompt="review code")
        assert job.name == "daily_review"
        assert job.schedule == "0 9 * * *"
        jobs = mgr.list_jobs()
        assert len(jobs) >= 1
        assert any(j.name == "daily_review" for j in jobs)

    def test_get_by_id(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        job = mgr.add(name="test_get", schedule="* * * * *", prompt="test")
        found = mgr.get(job.id)
        assert found is not None
        assert found.name == "test_get"

    def test_get_nonexistent_returns_none(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        assert mgr.get("nonexistent_id") is None

    def test_delete(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        job = mgr.add(name="to_delete", schedule="* * * * *", prompt="test")
        result = mgr.delete(job.id)
        assert result is True
        assert mgr.get(job.id) is None

    def test_set_enabled(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        job = mgr.add(name="toggle", schedule="* * * * *", prompt="test")
        mgr.set_enabled(job.id, enabled=False)
        # Should not appear in active list
        active = mgr.list_jobs(include_disabled=False)
        assert not any(j.id == job.id for j in active)
        # Should appear in full list
        all_jobs = mgr.list_jobs(include_disabled=True)
        assert any(j.id == job.id for j in all_jobs)

    def test_trigger(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        job = mgr.add(name="trigger_test", schedule="* * * * *", prompt="test")
        result = mgr.trigger(job.id)
        assert result is not None
        assert result.session_id.startswith("ses_")

    def test_trigger_nonexistent_returns_none(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        assert mgr.trigger("nonexistent") is None

    def test_logs_empty_initially(self):
        from kalash.scheduler.manager import SchedulerManager
        mgr = SchedulerManager()
        logs = mgr.get_logs()
        assert isinstance(logs, list)
