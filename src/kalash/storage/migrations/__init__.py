"""Forward-only database migrations.

Each migration is a numbered SQL file. No down-migrations.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kalash.storage.engine import StorageEngine

# Migration SQL statements in order
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "0001_init",
        _MIGRATION_0001 := """
-- Schema migrations tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    checksum    TEXT NOT NULL
);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    project_dir TEXT NOT NULL,
    agent       TEXT NOT NULL DEFAULT 'default',
    state       TEXT NOT NULL DEFAULT 'CREATED',
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    closed_at   TEXT,
    archived_at TEXT,
    metadata    TEXT
);

-- Turns
CREATE TABLE IF NOT EXISTS turns (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    state       TEXT NOT NULL DEFAULT 'PENDING',
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    token_count INTEGER,
    cost_usd    REAL,
    model       TEXT,
    provider    TEXT,
    metadata    TEXT,
    UNIQUE(session_id, seq)
);

-- Messages (append-only, I-021)
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    turn_id     TEXT NOT NULL REFERENCES turns(id),
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    content     TEXT,
    blob_ref    TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(turn_id, seq)
);

-- Content blocks
CREATE TABLE IF NOT EXISTS content_blocks (
    id          TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL REFERENCES messages(id),
    seq         INTEGER NOT NULL,
    block_type  TEXT NOT NULL,
    content     TEXT,
    blob_ref    TEXT,
    metadata    TEXT,
    UNIQUE(message_id, seq)
);

-- Tool calls
CREATE TABLE IF NOT EXISTS tool_calls (
    id          TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL REFERENCES messages(id),
    turn_id     TEXT NOT NULL REFERENCES turns(id),
    tool_name   TEXT NOT NULL,
    arguments   TEXT,
    result      TEXT,
    blob_ref    TEXT,
    state       TEXT NOT NULL DEFAULT 'RECEIVED',
    status      TEXT NOT NULL DEFAULT 'pending',
    started_at  TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    error_code  TEXT
);

-- Checkpoints (recovery)
CREATE TABLE IF NOT EXISTS checkpoints (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    turn_seq    INTEGER NOT NULL,
    trigger     TEXT NOT NULL,
    manifest    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL
);

-- File snapshots
CREATE TABLE IF NOT EXISTS file_snapshots (
    id          TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('file','symlink','untracked')),
    sha256      TEXT,
    target      TEXT,
    mode        INTEGER,
    size_bytes  INTEGER,
    blob_ref    TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_snapshots_cp ON file_snapshots(checkpoint_id);

-- Memory ledger
CREATE TABLE IF NOT EXISTS memory_ledger (
    id          TEXT PRIMARY KEY,
    record_id   TEXT,
    op          TEXT NOT NULL,
    kind        TEXT,
    scope_digest TEXT,
    content_hash TEXT,
    provenance_digest TEXT,
    source      TEXT NOT NULL,
    namespace   TEXT NOT NULL DEFAULT 'default',
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    embedding_ref TEXT,
    owner_provider TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    intent_at   TEXT NOT NULL,
    payload_ref TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ttl_seconds INTEGER
);

-- Memory ledger receipts
CREATE TABLE IF NOT EXISTS memory_ledger_receipts (
    id          TEXT PRIMARY KEY,
    ledger_id   TEXT NOT NULL REFERENCES memory_ledger(id),
    provider    TEXT NOT NULL,
    provider_record_id TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    acked_at    TEXT
);

-- Memory tombstones (I-018)
CREATE TABLE IF NOT EXISTS memory_tombstones (
    id          TEXT PRIMARY KEY,
    ledger_id   TEXT NOT NULL,
    content_hash TEXT,
    reason      TEXT NOT NULL,
    deleted_at  TEXT NOT NULL,
    retain_until TEXT NOT NULL
);

-- Provider state (circuit breaker, cursors)
CREATE TABLE IF NOT EXISTS provider_state (
    provider    TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    breaker_state TEXT NOT NULL DEFAULT 'CLOSED',
    failure_count INTEGER NOT NULL DEFAULT 0,
    opened_at   TEXT,
    updated_at  TEXT NOT NULL
);

-- Agent runs
CREATE TABLE IF NOT EXISTS agent_runs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    parent_run_id TEXT REFERENCES agent_runs(id),
    agent       TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'CREATED',
    prompt_digest TEXT,
    capabilities TEXT,
    sandbox_mode TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error_code  TEXT,
    tokens_used INTEGER,
    cost_usd    REAL
);

