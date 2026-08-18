# Data Model

> **Status:** normative.

SQLite persistence layer for Kalash Code. Database lives at `~/.kalash/kalash.db`.

---

## 1. Storage Principles

| Principle | Detail | Invariant |
|-----------|--------|-----------|
| Append-only messages | Messages are never mutated after write; corrections are new rows | I-021 |
| Monotonic `seq` | Per-session strictly increasing sequence; gaps allowed, reordering forbidden | I-021 |
| WAL mode | `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL` | — |
| Single writer | One process holds write lock; readers concurrent via WAL | I-023 |
| Content-addressed blobs | Files > 64 KiB stored at `~/.kalash/blobs/<sha256[:2]>/<sha256>`; verified on read | I-025 |
| Forward-only migrations | No down-migrations; backup before migrate | I-022 |
| ULIDs | Sortable, collision-resistant identifiers for all primary keys | — |
| UTC everywhere | All timestamps stored as ISO-8601 UTC; display converts to local | I-038 |
| Provider-agnostic JSON | Provider-specific data lives in JSON columns, never per-provider columns | — |

---

## 2. Schema DDL

### 2.1 Sessions

```sql
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,  -- ULID
    title           TEXT,
    project_dir     TEXT NOT NULL,
    agent           TEXT NOT NULL DEFAULT 'default',
    created_at      TEXT NOT NULL,     -- ISO-8601 UTC
    updated_at      TEXT NOT NULL,
    archived_at     TEXT,
    metadata        TEXT              -- JSON
);

CREATE TABLE turns (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    token_count     INTEGER,
    cost_usd        REAL,
    model           TEXT,
    provider        TEXT,
    UNIQUE(session_id, seq)
);

CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    turn_id         TEXT NOT NULL REFERENCES turns(id),
    seq             INTEGER NOT NULL,  -- monotonic within turn (I-021)
    role            TEXT NOT NULL,
    content_type    TEXT NOT NULL DEFAULT 'text',
    content         TEXT,              -- inline if ≤ 64 KiB
    blob_ref        TEXT,              -- SHA-256 if externalized (I-025)
    metadata        TEXT,              -- JSON: provider-specific fields
    created_at      TEXT NOT NULL,
    UNIQUE(turn_id, seq)
);

CREATE TABLE content_blocks (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(id),
    seq             INTEGER NOT NULL,
    block_type      TEXT NOT NULL,     -- text, image, tool_use, tool_result
    content         TEXT,
    blob_ref        TEXT,
    metadata        TEXT,
    UNIQUE(message_id, seq)
);

CREATE TABLE tool_calls (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(id),
    turn_id         TEXT NOT NULL REFERENCES turns(id),
    tool_name       TEXT NOT NULL,
    arguments       TEXT,              -- JSON
    result          TEXT,
    blob_ref        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    finished_at     TEXT,
    duration_ms     INTEGER,
    error_code      TEXT
);
```

### 2.2 Recovery

```sql
CREATE TABLE checkpoints (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    turn_seq        INTEGER NOT NULL,
    trigger         TEXT NOT NULL,     -- auto_turn, pre_git_destructive, manual
    manifest        TEXT NOT NULL,     -- JSON: file list with SHA-256 + metadata
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL
);

CREATE TABLE file_snapshots (
    id              TEXT PRIMARY KEY,
    checkpoint_id   TEXT NOT NULL REFERENCES checkpoints(id),
    path            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK(kind IN ('file','symlink','untracked')),
    sha256          TEXT,              -- NULL for symlinks
    target          TEXT,              -- symlink target (stored as link, not resolved)
    mode            INTEGER,
    size_bytes      INTEGER,
    blob_ref        TEXT               -- if > 64 KiB (I-025)
);
CREATE INDEX idx_file_snapshots_cp ON file_snapshots(checkpoint_id);
```

### 2.3 Memory

> KME's internal tables are defined in `memory.md` §13.1. The tables below support the federation/ledger layer.

```sql
CREATE TABLE memory_ledger (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,     -- 'kme' | MCP server URI
    namespace       TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    embedding_ref   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    ttl_seconds     INTEGER,
    UNIQUE(source, namespace, key)
);

CREATE TABLE memory_ledger_receipts (
    id              TEXT PRIMARY KEY,
    ledger_id       TEXT NOT NULL REFERENCES memory_ledger(id),
    provider        TEXT NOT NULL,
    synced_at       TEXT NOT NULL
);

CREATE TABLE memory_tombstones (
    id              TEXT PRIMARY KEY,
    ledger_id       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    deleted_at      TEXT NOT NULL,
    retain_until    TEXT NOT NULL      -- tombstones outlive records for sync
);

CREATE TABLE provider_state (
    provider        TEXT PRIMARY KEY,
    state           TEXT NOT NULL,     -- JSON: cursors, tokens, sync metadata
    updated_at      TEXT NOT NULL
);
```

