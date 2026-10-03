"""Owned SQLite connections; WAL snapshots and SQLite serialize mixed writers."""

from __future__ import annotations

import asyncio
import atexit
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from kalash.core.paths import kalash_db_path


class StorageEngine:
    """Manages SQLite connections with WAL mode and serialized writes."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or kalash_db_path()
        self._conn: sqlite3.Connection | None = None
        self._bootstrap_lock = threading.Lock()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            conn.close()
            raise
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Legacy bootstrap handle. Runtime operations own separate connections."""
        with self._bootstrap_lock:
            if self._conn is None:
                self._conn = self._new_connection()
            return self._conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Own a connection for the scope; never observe another caller's uncommitted data."""
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _begin_write(self) -> sqlite3.Connection:
        conn = self._new_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            conn.close()
            raise
        return conn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[sqlite3.Connection]:
        """Wait for SQLite's writer off the event loop, then own the transaction.

        Cancellation while waiting must still close the eventual connection.
        Cleanup may wait up to the SQLite busy timeout; no user SQL runs then.
        """
        opening = asyncio.create_task(asyncio.to_thread(self._begin_write))
        try:
            conn = await asyncio.shield(opening)
        except asyncio.CancelledError:
            while not opening.done():
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break  # _begin_write already closed its failed connection
            if not opening.cancelled() and opening.exception() is None:
                opening.result().close()  # closes and rolls back the empty transaction
            raise
        try:
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    async def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single write statement."""
        async with self.write() as conn:
            conn.execute(sql, params)

    def execute_write_sync(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Synchronous writer; SQLite orders it with async writers on the same database."""
        conn = self._begin_write()
        try:
            try:
                conn.execute(sql, params)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def execute_script(self, script: str) -> None:
        """Run bootstrap DDL on an owned connection, outside managed transactions."""
        with self.read() as conn:
            conn.executescript(script)

    async def execute_many(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        """Execute many write statements in a single transaction."""
        async with self.write() as conn:
            conn.executemany(sql, params_list)

    def execute_read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute a read query synchronously."""
        with self.read() as conn:
            return conn.execute(sql, params).fetchall()

    async def execute_read_async(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute a read query asynchronously."""
        return await asyncio.to_thread(self.execute_read, sql, params)

    async def initialize(self) -> None:
        """Initialize the database with schema migrations."""
        from kalash.storage.migrations import run_migrations

        await run_migrations(self)

    def close(self) -> None:
        """Release the legacy bootstrap handle; managed scopes close themselves."""
        with self._bootstrap_lock:
            if self._conn:
                self._conn.close()
                self._conn = None


# Global engine singleton
_engine: StorageEngine | None = None


def _close_global_engine() -> None:
    """atexit handler — checkpoint WAL and release the connection (R-5)."""
    global _engine
    if _engine is not None:
        _engine.close()
        _engine = None


atexit.register(_close_global_engine)


def get_engine() -> StorageEngine:
    """Get the global storage engine."""
    global _engine
    if _engine is None:
        _engine = StorageEngine()
    return _engine


async def initialize_storage() -> StorageEngine:
    """Initialize storage and run migrations."""
    engine = get_engine()
    await engine.initialize()
    return engine
