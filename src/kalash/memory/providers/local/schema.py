"""SQLite schema; existing records and revisions remain compatible."""

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
"""
