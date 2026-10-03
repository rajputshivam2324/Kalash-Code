"""Regression tests for transaction isolation, cancellation and atomic migrations."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from kalash.storage.engine import StorageEngine
from kalash.storage.migrations import run_migrations_sync


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    engine = StorageEngine(tmp_path / "owned.db")
    engine.execute_script("CREATE TABLE items (id INTEGER PRIMARY KEY)")
    return engine


async def test_reader_does_not_see_another_owners_uncommitted_write(engine: StorageEngine) -> None:
    async with engine.write() as conn:
        conn.execute("INSERT INTO items VALUES (1)")
        assert engine.execute_read("SELECT * FROM items") == []
    assert len(engine.execute_read("SELECT * FROM items")) == 1


async def test_mixed_writers_are_isolated_after_rollback(engine: StorageEngine) -> None:
    async def writer() -> None:
        await asyncio.to_thread(engine.execute_write_sync, "INSERT INTO items VALUES (2)")

    other: asyncio.Task[None] | None = None

    async def abort() -> None:
        nonlocal other
        async with engine.write() as conn:
            conn.execute("INSERT INTO items VALUES (1)")
            other = asyncio.create_task(writer())
            await asyncio.sleep(0.02)
            assert not other.done()
            raise ValueError("abort")

    with pytest.raises(ValueError, match="abort"):
        await abort()
    assert other is not None
    await other
    assert [row["id"] for row in engine.execute_read("SELECT * FROM items")] == [2]


async def test_cancel_inside_write_rolls_back_and_releases_writer(engine: StorageEngine) -> None:
    entered = asyncio.Event()

    async def writer() -> None:
        async with engine.write() as conn:
            conn.execute("INSERT INTO items VALUES (1)")
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await engine.execute_write("INSERT INTO items VALUES (2)")
    assert [row["id"] for row in engine.execute_read("SELECT * FROM items")] == [2]


async def test_repeated_cancel_while_waiting_never_runs_sql(engine: StorageEngine) -> None:
    with engine.read() as holder:
        holder.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(engine.execute_write("INSERT INTO items VALUES (1)"))
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        holder.execute("ROLLBACK")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    await engine.execute_write("INSERT INTO items VALUES (2)")
    assert [row["id"] for row in engine.execute_read("SELECT * FROM items")] == [2]


async def test_competing_initializers_apply_once(engine: StorageEngine) -> None:
    await asyncio.gather(*(asyncio.to_thread(run_migrations_sync, engine) for _ in range(4)))
    rows = engine.execute_read("SELECT version FROM schema_migrations")
    from kalash.storage.migrations import MIGRATIONS

    assert len(rows) == len(MIGRATIONS)


def test_failed_migration_rolls_back_schema_and_receipt(
    engine: StorageEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kalash.storage.migrations.MIGRATIONS",
        [(1, "broken", "CREATE TABLE attempted (id INT); INSERT INTO absent VALUES (1);")],
    )
    with pytest.raises(sqlite3.OperationalError, match="absent"):
        run_migrations_sync(engine)
    assert engine.execute_read("SELECT * FROM schema_migrations") == []
    assert engine.execute_read("SELECT name FROM sqlite_master WHERE name='attempted'") == []
