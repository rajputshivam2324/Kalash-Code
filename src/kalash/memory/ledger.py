"""Durable write-intent log for memory operations.

The MemoryLedger records every write intent BEFORE it is dispatched to providers.
This ensures:
- Offline writes survive: intents are persisted even if providers are unreachable.
- Crash recovery: replay from INTENT or DISPATCHED states on restart.
- Auditability: every memory mutation has a durable trace.
- Reconciliation: detect and repair provider divergence.

Ledger entries progress through states:
  INTENT → DISPATCHED → COMMITTED (or FAILED)

On crash recovery, entries in INTENT or DISPATCHED are replayed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Sequence

import structlog

from kalash.core.ids import generate_id
from kalash.storage.engine import StorageEngine

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class LedgerState(StrEnum):
    """Lifecycle state of a ledger entry."""

    INTENT = "intent"  # Recorded, not yet dispatched
    DISPATCHED = "dispatched"  # Sent to provider(s), awaiting receipt
    COMMITTED = "committed"  # Provider confirmed persistence
    FAILED = "failed"  # All retries exhausted


@dataclass
class LedgerEntry:
    """A single write-intent record in the ledger."""

    id: str  # ULID with led_ prefix
    state: LedgerState
    operation: str  # "write", "update", "forget"
    payload: dict[str, Any]  # Serialized MemoryWrite / MemoryEdit / ForgetSelector
    target_providers: list[str]  # Provider names to dispatch to
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)  # provider → receipt
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dispatched_at: datetime | None = None
    committed_at: datetime | None = None
    failed_at: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    error: str | None = None


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class MemoryLedger:
    """Durable write-ahead log for memory operations.

    All writes pass through the ledger before reaching providers.
    This guarantees intent durability regardless of provider availability.
    """

    TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS memory_ledger (
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'intent',
        operation TEXT NOT NULL,
        payload TEXT NOT NULL,
        target_providers TEXT NOT NULL,
        receipts TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        dispatched_at TEXT,
        committed_at TEXT,
        failed_at TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        error TEXT
    )
    """

    INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_ledger_state ON memory_ledger(state);
    CREATE INDEX IF NOT EXISTS idx_ledger_created ON memory_ledger(created_at);
    """

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine
        self._initialized = False

    async def initialize(self) -> None:
        """Create the ledger table if it doesn't exist."""
        if self._initialized:
            return

        async with self._engine.write() as conn:
            conn.execute(self.TABLE_DDL)
            conn.executescript(self.INDEX_DDL)

        self._initialized = True
        logger.debug("memory_ledger_initialized")

    # ------------------------------------------------------------------
    # Record intents
    # ------------------------------------------------------------------

    async def record_intent(
        self,
        operation: str,
        payload: dict[str, Any],
        target_providers: list[str],
    ) -> LedgerEntry:
        """Record a write intent BEFORE dispatching to providers.

        Args:
            operation: One of "write", "update", "forget".
            payload: Serialized operation data.
            target_providers: List of provider names to dispatch to.

        Returns:
            The created ledger entry.
        """
        entry = LedgerEntry(
            id=generate_id("led_"),
            state=LedgerState.INTENT,
            operation=operation,
            payload=payload,
            target_providers=target_providers,
        )

        await self._persist(entry)
        logger.debug(
            "memory_ledger_intent",
            ledger_id=entry.id,
            operation=operation,
            providers=target_providers,
        )
        return entry

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def mark_dispatched(self, entry_id: str) -> None:
        """Mark an entry as dispatched to providers."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            "UPDATE memory_ledger SET state = ?, dispatched_at = ? WHERE id = ?",
            (LedgerState.DISPATCHED.value, now, entry_id),
        )

    async def mark_committed(
        self, entry_id: str, receipts: dict[str, dict[str, Any]]
    ) -> None:
        """Mark an entry as fully committed with provider receipts."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            "UPDATE memory_ledger SET state = ?, committed_at = ?, receipts = ? WHERE id = ?",
            (LedgerState.COMMITTED.value, now, json.dumps(receipts, default=str), entry_id),
        )

    async def mark_failed(self, entry_id: str, error: str) -> None:
        """Mark an entry as permanently failed."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            "UPDATE memory_ledger SET state = ?, failed_at = ?, error = ? WHERE id = ?",
            (LedgerState.FAILED.value, now, error, entry_id),
        )

    async def increment_retry(self, entry_id: str) -> int:
        """Increment retry count, return new count."""
        rows = self._engine.execute_read(
            "SELECT retry_count, max_retries FROM memory_ledger WHERE id = ?",
            (entry_id,),
        )
        if not rows:
            return 0

        current = rows[0]["retry_count"]
        new_count = current + 1
        await self._engine.execute_write(
            "UPDATE memory_ledger SET retry_count = ? WHERE id = ?",
            (new_count, entry_id),
        )
        return new_count

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_pending(self) -> list[LedgerEntry]:
        """Get all entries in INTENT or DISPATCHED state (for replay)."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_ledger WHERE state IN (?, ?) ORDER BY created_at ASC",
            (LedgerState.INTENT.value, LedgerState.DISPATCHED.value),
        )
        return [self._row_to_entry(row) for row in rows]

    async def get_entry(self, entry_id: str) -> LedgerEntry | None:
        """Get a single ledger entry by ID."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_ledger WHERE id = ?",
            (entry_id,),
        )
        if not rows:
            return None
        return self._row_to_entry(rows[0])

    async def get_recent(self, limit: int = 50) -> list[LedgerEntry]:
        """Get recent ledger entries (newest first)."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_ledger ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_entry(row) for row in rows]

    async def get_failed(self) -> list[LedgerEntry]:
        """Get all failed entries for inspection."""
        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_ledger WHERE state = ? ORDER BY failed_at DESC",
            (LedgerState.FAILED.value,),
        )
        return [self._row_to_entry(row) for row in rows]

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    async def reconcile(self) -> list[LedgerEntry]:
        """Find entries needing replay and return them for re-dispatch.

        Called on startup to handle crash recovery. Entries in INTENT
        or DISPATCHED state are returned for the router to re-process.
        """
        pending = await self.get_pending()
        if pending:
            logger.info(
                "memory_ledger_reconcile",
                pending_count=len(pending),
                states={e.state.value: 1 for e in pending},
            )
        return pending

    async def cleanup_committed(self, older_than_days: int = 30) -> int:
        """Remove committed entries older than N days.

        Returns the number of entries cleaned up.
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        rows = self._engine.execute_read(
            "SELECT COUNT(*) as cnt FROM memory_ledger WHERE state = ? AND committed_at < ?",
            (LedgerState.COMMITTED.value, cutoff),
        )
        count = rows[0]["cnt"] if rows else 0

        if count > 0:
            await self._engine.execute_write(
                "DELETE FROM memory_ledger WHERE state = ? AND committed_at < ?",
                (LedgerState.COMMITTED.value, cutoff),
            )
            logger.info("memory_ledger_cleanup", removed=count, older_than_days=older_than_days)

        return count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _persist(self, entry: LedgerEntry) -> None:
        """Insert a new ledger entry."""
        await self._engine.execute_write(
            """INSERT INTO memory_ledger
               (id, state, operation, payload, target_providers, receipts,
                created_at, dispatched_at, committed_at, failed_at,
                retry_count, max_retries, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.id,
                entry.state.value,
                entry.operation,
                json.dumps(entry.payload, default=str),
                json.dumps(entry.target_providers),
                json.dumps(entry.receipts, default=str),
                entry.created_at.isoformat(),
                entry.dispatched_at.isoformat() if entry.dispatched_at else None,
                entry.committed_at.isoformat() if entry.committed_at else None,
                entry.failed_at.isoformat() if entry.failed_at else None,
                entry.retry_count,
                entry.max_retries,
                entry.error,
            ),
        )

    def _row_to_entry(self, row: sqlite3.Row) -> LedgerEntry:
        """Convert a database row to a LedgerEntry."""
        return LedgerEntry(
            id=row["id"],
            state=LedgerState(row["state"]),
            operation=row["operation"],
            payload=json.loads(row["payload"]),
            target_providers=json.loads(row["target_providers"]),
            receipts=json.loads(row["receipts"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            dispatched_at=datetime.fromisoformat(row["dispatched_at"]) if row["dispatched_at"] else None,
            committed_at=datetime.fromisoformat(row["committed_at"]) if row["committed_at"] else None,
            failed_at=datetime.fromisoformat(row["failed_at"]) if row["failed_at"] else None,
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            error=row["error"],
        )