### 2.4 Orchestration

```sql
CREATE TABLE agent_runs (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    parent_run_id   TEXT REFERENCES agent_runs(id),  -- subagent tree
    agent           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    prompt_digest   TEXT,
    capabilities    TEXT,              -- JSON array
    sandbox_mode    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    error_code      TEXT,
    tokens_used     INTEGER,
    cost_usd        REAL
);

CREATE TABLE blackboard (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES agent_runs(id),
    key             TEXT NOT NULL,
    value           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(run_id, key)
);
```

### 2.5 Scheduling

```sql
CREATE TABLE schedules (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    spec            TEXT NOT NULL,
    spec_kind       TEXT NOT NULL CHECK(spec_kind IN ('cron','interval','once','event')),
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    agent           TEXT NOT NULL DEFAULT 'default',
    prompt          TEXT NOT NULL,
    sandbox_mode    TEXT NOT NULL DEFAULT 'read-only',
    approval_policy TEXT NOT NULL DEFAULT 'never',
    capabilities    TEXT,              -- JSON array (attenuated)
    tools_allow     TEXT,              -- JSON array
    catchup_policy  TEXT NOT NULL DEFAULT 'skip_missed',
    overlap_policy  TEXT NOT NULL DEFAULT 'skip',
    jitter_s        INTEGER NOT NULL DEFAULT 0,
    max_runs        INTEGER,
    max_consecutive_failures INTEGER NOT NULL DEFAULT 3,
    enabled         INTEGER NOT NULL DEFAULT 1,
    next_run_at     TEXT,
    budget_tokens   INTEGER,
    budget_cost_usd REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE schedule_runs (
    id              TEXT PRIMARY KEY,
    schedule_id     TEXT NOT NULL REFERENCES schedules(id),
    agent_run_id    TEXT REFERENCES agent_runs(id),
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT,
    finished_at     TEXT,
    trigger_source  TEXT,              -- cron_tick | event:<type> | manual
    error_code      TEXT,
    tokens_used     INTEGER,
    cost_usd        REAL
);

CREATE TABLE schedule_leases (
    lease_id        TEXT PRIMARY KEY,
    schedule_id     TEXT NOT NULL REFERENCES schedules(id) UNIQUE,
    holder          TEXT NOT NULL,     -- process ID
    acquired_at     TEXT NOT NULL,
    heartbeat_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);
```

### 2.6 Extensibility

```sql
CREATE TABLE mcp_servers (
    id              TEXT PRIMARY KEY,
    uri             TEXT NOT NULL UNIQUE,
    name            TEXT,
    transport       TEXT NOT NULL,     -- stdio | sse | streamable_http
    config          TEXT,              -- JSON
    trust_level     TEXT NOT NULL DEFAULT 'untrusted',
    enabled         INTEGER NOT NULL DEFAULT 1,
    registered_at   TEXT NOT NULL
);

CREATE TABLE plugins (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    version         TEXT NOT NULL,
    entrypoint      TEXT NOT NULL,
    capabilities    TEXT,              -- JSON: requested caps
    trust_level     TEXT NOT NULL DEFAULT 'untrusted',
    installed_at    TEXT NOT NULL,
    verified_hash   TEXT
);

CREATE TABLE trust_grants (
    id              TEXT PRIMARY KEY,
    target_type     TEXT NOT NULL,     -- mcp_server | plugin | hook
    target_id       TEXT NOT NULL,
    capability      TEXT NOT NULL,
    granted_at      TEXT NOT NULL,
    expires_at      TEXT,
    granted_by      TEXT NOT NULL      -- user | schedule:<id>
);

CREATE TABLE hook_runs (
    id              TEXT PRIMARY KEY,
    hook_name       TEXT NOT NULL,
    trigger_event   TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    error_code      TEXT
);
```

### 2.7 Governance

```sql
CREATE TABLE permission_grants (
    id              TEXT PRIMARY KEY,
    principal       TEXT NOT NULL,
    capability      TEXT NOT NULL,
    scope           TEXT,              -- JSON: path globs, network targets
    decision        TEXT NOT NULL CHECK(decision IN ('allow','deny')),
    duration        TEXT NOT NULL,     -- 'session' | 'turn' | 'permanent' | ISO duration
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    source          TEXT NOT NULL      -- user_prompt | config | schedule
);

CREATE TABLE permission_requests (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES agent_runs(id),
    capability      TEXT NOT NULL,
    target          TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    decision        TEXT,
    decided_at      TEXT,
    decided_by      TEXT
);

CREATE TABLE usage_events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT REFERENCES sessions(id),
    event_type      TEXT NOT NULL,
    provider        TEXT,
    model           TEXT,
    tokens_input    INTEGER,
    tokens_output   INTEGER,
    cost_usd        REAL,
    created_at      TEXT NOT NULL
);

CREATE TABLE audit_log (
    id              TEXT PRIMARY KEY,
    timestamp       TEXT NOT NULL,
    session_id      TEXT,
    turn_id         TEXT,
    run_id          TEXT,
    actor           TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    target          TEXT,
    action          TEXT,
    arguments_digest TEXT,             -- SHA-256 of args (I-007)
    result_digest   TEXT,
    outcome         TEXT,
    error_code      TEXT,
    duration_ms     INTEGER,
    prev_hash       TEXT,
    hash            TEXT NOT NULL      -- SHA-256(canonical JSON + prev_hash)
);
CREATE INDEX idx_audit_session ON audit_log(session_id);
CREATE INDEX idx_audit_type ON audit_log(event_type);
```

