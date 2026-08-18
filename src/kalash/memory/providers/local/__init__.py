"""Built-in local memory provider (KME - Kalash Memory Engine).

The LocalProvider is the default memory backend:
- SQLite + FTS5 for keyword search
- Vector similarity stub (for sqlite-vec extension when available)
- Edge table for graph/entity relationships
- Full revision history per record
- Tombstone support (soft deletes)
- Scope isolation via WHERE clauses on scope columns
- Bulk export via AsyncIterator
- No external dependencies beyond SQLite

This provider is always available and serves as the baseline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import structlog

from kalash.core.ids import generate_id
from kalash.memory.protocol import (
    ForgetReceipt,
    ForgetSelector,
    MemoryEdit,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryWrite,
    Provenance,
    ProviderCapability,
    ProviderHealth,
    ProviderStats,
    RecallQuery,
    Scope,
    Source,
    Trust,
    Visibility,
    WriteReceipt,
)
from kalash.storage.engine import StorageEngine

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_DDL = """
-- Main memory records table
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT '1.0',
    salience TEXT NOT NULL DEFAULT '0.5',
    sensitive_class TEXT,
    subject_key TEXT,
    expires_at TEXT,
    superseded_by TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    embedding_model_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',

    -- Scope columns (for WHERE-based isolation)
    scope_user_id TEXT NOT NULL,
    scope_project_id TEXT,
    scope_org_id TEXT,
    scope_repo_id TEXT,
    scope_branch TEXT,
    scope_session_id TEXT,
    scope_agent_id TEXT,
    scope_visibility TEXT NOT NULL DEFAULT 'project',

    -- Provenance columns
    prov_source TEXT NOT NULL,
    prov_trust TEXT NOT NULL DEFAULT 'medium',
    prov_created_at TEXT NOT NULL,
    prov_origin_provider TEXT,
    prov_session_id TEXT,
    prov_turn_seq INTEGER,
    prov_tool_call_id TEXT,
    prov_file_path TEXT,
    prov_url TEXT,

    -- Tombstone
    tombstoned INTEGER NOT NULL DEFAULT 0,
    tombstoned_at TEXT,

    -- Timestamps
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Revision history table
CREATE TABLE IF NOT EXISTS memory_history (
    id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confidence TEXT NOT NULL,
    salience TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    changed_at TEXT NOT NULL,
    PRIMARY KEY (id, revision)
);

-- Graph edges for entity relationships
CREATE TABLE IF NOT EXISTS memory_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight TEXT NOT NULL DEFAULT '1.0',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, relation),
    FOREIGN KEY (source_id) REFERENCES memory_records(id),
    FOREIGN KEY (target_id) REFERENCES memory_records(id)
);

-- FTS5 virtual table for keyword search
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    content,
    subject_key,
    kind,
    tokenize='porter unicode61'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_records(
    scope_user_id, scope_project_id, scope_visibility
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_records(kind);
CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_records(subject_key);
CREATE INDEX IF NOT EXISTS idx_memory_hash ON memory_records(content_hash);
CREATE INDEX IF NOT EXISTS idx_memory_created ON memory_records(created_at);
CREATE INDEX IF NOT EXISTS idx_memory_tombstone ON memory_records(tombstoned);
CREATE INDEX IF NOT EXISTS idx_memory_expires ON memory_records(expires_at)
    WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id);