-- Blackboard (shared state between agents)
CREATE TABLE IF NOT EXISTS blackboard (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES agent_runs(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(run_id, key)
);

-- Schedules
CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    spec        TEXT NOT NULL,
    spec_kind   TEXT NOT NULL CHECK(spec_kind IN ('cron','interval','once','event')),
    timezone    TEXT NOT NULL DEFAULT 'UTC',
    agent       TEXT NOT NULL DEFAULT 'default',
    prompt      TEXT NOT NULL,
    sandbox_mode TEXT NOT NULL DEFAULT 'read-only',
    approval_policy TEXT NOT NULL DEFAULT 'never',
    capabilities TEXT,
    tools_allow TEXT,
    catchup_policy TEXT NOT NULL DEFAULT 'skip_missed',
    overlap_policy TEXT NOT NULL DEFAULT 'skip',
    jitter_s    INTEGER NOT NULL DEFAULT 0,
    max_runs    INTEGER,
    max_consecutive_failures INTEGER NOT NULL DEFAULT 3,
    enabled     INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    budget_tokens INTEGER,
    budget_cost_usd REAL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Schedule runs
CREATE TABLE IF NOT EXISTS schedule_runs (
    id          TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(id),
    agent_run_id TEXT REFERENCES agent_runs(id),
    status      TEXT NOT NULL DEFAULT 'pending',
    started_at  TEXT,
    finished_at TEXT,
    trigger_source TEXT,
    error_code  TEXT,
    tokens_used INTEGER,
    cost_usd    REAL
);

-- Schedule leases
CREATE TABLE IF NOT EXISTS schedule_leases (
    lease_id    TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL REFERENCES schedules(id) UNIQUE,
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- MCP servers
CREATE TABLE IF NOT EXISTS mcp_servers (
    id          TEXT PRIMARY KEY,
    uri         TEXT NOT NULL UNIQUE,
    name        TEXT,
    transport   TEXT NOT NULL,
    config      TEXT,
    state       TEXT NOT NULL DEFAULT 'CONFIGURED',
    trust_level TEXT NOT NULL DEFAULT 'untrusted',
    enabled     INTEGER NOT NULL DEFAULT 1,
    registered_at TEXT NOT NULL
);

-- Plugins
CREATE TABLE IF NOT EXISTS plugins (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    version     TEXT NOT NULL,
    entrypoint  TEXT NOT NULL,
    capabilities TEXT,
    trust_level TEXT NOT NULL DEFAULT 'untrusted',
    installed_at TEXT NOT NULL,
    verified_hash TEXT
);

-- Trust grants
CREATE TABLE IF NOT EXISTS trust_grants (
    id          TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    capability  TEXT NOT NULL,
    content_hash TEXT,
    state       TEXT NOT NULL DEFAULT 'UNTRUSTED',
    granted_at  TEXT,
    expires_at  TEXT,
    granted_by  TEXT
);

-- Hook runs
CREATE TABLE IF NOT EXISTS hook_runs (
    id          TEXT PRIMARY KEY,
    hook_name   TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    error_code  TEXT,
    output      TEXT
);

-- Permission grants
CREATE TABLE IF NOT EXISTS permission_grants (
    id          TEXT PRIMARY KEY,
    principal   TEXT NOT NULL,
    project_id  TEXT,
    capability  TEXT NOT NULL,
    target_pattern TEXT,
    scope       TEXT,
    decision    TEXT NOT NULL CHECK(decision IN ('allow','deny')),
    duration    TEXT NOT NULL,
    sandbox_mode TEXT,
    protocol_version TEXT,
    request_id  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT
);

-- Permission requests
CREATE TABLE IF NOT EXISTS permission_requests (
    id          TEXT PRIMARY KEY,
    run_id      TEXT,
    capability  TEXT NOT NULL,
    target      TEXT,
    prompt_text TEXT,
    status      TEXT NOT NULL DEFAULT 'UNDECIDED',
    decision    TEXT,
    decided_by  TEXT,
    decided_at  TEXT,
    created_at  TEXT NOT NULL
);

-- Usage events
CREATE TABLE IF NOT EXISTS usage_events (
    id          TEXT PRIMARY KEY,
    session_id  TEXT REFERENCES sessions(id),
    turn_id     TEXT REFERENCES turns(id),
    model       TEXT,
    provider    TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd    REAL,
    created_at  TEXT NOT NULL
);

-- Audit log (hash-chained, I-007)
CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    session_id  TEXT,
    turn_id     TEXT,
    run_id      TEXT,
    actor       TEXT,
    event_type  TEXT NOT NULL,
    target      TEXT,
    action      TEXT,
    arguments_digest TEXT,
    result_digest TEXT,
    capabilities_required TEXT,
    capabilities_source TEXT,
    permission_decision TEXT,
    outcome     TEXT,
    error_code  TEXT,
    duration_ms INTEGER,
    prev_hash   TEXT,
    hash        TEXT NOT NULL
);
""",
    ),
    (
        2,
        "0002_kalash_grants",
        """
-- Durable permission grants (aligned with permissions/grants.py)
CREATE TABLE IF NOT EXISTS kalash_grants (
    id              TEXT PRIMARY KEY,
    tool_pattern    TEXT NOT NULL,
    path_pattern    TEXT,
    host_pattern    TEXT,
    lifetime        TEXT NOT NULL,
    session_id      TEXT,
    decision        TEXT NOT NULL DEFAULT 'allow',
    reason          TEXT,
    created_at      TEXT NOT NULL,
    consumed        INTEGER NOT NULL DEFAULT 0,
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_kalash_grants_session
    ON kalash_grants(session_id) WHERE session_id IS NOT NULL;
""",
    ),
]


def _statements(sql: str) -> Iterator[str]:
    """Split trusted migration SQL without executescript's implicit commit."""
    start = 0
    for index, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : index + 1]):
            yield sql[start : index + 1]
            start = index + 1
    remainder = sql[start:].strip()
    if remainder:
        yield remainder


def run_migrations_sync(engine: StorageEngine) -> None:
    """Commit each migration and its receipt together, including competing callers."""
    with engine.read() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            )
        """)
        for version, name, sql in MIGRATIONS:
            conn.execute("BEGIN IMMEDIATE")
            try:
                applied = conn.execute(
                    "SELECT version FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied is None:
                    for statement in _statements(sql):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_migrations "
                        "(version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
                        (
                            version,
                            name,
                            datetime.now(UTC).isoformat(),
                            hashlib.sha256(sql.encode()).hexdigest(),
                        ),
                    )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise


async def run_migrations(engine: StorageEngine) -> None:
    """Keep SQLite's writer wait off the event loop during initialization."""
    await asyncio.to_thread(run_migrations_sync, engine)
