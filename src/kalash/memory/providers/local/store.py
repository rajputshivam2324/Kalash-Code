"""SQLite saved facts: FTS5 recall, scoped records, revisions and tombstones."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import structlog

    logger = structlog.get_logger()
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from kalash.memory.protocol import (
    ForgetReceipt,
    ForgetSelector,
    MemoryEdit,
    MemoryHit,
    MemoryRecord,
    ProviderCapability,
    ProviderHealth,
    ProviderStats,
    RecallQuery,
    Scope,
    WriteReceipt,
)
from kalash.storage.engine import StorageEngine

from .queries import recall_sql, scope_filter
from .records import row_to_hit, row_to_record
from .schema import SCHEMA_DDL


class LocalProvider:
    """Built-in SQLite-backed memory provider.

    Implements the MemoryProvider protocol with:
    - FTS5 keyword search
    - Full revision history
    - Tombstone-based deletion
    - Scope-isolated queries
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        db_path = config.get("db_path")
        self._engine = StorageEngine(Path(db_path) if db_path else None)
        self._initialized = False

    @property
    def name(self) -> str:
        return "local"

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        caps = {
            ProviderCapability.KEYWORD_SEARCH,
            ProviderCapability.HISTORY,
            ProviderCapability.NAMESPACES,
            ProviderCapability.BULK_EXPORT,
            ProviderCapability.TTL,
        }
        return frozenset(caps)

    async def initialize(self) -> None:
        """Initialize the compatible SQLite schema."""
        if self._initialized:
            return

        # executescript() issues implicit commits, which breaks the engine's
        # explicit BEGIN/COMMIT wrapper — run DDL on the raw connection instead.
        def _init_schema() -> None:
            conn = self._engine._get_connection()  # noqa: SLF001
            conn.executescript(SCHEMA_DDL)

        await asyncio.to_thread(_init_schema)

        self._initialized = True

    async def health(self) -> ProviderHealth:
        """Check SQLite connectivity by round-tripping a trivial query."""
        import time

        try:
            await self.initialize()
            started = time.perf_counter()
            # The result is irrelevant; not raising is the health signal.
            await self._engine.execute_read_async("SELECT 1", ())
            elapsed_ms = (time.perf_counter() - started) * 1000
            return ProviderHealth(
                provider=self.name,
                healthy=True,
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            return ProviderHealth(
                provider=self.name,
                healthy=False,
                error=str(exc),
            )

    async def write(self, record: MemoryRecord) -> WriteReceipt:
        """Persist a memory record to SQLite."""
        if not self._initialized:
            await self.initialize()

        now = datetime.now(UTC).isoformat()

        async with self._engine.write() as conn:
            existing = conn.execute(
                """SELECT id, revision FROM memory_records
                   WHERE content_hash = ? AND scope_user_id = ?
                     AND scope_project_id IS ? AND scope_session_id IS ?
                     AND scope_agent_id IS ? AND scope_visibility = ?
                     AND kind = ? AND tombstoned = 0 AND superseded_by IS NULL
                     AND (expires_at IS NULL OR expires_at > ?) LIMIT 1""",
                (
                    record.content_hash,
                    record.scope.user_id,
                    record.scope.project_id,
                    record.scope.session_id,
                    record.scope.agent_id,
                    record.scope.visibility.value,
                    record.kind.value,
                    now,
                ),
            ).fetchone()
            if existing:
                return WriteReceipt(
                    record_id=existing["id"], provider=self.name, revision=existing["revision"]
                )
            # Insert main record
            conn.execute(
                """INSERT INTO memory_records (
                    id, kind, content, content_hash, confidence, salience,
                    sensitive_class, subject_key, expires_at, superseded_by,
                    revision, embedding_model_id, metadata,
                    scope_user_id, scope_project_id, scope_org_id,
                    scope_repo_id, scope_branch, scope_session_id,
                    scope_agent_id, scope_visibility,
                    prov_source, prov_trust, prov_created_at,
                    prov_origin_provider, prov_session_id, prov_turn_seq,
                    prov_tool_call_id, prov_file_path, prov_url,
                    tombstoned, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    0, ?, ?
                )""",
                (
                    record.id,
                    record.kind.value,
                    record.content,
                    record.content_hash,
                    str(record.confidence),
                    str(record.salience),
                    record.sensitive_class,
                    record.subject_key,
                    record.expires_at.isoformat() if record.expires_at else None,
                    record.superseded_by,
                    record.revision,
                    record.embedding_model_id,
                    json.dumps(record.metadata),
                    # Scope
                    record.scope.user_id,
                    record.scope.project_id,
                    record.scope.org_id,
                    record.scope.repo_id,
                    record.scope.branch,
                    record.scope.session_id,
                    record.scope.agent_id,
                    record.scope.visibility.value,
                    # Provenance
                    record.provenance.source.value,
                    record.provenance.trust.value,
                    record.provenance.created_at.isoformat(),
                    record.provenance.origin_provider,
                    record.provenance.session_id,
                    record.provenance.turn_seq,
                    record.provenance.tool_call_id,
                    record.provenance.file_path,
                    record.provenance.url,
                    # Timestamps
                    now,
                    now,
                ),
            )

            # Insert into FTS index
            conn.execute(
                "INSERT INTO memory_fts (id, content, subject_key, kind) VALUES (?, ?, ?, ?)",
                (record.id, record.content, record.subject_key or "", record.kind.value),
            )

            # Insert initial history entry
            conn.execute(
                """INSERT INTO memory_history (id, revision, content, content_hash,
                   confidence, salience, metadata, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.revision,
                    record.content,
                    record.content_hash,
                    str(record.confidence),
                    str(record.salience),
                    json.dumps(record.metadata),
                    now,
                ),
            )

        return WriteReceipt(
            record_id=record.id,
            provider=self.name,
            revision=record.revision,
        )

    async def recall(self, query: RecallQuery) -> Sequence[MemoryHit]:
        """Retrieve scoped records using FTS5 keyword search or recency."""
        if not self._initialized:
            await self.initialize()

        sql, parameters = recall_sql(query)
        if sql is None:
            return []
        rows = await self._engine.execute_read_async(sql, parameters)
        return [row_to_hit(row) for row in rows]

    async def get(self, record_id: str) -> MemoryRecord | None:
        """Get a single record by ID."""
        if not self._initialized:
            await self.initialize()

        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_records WHERE id = ? AND tombstoned = 0",
            (record_id,),
        )
        if not rows:
            return None
        return row_to_record(rows[0])

    async def update(self, edit: MemoryEdit) -> WriteReceipt:
        """Commit the revision, history and search index in one transaction."""
        if not self._initialized:
            await self.initialize()
        now = datetime.now(UTC).isoformat()
        async with self._engine.write() as conn:
            row = conn.execute(
                "SELECT * FROM memory_records WHERE id = ? AND tombstoned = 0", (edit.id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Record not found: {edit.id}")
            current = dict(row)
            updates: dict[str, Any] = {"revision": current["revision"] + 1, "updated_at": now}
            for name in ("content", "subject_key", "sensitive_class", "superseded_by"):
                value = getattr(edit, name)
                if value is not None:
                    updates[name] = value
            for name in ("confidence", "salience"):
                value = getattr(edit, name)
                if value is not None:
                    updates[name] = str(value)
            if edit.content is not None:
                updates["content_hash"] = hashlib.sha256(edit.content.encode()).hexdigest()
            if edit.expires_at is not None:
                updates["expires_at"] = edit.expires_at.isoformat()
            if edit.metadata is not None:
                updates["metadata"] = json.dumps(edit.metadata)
            conn.execute(
                f"UPDATE memory_records SET {', '.join(f'{name} = ?' for name in updates)} WHERE id = ?",
                (*updates.values(), edit.id),
            )
            current.update(updates)
            conn.execute(
                """INSERT INTO memory_history
                   (id, revision, content, content_hash, confidence, salience, metadata, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    current[name]
                    for name in (
                        "id",
                        "revision",
                        "content",
                        "content_hash",
                        "confidence",
                        "salience",
                        "metadata",
                    )
                )
                + (now,),
            )
            if edit.content is not None or edit.subject_key is not None:
                conn.execute("DELETE FROM memory_fts WHERE id = ?", (edit.id,))
                conn.execute(
                    "INSERT INTO memory_fts (id, content, subject_key, kind) VALUES (?, ?, ?, ?)",
                    (edit.id, current["content"], current["subject_key"] or "", current["kind"]),
                )
        return WriteReceipt(record_id=edit.id, provider=self.name, revision=current["revision"])

    async def forget(self, selector: ForgetSelector) -> ForgetReceipt:
        """Tombstone records matching the selector."""
        if not self._initialized:
            await self.initialize()

        conditions: list[str] = ["tombstoned = 0"]
        params: list[Any] = []

        if selector.ids:
            placeholders = ",".join("?" * len(selector.ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(selector.ids)

        if selector.kinds:
            placeholders = ",".join("?" * len(selector.kinds))
            conditions.append(f"kind IN ({placeholders})")
            params.extend(k.value for k in selector.kinds)

        if not any(
            (selector.ids, selector.kinds, selector.scope, selector.subject_key, selector.before)
        ):
            raise ValueError("Forgetting memory requires an explicit selector")
        if selector.scope:
            scope_sql, scope_params = scope_filter(selector.scope, prefix="", exact=True)
            conditions.append(scope_sql)
            params.extend(scope_params)

        if selector.subject_key:
            conditions.append("subject_key = ?")
            params.append(selector.subject_key)

        if selector.before:
            conditions.append("prov_created_at < ?")
            params.append(selector.before.isoformat())

        where = " AND ".join(conditions)
        now = datetime.now(UTC).isoformat()

        async with self._engine.write() as conn:
            conn.execute(
                f"DELETE FROM memory_fts WHERE id IN (SELECT id FROM memory_records WHERE {where})",
                tuple(params),
            )
            count = conn.execute(
                f"UPDATE memory_records SET tombstoned = 1, tombstoned_at = ? WHERE {where}",
                (now, *params),
            ).rowcount

        return ForgetReceipt(
            count=count,
            provider=self.name,
            forgotten_at=datetime.now(UTC),
        )

    async def history(self, record_id: str) -> Sequence[MemoryRecord]:
        """Get all revisions of a record (newest first)."""
        if not self._initialized:
            await self.initialize()

        # Get the current record for scope/provenance
        current = await self.get(record_id)
        if current is None:
            return []

        rows = await self._engine.execute_read_async(
            "SELECT * FROM memory_history WHERE id = ? ORDER BY revision DESC",
            (record_id,),
        )

        records: list[MemoryRecord] = []
        for row in rows:
            records.append(
                MemoryRecord(
                    id=record_id,
                    kind=current.kind,
                    content=row["content"],
                    content_hash=row["content_hash"],
                    scope=current.scope,
                    provenance=current.provenance,
                    confidence=Decimal(row["confidence"]),
                    salience=Decimal(row["salience"]),
                    revision=row["revision"],
                    metadata=json.loads(row["metadata"]),
                )
            )

        return records

    async def export(self, scope: Scope) -> AsyncIterator[MemoryRecord]:
        """Bulk export all non-tombstoned records within a scope."""
        if not self._initialized:
            await self.initialize()

        scope_sql, params = scope_filter(scope, prefix="")
        conditions = ["tombstoned = 0", scope_sql]

        where = " AND ".join(conditions)
        batch_size = 100
        offset = 0

        while True:
            rows = await self._engine.execute_read_async(
                f"SELECT * FROM memory_records WHERE {where} ORDER BY created_at ASC LIMIT ? OFFSET ?",
                tuple(params) + (batch_size, offset),
            )

            if not rows:
                break

            for row in rows:
                yield row_to_record(row)

            offset += batch_size

    async def stats(self) -> ProviderStats:
        """Get provider statistics."""
        if not self._initialized:
            await self.initialize()

        total_rows = await self._engine.execute_read_async(
            "SELECT COUNT(*) as cnt FROM memory_records WHERE tombstoned = 0", ()
        )
        tomb_rows = await self._engine.execute_read_async(
            "SELECT COUNT(*) as cnt FROM memory_records WHERE tombstoned = 1", ()
        )
        kinds_rows = await self._engine.execute_read_async(
            "SELECT kind, COUNT(*) as cnt FROM memory_records WHERE tombstoned = 0 GROUP BY kind",
            (),
        )
        oldest_rows = await self._engine.execute_read_async(
            "SELECT MIN(created_at) as oldest FROM memory_records WHERE tombstoned = 0", ()
        )
        newest_rows = await self._engine.execute_read_async(
            "SELECT MAX(created_at) as newest FROM memory_records WHERE tombstoned = 0", ()
        )

        kinds_breakdown = {row["kind"]: row["cnt"] for row in kinds_rows}
        oldest = (
            datetime.fromisoformat(oldest_rows[0]["oldest"])
            if oldest_rows and oldest_rows[0]["oldest"]
            else None
        )
        newest = (
            datetime.fromisoformat(newest_rows[0]["newest"])
            if newest_rows and newest_rows[0]["newest"]
            else None
        )

        return ProviderStats(
            provider=self.name,
            total_records=total_rows[0]["cnt"] if total_rows else 0,
            total_tombstones=tomb_rows[0]["cnt"] if tomb_rows else 0,
            kinds_breakdown=kinds_breakdown,
            oldest_record=oldest,
            newest_record=newest,
        )

    async def close(self) -> None:
        self._engine.close()


def create_local_provider(config: dict[str, Any]) -> LocalProvider:
    """Factory function for the local provider.

    Registered as entry point: kalash.memory_providers = local
    """
    return LocalProvider(config)