"""


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class LocalProvider:
    """Built-in SQLite-backed memory provider.

    Implements the MemoryProvider protocol with:
    - FTS5 keyword search
    - Vector similarity stub (ready for sqlite-vec)
    - Graph edge table
    - Full revision history
    - Tombstone-based deletion
    - Scope-isolated queries
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        db_path = config.get("db_path")
        self._engine = StorageEngine(Path(db_path) if db_path else None)
        self._initialized = False
        self._vector_available = False  # Will be True if sqlite-vec is loaded

    # ------------------------------------------------------------------
    # Protocol properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "local"

    @property
    def capabilities(self) -> frozenset[ProviderCapability]:
        caps = {
            ProviderCapability.KEYWORD_SEARCH,
            ProviderCapability.GRAPH,
            ProviderCapability.HISTORY,
            ProviderCapability.NAMESPACES,
            ProviderCapability.BULK_EXPORT,
            ProviderCapability.TTL,
        }
        if self._vector_available:
            caps.add(ProviderCapability.SEMANTIC_SEARCH)
            caps.add(ProviderCapability.HYBRID_SEARCH)
        return frozenset(caps)

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize schema and check for vector extension."""
        if self._initialized:
            return

        # executescript() issues implicit commits, which breaks the engine's
        # explicit BEGIN/COMMIT wrapper — run DDL on the raw connection instead.
        def _init_schema() -> None:
            conn = self._engine._get_connection()  # noqa: SLF001
            conn.executescript(SCHEMA_DDL)

        await asyncio.to_thread(_init_schema)

        # Check for sqlite-vec extension
        self._vector_available = await self._check_vector_extension()
        self._initialized = True

    async def _check_vector_extension(self) -> bool:
        """Report whether sqlite-vec is loadable.

        Returns False until vector search is implemented. Kept as a method so
        the capability set is computed in one place rather than hardcoded at
        each call site.
        """
        return False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        """Check SQLite connectivity by round-tripping a trivial query."""
        import time

        try:
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

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def write(self, record: MemoryRecord) -> WriteReceipt:
        """Persist a memory record to SQLite."""
        if not self._initialized:
            await self.initialize()

        now = datetime.now(timezone.utc).isoformat()

        async with self._engine.write() as conn:
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
                    record.id, record.kind.value, record.content, record.content_hash,
                    str(record.confidence), str(record.salience),
                    record.sensitive_class, record.subject_key,
                    record.expires_at.isoformat() if record.expires_at else None,
                    record.superseded_by, record.revision,
                    record.embedding_model_id, json.dumps(record.metadata),
                    # Scope
                    record.scope.user_id, record.scope.project_id,
                    record.scope.org_id, record.scope.repo_id,
                    record.scope.branch, record.scope.session_id,
                    record.scope.agent_id, record.scope.visibility.value,
                    # Provenance
                    record.provenance.source.value, record.provenance.trust.value,
                    record.provenance.created_at.isoformat(),
                    record.provenance.origin_provider, record.provenance.session_id,
                    record.provenance.turn_seq, record.provenance.tool_call_id,
                    record.provenance.file_path, record.provenance.url,
                    # Timestamps
                    now, now,
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
                    record.id, record.revision, record.content, record.content_hash,
                    str(record.confidence), str(record.salience),
                    json.dumps(record.metadata), now,
                ),
            )

        return WriteReceipt(
            record_id=record.id,
            provider=self.name,
            revision=record.revision,
        )

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    async def recall(self, query: RecallQuery) -> Sequence[MemoryHit]:
        """Retrieve records using FTS5 keyword search (+ vector when available)."""
        if not self._initialized:
            await self.initialize()

        # Build WHERE clause from query parameters
        conditions: list[str] = ["r.tombstoned = 0"]
        params: list[Any] = []

        # Scope filtering
        if query.scope:
            conditions.append("r.scope_user_id = ?")
            params.append(query.scope.user_id)
            if query.scope.project_id:
                conditions.append("(r.scope_project_id = ? OR r.scope_visibility = 'global')")
                params.append(query.scope.project_id)
            if query.scope.session_id:
                conditions.append(
                    "(r.scope_session_id = ? OR r.scope_visibility IN ('global', 'project'))"
                )
                params.append(query.scope.session_id)

        # Kind filter
        if query.kinds:
            placeholders = ",".join("?" * len(query.kinds))
            conditions.append(f"r.kind IN ({placeholders})")
            params.extend(k.value for k in query.kinds)

        # Confidence / salience filters
        if query.min_confidence:
            conditions.append("CAST(r.confidence AS REAL) >= ?")
            params.append(float(query.min_confidence))
        if query.min_salience:
            conditions.append("CAST(r.salience AS REAL) >= ?")
            params.append(float(query.min_salience))

        # Subject key
        if query.subject_key:
            conditions.append("r.subject_key = ?")
            params.append(query.subject_key)

        # Time range
        if query.after:
            conditions.append("r.prov_created_at >= ?")
            params.append(query.after.isoformat())
        if query.before:
            conditions.append("r.prov_created_at <= ?")
            params.append(query.before.isoformat())

        # Superseded filter
        if not query.include_superseded:
            conditions.append("r.superseded_by IS NULL")

        # Expired filter
        if not query.include_expired:
            now_iso = datetime.now(timezone.utc).isoformat()
            conditions.append("(r.expires_at IS NULL OR r.expires_at > ?)")
            params.append(now_iso)

        where_clause = " AND ".join(conditions)

        # Use FTS5 if we have a text query
        if query.text:
            # FTS5 rank scoring
            fts_query = self._build_fts_query(query.text)
            sql = f"""
                SELECT r.*, fts.rank AS fts_rank
                FROM memory_fts fts
                JOIN memory_records r ON r.id = fts.id
                WHERE memory_fts MATCH ? AND {where_clause}
                ORDER BY fts.rank
                LIMIT ?
            """
            all_params = [fts_query] + params + [query.limit]
        else:
            # No text query — return by recency * salience
            sql = f"""
                SELECT r.*, 0 AS fts_rank
                FROM memory_records r
                WHERE {where_clause}
                ORDER BY CAST(r.salience AS REAL) DESC, r.created_at DESC
                LIMIT ?
            """
            all_params = params + [query.limit]

        rows = await self._engine.execute_read_async(sql, tuple(all_params))
        return [self._row_to_hit(row) for row in rows]

    def _build_fts_query(self, text: str) -> str:
        """Build an FTS5 query from natural text.

        Splits into tokens and joins with OR for broad matching.
        Escapes special FTS5 characters.
        """
        # Remove FTS5 special chars
        cleaned = text.replace('"', "").replace("*", "").replace("-", " ")
        tokens = [t.strip() for t in cleaned.split() if len(t.strip()) >= 2]
        if not tokens:
            return text
        # OR-join for broad recall, with prefix matching
        return " OR ".join(f'"{t}"' for t in tokens[:10])

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

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
        return self._row_to_record(rows[0])

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update(self, edit: MemoryEdit) -> WriteReceipt:
        """Update a record, creating a new revision."""
        if not self._initialized:
            await self.initialize()

        now = datetime.now(timezone.utc).isoformat()

        # Get current record for revision increment
        rows = await self._engine.execute_read_async(
            "SELECT revision, content, content_hash, confidence, salience, metadata FROM memory_records WHERE id = ?",
            (edit.id,),
        )
        if not rows:
            raise ValueError(f"Record not found: {edit.id}")

        current = rows[0]
        new_revision = current["revision"] + 1

        # Build update fields
        updates: list[str] = ["revision = ?", "updated_at = ?"]
        params: list[Any] = [new_revision, now]

        new_content = edit.content or current["content"]
        new_hash = (
            hashlib.sha256(edit.content.encode()).hexdigest()
            if edit.content else current["content_hash"]
        )

        if edit.content is not None:
            updates.extend(["content = ?", "content_hash = ?"])
            params.extend([edit.content, new_hash])
        if edit.confidence is not None:
            updates.append("confidence = ?")
            params.append(str(edit.confidence))
        if edit.salience is not None:
            updates.append("salience = ?")
            params.append(str(edit.salience))
        if edit.sensitive_class is not None:
            updates.append("sensitive_class = ?")
            params.append(edit.sensitive_class)
        if edit.subject_key is not None:
            updates.append("subject_key = ?")
            params.append(edit.subject_key)
        if edit.expires_at is not None:
            updates.append("expires_at = ?")
            params.append(edit.expires_at.isoformat())
        if edit.superseded_by is not None:
            updates.append("superseded_by = ?")
            params.append(edit.superseded_by)
        if edit.metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(edit.metadata))

        params.append(edit.id)

        async with self._engine.write() as conn:
            conn.execute(
                f"UPDATE memory_records SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )

            # Save to history
            conn.execute(
                """INSERT INTO memory_history (id, revision, content, content_hash,
                   confidence, salience, metadata, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    edit.id, new_revision, new_content, new_hash,
                    str(edit.confidence or current["confidence"]),
                    str(edit.salience or current["salience"]),
                    json.dumps(edit.metadata) if edit.metadata else current["metadata"],
                    now,
                ),
            )

            # Update FTS if content changed
            if edit.content is not None:
                conn.execute("DELETE FROM memory_fts WHERE id = ?", (edit.id,))
                conn.execute(
                    "INSERT INTO memory_fts (id, content, subject_key, kind) VALUES (?, ?, ?, ?)",
                    (edit.id, edit.content, edit.subject_key or "", ""),
                )

        return WriteReceipt(
            record_id=edit.id,
            provider=self.name,
            revision=new_revision,
        )

    # ------------------------------------------------------------------
    # Forget
    # ------------------------------------------------------------------

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

        if selector.scope:
            conditions.append("scope_user_id = ?")
            params.append(selector.scope.user_id)
            if selector.scope.project_id:
                conditions.append("scope_project_id = ?")
                params.append(selector.scope.project_id)

        if selector.subject_key:
            conditions.append("subject_key = ?")
            params.append(selector.subject_key)

        if selector.before:
            conditions.append("prov_created_at < ?")
            params.append(selector.before.isoformat())

        where = " AND ".join(conditions)
        now = datetime.now(timezone.utc).isoformat()

        # Count affected rows
        count_rows = await self._engine.execute_read_async(
            f"SELECT COUNT(*) as cnt FROM memory_records WHERE {where}",
            tuple(params),
        )
        count = count_rows[0]["cnt"] if count_rows else 0

        if count > 0:
            # Tombstone (soft delete)
            await self._engine.execute_write(
                f"UPDATE memory_records SET tombstoned = 1, tombstoned_at = ? WHERE {where}",
                (now,) + tuple(params),
            )

            # Remove from FTS
            if selector.ids:
                for rid in selector.ids:
                    await self._engine.execute_write(
                        "DELETE FROM memory_fts WHERE id = ?", (rid,)
                    )

        return ForgetReceipt(
            count=count,
            provider=self.name,
            forgotten_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

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
            records.append(MemoryRecord(
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
            ))

        return records

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def export(self, scope: Scope) -> AsyncIterator[MemoryRecord]:
        """Bulk export all non-tombstoned records within a scope."""
        if not self._initialized:
            await self.initialize()

        conditions = ["tombstoned = 0", "scope_user_id = ?"]
        params: list[Any] = [scope.user_id]

        if scope.project_id:
            conditions.append("scope_project_id = ?")
            params.append(scope.project_id)

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
                yield self._row_to_record(row)

            offset += batch_size

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

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
            "SELECT kind, COUNT(*) as cnt FROM memory_records WHERE tombstoned = 0 GROUP BY kind", ()
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

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: Decimal = Decimal("1.0"),
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a graph edge between two memory records."""
        now = datetime.now(timezone.utc).isoformat()
        await self._engine.execute_write(
            """INSERT OR REPLACE INTO memory_edges
               (source_id, target_id, relation, weight, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, target_id, relation, str(weight), json.dumps(metadata or {}), now),
        )

    async def get_edges(
        self,
        record_id: str,
        direction: str = "both",
        relation: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get graph edges for a record.

        Args:
            record_id: The record to get edges for.
            direction: 'outgoing', 'incoming', or 'both'.
            relation: Optional filter by relation type.
        """
        results: list[dict[str, Any]] = []

        if direction in ("outgoing", "both"):
            sql = "SELECT * FROM memory_edges WHERE source_id = ?"
            params: list[Any] = [record_id]
            if relation:
                sql += " AND relation = ?"
                params.append(relation)
            rows = await self._engine.execute_read_async(sql, tuple(params))
            results.extend(dict(row) for row in rows)

        if direction in ("incoming", "both"):
            sql = "SELECT * FROM memory_edges WHERE target_id = ?"
            params = [record_id]
            if relation:
                sql += " AND relation = ?"
                params.append(relation)
            rows = await self._engine.execute_read_async(sql, tuple(params))
            results.extend(dict(row) for row in rows)

        return results

    # ------------------------------------------------------------------
    # Row conversion helpers
    # ------------------------------------------------------------------

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        """Convert a database row to a MemoryRecord."""
        scope = Scope(
            user_id=row["scope_user_id"],
            project_id=row["scope_project_id"],
            org_id=row["scope_org_id"],
            repo_id=row["scope_repo_id"],
            branch=row["scope_branch"],
            session_id=row["scope_session_id"],
            agent_id=row["scope_agent_id"],
            visibility=Visibility(row["scope_visibility"]),
        )

        provenance = Provenance(
            source=Source(row["prov_source"]),
            trust=Trust(row["prov_trust"]),
            created_at=datetime.fromisoformat(row["prov_created_at"]),
            origin_provider=row["prov_origin_provider"],
            session_id=row["prov_session_id"],
            turn_seq=row["prov_turn_seq"],
            tool_call_id=row["prov_tool_call_id"],
            file_path=row["prov_file_path"],
            url=row["prov_url"],
        )

        return MemoryRecord(
            id=row["id"],
            kind=MemoryKind(row["kind"]),
            content=row["content"],
            content_hash=row["content_hash"],
            scope=scope,
            provenance=provenance,
            confidence=Decimal(row["confidence"]),
            salience=Decimal(row["salience"]),
            sensitive_class=row["sensitive_class"],
            subject_key=row["subject_key"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            superseded_by=row["superseded_by"],
            revision=row["revision"],
            embedding_model_id=row["embedding_model_id"],
            metadata=json.loads(row["metadata"]),
        )

    def _row_to_hit(self, row: sqlite3.Row) -> MemoryHit:
        """Convert a database row to a MemoryHit with score."""
        record = self._row_to_record(row)

        # Compute score from FTS rank (negative; closer to 0 is better)
        fts_rank = row["fts_rank"] if "fts_rank" in row.keys() else 0
        if fts_rank < 0:
            # FTS5 rank is negative; normalize to [0, 1]
            score = Decimal(str(min(1.0, 1.0 / (1.0 + abs(fts_rank)))))
        else:
            # No FTS rank — use salience as score
            score = record.salience

        return MemoryHit(
            record=record,
            score=score,
            provider=self.name,
            match_type="keyword" if fts_rank != 0 else "recency",
        )


# ---------------------------------------------------------------------------
# Factory function (for entry point discovery)
# ---------------------------------------------------------------------------


def create_local_provider(config: dict[str, Any]) -> LocalProvider:
    """Factory function for the local provider.

    Registered as entry point: kalash.memory_providers = local
    """
    return LocalProvider(config)
