"""SQLite connection management with WAL mode and single-writer queue.

All writes serialize through one queue. Reads go wide via WAL.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from kalash.core.paths import kalash_db_path


class StorageEngine:
    """Manages SQLite connections with WAL mode and serialized writes."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or kalash_db_path()
        self._write_lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create the database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                timeout=5.0,  # busy_timeout equivalent
                isolation_level=None,  # autocommit; we manage transactions explicitly
                check_same_thread=False,  # We serialize writes via asyncio.Lock
            )
            self._conn.row_factory = sqlite3.Row
            # WAL mode, foreign keys, synchronous=NORMAL (I-023)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Get a connection for reading. Concurrent reads are fine with WAL."""
        conn = self._get_connection()
        yield conn

    @asynccontextmanager
    async def write(self) -> AsyncIterator[sqlite3.Connection]:
        """Get a connection for writing. Serialized via lock (I-023)."""
        async with self._write_lock:
            conn = self._get_connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    async def execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single write statement."""
        async with self.write() as conn:
            conn.execute(sql, params)

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
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


# Global engine singleton
_engine: StorageEngine | None = None


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
