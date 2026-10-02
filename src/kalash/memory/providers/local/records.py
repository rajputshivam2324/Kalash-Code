"""SQLite row decoding and FTS relevance scores."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from kalash.memory.protocol import (
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Scope,
    Source,
    Trust,
    Visibility,
)


def row_to_record(row: sqlite3.Row) -> MemoryRecord:
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


def row_to_hit(row: sqlite3.Row) -> MemoryHit:
    """Convert a database row to a MemoryHit with score."""
    record = row_to_record(row)

    # Compute score from FTS rank (negative; more negative is more relevant)
    fts_rank = row["fts_rank"] if "fts_rank" in row.keys() else 0
    if fts_rank < 0:
        # FTS5 rank is negative; normalize to [0, 1]
        score = Decimal(str(abs(fts_rank) / (1.0 + abs(fts_rank))))
    else:
        # No FTS rank — use salience as score
        score = record.salience

    return MemoryHit(
        record=record,
        score=score,
        provider="local",
        match_type="keyword" if fts_rank != 0 else "recency",
    )