### 2.8 Meta

```sql
CREATE TABLE schema_migrations (
    version         INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    checksum        TEXT NOT NULL      -- SHA-256 of migration SQL
);

CREATE TABLE kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

---

## 3. Migrations

| Rule | Detail |
|------|--------|
| Numbering | Sequential integers `0001_init.sql`, `0002_add_schedules.sql`, ... |
| Direction | Forward-only; no down-migrations (I-022) |
| Pre-flight | `VACUUM INTO` backup before applying |
| Checksum | Each migration has SHA-256 recorded in `schema_migrations` |
| Testing | Migration matrix: test upgrade from every prior version to HEAD |
| Atomicity | Each migration runs in a single transaction |

---

## 4. Checkpoints & Rewind

| Trigger | Condition |
|---------|-----------|
| `auto_turn` | Every turn boundary |
| `pre_git_destructive` | Before `git reset`, `git checkout --`, `git clean` |
| `manual` | User invokes `kalash checkpoint` |

Manifest structure:
- Pairs message `seq` with content-addressed file list
- Captures untracked files (git ls-files --others --exclude-standard)
- Stores symlinks as links (not resolved targets)
- Files > 64 KiB externalized to blob store (I-025)

Rewind semantics:
- Restores **working tree only** — git refs, index, and stash are the user's domain
- `kalash rewind --list` — show available checkpoints
- `kalash rewind --show <id>` — display manifest diff
- `kalash rewind --dry-run <id>` — preview changes without applying

GC: checkpoints retained 30 days by default (configurable).

---

## 5. Backup & Restore

| Operation | Mechanism |
|-----------|-----------|
| `kalash backup` | `VACUUM INTO` (consistent without stopping writers) |
| Blob inclusion | Referenced blobs copied into archive |
| Encryption | Optional AES-256-GCM (key from keyring or passphrase) |
| Integrity | `PRAGMA integrity_check` before and after |
| Secrets | **Not** in DB (I-035); restore requires credentials re-supplied |
| Corruption | Detected via `PRAGMA integrity_check`; user alerted |

---

## 6. Export Format

Archive: `kalash-export/` directory or `.tar.zst`.

```
kalash-export/
  manifest.json       # schema_version, created_at, kalash_version
  sessions/           # one JSON per session with turns/messages
  memory/             # vendor-neutral ledger records
  blobs/              # content-addressed, verified on import
  checksums.txt       # SHA-256 per file
```

| Behaviour | Default | Option |
|-----------|---------|--------|
| Redaction | Strip secrets, API keys, tokens | `--no-redact` (requires confirmation) |
| Conflicts on import | Skip duplicates | `--conflict=overwrite\|merge` |
| Compression | zstd level 3 | `--compress=none\|gzip\|zstd` |

---

## 7. Retention & GC

| Class | Default retention | Notes |
|-------|-------------------|-------|
| Messages | Indefinite | User deletes sessions explicitly |
| Tombstones | Outlive source +90d | Required for sync propagation |
| Blobs | Orphan-scanned | Removed when no reference exists |
| Checkpoints | 30 days | Configurable per-project |
| Audit log | 1 year | Compliance minimum |
| Schedule runs | 90 days | Configurable |
| Hook runs | 30 days | — |

`kalash gc` supports `--dry-run`. Opportunistic GC runs on startup if last run >24h ago.

---

## 8. At-Rest Encryption

| Option | Detail |
|--------|--------|
| SQLCipher | Optional; not default |
| Rationale | Key lives in OS keyring a local attacker can read; FDE is the real defence |
| Enablement | `kalash config set storage.encryption=sqlcipher`; triggers re-encryption migration |
| Key rotation | `kalash db rekey`; atomic via SQLCipher PRAGMA |

---

*See also:* [`state-machines.md`](./state-machines.md) §12-13 (crash recovery), [`memory.md`](./memory.md) §13.1 (KME tables), [`security.md`](./security.md) §4 (secrets management), [`observability.md`](./observability.md) (audit schema).
